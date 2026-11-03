from fastapi import APIRouter,Depends,Request,Response
from app.api.routes.accounts import require_admin
router=APIRouter(prefix='/commodities',tags=['大宗商品'])
def service(request:Request):return request.app.state.commodities
@router.get('/a00')
def history(response:Response, monitor=Depends(service)):
    response.headers['Cache-Control']='no-store'
    return monitor.history()
@router.post('/a00/refresh',dependencies=[Depends(require_admin)],status_code=202)
def refresh(monitor=Depends(service)):
    return {'started':monitor.refresh()}


def coal_service(request: Request):
    return request.app.state.cci_coal

@router.get('/cci')
def coal_history(response: Response, monitor=Depends(coal_service)):
    response.headers['Cache-Control']='no-store'
    return monitor.history()

@router.post('/cci/refresh', dependencies=[Depends(require_admin)], status_code=202)
def coal_refresh(monitor=Depends(coal_service)):
    return {'started': monitor.refresh()}
