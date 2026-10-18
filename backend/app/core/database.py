from collections.abc import Iterator

from fastapi import Request, Response
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import Settings


def build_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url, pool_pre_ping=True, pool_recycle=1800,
        pool_size=5, max_overflow=5,
        connect_args={'connect_timeout': 8, 'read_timeout': 20, 'write_timeout': 20},
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
            response.headers['X-Data-Source'] = 'database'
            yield session
