import logging
import asyncio
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.api.router import router
from app.core.cache import CacheUnavailable, SnapshotCache
from app.core.config import Settings
from app.core.cache_schedule import CacheScheduleError
from app.core.database import build_engine, VersionedDatabase
from app.core.logging import ensure_logging, PollingAccessFilter
from app.core.diagnostics import diagnostic_details
from app.services.social import SocialMonitor
from app.services.social_browser import BrowserLogin
from app.services.margin_guarantee import MarginGuarantee
from app.services.growth_dividend import GrowthDividend
from app.services.valuation_ruler import ValuationRuler
from app.services.commodities import Commodities
from app.services.cci_coal import CCICoal
from app.services.cash_cow import CashCowScreen
from app.services.cash_cow_schedule import MonthlyCashCowScheduler
from app.services.accounts import Accounts
from app.services.etf_collection import ETFCollection
from app.services.market_collection import MarketCollection
from app.services.personal_holdings import PersonalHoldings
from app.api.routes.accounts import router as accounts_router, require_user
from fastapi import Depends

logger = logging.getLogger(__name__)


def request_route(request):
    route = getattr(request.scope.get('route'), 'path', '<unmatched>')
    # Nested FastAPI routers may expose a template without the outer prefix.
    # Only attach a fixed known prefix; never log concrete URL path values.
    if route != '<unmatched>' and request.url.path.startswith('/api/v1/') and not route.startswith('/api/v1/'):
        route = '/api/v1' + route
    return route


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or Settings()
    database = engine if engine is not None else VersionedDatabase(settings, for_cache=settings.cache_enabled)

    cache = SnapshotCache(settings, database)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        ensure_logging(settings)
        def start_worker(name, worker):
            started = time.monotonic()
            try:
                worker.start()
            except Exception as exc:
                logger.error('Worker lifecycle failed worker=%s stage=start %s', name, diagnostic_details(exc))
                raise
            logger.info('Worker startup checked worker=%s seconds=%.3f', name, time.monotonic() - started)

        async def stop_worker(name, worker):
            started = time.monotonic()
            try:
                await asyncio.to_thread(worker.stop)
            except Exception as exc:
                logger.error('Worker lifecycle failed worker=%s stage=stop %s', name, diagnostic_details(exc))
                raise
            logger.info('Worker shutdown checked worker=%s seconds=%.3f', name, time.monotonic() - started)

        # Register cleanup before starting workers, including partially failed startup.
        # ExitStack also continues cleanup if an individual stop() raises.
        async with AsyncExitStack() as cleanup:
            cleanup.callback(database.dispose)
            cleanup.push_async_callback(stop_worker, 'cache', cache)
            social = SocialMonitor(settings)
            cleanup.push_async_callback(stop_worker, 'social', social)
            application.state.social = social
            browser_login = BrowserLogin(settings, social)
            cleanup.push_async_callback(stop_worker, 'browser_login', browser_login)
            application.state.social_browser = browser_login
            social.browser_login = browser_login
            margin_guarantee = MarginGuarantee(settings)
            cleanup.push_async_callback(stop_worker, 'margin_guarantee', margin_guarantee)
            application.state.margin_guarantee = margin_guarantee
            growth_dividend = GrowthDividend(settings)
            cleanup.push_async_callback(stop_worker, 'growth_dividend', growth_dividend)
            application.state.growth_dividend = growth_dividend
            valuation_ruler = ValuationRuler(settings)
            cleanup.push_async_callback(stop_worker, 'valuation_ruler', valuation_ruler)
            application.state.valuation_ruler = valuation_ruler
            cache.history_services.update({
                'growth-dividend': growth_dividend, 'valuation-ruler': valuation_ruler})
            commodities = Commodities(settings)
            cleanup.push_async_callback(stop_worker, 'commodities', commodities)
            application.state.commodities = commodities
            cci_coal = CCICoal(settings)
            cleanup.push_async_callback(stop_worker, 'cci_coal', cci_coal)
            application.state.cci_coal = cci_coal
            cash_cow = {years: CashCowScreen(settings, years=years) for years in (3, 5, 10)}
            for screen in cash_cow.values():
                cleanup.push_async_callback(stop_worker, f'cash_cow_{screen.years}', screen)
            application.state.cash_cow = cash_cow
            cash_cow_schedule = MonthlyCashCowScheduler(settings, cash_cow)
            cleanup.push_async_callback(stop_worker, 'cash_cow_schedule', cash_cow_schedule)
            application.state.cash_cow_schedule = cash_cow_schedule
            holdings = PersonalHoldings(settings)
            cleanup.push_async_callback(stop_worker, 'holdings', holdings)
            application.state.personal_holdings = holdings
            etf_collection = ETFCollection(settings, refresh=cache.refresh_etf_collection)
            cleanup.push_async_callback(stop_worker, 'etf_collection', etf_collection)
            application.state.etf_collection = etf_collection
            start_worker('etf_collection', etf_collection)
            market_collection = MarketCollection(settings, refresh=cache.refresh_market_collection)
            market_collection.shared_configuration = etf_collection.configuration
            cleanup.push_async_callback(stop_worker, 'market_collection', market_collection)
            application.state.market_collection = market_collection
            start_worker('market_collection', market_collection)
            start_worker('holdings', holdings)
            start_worker('cache', cache)
            start_worker('social', social)
            start_worker('margin_guarantee', margin_guarantee)
            start_worker('growth_dividend', growth_dividend)
            start_worker('valuation_ruler', valuation_ruler)
            start_worker('commodities', commodities)
            start_worker('cci_coal', cci_coal)
            start_worker('cash_cow_schedule', cash_cow_schedule)
            from app.services.collection_notifications import start_notifications, stop_notifications
            start_notifications(settings)
            from app.services.system_runtime import start_runtime, stop_runtime
            start_runtime(settings)
            cleanup.push_async_callback(asyncio.to_thread, stop_runtime, settings)
            cleanup.push_async_callback(asyncio.to_thread, stop_notifications, settings)
            logger.info('Application ready cache_enabled=%s', settings.cache_enabled)
            yield
        logger.info('Application shutdown complete')

    application = FastAPI(title=settings.app_name, version='1.0.0', lifespan=lifespan,
                          docs_url=None, redoc_url=None, openapi_url=None)
    application.state.cache = cache
    application.state.accounts = Accounts(settings)
    application.state.session_factory = sessionmaker(bind=database.current() if isinstance(database, VersionedDatabase) else database, autoflush=False)
    application.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                               allow_methods=['GET', 'POST', 'PUT'], allow_headers=['Content-Type', 'X-CSRF-Token', 'X-App-Client'])

    @application.middleware('http')
    async def private_responses(request: Request, call_next):
        started = time.monotonic()
        # Generated locally: never trust an incoming identifier or log request bodies,
        # query strings, session headers, or concrete path parameters.
        request.state.diagnostic_id = uuid.uuid4().hex[:12]
        try:
            response = await call_next(request)
        except Exception as exc:
            route = request_route(request)
            logger.error('Request failed request=%s method=%s route=%s seconds=%.3f %s',
                         request.state.diagnostic_id, request.method, route,
                         time.monotonic() - started, diagnostic_details(exc))
            raise
        elapsed = time.monotonic() - started
        route = request_route(request)
        quiet_poll = request.method == 'GET' and route in PollingAccessFilter.paths and response.status_code < 400
        if response.status_code >= 400 or (not quiet_poll and (request.method != 'GET' or elapsed >= 1)):
            level = logging.ERROR if response.status_code >= 500 else logging.WARNING if response.status_code >= 400 else logging.INFO
            logger.log(level, 'Request complete request=%s method=%s route=%s status=%s seconds=%.3f',
                       request.state.diagnostic_id, request.method, route, response.status_code, elapsed)
        response.headers['X-Request-ID'] = request.state.diagnostic_id
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        return response

    @application.exception_handler(CacheScheduleError)
    @application.exception_handler(CacheUnavailable)
    async def cache_unavailable(request: Request, exc: CacheUnavailable):
        return JSONResponse(status_code=503, content={'detail': str(exc), 'code': 'CACHE_NOT_READY'})

    @application.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        logger.log(logging.ERROR if exc.status_code >= 500 else logging.WARNING,
                   'Request rejected request=%s route=%s status=%s %s',
                   getattr(request.state, 'diagnostic_id', '-'),
                   request_route(request),
                   exc.status_code, diagnostic_details(exc))
        return await http_exception_handler(request, exc)

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        # Do not return connection strings, SQL parameters or raw driver messages.
        logger.error('Database request failed request=%s method=%s route=%s Traceback diagnostics: %s',
                     getattr(request.state, 'diagnostic_id', '-'), request.method,
                     request_route(request), diagnostic_details(exc))
        return JSONResponse(status_code=503, content={
            'detail': ('暂时无法读取本地快照，请查看数据同步状态并重新同步。' if settings.cache_enabled
                       else '暂时无法读取数据，请检查后端数据库连接后重试。'),
            'code': 'CACHE_READ_FAILED' if settings.cache_enabled else 'DATABASE_UNAVAILABLE',
        })

    # Health checks remain public; all data and operations require a session.
    @application.get('/api/v1/health')
    def health():
        return {'status': 'ok', 'service': 'showallopts-api'}

    application.include_router(accounts_router)
    application.include_router(router, dependencies=[Depends(require_user)])
    return application


app = create_app()
