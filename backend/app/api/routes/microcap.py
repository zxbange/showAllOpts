from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.repositories.microcap import MicrocapRepository
from app.schemas.microcap import MicrocapHistory
from app.services.microcap import MicrocapService
from app.services.momentum import china_today

router = APIRouter(prefix='/microcap-temperature', tags=['微盘温度计'])


@router.get('/series', response_model=list[str])
def available_series(session: Annotated[Session, Depends(get_session)]) -> list[str]:
    return MicrocapRepository(session).available_series()


@router.get('/history', response_model=MicrocapHistory)
def history(
    session: Annotated[Session, Depends(get_session)],
    days: int = Query(default=365, ge=0, le=365),
    end_date: date | None = None,
    series_id: str | None = None,
) -> MicrocapHistory:
    if days not in (0, 30, 90, 180, 365):
        raise HTTPException(status_code=422, detail='时间范围仅支持 0、30、90、180、365 天，0 表示全部。')
    if series_id is not None and not MicrocapRepository.supported_series(series_id):
        raise HTTPException(status_code=422, detail='不支持的微盘序列。')
    end = end_date or china_today()
    if end > china_today():
        raise HTTPException(status_code=422, detail='结束日期不能晚于今天。')
    return MicrocapService(MicrocapRepository(session)).history(days, end, series_id)
