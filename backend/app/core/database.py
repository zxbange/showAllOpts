from collections.abc import Iterator

from fastapi import HTTPException, Request, Response
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import Settings


def build_engine(settings: Settings, *, for_cache: bool = False) -> Engine:
    return create_engine(
        settings.database_url, pool_pre_ping=True, pool_recycle=1800,
        pool_size=5, max_overflow=5,
        connect_args={'connect_timeout': 8,
                      'read_timeout': settings.cache_db_read_timeout if for_cache else 20,
                      'write_timeout': 20},
        hide_parameters=True,
    )


def get_session(request: Request, response: Response) -> Iterator[Session]:
    cache = request.app.state.cache
    if cache.settings.cache_enabled:
        module = request.url.path.split('/')[3]
        with cache.session(module) as session:
            response.headers['X-Data-Source'] = 'local-cache'
            response.headers['X-Data-Updated-At'] = session.info['cache_updated_at']
            response.headers['Cache-Control'] = 'no-store'
            yield session
    else:
        with request.app.state.session_factory() as session:
            from app.models.raw import RAW_MODELS
            from app.core.cache_sync import raw_available
            model = RAW_MODELS.get(request.url.path.split('/')[3])
            if model is not None and raw_available(session.connection(), model):
                raise HTTPException(503, '该模块已使用原始采集，请开启 CACHE_ENABLED 并同步本地计算结果。')
            response.headers['X-Data-Source'] = 'database'
            yield session
