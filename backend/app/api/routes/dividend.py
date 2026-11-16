from typing import Annotated
from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.orm import Session
from app.core.database import get_session
from app.repositories.dividend import DividendRepository
from app.schemas.dividend import DividendOverview
from app.services.dividend import DividendService

router = APIRouter(prefix='/dividend-selection', tags=['高股息选股'])


@router.get('/latest', response_model=DividendOverview)
def latest(session: Annotated[Session, Depends(get_session)]) -> DividendOverview:
    return DividendService(DividendRepository(session)).latest()


def collection(request: Request):
    return request.app.state.dividend_collection


@router.get('/status')
def collection_status(service=Depends(collection)):
    return service.status()


@router.post('/refresh', status_code=202)
def refresh(service=Depends(collection)):
    from app.services.dividend_collection import DividendCollectionError
    try:
        return {'started': service.refresh()}
    except DividendCollectionError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post('/cancel', status_code=202)
def cancel(service=Depends(collection)):
    return {'cancelled': service.cancel()}
