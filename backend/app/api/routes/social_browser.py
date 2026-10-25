import hmac
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field


router = APIRouter(prefix='/social-monitor/browser', tags=['平台浏览器登录'])
Platform = Literal['weibo', 'xueqiu']


def login_service(request: Request):
    service = request.app.state.social_browser
    if not service.available:
        raise HTTPException(503, '请先在服务器私有配置中设置至少 24 位的 SOCIAL_BROWSER_LOGIN_KEY')
    supplied = request.headers.get('X-Browser-Login-Key', '')
    expected = service.settings.social_browser_login_key.get_secret_value()
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(403, '浏览器登录管理密钥不正确')
    return service


class Action(BaseModel):
    id: str = Field(pattern=r'^[0-9a-f]{32}$')
    action: Literal['click', 'check', 'reload', 'cancel']
    x: int | None = Field(default=None, ge=0, lt=1100)
    y: int | None = Field(default=None, ge=0, lt=800)


@router.get('/{platform}/status')
def status(platform: Platform, response: Response, service=Depends(login_service)):
    response.headers['Cache-Control'] = 'no-store'
    return service.status(platform)


@router.post('/{platform}/start')
def start(platform: Platform, response: Response, service=Depends(login_service)):
    response.headers['Cache-Control'] = 'no-store'
    try:
        return service.start(platform)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.post('/{platform}/action')
def action(platform: Platform, payload: Action, response: Response, service=Depends(login_service)):
    response.headers['Cache-Control'] = 'no-store'
    try:
        return service.action(platform, payload.id, payload.action, payload.x, payload.y)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.get('/{platform}/image/{job_id}')
def image(platform: Platform, job_id: str, service=Depends(login_service)):
    data = service.image(platform, job_id)
    if data is None:
        raise HTTPException(410, '登录画面已过期或尚未就绪')
    return Response(data, media_type='image/png', headers={
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        'Referrer-Policy': 'no-referrer',
    })
