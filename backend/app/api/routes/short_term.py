from datetime import date
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.core.database import get_session
from app.repositories.short_term import ShortTermRepository
from app.services.short_term import ShortTermService
from app.schemas.short_term import ShortTermOverview

router = APIRouter(prefix='/short-term', tags=['短线选股'])


@router.get('/latest', response_model=ShortTermOverview)
def latest(session: Annotated[Session, Depends(get_session)], day: date | None = None,
           method: Literal['bukou', 'redhover', 'xuli', 'oversold'] = 'bukou',
           offset: int = Query(0, ge=0), limit: int = Query(6, ge=1, le=24)):
    return ShortTermService(ShortTermRepository(session)).overview(day, method, offset, limit)
