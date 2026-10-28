"""Manual module synchronization; concurrent refresh jobs share a single lock."""
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from app.core.cache import MODULES

router = APIRouter(prefix='/cache', tags=['数据同步'])


class RefreshRequest(BaseModel):
    module: str | None = Field(default=None, max_length=64)
    full: bool = False


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
    accepted = cache.refresh(body.module, full=True) if body.full else cache.refresh(body.module)
    if not accepted:
        raise HTTPException(status_code=409, detail='已有同步任务正在执行，请等待完成。')
    return {'accepted': True}


class CancelRequest(BaseModel):
    module: str | None = Field(default=None, max_length=64)
    run_id: str = Field(pattern=r'^[0-9a-f]{32}$')


@router.post('/cancel', status_code=202)
def cancel(body: CancelRequest, request: Request):
    if not request.app.state.cache.cancel(body.run_id, body.module):
        raise HTTPException(status_code=409, detail='该同步任务已结束，请刷新状态。')
    return {'accepted': True}
