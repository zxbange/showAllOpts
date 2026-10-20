"""Cache persistence, local-only reads, isolated failures and multi-worker scheduling."""
from datetime import datetime, timedelta
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError

from app.core.cache import CHINA, MODULES, SnapshotCache, latest_slot
from app.core.config import Settings
from app.main import create_app
from app.models.momentum import IndustryMomentum
from test_momentum_api import row
from test_growth_api import seed as growth_seed
from test_dividend_api import seed as dividend_seed
from test_earnings_api import seed as earnings_seed


@pytest.fixture
def cached(engine, session, tmp_path):
    session.add_all([row(datetime(2026,1,5).date(), 'A', 3), row(datetime(2026,1,6).date(), 'A', 5)])
    session.commit()
    growth_seed(session)
    dividend_seed(session)
    earnings_seed(session)
    settings = Settings(_env_file=None, cache_enabled=True, cache_auto_refresh=False,
                        log_dir=tmp_path/'log')
    cache = SnapshotCache(settings, engine)
    return settings, cache


PATHS = ['/industry-momentum/latest', '/industry-momentum/industries/A/history',
         '/market-crowding/history', '/microcap-temperature/history', '/etf-shares/history',
         '/earnings-gap/stocks?period=2026-09-30&search=测试&sort=profit_yoy',
         '/earnings-gap/stocks/600001.XSHG?period=2026-09-30',
         '/microcap-breadth/history', '/dividend-selection/latest', '/high-growth/latest']


def test_all_modules_and_detail_read_locally_with_source_disconnected(cached, engine):
    settings, cache = cached
    with TestClient(create_app(settings.model_copy(update={'cache_enabled':False}), engine)) as direct:
        expected = {path: direct.get('/api/v1'+path).json() for path in PATHS}
        cache.refresh(background=False)
    assert all(m['ready'] and not m['error'] for m in cache.status()['modules'])
    def no_network(*args):
        raise AssertionError('Page query reached the source database')
    event.listen(engine, 'before_cursor_execute', no_network)
    try:
        with TestClient(create_app(settings, engine)) as client:
            for path in PATHS:
                response = client.get('/api/v1'+path)
                assert response.status_code == 200, response.text
                assert response.headers['x-data-source'] == 'local-cache'
                assert response.json() == expected[path]
    finally:
        event.remove(engine, 'before_cursor_execute', no_network)


def test_snapshot_survives_manager_restart_and_is_readonly(cached, engine):
    settings, cache = cached
    assert cache.refresh('industry-momentum', background=False)
    other = SnapshotCache(settings, engine)
    with other.session('industry-momentum') as local:
        assert len(local.scalars(select(IndustryMomentum)).all()) == 2
        with pytest.raises(OperationalError):
            local.execute(text('DELETE FROM industry_momentum'))
    assert not other.refreshing()


def test_refresh_keeps_old_request_snapshot_and_new_request_sees_update(cached, session):
    _, cache = cached
    cache.refresh('industry-momentum', background=False)
    with cache.session('industry-momentum') as before:
        assert before.scalar(select(IndustryMomentum.momentum).order_by(IndustryMomentum.trade_date.desc())) == 5
        session.query(IndustryMomentum).filter(IndustryMomentum.industry_code=='A').update({'momentum':9, 'updated_at':datetime(2026,1,7,17)})
        session.commit()
        cache.refresh('industry-momentum', background=False)
        assert before.scalar(select(IndustryMomentum.momentum).order_by(IndustryMomentum.trade_date.desc())) == 5
        with cache.session('industry-momentum') as after:
            assert after.scalar(select(IndustryMomentum.momentum).order_by(IndustryMomentum.trade_date.desc())) == 9


