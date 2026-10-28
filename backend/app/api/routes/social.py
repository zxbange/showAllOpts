from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, SecretStr

from app.services.social_cube_sources import cube_symbol
from app.services.social_sources import author_uid

def monitor(request: Request):
    return request.app.state.social


class SecretSafeRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError as error:
                if request.url.path.endswith('/config'):
                    # Pydantic's default errors include raw input, even for secrets.
                    detail = [{key: item[key] for key in ('loc', 'msg', 'type') if key in item}
                              for item in error.errors()]
                    raise HTTPException(422, detail=detail) from None
                raise
        return safe_handler


router = APIRouter(prefix='/social-monitor', tags=['博主监控'], route_class=SecretSafeRoute)


class ConfigUpdate(BaseModel):
    enabled: bool | None = None
    interval_min: int | None = Field(default=None, strict=True, ge=60, le=86400, multiple_of=60)
    interval_max: int | None = Field(default=None, strict=True, ge=60, le=86400, multiple_of=60)
    token: SecretStr | None = Field(default=None, max_length=300)
    chat_id: SecretStr | None = Field(default=None, max_length=100)
    telegram_enabled: bool | None = None
    feishu_enabled: bool | None = None
    feishu_webhook: SecretStr | None = Field(default=None, max_length=2048)
    feishu_secret: SecretStr | None = Field(default=None, max_length=512)
    weibo_cookie: SecretStr | None = Field(default=None, max_length=20000)
    xueqiu_cookie: SecretStr | None = Field(default=None, max_length=20000)


class AuthorInput(BaseModel):
    platform: Literal['weibo', 'xueqiu']
    url: str = Field(min_length=1, max_length=500)
    name: str = Field(default='', max_length=100)


class AuthorUpdate(BaseModel):
    enabled: bool


class CubeInput(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    name: str = Field(default='', max_length=100)


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


@router.post('/cubes')
def add_cube(payload: CubeInput, service=Depends(monitor)):
    try:
        symbol = cube_symbol(payload.url)
        service.add_cube(symbol, payload.name.strip())
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return service.overview()


@router.post('/cubes/{identifier}')
def update_cube(identifier: int, payload: AuthorUpdate, service=Depends(monitor)):
    if not service.change_cube(identifier, payload.enabled):
        raise HTTPException(404, '监控组合不存在')
    return service.overview()


@router.post('/cubes/{identifier}/delete')
def delete_cube(identifier: int, service=Depends(monitor)):
    if not service.change_cube(identifier, delete=True):
        raise HTTPException(404, '监控组合不存在')
    return service.overview()


@router.get('/cube-rebalances')
def cube_rebalances(cube_id: int | None = None,
                    page: int = Query(default=1, ge=1), size: int = Query(default=20, ge=1, le=100),
                    service=Depends(monitor)):
    return service.cube_rebalances(cube_id, page, size)


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


@router.post('/feishu-test')
def feishu_test(service=Depends(monitor)):
    try:
        service.test_feishu()
    except (ValueError, RuntimeError) as error:
        raise HTTPException(400, str(error)) from None
    return {'message': '测试消息已发送，请检查飞书群'}
