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

# Validation management includes credentials/network probes, so all operations are admin-only.
import hmac
from fastapi import Request, HTTPException, Response
from pydantic import BaseModel, Field, SecretStr, ValidationError
from starlette.concurrency import run_in_threadpool
from app.services.validation_sources import SourceError


class SourceRef(BaseModel):
    id: str = Field(pattern=r'^(?:service|[0-9a-f]{32})$')
    revision: int = Field(ge=1)


class ValidationRequest(BaseModel):
    methods: list[Literal['bukou', 'redhover', 'xuli', 'oversold']] = Field(min_length=1, max_length=4)
    start: date = date(2024, 1, 1)
    end: date | None = None
    observed: date | None = None
    request_id: str | None = Field(default=None, max_length=64, pattern=r'^[a-zA-Z0-9-]+$')
    source: SourceRef | None = None


class ConnectionInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=253, pattern=r'^[a-zA-Z0-9.:-]+$')
    port: int = Field(default=3306, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr | None = None
    tls: Literal['verify', 'required', 'disabled'] = 'verify'
    ca_pem: str = Field(default='', max_length=32768)
    id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    revision: int | None = Field(default=None, ge=1)


def validation_admin(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Vary'] = 'Authorization'
    service = request.app.state.short_validation
    settings = service.sources.settings
    expected = settings.validation_admin_key.get_secret_value()
    if len(expected) < 32:
        raise HTTPException(503, '请部署者设置至少32位 VALIDATION_ADMIN_KEY')
    if request.url.scheme != 'https':
        raise HTTPException(403, '验证管理须经HTTPS访问；请正确配置可信反向代理')
    supplied = request.headers.get('Authorization', '')
    if not hmac.compare_digest(supplied.encode(), ('Bearer '+expected).encode()):
        raise HTTPException(403, '验证管理密钥不正确')
    return service


def public_job(job):
    return {key: value for key, value in job.items() if key not in ('calendar', 'codes')}


@router.get('/validation/access')
def validation_access(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    settings = request.app.state.short_validation.sources.settings
    return {'admin_configured': len(settings.validation_admin_key.get_secret_value()) >= 32,
            'encryption_configured': bool(settings.validation_encryption_key.get_secret_value()),
            'targets_configured': bool(settings.validation_allowed_targets), 'https_required': True}


@router.get('/validation/jobs')
def validation_jobs(service=Depends(validation_admin)):
    return {'jobs': [public_job(job) for job in service.list()]}


@router.post('/validation/jobs')
def create_validation(body: ValidationRequest, service=Depends(validation_admin)):
    try:
        source = body.source.model_dump() if body.source else None
        return public_job(service.create(body.methods, body.start, body.end, body.observed, body.request_id, source))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post('/validation/jobs/{ident}/{action}')
def validation_action(ident: str, action: Literal['retry', 'pause', 'cancel'], service=Depends(validation_admin)):
    try:
        return public_job(service.action(ident, action))
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
        raise HTTPException(422, '连接配置格式无效，请检查必填项、端口和TLS选项；敏感内容不会返回') from None
    config = body.model_dump(exclude={'id', 'revision', 'password'})
    config['password'] = body.password.get_secret_value() if body.password is not None else None
    if config['password'] is None:
        if not body.id or not body.revision:
            raise HTTPException(422, '新连接需要填写密码')
        try:
            old = service.sources.get(body.id, body.revision)
        except SourceError as exc:
            raise HTTPException(409, str(exc)) from None
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
        return await run_in_threadpool(service.sources.test, config)
    except SourceError as exc:
        raise HTTPException(409, str(exc)) from None