def test_module_failure_retains_old_snapshot_other_modules_still_refresh(cached, monkeypatch):
    _, cache = cached
    cache.refresh(background=False)
    previous = cache.read_state()['modules']['industry-momentum'].copy()
    original = cache.copy_module
    calls = []
    def copy(module):
        calls.append(module)
        if module == 'industry-momentum':
            raise RuntimeError('mysql://private:password@private-host')
        return original(module)
    monkeypatch.setattr(cache, 'copy_module', copy)
    cache.refresh(background=False)
    current = cache.read_state()['modules']['industry-momentum']
    assert current['file'] == previous['file'] and current['updated_at'] == previous['updated_at']
    assert current['error'] and 'password' not in json.dumps(cache.status())
    assert calls == list(MODULES)
    with cache.session('industry-momentum') as local:
        assert local.scalar(select(IndustryMomentum.momentum).order_by(IndustryMomentum.trade_date.desc())) == 5


def test_refresh_selects_only_source_columns_and_does_not_write_source(cached, engine):
    _, cache = cached
    statements = []
    def record(conn,cursor,sql,*args): statements.append(sql)
    event.listen(engine,'before_cursor_execute',record)
    try: cache.refresh(background=False)
    finally: event.remove(engine,'before_cursor_execute',record)
    assert len(statements) == 11
    assert all(s.lstrip().startswith('SELECT') for s in statements)
    assert all('member_codes' not in s and 'analysis' not in s for s in statements)


def test_cross_instance_lock_rejects_duplicate_jobs(cached, engine):
    settings, cache = cached
    lock = cache.acquire()
    assert lock is not None
    other = SnapshotCache(settings, engine)
    try:
        assert other.refreshing() and other.refresh(background=False) is False
        other.tick()
        assert not other.read_state()['modules']
    finally: lock.release()
    assert not other.refreshing()


def test_daily_schedule_and_restart_do_not_duplicate_completed_slot(cached, engine, monkeypatch):
    settings, cache = cached
    morning = datetime(2026,10,18,10,tzinfo=CHINA)
    monkeypatch.setattr('app.core.cache.now', lambda: morning)
    cache.tick(morning)
    first = cache.read_state()['modules']['industry-momentum']['file']
    SnapshotCache(settings,engine).tick(morning)
    assert cache.read_state()['modules']['industry-momentum']['file'] == first
    evening = morning.replace(hour=18)
    monkeypatch.setattr('app.core.cache.now', lambda: evening)
    cache.tick(evening)
    second = cache.read_state()['modules']['industry-momentum']['file']
    assert second != first
    cache.tick(evening+timedelta(seconds=30))
    assert cache.read_state()['modules']['industry-momentum']['file'] == second
    assert latest_slot(morning,'18:00').isoformat() == '2026-10-17T18:00:00+08:00'


def test_api_missing_cache_never_falls_back_and_sync_needs_no_secret(cached, engine, monkeypatch):
    settings, cache = cached
    app = create_app(settings,engine)
    with TestClient(app) as client:
        missing = client.get('/api/v1/industry-momentum/latest')
        assert missing.status_code == 503 and missing.json()['code'] == 'CACHE_NOT_READY'
        calls = []
        monkeypatch.setattr(app.state.cache,'refresh', lambda module: calls.append(module) or True)
        assert client.post('/api/v1/cache/refresh',json={}).status_code == 202
        assert client.post('/api/v1/cache/refresh',json={'module':None}).status_code == 202
        for module in MODULES:
            assert client.post('/api/v1/cache/refresh',json={'module':module}).status_code == 202
        assert calls == [None, None, *MODULES]
        assert client.post('/api/v1/cache/refresh',json={'module':'unknown'}).status_code == 422
        monkeypatch.setattr(app.state.cache,'refresh',lambda module: False)
        assert client.post('/api/v1/cache/refresh',json={}).status_code == 409
        status = client.get('/api/v1/cache/status')
        assert status.headers['cache-control'] == 'no-store'
        assert status.json()['manual_enabled'] is True


def test_cache_disabled_still_rejects_refresh(cached, engine, monkeypatch):
    settings, _ = cached
    app = create_app(settings.model_copy(update={'cache_enabled':False}),engine)
    calls = []
    monkeypatch.setattr(app.state.cache, 'refresh', lambda module: calls.append(module) or True)
    with TestClient(app) as client:
        assert client.post('/api/v1/cache/refresh',json={}).status_code == 409
        assert client.get('/api/v1/cache/status').json()['manual_enabled'] is False
    assert calls == []


