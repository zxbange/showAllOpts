import logging
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.api.router import router
from app.core.cache import CacheUnavailable, SnapshotCache
from app.core.config import Settings
from app.core.cache_schedule import CacheScheduleError
from app.core.database import build_engine
from app.core.logging import ensure_logging
from app.services.social import SocialMonitor
from app.services.social_browser import BrowserLogin
from app.services.margin_guarantee import MarginGuarantee
from app.services.commodities import Commodities
from app.services.cci_coal import CCICoal
from app.services.accounts import Accounts
from app.services.etf_collection import ETFCollection
from app.services.personal_holdings import PersonalHoldings
from app.api.routes.accounts import router as accounts_router, require_user
from fastapi import Depends

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or Settings()
    database = engine if engine is not None else build_engine(settings, for_cache=settings.cache_enabled)

    cache = SnapshotCache(settings, database)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        ensure_logging(settings)
        # Register cleanup before starting workers, including partially failed startup.
        # ExitStack also continues cleanup if an individual stop() raises.
        async with AsyncExitStack() as cleanup:
            cleanup.callback(database.dispose)
            cleanup.push_async_callback(asyncio.to_thread, cache.stop)
            social = SocialMonitor(settings)
            cleanup.push_async_callback(asyncio.to_thread, social.stop)
            application.state.social = social
            browser_login = BrowserLogin(settings, social)
            cleanup.push_async_callback(asyncio.to_thread, browser_login.stop)
            application.state.social_browser = browser_login
            social.browser_login = browser_login
            margin_guarantee = MarginGuarantee(settings)
            cleanup.push_async_callback(asyncio.to_thread, margin_guarantee.stop)
            application.state.margin_guarantee = margin_guarantee
            commodities = Commodities(settings)
            cleanup.push_async_callback(asyncio.to_thread, commodities.stop)
            application.state.commodities = commodities
            cci_coal = CCICoal(settings)
            cleanup.push_async_callback(asyncio.to_thread, cci_coal.stop)
            application.state.cci_coal = cci_coal
            holdings = PersonalHoldings(settings)
            cleanup.push_async_callback(asyncio.to_thread, holdings.stop)
            application.state.personal_holdings = holdings
            etf_collection = ETFCollection(settings, refresh=cache.refresh_etf_collection)
            cleanup.push_async_callback(asyncio.to_thread, etf_collection.stop)
            application.state.etf_collection = etf_collection
            etf_collection.start()
            holdings.start()
            cache.start()
            social.start()
            margin_guarantee.start()
            commodities.start()
            cci_coal.start()
            logger.info('Application ready cache_enabled=%s', settings.cache_enabled)
            yield
        logger.info('Application shutdown complete')

    application = FastAPI(title=settings.app_name, version='1.0.0', lifespan=lifespan,
                          docs_url=None, redoc_url=None, openapi_url=None)
    application.state.cache = cache
    application.state.accounts = Accounts(settings)
    application.state.session_factory = sessionmaker(bind=database, autoflush=False)
    application.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                               allow_methods=['GET', 'POST'], allow_headers=['Content-Type', 'X-CSRF-Token', 'X-App-Client'])

    @application.middleware('http')
    async def private_responses(request: Request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        return response

    @application.exception_handler(CacheScheduleError)
    @application.exception_handler(CacheUnavailable)
    async def cache_unavailable(request: Request, exc: CacheUnavailable):
        return JSONResponse(status_code=503, content={'detail': str(exc), 'code': 'CACHE_NOT_READY'})

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        # Do not return connection strings, SQL parameters or raw driver messages.
        logger.error('Database request failed: %s method=%s path=%s',
                     type(exc).__name__, request.method, request.url.path, exc_info=True)
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
