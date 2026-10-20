"""Manual module synchronization; concurrent refresh jobs share a single lock."""
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from app.core.cache import MODULES

router = APIRouter(prefix='/cache', tags=['数据同步'])


class RefreshRequest(BaseModel):
    module: str | None = Field(default=None, max_length=64)


@router.get('/status')
def status(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return request.app.state.cache.status()


@router.post('/refresh', status_code=202)
def refresh(body: RefreshRequest, request: Request):
    cache = request.app.state.cache
    if not cache.settings.cache_enabled:
        raise HTTPException(status_code=409, detail='当前未启用本地缓存。')
    if body.module is not None and body.module not in MODULES:
        raise HTTPException(status_code=422, detail='未知模块。')
    if not cache.refresh(body.module):
        raise HTTPException(status_code=409, detail='已有同步任务正在执行，请等待完成。')
    return {'accepted': True}
