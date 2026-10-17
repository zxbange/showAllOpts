from datetime import date
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.core.database import get_session
from app.repositories.earnings import EarningsRepository
from app.schemas.earnings import EarningsOverview, StockDetail
from app.services.earnings import EarningsService

router = APIRouter(prefix='/earnings-gap', tags=['净利润断层'])


@router.get('/stocks', response_model=EarningsOverview)
def stocks(
    session: Annotated[Session, Depends(get_session)],
    period: date | None = None,
    search: str = Query('', max_length=64),
    industry: str = Query('', max_length=32),
    source: Literal['', 'express', 'forecast', 'formal'] = '',
    sort: Literal['signal_date', 'profit_yoy', 'open_gap_pct', 'market_cap', 'pe_ttm', 'pb'] = 'signal_date',
    direction: Literal['asc', 'desc'] = 'desc',
    page: int = Query(1, ge=1, le=10000),
    page_size: int = Query(30, ge=1, le=100),
) -> EarningsOverview:
    return EarningsService(EarningsRepository(session)).overview(
        period, search.strip(), industry, source, sort, direction, page, page_size)


@router.get('/stocks/{code}', response_model=StockDetail)
def detail(code: str, period: date, session: Annotated[Session, Depends(get_session)]) -> StockDetail:
    return EarningsService(EarningsRepository(session)).detail(period, code)
