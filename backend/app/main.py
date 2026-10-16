import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.api.router import router
from app.core.config import Settings
from app.core.database import build_engine
from app.core.logging import ensure_logging

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or Settings()
    database = engine if engine is not None else build_engine(settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        ensure_logging(settings)
        logger.info('Application ready')
        try:
            yield
        finally:
            database.dispose()
            logger.info('Application shutdown complete')

    application = FastAPI(title=settings.app_name, version='1.0.0', lifespan=lifespan)
    application.state.session_factory = sessionmaker(bind=database, autoflush=False)
    application.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                               allow_methods=['GET'], allow_headers=['Content-Type'])

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        # Do not return connection strings, SQL parameters or raw driver messages.
        logger.error('Database request failed: %s method=%s path=%s',
                     type(exc).__name__, request.method, request.url.path, exc_info=True)
        return JSONResponse(status_code=503, content={
            'detail': '暂时无法读取数据，请检查后端数据库连接后重试。',
            'code': 'DATABASE_UNAVAILABLE',
        })

    application.include_router(router)
    return application


app = create_app()
