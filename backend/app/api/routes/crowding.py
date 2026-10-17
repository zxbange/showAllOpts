from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.repositories.crowding import CrowdingRepository
from app.schemas.crowding import CrowdingHistory
from app.services.crowding import CrowdingService
from app.services.momentum import china_today

router = APIRouter(prefix='/market-crowding', tags=['抱团拥挤度'])


@router.get('/history', response_model=CrowdingHistory)
def history(
    session: Annotated[Session, Depends(get_session)],
    days: int = Query(default=365, ge=0, le=365),
    end_date: date | None = None,
) -> CrowdingHistory:
    if days not in (0, 30, 90, 180, 365):
        raise HTTPException(status_code=422, detail='时间范围仅支持 0、30、90、180、365 天，0 表示全部。')
    end = end_date or china_today()
    if end > china_today():
        raise HTTPException(status_code=422, detail='结束日期不能晚于今天。')
    return CrowdingService(CrowdingRepository(session)).history(days, end)
