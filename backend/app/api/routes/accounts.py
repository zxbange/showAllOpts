"""Same-origin account authentication; old module keys are never accepted here."""
import hmac
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, SecretStr

from app.services.accounts import AccountError, COOKIE, SESSION_SECONDS


def service(request):
    return request.app.state.accounts


def transport(request):
    settings = service(request).settings
    if request.url.scheme != 'https' and not settings.auth_allow_http:
        raise HTTPException(403, '请通过HTTPS登录；HTTP无法保护密码和登录会话')


def origin_check(request):
    expected = service(request).settings.auth_public_origin.rstrip('/') or str(request.base_url).rstrip('/')
    supplied = request.headers.get('origin', '')
    if (supplied != expected or request.headers.get('sec-fetch-site') not in (None, 'same-origin', 'none')
            or request.headers.get('x-app-client') != 'browser'):
        raise HTTPException(403, '请从本站页面操作')
    if urlsplit(expected).scheme not in ('http', 'https'):
        raise HTTPException(503, '站点来源配置无效')


def principal(request: Request):
    if not hasattr(request.state, 'account_session'):
        transport(request)
        try:
            request.state.account_session = service(request).session(request.cookies.get(COOKIE, ''))
        except AccountError as error:
            raise HTTPException(error.status, str(error)) from None
    return request.state.account_session


def check_csrf(request, session):
    origin_check(request)
    if not hmac.compare_digest(request.headers.get('x-csrf-token', '').encode(), session['csrf_token'].encode()):
        raise HTTPException(403, '页面校验已失效，请刷新后重试')


def require_user(request: Request):
    session = principal(request)
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        check_csrf(request, session)
        if session['user']['role'] != 'admin':
            raise HTTPException(403, '普通用户仅可查看数据')
    return session['user']


def require_admin(request: Request):
    user = require_user(request)
    if user['role'] != 'admin':
        raise HTTPException(403, '此功能需要管理员权限')
    return user


class SafeRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def safe(request):
            try:
                response = await handler(request)
                response.headers['Cache-Control'] = 'no-store'
                return response
            except RequestValidationError:
                raise HTTPException(422, '输入格式无效，请检查用户名、密码长度和角色') from None
            except AccountError as error:
                raise HTTPException(error.status, str(error)) from None
        return safe


router = APIRouter(prefix='/api/auth', tags=['账号'], route_class=SafeRoute)


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: SecretStr = Field(min_length=1, max_length=128)


class NewUser(Credentials):
    role: Literal['admin', 'viewer'] = 'viewer'


class UpdateUser(BaseModel):
    role: Literal['admin', 'viewer'] | None = None
    enabled: bool | None = Field(default=None, strict=True)
    password: SecretStr | None = Field(default=None, min_length=12, max_length=128)


class PasswordChange(BaseModel):
    old_password: SecretStr = Field(min_length=1, max_length=128)
    new_password: SecretStr = Field(min_length=12, max_length=128)


@router.get('/session')
def session_status(request: Request):
    result = {'user': None, 'csrf_token': None, 'configured': service(request).configured(),
              'https_required': not service(request).settings.auth_allow_http}
    try:
        result.update(principal(request))
    except HTTPException:
        pass
    return result


@router.post('/login')
def login(body: Credentials, request: Request, response: Response):
    transport(request)
    origin_check(request)
    token = service(request).login(body.username, body.password.get_secret_value(),
                                   request.client.host if request.client else 'unknown')
    # Re-login replaces the current browser session instead of leaving it active.
    service(request).logout(request.cookies.get(COOKIE, ''))
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                        secure=request.url.scheme == 'https', samesite='strict', path='/')
    return dict(service(request).session(token), configured=True,
                https_required=not service(request).settings.auth_allow_http)


@router.post('/logout')
def logout(request: Request, response: Response):
    current = principal(request)
    check_csrf(request, current)
    service(request).logout(request.cookies.get(COOKIE, ''))
    response.delete_cookie(COOKIE, path='/', httponly=True, secure=request.url.scheme == 'https', samesite='strict')
    return {'ok': True}


@router.post('/password')
def password(body: PasswordChange, request: Request, response: Response):
    current = principal(request)
    check_csrf(request, current)
    service(request).change_password(current['user']['id'], body.old_password.get_secret_value(), body.new_password.get_secret_value())
    response.delete_cookie(COOKIE, path='/', httponly=True, secure=request.url.scheme == 'https', samesite='strict')
    return {'ok': True}


@router.get('/users')
def users(request: Request, user=Depends(require_admin)):
    return {'users': service(request).list()}


@router.post('/users')
def create_user(body: NewUser, request: Request, user=Depends(require_admin)):
    return service(request).create(body.username, body.password.get_secret_value(), body.role, user['id'])


@router.post('/users/{ident}')
def update_user(ident: str, body: UpdateUser, request: Request, user=Depends(require_admin)):
    return service(request).update(ident, user['id'], role=body.role, enabled=body.enabled,
                                    password=body.password.get_secret_value() if body.password is not None else None)
