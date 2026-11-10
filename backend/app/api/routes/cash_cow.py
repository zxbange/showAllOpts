from fastapi import APIRouter, Depends, Request, Query, HTTPException

router = APIRouter(prefix='/cash-cow', tags=['奶牛股'])


def service(request: Request, years: int = Query(10)):
    if years not in (3, 5, 10):
        raise HTTPException(422, '仅支持近3年、5年或10年')
    return request.app.state.cash_cow[years]


@router.get('/latest')
def latest(screen=Depends(service)):
    return screen.latest()


@router.get('/status')
def status(screen=Depends(service)):
    return screen.status()


@router.post('/refresh', status_code=202)
def refresh(screen=Depends(service)):
    return {'started': screen.refresh()}


@router.post('/cancel', status_code=202)
def cancel(screen=Depends(service)):
    return {'cancelled': screen.cancel()}
