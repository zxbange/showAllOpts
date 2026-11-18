from fastapi import APIRouter, Request, Response, HTTPException
from app.services.collection_outcomes import latest_outcomes, OutcomeUnavailable

router = APIRouter()


@router.get('/collection-outcomes')
def outcomes(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        return latest_outcomes(request.app.state.accounts.settings)
    except OutcomeUnavailable:
        raise HTTPException(503, '采集结果暂不可用') from None
