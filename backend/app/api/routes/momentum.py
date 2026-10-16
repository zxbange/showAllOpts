from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.repositories.momentum import MomentumRepository
from app.schemas.momentum import IndustryHistory, Snapshot
from app.services.momentum import MomentumService, china_today

router = APIRouter(prefix='/industry-momentum', tags=['行业动量'])
IndustryType = Literal['sw_l1', 'sw_l2', 'sw_l3']


def get_service(session: Annotated[Session, Depends(get_session)]) -> MomentumService:
    return MomentumService(MomentumRepository(session))


@router.get('/latest', response_model=Snapshot)
def latest(service: Annotated[MomentumService, Depends(get_service)],
           industry_type: IndustryType = 'sw_l2') -> Snapshot:
    return service.latest(industry_type)


@router.get('/industries/{code}/history', response_model=IndustryHistory)
def history(code: str, service: Annotated[MomentumService, Depends(get_service)],
            industry_type: IndustryType = 'sw_l2', days: int = Query(default=365, ge=7, le=1095),
            end_date: date | None = None) -> IndustryHistory:
    if len(code) > 32:
        raise HTTPException(status_code=422, detail='行业代码长度不能超过 32。')
    if end_date and end_date > china_today():
        raise HTTPException(status_code=422, detail='结束日期不能晚于今天。')
    result = service.history(industry_type, code, days, end_date)
    if result is None:
        raise HTTPException(status_code=404, detail='未找到该行业的数据。')
    return result
