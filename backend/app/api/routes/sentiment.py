from datetime import date
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.core.database import get_session
from app.repositories.sentiment import SentimentRepository
from app.schemas.sentiment import SentimentHistory
from app.services.sentiment import SentimentService
from app.services.momentum import china_today

router = APIRouter(prefix='/market-sentiment', tags=['市场情绪跟踪'])


@router.get('/history', response_model=SentimentHistory)
def history(session: Annotated[Session, Depends(get_session)], days: int = Query(default=0, ge=0, le=365),
            end_date: date | None = None) -> SentimentHistory:
    if days not in (0, 30, 90, 180, 365):
        raise HTTPException(status_code=422, detail='时间范围仅支持 0、30、90、180、365 天，0 表示近三年。')
    end = end_date or china_today()
    if end > china_today():
        raise HTTPException(status_code=422, detail='结束日期不能晚于今天。')
    return SentimentService(SentimentRepository(session)).history(days, end)
