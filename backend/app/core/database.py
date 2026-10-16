from collections.abc import Iterator

from fastapi import Request
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


def get_session(request: Request) -> Iterator[Session]:
    # This API only selects existing data. No schema creation or commit paths.
    with request.app.state.session_factory() as session:
        yield session
