from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, SecretStr

from app.services.social_sources import author_uid

security = HTTPBasic(auto_error=False)


def monitor(request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    service = request.app.state.social
    valid, limited = service.authenticate(
        credentials.username if credentials else '', credentials.password if credentials else '',
        request.client.host if request.client else 'unknown')
    if limited:
        raise HTTPException(429, '登录失败过多，请一分钟后再试')
    if not valid:
        raise HTTPException(401, '账号或密码错误，请重新登录')
    return service


router = APIRouter(prefix='/social-monitor', tags=['博主监控'], dependencies=[Depends(monitor)])


class ConfigUpdate(BaseModel):
    enabled: bool | None = None
    token: SecretStr | None = Field(default=None, max_length=300)
    chat_id: SecretStr | None = Field(default=None, max_length=100)
    weibo_cookie: SecretStr | None = Field(default=None, max_length=20000)
    xueqiu_cookie: SecretStr | None = Field(default=None, max_length=20000)


class AuthorInput(BaseModel):
    platform: Literal['weibo', 'xueqiu']
    url: str = Field(min_length=1, max_length=500)
    name: str = Field(default='', max_length=100)


class AuthorUpdate(BaseModel):
    enabled: bool


@router.get('/status')
def status(service=Depends(monitor)):
    return service.overview()


@router.post('/config')
def save_config(payload: ConfigUpdate, service=Depends(monitor)):
    changes = {key: value.get_secret_value() if isinstance(value, SecretStr) else value
               for key, value in payload.model_dump(exclude_unset=True).items()}
    try:
        service.update_config(changes)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return service.overview()


@router.post('/authors')
def add_author(payload: AuthorInput, service=Depends(monitor)):
    try:
        uid = author_uid(payload.model_dump())
        service.add_author(payload.platform, uid, payload.name.strip())
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return service.overview()


@router.post('/authors/{identifier}')
def update_author(identifier: int, payload: AuthorUpdate, service=Depends(monitor)):
    if not service.change_author(identifier, payload.enabled):
        raise HTTPException(404, '监控用户不存在')
    return service.overview()


@router.post('/authors/{identifier}/delete')
def delete_author(identifier: int, service=Depends(monitor)):
    if not service.change_author(identifier, delete=True):
        raise HTTPException(404, '监控用户不存在')
    return service.overview()


@router.get('/posts')
def posts(author_id: int | None = None, platform: Literal['weibo', 'xueqiu'] | None = None,
          page: int = Query(default=1, ge=1), size: int = Query(default=20, ge=1, le=100),
          keyword: str = Query(default='', max_length=200), service=Depends(monitor)):
    return service.posts(author_id, platform, page, size, keyword)


@router.post('/telegram-test')
def telegram_test(service=Depends(monitor)):
    try:
        service.test_telegram()
    except (ValueError, RuntimeError) as error:
        raise HTTPException(400, str(error)) from None
    return {'message': '测试消息已发送，请检查 Telegram'}


@router.post('/platforms/{platform}/resume')
def resume_platform(platform: Literal['weibo', 'xueqiu'], service=Depends(monitor)):
    service.resume_platform(platform)
    return service.overview()
