from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.api.routes.accounts import require_admin
from app.services.accounts import AccountError
from app.services.etf_configuration import ConfigurationFailure
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
        raise HTTPException(status_code=422, detail='分组仅支持 ' + '、'.join(ETF_GROUPS) + '。')
    if days not in (0, 30, 90, 180, 365):
        raise HTTPException(status_code=422, detail='时间范围仅支持 0、30、90、180、365 天，0 表示全部。')
    end = end_date or china_today()
    if end > china_today():
        raise HTTPException(status_code=422, detail='结束日期不能晚于今天。')
    return ETFService(ETFRepository(session)).history(group, days, end)


@router.get('/collection-status')
def collection_status(request: Request):
    """Authenticated, read-only scheduler diagnostics; never expose writer credentials."""
    return request.app.state.etf_collection.status()


# Page administration keeps the same account, same-origin and CSRF gates as
# all other mutations. Raw validation bodies and driver errors never escape.
class CollectionConfiguration(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    revision: int = Field(ge=0, strict=True)
    username: str = Field(min_length=1, max_length=128)
    host: str | None = Field(default=None, min_length=1, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535, strict=True)
    database: str | None = Field(default=None, min_length=1, max_length=64)
    password: SecretStr | None = None
    enabled: bool = Field(strict=True)
    lookback_days: int = Field(ge=1, le=1500, strict=True)
    batch_days: int = Field(ge=1, le=20, strict=True)
    schedule: list[str] = Field(min_length=1, max_length=1)
    db_tls: bool = Field(strict=True)
    db_transport_acknowledged: bool = Field(default=False, strict=True)


async def collection_body(request):
    try:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                raise ValueError()
        parsed = CollectionConfiguration.model_validate_json(body)
        password = parsed.password.get_secret_value() if parsed.password is not None else None
        if password is not None and len(password) > 4096:
            raise ValueError()
        result = parsed.model_dump(exclude={'password'}) | {'password': password}
    except Exception:
        raise HTTPException(422, '采集配置格式无效，请检查账号、时间、范围及必填项') from None
    if request.url.scheme != 'https':
        raise HTTPException(403, '请使用HTTPS配置数据库账号，HTTP无法保护凭据')
    return result


async def collection_call(function, *args):
    try:
        return await run_in_threadpool(function, *args)
    except ConfigurationFailure as error:
        raise HTTPException(409, str(error)) from None
    except Exception:
        raise HTTPException(503, 'ETF采集管理暂不可用，请稍后重试；已保存配置及历史数据保留') from None


def public_configuration(request, result):
    return result | dict(transport_secure=request.url.scheme == 'https',
                         allow_insecure_transport=False, https_required=True)


@router.get('/collection-configuration', dependencies=[Depends(require_admin)])
async def collection_configuration(request: Request):
    result = await collection_call(request.app.state.etf_collection.configuration.public)
    return public_configuration(request, result)


@router.post('/collection-configuration', dependencies=[Depends(require_admin)])
async def save_collection_configuration(request: Request):
    result = await collection_call(request.app.state.etf_collection.configure, await collection_body(request))
    return public_configuration(request, result)


@router.post('/collection-test')
async def test_collection_configuration(request: Request, user=Depends(require_admin)):
    try:
        request.app.state.accounts.limit_probe(user['id'])
    except AccountError as error:
        raise HTTPException(error.status, str(error)) from None
    return await collection_call(request.app.state.etf_collection.test_configuration, await collection_body(request))


class CollectionRun(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(pattern=r'^[a-zA-Z0-9-]{16,64}$')
    retry_exhausted: bool = Field(default=False, strict=True)


@router.post('/collection-run', status_code=202, dependencies=[Depends(require_admin)])
async def run_collection(request: Request):
    try:
        raw = await request.body()
        if len(raw) > 1024:
            raise ValueError()
        body = CollectionRun.model_validate_json(raw)
    except Exception:
        raise HTTPException(422, '采集请求格式无效，请刷新页面重试') from None
    return await collection_call(request.app.state.etf_collection.request_run, body.request_id, body.retry_exhausted)
