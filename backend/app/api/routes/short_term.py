from datetime import date
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.core.database import get_session
from app.repositories.short_term import ShortTermRepository
from app.services.short_term import ShortTermService
from app.schemas.short_term import ShortTermOverview

router = APIRouter(prefix='/short-term', tags=['短线选股'])


@router.get('/latest', response_model=ShortTermOverview)
def latest(session: Annotated[Session, Depends(get_session)], day: date | None = None,
           method: Literal['bukou', 'redhover', 'xuli', 'oversold'] = 'bukou',
           offset: int = Query(0, ge=0), limit: int = Query(6, ge=1, le=24)):
    return ShortTermService(ShortTermRepository(session)).overview(day, method, offset, limit)

# Validation management includes credentials/network probes, so all operations are admin-only.
import hmac
from fastapi import Request, HTTPException, Response
from pydantic import BaseModel, Field, SecretStr, ValidationError
from starlette.concurrency import run_in_threadpool
from app.services.validation_sources import SourceError
from app.services.validation_access import ValidationAccess, AccessError, COOKIE, COOKIE_PATH, canonical_origin


class SourceRef(BaseModel):
    id: str = Field(pattern=r'^(?:service|[0-9a-f]{32})$')
    revision: int = Field(ge=1)


class ValidationRequest(BaseModel):
    methods: list[Literal['bukou', 'redhover', 'xuli', 'oversold']] = Field(min_length=1, max_length=4)
    start: date = date(2024, 1, 1)
    end: date | None = None
    observed: date | None = None
    request_id: str | None = Field(default=None, max_length=64, pattern=r'^[a-zA-Z0-9-]+$')
    source: SourceRef | None = None


class ConnectionInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=253, pattern=r'^[a-zA-Z0-9.:-]+$')
    port: int = Field(default=3306, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr | None = None
    tls: Literal['verify', 'required', 'disabled'] = 'verify'
    ca_pem: str = Field(default='', max_length=32768)
    id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    revision: int | None = Field(default=None, ge=1)


def access_service(request):
    return ValidationAccess(request.app.state.short_validation.sources.settings)


def request_origin(request, mutation=False):
    from urllib.parse import urlsplit
    supplied = request.headers.get('origin')
    declared = request.headers.get('x-validation-origin')
    try:
        upstream = canonical_origin(str(request.base_url))
        candidate = canonical_origin(supplied or declared or upstream)
    except AccessError:
        raise HTTPException(403, '站点来源无效，请从本站验证页面操作') from None
    # Existing trusted proxies can strip the external port from Host. The exact
    # origin is authorized by the console-issued code / persisted session below,
    # never by a caller-controlled header alone. Still require same host/scheme.
    actual, proxied = urlsplit(candidate), urlsplit(upstream)
    if (request.headers.get('sec-fetch-site') in ('cross-site', 'same-site')
            or (supplied is not None and supplied != candidate)
            or (declared is not None and declared != candidate)
            or (mutation and supplied != candidate)
            or actual.hostname != proxied.hostname or actual.scheme != proxied.scheme):
        raise HTTPException(403, '仅允许本站页面请求，请从本站验证页面操作')
    return candidate


def paired_session(request, mutation=False):
    origin = request_origin(request, mutation)
    if request.headers.get('x-validation-client') != 'browser':
        raise HTTPException(403, '请从本站验证页面操作')
    try:
        session = access_service(request).session(request.cookies.get(COOKIE, ''), origin)
    except AccessError as error:
        raise HTTPException(error.status, str(error)) from None
    if mutation and not hmac.compare_digest(request.headers.get('x-validation-csrf', '').encode(), session['csrf_token'].encode()):
        raise HTTPException(403, '页面校验已失效，请刷新后重试')
    return session


def validation_admin(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Vary'] = 'Authorization, Cookie'
    service = request.app.state.short_validation
    settings = service.sources.settings
    expected = settings.validation_admin_key.get_secret_value()
    if not expected:
        paired_session(request, request.method != 'GET')
        request.state.validation_paired = True
        return service
    if len(expected) < 32:
        raise HTTPException(503, '既有管理密钥无效；配对不能绕过已有管理设置')
    if request.url.scheme != 'https' and not settings.validation_allow_http:
        raise HTTPException(403, '验证管理须经HTTPS访问；请正确配置可信反向代理')
    supplied = request.headers.get('Authorization', '')
    if not hmac.compare_digest(supplied.encode(), ('Bearer '+expected).encode()):
        raise HTTPException(403, '验证管理密钥不正确')
    return service


def public_job(job):
    return {key: value for key, value in job.items() if key not in ('calendar', 'codes')}


@router.get('/validation/access')
def validation_access(request: Request, response: Response):
    from app.services.validation_keys import POSIX_KEY_STORAGE
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Vary'] = 'Cookie'
    access = access_service(request)
    settings = access.settings
    origin = request_origin(request)
    data = {'mode': 'legacy' if access.legacy else 'pair', 'authenticated': False,
            'admin_configured': len(settings.validation_admin_key.get_secret_value()) >= 32,
            'encryption_configured': bool(settings.validation_encryption_key.get_secret_value()),
            'encryption_automatic': POSIX_KEY_STORAGE,
            'targets_configured': bool(settings.validation_allowed_targets),
            'https_required': access.legacy and not settings.validation_allow_http}
    if not access.legacy and request.cookies.get(COOKIE):
        try:
            data.update(access.session(request.cookies[COOKIE], origin))
            data['authenticated'] = True
        except AccessError:
            pass
    return data


@router.post('/validation/pair')
async def pair_validation(request: Request, response: Response):
    origin = request_origin(request, True)
    if request.headers.get('x-validation-client') != 'browser':
        raise HTTPException(403, '请从本站验证页面操作')
    try:
        raw = await request.body()
        if len(raw) > 1024:
            raise ValueError()
        import json
        body = json.loads(raw)
        code = body.get('code')
        if not isinstance(code, str) or not 1 <= len(code) <= 100:
            raise ValueError()
        confirmed = body.get('http_confirmed') is True
    except Exception:
        raise HTTPException(422, '配对输入格式无效') from None
    try:
        access = access_service(request)
        token = access.pair(code, origin, confirmed)
        session = access.session(token, origin)
    except AccessError as error:
        raise HTTPException(error.status, str(error)) from None
    response.set_cookie(COOKIE, token, httponly=True, secure=request.url.scheme == 'https', samesite='strict', path=COOKIE_PATH)
    response.headers['Cache-Control'] = 'no-store'
    return session


@router.post('/validation/logout')
def logout_validation(request: Request, response: Response):
    paired_session(request, True)
    access_service(request).logout(request.cookies[COOKIE])
    response.delete_cookie(COOKIE, path=COOKIE_PATH, httponly=True, secure=request.url.scheme == 'https', samesite='strict')
    response.headers['Cache-Control'] = 'no-store'
    return {'ok': True}


@router.get('/validation/jobs')
def validation_jobs(service=Depends(validation_admin)):
    return {'jobs': [public_job(job) for job in service.list()]}


@router.post('/validation/jobs')
def create_validation(body: ValidationRequest, request: Request, service=Depends(validation_admin)):
    try:
        source = body.source.model_dump() if body.source else None
        if getattr(request.state, 'validation_paired', False) and (source is None or source['id'] == 'service'):
            raise ValueError('请先填写并保存要验证的数据库连接')
        return public_job(service.create(body.methods, body.start, body.end, body.observed, body.request_id, source))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post('/validation/jobs/{ident}/{action}')
def validation_action(ident: str, action: Literal['retry', 'pause', 'cancel'], service=Depends(validation_admin)):
    try:
        return public_job(service.action(ident, action))
    except KeyError:
        raise HTTPException(404, '任务不存在') from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get('/validation/sources')
def validation_sources(service=Depends(validation_admin)):
    try:
        return {'sources': service.sources.list()}
    except SourceError as exc:
        raise HTTPException(409, str(exc)) from None


async def connection_input(request, service):
    # Do not use automatic body validation: FastAPI's default errors include raw inputs.
    try:
        raw = await request.body()
        if len(raw) > 65536:
            raise ValueError()
        body = ConnectionInput.model_validate_json(raw)
        if body.password is not None and len(body.password.get_secret_value()) > 4096:
            raise ValueError()
    except Exception:
        raise HTTPException(422, '连接配置格式无效，请检查必填项、端口和TLS选项；敏感内容不会返回') from None
    config = body.model_dump(exclude={'id', 'revision', 'password'})
    config['password'] = body.password.get_secret_value() if body.password is not None else None
    if config['password'] is None:
        if not body.id or not body.revision:
            raise HTTPException(422, '新连接需要填写密码')
        try:
            old = service.sources.get(body.id, body.revision)
        except SourceError as exc:
            raise HTTPException(409, str(exc)) from None
        config['password'] = old['password']
    return body, config


@router.post('/validation/sources/save')
async def save_validation_source(request: Request, service=Depends(validation_admin)):
    body, config = await connection_input(request, service)
    try:
        ref = await run_in_threadpool(service.sources.save, config, body.id, body.revision)
        return service.sources.public(ref['id'], ref['revision'], config)
    except SourceError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post('/validation/sources/test')
async def test_validation_source(request: Request, service=Depends(validation_admin)):
    _, config = await connection_input(request, service)
    try:
        if not access_service(request).legacy:
            access_service(request).limit_probe(request.cookies[COOKIE])
        return await run_in_threadpool(service.sources.test, config)
    except AccessError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    except SourceError as exc:
        raise HTTPException(409, str(exc)) from None
