from typing import Annotated
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.database import get_session
from app.repositories.dividend import DividendRepository
from app.schemas.dividend import DividendOverview
from app.services.dividend import DividendService

router = APIRouter(prefix='/dividend-selection', tags=['高股息选股'])


@router.get('/latest', response_model=DividendOverview)
def latest(session: Annotated[Session, Depends(get_session)]) -> DividendOverview:
    return DividendService(DividendRepository(session)).latest()
