"""Authenticated management of the shared daily market data job."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.concurrency import run_in_threadpool

from app.api.routes.accounts import require_admin
from app.services.accounts import AccountError
from app.services.etf_configuration import ConfigurationFailure
from app.services.market_collection import CollectionBusy


router = APIRouter(prefix='/market-collection', tags=['市场数据自动采集'])


class ConfigurationBody(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    revision: int = Field(ge=0, strict=True)
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr | None = None
    enabled: bool = Field(strict=True)


class RunBody(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    request_id: str = Field(pattern=r'^[a-zA-Z0-9-]{16,64}$')


async def bounded_body(request, schema, limit=8192):
    try:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > limit:
                raise ValueError()
        return schema.model_validate_json(raw)
    except Exception:
        raise HTTPException(422, '请求格式无效，请检查必填项后重试') from None


async def configuration_body(request):
    parsed = await bounded_body(request, ConfigurationBody)
    password = parsed.password.get_secret_value() if parsed.password is not None else None
    if password is not None and len(password) > 4096:
        raise HTTPException(422, '采集配置格式无效')
    if request.url.scheme != 'https':
        raise HTTPException(403, '请使用HTTPS配置数据库账号，HTTP无法保护凭据')
    return parsed.model_dump(exclude={'password'}) | {'password': password}


async def call(function, *args):
    try:
        return await run_in_threadpool(function, *args)
    except CollectionBusy as error:
        raise HTTPException(409, error.detail()) from None
    except ConfigurationFailure as error:
        raise HTTPException(409, str(error)) from None
    except Exception:
        raise HTTPException(503, '市场采集暂不可用，已保存数据保留，请稍后重试') from None


def public_configuration(request, value):
    return value | {'transport_secure': request.url.scheme == 'https', 'https_required': True}


@router.get('/status')
async def status(request: Request):
    return await call(request.app.state.market_collection.status)


@router.get('/configuration', dependencies=[Depends(require_admin)])
async def configuration(request: Request):
    value = await call(request.app.state.market_collection.configuration.public)
    return public_configuration(request, value)


@router.post('/configuration', dependencies=[Depends(require_admin)])
async def configure(request: Request):
    value = await call(request.app.state.market_collection.configure, await configuration_body(request))
    return public_configuration(request, value)


@router.post('/test')
async def test(request: Request, user=Depends(require_admin)):
    try:
        request.app.state.accounts.limit_probe(user['id'])
    except AccountError as error:
        raise HTTPException(error.status, str(error)) from None
    return await call(request.app.state.market_collection.test_configuration, await configuration_body(request))


@router.post('/run', status_code=202, dependencies=[Depends(require_admin)])
async def run(request: Request):
    parsed = await bounded_body(request, RunBody, limit=1024)
    return await call(request.app.state.market_collection.request_run, parsed.request_id)
