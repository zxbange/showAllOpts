from fastapi import APIRouter, Depends, Request, Response

router = APIRouter(prefix='/growth-dividend', tags=['创红比'])


def service(request: Request):
    return request.app.state.growth_dividend


@router.get('/history')
def history(response: Response, monitor=Depends(service)):
    response.headers['Cache-Control'] = 'no-store'
    return monitor.history()


@router.get('/status')
def status(response: Response, monitor=Depends(service)):
    response.headers['Cache-Control'] = 'no-store'
    return monitor.status()


@router.post('/refresh', status_code=202)
def refresh(monitor=Depends(service)):
    return {'started': monitor.refresh()}


@router.post('/cancel', status_code=202)
def cancel(monitor=Depends(service)):
    return {'cancelled': monitor.cancel()}


@router.post('/sync-joinquant', status_code=202)
def sync_joinquant(monitor=Depends(service)):
    from app.services.growth_dividend_import import start_import
    return {'started': start_import(monitor)}
