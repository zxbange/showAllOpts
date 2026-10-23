from datetime import date
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session
from app.core.database import get_session
from app.repositories.ownership import OwnershipRepository
from app.schemas.ownership import OwnershipSearch, OwnershipDetail, SocialEntries
from app.services.ownership import OwnershipService, stock_item

router = APIRouter(prefix='/institution-ownership', tags=['个股机构持仓'])

@router.get('/stocks', response_model=OwnershipSearch)
def search(session: Annotated[Session, Depends(get_session)], q: str = Query('', max_length=80)):
    return {'stocks': [stock_item(s) for s in OwnershipRepository(session).search(q.strip())]}

@router.get('/stocks/{code}', response_model=OwnershipDetail)
def detail(session: Annotated[Session, Depends(get_session)],
           code: str = Path(pattern=r'^\d{6}(\.(XSHG|XSHE|XBSE))?$')):
    return OwnershipService(OwnershipRepository(session)).detail(code)

@router.get('/social-entries', response_model=SocialEntries)
def social(session: Annotated[Session, Depends(get_session)], period: date | None = None,
           mode: Literal['first', 'combination', 'increase'] = 'first', q: str = Query('', max_length=80)):
    return OwnershipService(OwnershipRepository(session)).social(period, mode, q.strip())
