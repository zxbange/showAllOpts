from fastapi import APIRouter, Request, Response, HTTPException, Query
from app.services.collection_outcomes import latest_outcomes, outcome_history, MODULES, OutcomeUnavailable

router = APIRouter()


@router.get('/collection-outcomes')
def outcomes(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        return latest_outcomes(request.app.state.accounts.settings)
    except OutcomeUnavailable:
        raise HTTPException(503, '采集结果暂不可用') from None


@router.get('/collection-outcomes/history')
def history(request: Request, response: Response, module: str = Query(...), limit: int = Query(5, ge=1, le=5)):
    response.headers['Cache-Control'] = 'no-store'
    if module not in MODULES:
        raise HTTPException(422, '未知采集模块')
    try:
        return outcome_history(request.app.state.accounts.settings, module, limit)
    except OutcomeUnavailable:
        raise HTTPException(503, '采集历史暂不可用') from None
