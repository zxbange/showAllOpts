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

# Uses the same deployment access boundary as cache/refresh and margin jobs.
from fastapi import Request, HTTPException
from pydantic import BaseModel, Field


class ValidationRequest(BaseModel):
    methods: list[Literal['bukou', 'redhover', 'xuli', 'oversold']] = Field(min_length=1, max_length=4)
    start: date = date(2024, 1, 1)
    end: date | None = None
    observed: date | None = None
    request_id: str | None = Field(default=None, max_length=64, pattern=r"^[a-zA-Z0-9-]+$")


def public_job(job):
    return {key: value for key, value in job.items() if key not in ('calendar', 'codes')}


@router.get('/validation/jobs')
def validation_jobs(request: Request):
    return {'jobs': [public_job(job) for job in request.app.state.short_validation.list()]}


@router.post('/validation/jobs')
def create_validation(body: ValidationRequest, request: Request):
    try:
        return public_job(request.app.state.short_validation.create(body.methods, body.start, body.end, body.observed, body.request_id))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post('/validation/jobs/{ident}/{action}')
def validation_action(ident: str, action: Literal['retry', 'cancel'], request: Request):
    try:
        return public_job(request.app.state.short_validation.action(ident, action))
    except KeyError:
        raise HTTPException(404, '任务不存在') from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
