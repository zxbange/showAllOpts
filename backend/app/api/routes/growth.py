from typing import Annotated
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.database import get_session
from app.repositories.growth import GrowthRepository
from app.schemas.growth import GrowthOverview
from app.services.growth import GrowthService

router = APIRouter(prefix='/high-growth', tags=['高增长'])


@router.get('/latest', response_model=GrowthOverview)
def latest(session: Annotated[Session, Depends(get_session)]) -> GrowthOverview:
    return GrowthService(GrowthRepository(session)).latest()
