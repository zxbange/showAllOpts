import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.main import create_app
from app.models.momentum import Base


@pytest.fixture
def engine():
    database = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(database)
    yield database
    database.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture
def client(engine, tmp_path):
    app = create_app(Settings(_env_file=None, log_dir=tmp_path / 'log'), engine=engine)
    with TestClient(app) as client:
        yield client
    logging.shutdown()


@pytest.fixture(autouse=True)
def direct_database_for_existing_tests(monkeypatch):
    # Existing repository/API tests intentionally verify source queries. Cache tests opt in explicitly.
    monkeypatch.setenv('CACHE_ENABLED', 'false')
