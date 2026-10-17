from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.etf_catalog import ETF_GROUPS
from app.repositories.etf import ETFRepository
from app.schemas.etf import ETFHistory
from app.services.etf import ETFService
from app.services.momentum import china_today

router = APIRouter(prefix='/etf-shares', tags=['ETF份额仪表盘'])


@router.get('/history', response_model=ETFHistory)
def history(
    session: Annotated[Session, Depends(get_session)],
    group: str = 'hs300',
    days: int = Query(default=365, ge=0, le=365),
    end_date: date | None = None,
) -> ETFHistory:
    if group not in ETF_GROUPS:
        raise HTTPException(status_code=422, detail='分组仅支持 hs300、zz500。')
    if days not in (0, 30, 90, 180, 365):
        raise HTTPException(status_code=422, detail='时间范围仅支持 0、30、90、180、365 天，0 表示全部。')
    end = end_date or china_today()
    if end > china_today():
        raise HTTPException(status_code=422, detail='结束日期不能晚于今天。')
    return ETFService(ETFRepository(session)).history(group, days, end)
