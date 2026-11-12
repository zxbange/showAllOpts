"""Administrator-only centralized configuration; credentials are write-only."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from app.api.routes.accounts import require_admin
from app.services.system_configuration import SystemConfiguration, SystemConfigurationError

router = APIRouter(prefix='/system-configuration', tags=['系统配置'], dependencies=[Depends(require_admin)])

def service(request):
    return SystemConfiguration(request.app.state.accounts.settings)

async def body(request):
    import json
    if request.url.scheme != 'https':
        raise HTTPException(403, '请使用HTTPS配置数据库账号')
    try:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 32768:
                raise ValueError()
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except Exception:
        raise HTTPException(422, '配置格式无效') from None

async def call(function, *args):
    try:
        return await run_in_threadpool(function, *args)
    except SystemConfigurationError as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception:
        raise HTTPException(503, '系统配置暂不可用，请查看服务日志') from None

@router.get('')
async def get_configuration(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return await call(service(request).public)

@router.put('')
async def save_configuration(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return await call(save_with_locks, request, await body(request))

@router.post('/test')
async def test_configuration(request: Request, user=Depends(require_admin)):
    from app.services.accounts import AccountError
    try:
        request.app.state.accounts.limit_probe(user['id'])
    except AccountError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    return await call(service(request).test, await body(request))

def save_with_locks(request, value):
    from contextlib import ExitStack
    import portalocker
    configuration = service(request)
    previous = configuration.load()
    candidate = configuration.validate(value, previous)
    database_changed = candidate['database'] != previous['database']
    holdings_changed = candidate['schedules']['holdings'] != previous['schedules']['holdings']
    if not database_changed and not holdings_changed:
        return configuration.save(value)
    with ExitStack() as stack:
        holdings = request.app.state.personal_holdings
        holdings.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = portalocker.Lock(str(holdings.root / 'configuration.lock'), timeout=0, flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
        try:
            lock.acquire()
        except portalocker.exceptions.LockException:
            raise SystemConfigurationError('持仓配置正在更新，请稍后重试') from None
        stack.callback(lock.release)
        if not database_changed:
            return configuration.save(value)
        cache = request.app.state.cache
        lock = cache.acquire()
        if lock is None:
            raise SystemConfigurationError('数据库同步正在运行，请完成或取消后修改数据库')
        stack.callback(lock.release)
        etf = request.app.state.etf_collection
        etf.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = portalocker.Lock(str(etf.root / 'collection.lock'), timeout=0, flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
        try:
            lock.acquire()
        except portalocker.exceptions.LockException:
            raise SystemConfigurationError('ETF采集正在运行，请完成或取消后修改数据库') from None
        stack.callback(lock.release)
        market = request.app.state.market_collection
        lock = market._acquire()
        if lock is None:
            raise SystemConfigurationError('市场采集正在运行，请完成或取消后修改数据库')
        stack.callback(lock.release)
        for owner in (etf, market):
            with owner.connect() as db:
                if db.execute("SELECT 1 FROM manual_runs WHERE status='queued'").fetchone():
                    raise SystemConfigurationError('采集任务已排队，请完成或取消后修改数据库')
        return configuration.save(value)
