from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, SecretStr
from starlette.concurrency import run_in_threadpool

from app.api.routes.accounts import require_admin
from app.services.accounts import AccountError
from app.services.holdings_snapshot import HoldingsError
from app.services.database_sources import SourceError

router = APIRouter(prefix='/personal-holdings', tags=['个人持仓'], dependencies=[Depends(require_admin)])


def service(request: Request):
    return request.app.state.personal_holdings


class Connection(BaseModel):
    host: str = Field(min_length=1, max_length=253, pattern=r'^[a-zA-Z0-9.:-]+$')
    port: int = Field(default=3306, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=128)
    table: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
    password: SecretStr | None = None


async def configuration_body(request):
    try:
        raw = await request.body()
        if len(raw) > 16384:
            raise ValueError()
        body = Connection.model_validate_json(raw)
        password = body.password.get_secret_value() if body.password is not None else None
        if password is not None and len(password)>4096:
            raise ValueError()
        return body.model_dump(exclude={'password'}) | {'password': password}
    except Exception:
        raise HTTPException(422, '配置格式无效，请检查必填项、端口与表名') from None


async def safe_call(function, *args):
    try:
        return await run_in_threadpool(function, *args)
    except (HoldingsError, SourceError) as error:
        raise HTTPException(409, str(error)) from None
    except Exception:
        # Do not forward raw SQL, credentials, private rows or driver errors to
        # the global SQLAlchemy exception logger.
        raise HTTPException(503, '持仓管理暂时不可用，请稍后重试；原快照和历史保留') from None


@router.get('/status')
async def status(monitor=Depends(service)):
    return await safe_call(monitor.status)


@router.get('/configuration')
async def configuration(monitor=Depends(service)):
    return await safe_call(monitor.configuration)


@router.post('/configuration')
async def save(request: Request, monitor=Depends(service)):
    return await safe_call(monitor.configure, await configuration_body(request))


@router.post('/test')
async def test(request: Request, monitor=Depends(service), user=Depends(require_admin)):
    try:
        request.app.state.accounts.limit_probe(user['id'])
    except AccountError as error:
        raise HTTPException(error.status, str(error)) from None
    return await safe_call(monitor.test, await configuration_body(request))


@router.post('/start')
async def start(monitor=Depends(service)):
    await safe_call(monitor.set_enabled, True)
    return await safe_call(monitor.status)


@router.post('/stop')
async def stop(monitor=Depends(service)):
    await safe_call(monitor.set_enabled, False)
    return await safe_call(monitor.status)


@router.get('/history')
async def history(day: date, before: int | None = Query(default=None, ge=1), monitor=Depends(service)):
    return await safe_call(monitor.history, day.isoformat(), before)


class SyncRequest(BaseModel):
    request_id: str = Field(pattern=r'^[a-zA-Z0-9-]{16,64}$')


@router.post('/sync')
async def sync(body: SyncRequest, monitor=Depends(service)):
    return await safe_call(monitor.request_sync, body.request_id)
