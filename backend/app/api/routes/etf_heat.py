from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.core.database import get_session
from app.repositories.etf_heat import ETFHeatRepository
from app.services.etf_heat import ETFHeatService
from app.schemas.etf_heat import HeatHistory, HeatSnapshot

router = APIRouter(prefix='/etf-momentum', tags=['ETF动量热度'])

def service(session: Annotated[Session, Depends(get_session)]):
    return ETFHeatService(ETFHeatRepository(session))

@router.get('/latest', response_model=HeatSnapshot)
def latest(current: Annotated[ETFHeatService, Depends(service)]):
    return current.latest()

@router.get('/funds/{code}/history', response_model=HeatHistory)
def history(code: str, current: Annotated[ETFHeatService, Depends(service)],
            days: int = Query(365, ge=7, le=1095)):
    if len(code)>16:
        raise HTTPException(422, '基金代码过长')
    result = current.history(code, days)
    if result is None:
        raise HTTPException(404, '该基金暂无动量记录')
    return result
