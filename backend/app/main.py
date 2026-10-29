import logging
import asyncio
from contextlib import asynccontextmanager

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

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or Settings()
    database = engine if engine is not None else build_engine(settings, for_cache=settings.cache_enabled)

    cache = SnapshotCache(settings, database)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        ensure_logging(settings)
        social = SocialMonitor(settings)
        application.state.social = social
        browser_login = BrowserLogin(settings, social)
        application.state.social_browser = browser_login
        social.browser_login = browser_login
        margin_guarantee = MarginGuarantee(settings)
        application.state.margin_guarantee = margin_guarantee
        cache.start()
        social.start()
        margin_guarantee.start()
        logger.info('Application ready cache_enabled=%s', settings.cache_enabled)
        try:
            yield
        finally:
            await asyncio.to_thread(margin_guarantee.stop)
            await asyncio.to_thread(browser_login.stop)
            await asyncio.to_thread(social.stop)
            await asyncio.to_thread(cache.stop)
            database.dispose()
            logger.info('Application shutdown complete')

    application = FastAPI(title=settings.app_name, version='1.0.0', lifespan=lifespan)
    application.state.cache = cache
    application.state.session_factory = sessionmaker(bind=database, autoflush=False)
    application.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                               allow_methods=['GET', 'POST'], allow_headers=['Content-Type', 'Authorization', 'X-Browser-Login-Key'])

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

    application.include_router(router)
    return application


app = create_app()
