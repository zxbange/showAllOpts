from datetime import date
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from app.core.database import get_session
from app.repositories.sentiment import SentimentRepository
from app.schemas.sentiment import SentimentHistory
from app.services.sentiment import SentimentService
from app.services.momentum import china_today
from app.services.market_provenance import history_provenance

router = APIRouter(prefix='/market-sentiment', tags=['市场热度'])


@router.get('/history', response_model=SentimentHistory)
def history(request: Request, session: Annotated[Session, Depends(get_session)], days: int = Query(default=0, ge=0, le=365),
            end_date: date | None = None) -> SentimentHistory:
    if days not in (0, 30, 90, 180, 365):
        raise HTTPException(status_code=422, detail='时间范围仅支持 0、30、90、180、365 天，0 表示全部已采集历史。')
    end = end_date or china_today()
    if end > china_today():
        raise HTTPException(status_code=422, detail='结束日期不能晚于今天。')
    return SentimentService(SentimentRepository(session), history_provenance(request, 'heat')).history(days, end)
