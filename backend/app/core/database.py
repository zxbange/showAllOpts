from collections.abc import Iterator
import hashlib
import json
import logging
import socket
import threading

import pymysql
from fastapi import Request, Response
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import Settings


def database_identity(config):
    return hashlib.sha256(json.dumps([config['host'].lower().rstrip('.'), int(config['port']), config['database']]).encode()).hexdigest()


def build_engine(settings: Settings, *, for_cache: bool = False, configuration=None,
                 read_timeout_seconds=None, write_timeout_seconds=None, connect_timeout_seconds=None) -> Engine:
    from app.services.system_configuration import database_config, SystemConfiguration
    from app.services.database_sources import DatabaseSources, SourceError
    from app.core.diagnostics import diagnostic_details
    config = dict(configuration or database_config(settings))
    from app.services.system_runtime import runtime_config
    sync_options = runtime_config(settings)['database_sync']
    for value in (read_timeout_seconds, write_timeout_seconds, connect_timeout_seconds):
        if value is not None and (type(value) is not int or not 1 <= value <= 3600):
            raise ValueError('Invalid database socket timeout')
    connect_seconds = max(60, connect_timeout_seconds if connect_timeout_seconds is not None else 8)
    read_seconds = max(60, read_timeout_seconds if read_timeout_seconds is not None else
                       (sync_options['read_timeout_seconds'] if for_cache else 20))
    write_seconds = max(60, write_timeout_seconds if write_timeout_seconds is not None else 20)
    validator = object.__new__(DatabaseSources)
    validator.settings = settings
    def connect():
        connection = sock = None
        stage = 'validate_address'
        try:
            address = validator.allowed(config)
            connection = pymysql.connect(host=config['host'], port=config['port'], user=config['username'],
                password=config['password'], database=config['database'], charset='utf8mb4',
                connect_timeout=connect_seconds, read_timeout=read_seconds,
                write_timeout=write_seconds, ssl_disabled=True, defer_connect=True, autocommit=False)
            stage = 'connect_socket'
            sock = socket.create_connection((address, config['port']), timeout=connect_seconds)
            stage = 'authenticate'
            connection.connect(sock=sock)
            return connection
        except Exception as exc:
            logging.getLogger(__name__).error('Shared database failed stage=%s %s', stage, diagnostic_details(exc))
            if connection is not None:
                try: connection.close()
                except Exception: pass
            if sock is not None: sock.close()
            raise SourceError('数据库连接失败，请检查网络、账号和库名；详细凭证不会返回') from None
    return create_engine(SystemConfiguration.url(config), creator=connect, pool_pre_ping=True,
        pool_recycle=1800, pool_size=5, max_overflow=5, hide_parameters=True)


class VersionedDatabase:
    def __init__(self, settings, *, for_cache=False):
        self.settings, self.for_cache = settings, for_cache
        self._engine = None
        self._revision = None
        self._lock = threading.RLock()

    def current(self):
        from app.services.system_configuration import database_config
        config = database_config(self.settings)
        from app.services.system_runtime import runtime_config
        timeout = runtime_config(self.settings)['database_sync']['read_timeout_seconds'] if self.for_cache else None
        revision = (config['revision'], timeout)
        with self._lock:
            if self._engine is None or revision != self._revision:
                engine = build_engine(self.settings, for_cache=self.for_cache, configuration=config)
                old = self._engine
                self._engine, self._revision = engine, revision
                if old is not None: old.dispose()
            return self._engine

    def target_identity(self):
        from app.services.system_configuration import database_config
        return database_identity(database_config(self.settings))

    @property
    def url(self): return self.current().url
    @property
    def dialect(self): return self.current().dialect
    def connect(self): return self.current().connect()
    def begin(self): return self.current().begin()
    def dispose(self):
        with self._lock:
            if self._engine is not None: self._engine.dispose()
            self._engine = None


def get_session(request: Request, response: Response) -> Iterator[Session]:
    cache = request.app.state.cache
    module = request.url.path.split('/')[3]
    if cache.settings.cache_enabled:
        with cache.session(module) as session:
            response.headers['X-Data-Source'] = 'local-cache'
            response.headers['X-Data-Updated-At'] = session.info['cache_updated_at']
            response.headers['Cache-Control'] = 'no-store'
            yield session
    elif isinstance(cache.source, VersionedDatabase):
        with Session(cache.source.current()) as session:
            response.headers['X-Data-Source'] = 'database'
            response.headers['Cache-Control'] = 'no-store'
            yield session
    elif module == 'etf-shares':
        with cache.etf_source() as engine, Session(engine) as session:
            response.headers['X-Data-Source'] = 'database'
            response.headers['Cache-Control'] = 'no-store'
            yield session
    else:
        with request.app.state.session_factory() as session:
            response.headers['X-Data-Source'] = 'database'
            yield session
