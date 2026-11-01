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
           method: Literal['bukou', 'oversold'] = 'bukou',
           offset: int = Query(0, ge=0), limit: int = Query(6, ge=1, le=24)):
    return ShortTermService(ShortTermRepository(session)).overview(day, method, offset, limit)

# Validation management includes credentials/network probes, so all operations are admin-only.
import sqlite3
from fastapi import Request, HTTPException, Response
from pydantic import BaseModel, Field, SecretStr
from starlette.concurrency import run_in_threadpool
from app.services.validation_sources import SourceError
from app.services.short_validation import DeleteConflict
from app.api.routes.accounts import require_admin, principal
from app.services.accounts import AccountError


class SourceRef(BaseModel):
    id: str = Field(pattern=r'^(?:service|[0-9a-f]{32})$')
    revision: int = Field(ge=1)


class ValidationRequest(BaseModel):
    methods: list[Literal['bukou', 'oversold']] = Field(min_length=1, max_length=2)
    horizons: list[Annotated[int, Field(strict=True, ge=1, le=120)]] = Field(default_factory=lambda: [1, 2, 3], min_length=1, max_length=3)
    start: date = date(2024, 1, 1)
    end: date | None = None
    observed: date | None = Field(default=None, deprecated=True, description="兼容旧客户端；新任务忽略此字段，以end作为统一截止")
    request_id: str | None = Field(default=None, max_length=64, pattern=r'^[a-zA-Z0-9-]+$')
    source: SourceRef | None = None


class ConnectionInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=253, pattern=r'^[a-zA-Z0-9.:-]+$')
    port: int = Field(default=3306, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr | None = None
    id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    revision: int | None = Field(default=None, ge=1)


def validation_admin(request: Request, response: Response):
    require_admin(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Vary'] = 'Cookie'
    return request.app.state.short_validation


def public_job(job, service=None):
    result = {key: value for key, value in job.items() if key not in ('calendar', 'codes')}
    result['source_name'] = '未绑定数据源'
    if job.get('source'):
        result['source_name'] = '已保存数据库（名称暂不可用）'
        if service is not None and service.sources is not None:
            try:
                result['source_name'] = service.sources.get(job['source']['id'], job['source']['revision'])['name']
            except SourceError:
                pass  # Keep historical tasks readable even if their key is unavailable.
    return result


@router.get('/validation/jobs')
def validation_jobs(service=Depends(validation_admin)):
    return {'jobs': [public_job(job, service) for job in service.list()]}


@router.post('/validation/jobs')
def create_validation(body: ValidationRequest, request: Request, service=Depends(validation_admin)):
    try:
        source = body.source.model_dump() if body.source else None
        return public_job(service.create(body.methods, body.start, body.end, None, body.request_id, source, horizons=body.horizons), service)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post('/validation/jobs/{ident}/delete')
def delete_validation(ident: str, service=Depends(validation_admin)):
    try:
        service.delete(ident)
        return {'deleted': True}
    except DeleteConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, '删除暂未完成，请稍后重试；不会清除源行情或其他任务') from None


@router.post('/validation/jobs/{ident}/{action}')
def validation_action(ident: str, action: Literal['retry', 'pause', 'cancel'], service=Depends(validation_admin)):
    try:
        return public_job(service.action(ident, action), service)
    except KeyError:
        raise HTTPException(404, '任务不存在') from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get('/validation/sources')
def validation_sources(service=Depends(validation_admin)):
    try:
        return {'sources': service.sources.list()}
    except SourceError as exc:
        raise HTTPException(409, str(exc)) from None


async def connection_input(request, service):
    # Do not use automatic body validation: FastAPI's default errors include raw inputs.
    try:
        raw = await request.body()
        if len(raw) > 65536:
            raise ValueError()
        body = ConnectionInput.model_validate_json(raw)
        if body.password is not None and len(body.password.get_secret_value()) > 4096:
            raise ValueError()
    except Exception:
        raise HTTPException(422, '连接配置格式无效，请检查必填项和端口；敏感内容不会返回') from None
    config = body.model_dump(exclude={'id', 'revision', 'password'})
    config['password'] = body.password.get_secret_value() if body.password is not None else None
    if config['password'] is None:
        if not body.id or not body.revision:
            raise HTTPException(422, '新连接需要填写密码')
        try:
            old = service.sources.get(body.id, body.revision)
        except SourceError as exc:
            raise HTTPException(409, str(exc)) from None
        if not service.sources.can_retain_password(old, config):
            raise HTTPException(422, '数据库名称或连接信息已变更，请重新输入密码')
        config['password'] = old['password']
    return body, config


@router.post('/validation/sources/save')
async def save_validation_source(request: Request, service=Depends(validation_admin)):
    body, config = await connection_input(request, service)
    try:
        ref = await run_in_threadpool(service.sources.save, config, body.id, body.revision)
        return service.sources.public(ref['id'], ref['revision'], config)
    except SourceError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post('/validation/sources/test')
async def test_validation_source(request: Request, service=Depends(validation_admin)):
    _, config = await connection_input(request, service)
    try:
        request.app.state.accounts.limit_probe(principal(request)['user']['id'])
        return await run_in_threadpool(service.sources.test, config)
    except AccountError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    except SourceError as exc:
        raise HTTPException(409, str(exc)) from None
