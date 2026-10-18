from datetime import date
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.core.database import get_session
from app.repositories.breadth import BreadthRepository
from app.schemas.breadth import BreadthHistory
from app.services.breadth import BreadthService
from app.services.momentum import china_today

router = APIRouter(prefix='/microcap-breadth', tags=['微盘新高新低'])


@router.get('/history', response_model=BreadthHistory)
def history(session: Annotated[Session, Depends(get_session)],
            days: int = Query(default=180, ge=1, le=300), end_date: date | None = None) -> BreadthHistory:
    if days not in (30, 90, 180, 300):
        raise HTTPException(status_code=422, detail='仅支持最近 30、90、180、300 个已采集交易日。')
    end = end_date or china_today()
    if end > china_today():
        raise HTTPException(status_code=422, detail='结束日期不能晚于今天。')
    return BreadthService(BreadthRepository(session)).history(days, end)
