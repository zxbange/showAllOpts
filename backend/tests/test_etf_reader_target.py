"""Custom ETF sources must never reuse another database's published snapshot."""
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import Response
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.cache import CacheUnavailable
from app.core.database import get_session
from app.models.etf import ETFShare, ETFPrice, ETFTradingDay
from app.services.etf_configuration import ConfigurationFailure
from test_cache import cached
from test_etf_api import row


@pytest.fixture
def targets(cached, tmp_path, monkeypatch):
    _, cache = cached
    urls = {}
    for name, shares in [('a', 101), ('b', 202)]:
        url = 'sqlite:///' + str(tmp_path / (name + '.sqlite'))
        urls[name] = url
        engine = create_engine(url)
        ETFShare.metadata.create_all(engine, tables=[ETFShare.__table__, ETFPrice.__table__, ETFTradingDay.__table__])
        with Session(engine) as session:
            session.add(row(date(2026, 1, 6), shares))
            session.commit()
        engine.dispose()
    class Configuration:
        config = None
        broken = False
        def reader_configuration(self):
            if self.broken:
                raise ConfigurationFailure('unreadable configuration')
            return dict(self.config) if self.config is not None else None
        def engine(self, config):
            return create_engine(urls[config['database']])
        def choose(self, name, revision=1):
            self.config = dict(host='example.invalid', port=3306, database=name, username='writer',
                               password='never-in-a-snapshot', revision=revision)
    configuration = Configuration()
    monkeypatch.setattr(cache, 'etf_configuration', lambda: configuration)
    return cache, configuration


def shares(cache):
    with cache.session('etf-shares') as session:
        return list(session.scalars(select(ETFShare.shares)))


def test_switch_rejects_snapshot_and_rebuilds_only_etf(targets):
    cache, config = targets
    cache.refresh('industry-momentum', background=False)
    other = cache.read_state()['modules']['industry-momentum']['file']
    root = cache.root
    config.choose('a')
    cache.refresh('etf-shares', background=False)
    assert shares(cache) == [101]
    first = cache.read_state()['modules']['etf-shares']['file']
    assert b'never-in-a-snapshot' not in (cache.root / first).read_bytes()
    config.choose('b', revision=2)
    with pytest.raises(CacheUnavailable):
        shares(cache)
    assert next(item for item in cache.status()['modules'] if item['id'] == 'etf-shares')['ready'] is False
    cache.refresh('etf-shares', background=False)
    assert shares(cache) == [202]
    assert cache.root == root
    assert cache.read_state()['modules']['industry-momentum']['file'] == other


def test_revision_only_preserves_snapshot_and_noop(targets):
    cache, config = targets
    config.choose('a')
    cache.refresh('etf-shares', background=False)
    first = cache.read_state()['modules']['etf-shares']['file']
    config.config.update(revision=2, password='rotated', schedule=['12:00'])
    assert shares(cache) == [101]
    cache.refresh('etf-shares', background=False)
    assert cache.read_state()['modules']['etf-shares']['file'] == first


@pytest.mark.parametrize('legacy_tls', [False, True])
def test_legacy_transport_cache_identity_refreshes_once(targets, legacy_tls):
    import hashlib
    import json
    import sqlite3
    cache, config = targets
    config.choose('a')
    cache.refresh('etf-shares', background=False)
    entry = cache.read_state()['modules']['etf-shares']
    identity = [config.config[key] for key in ('host', 'port', 'database', 'username')] + [legacy_tls]
    old_fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()
    with sqlite3.connect(cache.root / entry['file']) as connection:
        connection.execute('UPDATE etf_cache_source_v1 SET identity=? WHERE id=1', (old_fingerprint,))
    with pytest.raises(CacheUnavailable):
        shares(cache)
    cache.refresh('etf-shares', background=False)
    assert shares(cache) == [101]
    refreshed = cache.read_state()['modules']['etf-shares']['file']
    cache.refresh('etf-shares', background=False)
    assert cache.read_state()['modules']['etf-shares']['file'] == refreshed


@pytest.mark.parametrize('noop', [False, True])
def test_inflight_target_change_cannot_publish(targets, monkeypatch, noop):
    from app.core import etf_sync
    cache, config = targets
    config.choose('a')
    if noop:
        cache.refresh('etf-shares', background=False)
    original = etf_sync.copy_etf
    def changing(*args, **kwargs):
        result = original(*args, **kwargs)
        config.choose('b', revision=2)
        return result
    monkeypatch.setattr(etf_sync, 'copy_etf', changing)
    cache.refresh('etf-shares', background=False)
    assert cache.read_state()['modules']['etf-shares']['error']
    with pytest.raises(CacheUnavailable):
        shares(cache)


def test_bad_configuration_never_falls_back(targets):
    cache, config = targets
    config.choose('a')
    cache.refresh('etf-shares', background=False)
    config.broken = True
    with pytest.raises(CacheUnavailable):
        shares(cache)
    with pytest.raises(CacheUnavailable):
        with cache.etf_source():
            pytest.fail('must not connect')


def test_direct_reads_use_custom_target_and_other_routes_keep_factory(targets):
    cache, config = targets
    cache.settings.cache_enabled = False
    config.choose('b')
    app = SimpleNamespace(state=SimpleNamespace(cache=cache, session_factory=lambda: Session(cache.source)))
    request = SimpleNamespace(app=app, url=SimpleNamespace(path='/api/v1/etf-shares/history'))
    response = Response()
    generator = get_session(request, response)
    session = next(generator)
    assert list(session.scalars(select(ETFShare.shares))) == [202]
    generator.close()
    assert response.headers['Cache-Control'] == 'no-store'
    request.url.path = '/api/v1/industry-momentum/latest'
    generator = get_session(request, Response())
    assert next(generator).bind is cache.source
    generator.close()


def test_legacy_default_snapshot_not_accepted_for_custom_source(targets):
    import sqlite3
    cache, config = targets
    cache.refresh('etf-shares', background=False)
    entry = cache.read_state()['modules']['etf-shares']
    path = cache.root / entry['file']
    with sqlite3.connect(path) as connection:
        connection.execute('DROP TABLE etf_cache_source_v1')
    assert cache.snapshot_path('etf-shares', entry) == path
    config.choose('a')
    assert cache.snapshot_path('etf-shares', entry) is None


def test_late_publication_keeps_original_source_identity(targets, monkeypatch):
    cache, config = targets
    config.choose('a')
    write = cache.write_state
    changed = False
    def switch_before_publication(state):
        nonlocal changed
        entry = state['modules'].get('etf-shares', {})
        if not changed and entry.get('progress', {}).get('status') == 'complete':
            changed = True
            config.choose('b', revision=2)
        write(state)
    monkeypatch.setattr(cache, 'write_state', switch_before_publication)
    cache.refresh('etf-shares', background=False)
    assert changed
    with pytest.raises(CacheUnavailable):
        shares(cache)
    cache.refresh('etf-shares', background=False)
    assert shares(cache) == [202]


def test_lazy_connection_failure_is_safe_and_disposes(targets, monkeypatch):
    cache, config = targets
    config.choose('a')
    disposed = []
    engine = SimpleNamespace(dispose=lambda: disposed.append(True))
    monkeypatch.setattr(config, 'engine', lambda _: engine)
    with pytest.raises(CacheUnavailable, match='不会读取旧数据源'):
        with cache.etf_source():
            raise ConfigurationFailure('connection failed')
    assert disposed == [True]