def test_legacy_secret_config_is_ignored(tmp_path):
    config = tmp_path / 'app.config'
    config.write_text('CACHE_REFRESH_TOKEN=unused-legacy-setting',encoding='utf-8')
    settings = Settings(_env_file=config, cache_enabled=True)
    assert settings.cache_enabled
    assert 'cache_refresh_token' not in settings.model_dump()


def test_source_change_does_not_reuse_snapshot(cached, tmp_path):
    from sqlalchemy import create_engine
    settings, cache = cached
    different = create_engine('sqlite:///'+str(tmp_path/'other.sqlite'))
    try: assert SnapshotCache(settings,different).root != cache.root
    finally: different.dispose()

def test_failed_second_table_never_publishes_half_snapshot(cached, engine):
    _, cache = cached
    cache.refresh('dividend-selection',background=False)
    old = cache.read_state()['modules']['dividend-selection']['file']
    def fail_detail(conn,cursor,sql,*args):
        if 'FROM dividend_selection_stock' in sql: raise RuntimeError('simulated interrupted transfer')
    event.listen(engine,'before_cursor_execute',fail_detail)
    try: cache.refresh('dividend-selection',background=False)
    finally: event.remove(engine,'before_cursor_execute',fail_detail)
    assert cache.read_state()['modules']['dividend-selection']['file'] == old
    assert len(list(cache.root.glob('dividend-selection-*.sqlite'))) == 1


def test_successful_empty_source_replaces_old_rows(cached, session):
    _, cache = cached
    cache.refresh('industry-momentum',background=False)
    session.query(IndustryMomentum).delete()
    session.commit()
    cache.refresh('industry-momentum',background=False)
    entry = cache.status()['modules'][0]
    assert entry['ready'] and entry['rows'] == 0 and not entry['error']
    with cache.session('industry-momentum') as local:
        assert not local.scalars(select(IndustryMomentum)).all()


def test_real_background_refresh_and_lifespan_shutdown(cached, engine):
    import time
    settings, _ = cached
    app = create_app(settings,engine)
    with TestClient(app) as client:
        response=client.post('/api/v1/cache/refresh',json={})
        assert response.status_code == 202
        app.state.cache.worker.join(timeout=5)
        assert not app.state.cache.worker.is_alive()
        status=client.get('/api/v1/cache/status').json()
        assert not status['refreshing'] and all(m['ready'] for m in status['modules'])
        assert client.get('/api/v1/dividend-selection/latest').status_code == 200
    assert app.state.cache.stop_event.is_set()


def test_interrupted_schedule_resumes_pending_modules(cached, monkeypatch):
    _, cache = cached
    clock=datetime(2026,10,18,18,tzinfo=CHINA)
    monkeypatch.setattr('app.core.cache.now',lambda:clock)
    cache.refresh(background=False)
    state=cache.read_state()
    previous=state['modules']['industry-momentum']['file']
    state['pending']=['dividend-selection']
    dividend_before=state['modules']['dividend-selection']['file']
    cache.write_state(state)
    cache.tick(clock)
    state=cache.read_state()
    assert state['pending'] == []
    assert state['modules']['industry-momentum']['file'] == previous
    assert state['modules']['dividend-selection']['file'] != dividend_before


def test_missing_module_retries_after_five_minutes_not_every_request(cached, monkeypatch):
    _, cache = cached
    clock=datetime(2026,10,18,18,tzinfo=CHINA)
    monkeypatch.setattr('app.core.cache.now',lambda:clock)
    original=cache.copy_module
    calls=[]
    def copy(module):
        calls.append(module)
        if module=='industry-momentum': raise RuntimeError('source unavailable')
        return original(module)
    monkeypatch.setattr(cache,'copy_module',copy)
    cache.tick(clock)
    assert len(calls)==len(MODULES)
    cache.tick(clock+timedelta(minutes=1))
    assert len(calls)==len(MODULES)
    cache.tick(clock+timedelta(minutes=5))
    assert calls[len(MODULES):]==['industry-momentum']
