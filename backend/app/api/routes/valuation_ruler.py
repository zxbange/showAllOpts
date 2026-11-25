from fastapi import APIRouter, Depends, Request, Response
from typing import Literal
import logging

LOG = logging.getLogger(__name__)

router=APIRouter(prefix='/valuation-ruler',tags=['估值标尺'])


def service(request: Request):
    return request.app.state.valuation_ruler


@router.get('/history')
def history(response: Response,monitor=Depends(service),
            index_code: Literal['000300.XSHG','000016.XSHG','000852.XSHG']='000300.XSHG',
            basis: Literal['ttm','lyr']='ttm'):
    response.headers['Cache-Control']='no-store'
    return monitor.history(index_code=index_code,basis=basis)


@router.get('/status')
def status(response: Response,monitor=Depends(service)):
    response.headers['Cache-Control']='no-store'
    return monitor.status()


@router.post('/refresh',status_code=202)
def refresh(monitor=Depends(service)):
    from app.services.valuation_ruler import ValuationNotDue
    try:
        run_id = monitor.refresh(return_run_id=True)
        if run_id:
            LOG.info('Valuation manual collection accepted run=%s', run_id)
            return {'started': True, 'run_id': run_id}
        LOG.info('Valuation manual collection not started reason=busy_or_unavailable')
        return {'started': False, 'detail': '当前未启动新采集，请等待已有任务结束或稍后重试。'}
    except ValuationNotDue as exc:
        # Definite no-start, not an ambiguous failed mutation response.
        LOG.info('Valuation manual collection not started reason=not_due')
        return {'started': False, 'detail': str(exc)}


@router.post('/cancel',status_code=202)
def cancel(monitor=Depends(service)):
    return {'cancelled':monitor.cancel()}
