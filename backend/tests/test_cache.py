"""Cache persistence, local-only reads, isolated failures and multi-worker scheduling."""
from datetime import datetime, timedelta
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError

from app.core.cache import CHINA, MODULES, DATABASE_SYNC_MODULES, COLLECTOR_OWNED_MODULES, SnapshotCache, latest_slot
from app.core.config import Settings
from app.main import create_app
from app.models.momentum import IndustryMomentum
from test_momentum_api import row
from test_dividend_api import seed as dividend_seed


def seed_cash_cow(session):
    from app.models.cash_cow import CashCowSnapshot
    from app.services.cash_cow_import import RULE_VERSION, PUBLIC_SOURCE
    payload = {str(p): dict(snapshot=dict(years=p, as_of='2026-01-06', updated_at='2026-01-06T18:00:00+08:00',
        run_id='test-cow-run', rule_version=RULE_VERSION, source=PUBLIC_SOURCE,
        start_year=2026-p, end_year=2025, universe_count=1, evaluated_count=1,
        excluded_count=1, insufficient_count=0, matched_count=0), items=[]) for p in (3,5,10)}
    session.add(CashCowSnapshot(id=1, as_of_date=datetime(2026,1,6).date(),
                                updated_at=datetime(2026,1,6,18), payload=payload))
    session.commit()


@pytest.fixture
def cached(engine, session, tmp_path):
    session.add_all([row(datetime(2026,1,5).date(), 'A', 3), row(datetime(2026,1,6).date(), 'A', 5)])
    session.commit()
    from history_sync_helpers import seed_history_sources
    seed_history_sources(engine)
    dividend_seed(session)
    seed_cash_cow(session)
    settings = Settings(_env_file=None, cache_enabled=True, cache_auto_refresh=False,
                        log_dir=tmp_path/'log')
    cache = SnapshotCache(settings, engine)
    return settings, cache


PATHS = ['/industry-momentum/latest', '/industry-momentum/industries/A/history',
         '/market-crowding/history', '/market-sentiment/history', '/microcap-temperature/history', '/etf-shares/history',
         '/microcap-breadth/history', '/dividend-selection/latest', '/etf-momentum/latest']


def test_all_modules_and_detail_read_locally_with_source_disconnected(cached, engine):
    settings, cache = cached
    with TestClient(create_app(settings.model_copy(update={'cache_enabled':False}), engine)) as direct:
        expected = {path: direct.get('/api/v1'+path).json() for path in PATHS}
        for module in MODULES:
            cache.refresh(module, background=False)
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
        cache.refresh('industry-momentum', background=False, full=True)
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
    assert calls == list(DATABASE_SYNC_MODULES)
    with cache.session('industry-momentum') as local:
        assert local.scalar(select(IndustryMomentum.momentum).order_by(IndustryMomentum.trade_date.desc())) == 5


def test_refresh_selects_only_source_columns_and_does_not_write_source(cached, engine, session):
    _, cache = cached
    statements = []
    def record(conn,cursor,sql,*args): statements.append(sql)
    event.listen(engine,'before_cursor_execute',record)
    try: cache.refresh(background=False)
    finally: event.remove(engine,'before_cursor_execute',record)
    selects = [s for s in statements if s.lstrip().startswith('SELECT')]
    # Collector-owned tables are not imported by the database sync center.
    assert sum('GROUP BY' in sql for sql in selects) == 0
    assert not any('FROM etf_price_daily' in sql for sql in selects)
    for _, models in DATABASE_SYNC_MODULES.values():
        for model in models:
            queried = any(model.__tablename__ in sql for sql in selects)
            assert queried, model.__tablename__
    # Optional ownership journal is detected with read-only SQLite table introspection.
    assert all(s.lstrip().startswith(('SELECT', 'PRAGMA main.table_info(', 'PRAGMA temp.table_info(', 'PRAGMA main.table_xinfo(', 'PRAGMA temp.table_xinfo('))
               for s in statements)
    assert all('member_codes' not in s and 'analysis' not in s for s in statements)
    assert all(m['ready'] and not m['error'] for m in cache.status()['modules'])


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
    morning = datetime(2026,10,19,6,tzinfo=CHINA)
    monkeypatch.setattr('app.core.cache.now', lambda: morning)
    cache.tick(morning)
    assert cache.read_state()['modules'] == {}
    cache.refresh(background=False)
    first = cache.read_state()['modules']['industry-momentum']['file']
    SnapshotCache(settings,engine).tick(morning)
    assert cache.read_state()['modules']['industry-momentum']['file'] == first
    assert settings.cache_refresh_time == '07:00'
    cache.tick(morning.replace(hour=6, minute=30))
    cache.tick(morning.replace(hour=6, minute=59))
    assert cache.read_state()['modules']['industry-momentum']['file'] == first
    evening = morning.replace(hour=7)
    monkeypatch.setattr('app.core.cache.now', lambda: evening)
    cache.tick(evening)
    second = cache.read_state()['modules']['industry-momentum']['file']
    assert second != first  # No journal: the scheduled refresh reconciles all keys.
    assert cache.read_state()['scheduled_slot'] == latest_slot(evening, '07:00').isoformat()
    cache.tick(evening+timedelta(seconds=30))
    assert cache.read_state()['modules']['industry-momentum']['file'] == second
    assert latest_slot(morning,'07:00').isoformat() == '2026-10-18T07:00:00+08:00'


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
        for module in DATABASE_SYNC_MODULES:
            assert client.post('/api/v1/cache/refresh',json={'module':module}).status_code == 202
        assert calls == [None, None, *DATABASE_SYNC_MODULES]
        for module in COLLECTOR_OWNED_MODULES:
            for full in (False, True):
                assert client.post('/api/v1/cache/refresh', json={'module': module, 'full': full}).status_code == 422
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
        assert client.get('/api/v1/industry-momentum/latest').status_code == 200
    assert app.state.cache.stop_event.is_set()


def test_interrupted_schedule_resumes_pending_modules(cached, monkeypatch):
    _, cache = cached
    clock=datetime(2026,10,19,6,tzinfo=CHINA)
    monkeypatch.setattr('app.core.cache.now',lambda:clock)
    cache.refresh(background=False)
    state=cache.read_state()
    previous=state['modules']['industry-momentum']['file']
    state['pending']=['microcap-temperature']
    dividend_before=state['modules']['microcap-temperature']['file']
    cache.write_state(state)
    cache.tick(clock)
    state=cache.read_state()
    assert state['pending'] == ['microcap-temperature']
    assert state['modules']['microcap-temperature']['file'] == dividend_before
    cache.refresh('microcap-temperature', background=False)
    state = cache.read_state()
    assert state['pending'] == []
    assert state['modules']['industry-momentum']['file'] == previous
    assert state['modules']['microcap-temperature']['file'] != dividend_before


def test_missing_module_waits_for_manual_or_next_schedule(cached, monkeypatch):
    _, cache = cached
    clock=datetime(2026,10,19,6,tzinfo=CHINA)
    monkeypatch.setattr('app.core.cache.now',lambda:clock)
    original=cache.copy_module
    calls=[]
    def copy(module):
        calls.append(module)
        if module=='industry-momentum': raise RuntimeError('source unavailable')
        return original(module)
    monkeypatch.setattr(cache,'copy_module',copy)
    cache.tick(clock)
    assert calls == []
    cache.tick(clock+timedelta(minutes=1))
    cache.tick(clock+timedelta(minutes=5))
    assert calls == []
    cache.tick(clock.replace(hour=7))
    assert len(calls) == len(DATABASE_SYNC_MODULES)
    cache.tick(clock.replace(hour=7, minute=5))
    assert len(calls) == len(DATABASE_SYNC_MODULES)


def test_progress_shared_between_workers_and_interruption(cached, engine, monkeypatch):
    from app.core.sync_progress import report, reporter
    settings, cache = cached
    other = SnapshotCache(settings, engine)
    original = cache.copy_module
    def copy(module):
        report('读取测试表', 2000, percent=35)
        report('重试较早阶段', percent=10)
        state = other.status()
        item = next(m for m in state['modules'] if m['id'] == module)
        assert state['refreshing']
        assert item['progress']['status'] == 'running'
        assert item['progress']['processed'] == 2000
        assert item['progress']['percent'] == 35
        return original(module)
    monkeypatch.setattr(cache, 'copy_module', copy)
    cache.refresh('industry-momentum', background=False)
    item = next(m for m in other.status()['modules'] if m['id'] == 'industry-momentum')
    assert item['progress']['status'] == 'complete'
    assert item['progress']['percent'] == 100
    assert reporter.get() is None
    state = cache.read_state()
    state['modules']['industry-momentum']['progress']['status'] = 'running'
    cache.write_state(state)
    assert other.status()['modules'][0]['progress']['status'] == 'interrupted'


def test_evening_restart_does_not_catch_up(cached, monkeypatch):
    _, cache = cached
    calls = []
    monkeypatch.setattr(cache, '_refresh_locked', lambda *args: calls.append(args))
    clock = datetime(2026,10,19,22,tzinfo=CHINA)
    cache.tick(clock)
    cache.tick(clock + timedelta(minutes=5))
    assert calls == []


def test_failure_never_reports_one_hundred_percent(cached, monkeypatch):
    from app.core.sync_progress import report
    _, cache = cached
    def fail(module):
        report('校验', percent=100)
        raise ValueError('invalid snapshot')
    monkeypatch.setattr(cache, 'copy_module', fail)
    cache.refresh('industry-momentum', background=False)
    progress = cache.status()['modules'][0]['progress']
    assert progress['status'] == 'failed'
    assert progress['percent'] == 99


def test_nested_stage_ranges_restore_after_exception():
    from app.core.sync_progress import report, reporter, stage_range
    seen = []
    token = reporter.set(lambda stage, rows, percent: seen.append(percent))
    try:
        with pytest.raises(ValueError):
            with stage_range(10, 90):
                report('half', percent=50)
                raise ValueError()
        report('outside', percent=95)
        assert seen == [50, 95]
    finally:
        reporter.reset(token)


def test_cancel_cross_worker_keeps_snapshot_and_clears_retry_errors(cached, engine, monkeypatch):
    from app.core.sync_progress import report
    settings, cache = cached
    module = 'industry-momentum'
    cache.refresh(module, background=False)
    original = cache.read_state()['modules'][module]['file']
    state = cache.read_state()
    state['modules'][module]['error'] = 'old failure'
    cache.write_state(state)
    other = SnapshotCache(settings, engine)
    real_copy = cache.copy_module
    calls = []
    def cancel_copy(selected, full=False):
        calls.append(selected)
        status = other.status()
        assert not next(m for m in status['modules'] if m['id'] == module)['error']
        assert not other.cancel('0' * 32)
        assert other.cancel(status['run_id'])
        assert other.status()['cancelling']
        report('page boundary')
        raise AssertionError('Cancellation was ignored')
    monkeypatch.setattr(cache, 'copy_module', cancel_copy)
    cache.refresh(background=False)
    state = cache.read_state()
    assert calls == [module]
    assert state['pending'] == []
    assert state['modules'][module]['file'] == original
    assert all(m['progress']['status'] == 'cancelled' for m in state['modules'].values())
    assert all(not m.get('error') for m in state['modules'].values())
    assert not cache.stop_event.is_set()
    assert not other.refreshing()
    assert not other.cancel(state['run_id'])
    monkeypatch.setattr(cache, 'copy_module', real_copy)
    cache.refresh(module, background=False)
    assert cache.read_state()['modules'][module]['progress']['status'] == 'complete'


def test_cancel_endpoint_rejects_idle_and_invalid_run(cached, engine):
    settings, _ = cached
    with TestClient(create_app(settings, engine)) as client:
        assert client.post('/api/v1/cache/cancel', json={'run_id': '0'*32}).status_code == 409
        assert client.post('/api/v1/cache/cancel', json={'run_id': '../bad'}).status_code == 422


def test_cancel_one_running_and_one_queued_module_continues_others(cached, engine, monkeypatch):
    from app.core.sync_progress import report
    settings, cache = cached
    other = SnapshotCache(settings, engine)
    running, queued = list(DATABASE_SYNC_MODULES)[:2]
    cache.refresh(running, background=False)
    previous = cache.read_state()['modules'][running]['file']
    original = cache.copy_module
    calls = []
    def copy(module, full=False):
        calls.append(module)
        if module == running:
            run_id = other.status()['run_id']
            assert not other.cancel(run_id, 'unknown')
            assert other.cancel(run_id, queued)
            assert other.cancel(run_id, running)
            statuses = {m['id']: m for m in other.status()['modules']}
            assert statuses[running]['cancelling'] and statuses[queued]['cancelling']
            assert not other.status()['cancelling']
            report('safe boundary')
            pytest.fail('cancel ignored')
        return original(module, full)
    monkeypatch.setattr(cache, 'copy_module', copy)
    cache.refresh(background=False)
    state = cache.read_state()
    assert queued not in calls
    assert len(calls) == len(DATABASE_SYNC_MODULES) - 1
    assert state['modules'][running]['file'] == previous
    for module, entry in state['modules'].items():
        assert entry['progress']['status'] == ('cancelled' if module in (running, queued) else 'complete')
        assert not entry.get('error')
    assert state['pending'] == []
    assert not cache.stop_event.is_set()


def test_progress_stage_is_persisted_before_blocking_operation(cached, engine, monkeypatch):
    from app.core.sync_progress import report
    settings, cache = cached
    other = SnapshotCache(settings, engine)
    original = cache.copy_module
    monkeypatch.setattr('app.core.cache.time.monotonic', lambda: 100.0)
    def copy(module):
        report('resume checkpoint', percent=10)
        report('read next page')
        progress = other.status()['modules'][0]['progress']
        assert progress['stage'] == 'read next page'
        assert progress['percent'] == 10
        return original(module)
    monkeypatch.setattr(cache, 'copy_module', copy)
    cache.refresh('industry-momentum', background=False)
    assert not cache.read_state()['modules']['industry-momentum']['error']


def test_microcap_repair_marker_syncs_when_mysql_timestamps_are_the_same(cached,engine,session):
    from app.models.microcap import MicrocapTemperature,DEFAULT_SERIES
    from test_microcap_api import row as micro_row
    _,cache=cached
    record=micro_row(datetime(2026,1,6).date(),.4)
    session.add(record)
    session.commit()
    cache.refresh('microcap-temperature',background=False)
    record.sample_count=0
    session.commit()
    cache.refresh('microcap-temperature',background=False)
    with cache.session('microcap-temperature') as local:
        assert local.get(MicrocapTemperature,(DEFAULT_SERIES,record.trade_date)).sample_count==0
    record.sample_count=21
    session.commit()
    cache.refresh('microcap-temperature',background=False)
    with cache.session('microcap-temperature') as local:
        assert local.get(MicrocapTemperature,(DEFAULT_SERIES,record.trade_date)).sample_count==21


def test_api_explicit_full_rebuild_is_forwarded(cached, engine, monkeypatch):
    settings, _ = cached
    app = create_app(settings, engine)
    calls = []
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.cache, 'refresh', lambda module, **kwargs: calls.append((module, kwargs)) or True)
        assert client.post('/api/v1/cache/refresh', json={'module': 'industry-momentum', 'full': True}).status_code == 202
        assert client.post('/api/v1/cache/refresh', json={'full': True}).status_code == 202
        assert calls == [('industry-momentum', {'full': True}), (None, {'full': True})]
        assert client.post('/api/v1/cache/refresh', json={'module': 'unknown', 'full': True}).status_code == 422


@pytest.mark.parametrize('full', [False, True])
def test_public_sync_excludes_collector_owned_modules_and_stale_pending(cached, monkeypatch, full):
    _, cache = cached
    calls = []
    original = cache.copy_module
    monkeypatch.setattr(cache, 'copy_module', lambda module, **kwargs: calls.append(module) or original(module, **kwargs))
    state = cache.read_state()
    cache.root.mkdir(parents=True, exist_ok=True)
    state['pending'] = list(COLLECTOR_OWNED_MODULES)
    state['pending_full'] = True
    cache.write_state(state)
    assert cache.refresh(background=False, full=full)
    assert calls == list(DATABASE_SYNC_MODULES)
    assert {item['id'] for item in cache.status()['modules']} == set(DATABASE_SYNC_MODULES)
    assert not cache.read_state().get('pending')


def test_internal_collection_can_publish_hidden_modules(cached, monkeypatch):
    _, cache = cached
    calls = []
    original = cache.copy_module
    monkeypatch.setattr(cache, 'copy_module', lambda module, **kwargs: calls.append(module) or original(module, **kwargs))
    assert cache.refresh_market_collection()
    assert cache.refresh_etf_collection()
    assert cache.refresh('dividend-selection', background=False, reason='dividend-collection')
    assert calls == ['market-sentiment', 'market-crowding', 'etf-shares', 'dividend-selection']


@pytest.mark.parametrize('stamp', ['2026-11-07T10:00:00+08:00', '2026-10-01T10:00:00+08:00'])
def test_closed_day_manual_failure_is_not_retried(cached, monkeypatch, stamp):
    _, cache = cached
    monkeypatch.setattr('app.core.cache.now', lambda: datetime.fromisoformat(stamp))
    calls = []
    def fail(module):
        calls.append(module)
        raise OperationalError('read', {}, Exception(2003, 'test'))
    monkeypatch.setattr(cache, 'copy_module', fail)
    assert cache.refresh('industry-momentum', background=False)
    assert calls == ['industry-momentum']
    state = cache.read_state()
    assert not state['pending']
    assert state['pending_manual_isolated']
    assert state['modules']['industry-momentum']['progress']['status'] == 'failed'


def test_scheduled_sync_stops_before_next_module_on_closed_day(cached, monkeypatch):
    _, cache = cached
    moment = [datetime.fromisoformat('2026-11-06T23:59:59+08:00')]
    monkeypatch.setattr('app.core.cache.now', lambda: moment[0])
    calls = []
    def copy(module):
        calls.append(module)
        moment[0] = datetime.fromisoformat('2026-11-07T00:00:01+08:00')
        raise OperationalError('read', {}, Exception(2003, 'test'))
    monkeypatch.setattr(cache, 'copy_module', copy)
    assert cache.refresh(background=False, reason='scheduled')
    assert len(calls) == 1


@pytest.fixture
def outcome_events(monkeypatch):
    import sys
    from types import ModuleType
    events = []
    fake = ModuleType('app.services.collection_outcomes')
    fake.record_outcome = lambda settings, module, **kwargs: events.append(dict(module=module, **kwargs))
    monkeypatch.setitem(sys.modules, 'app.services.collection_outcomes', fake)
    return events


def test_sync_outcomes_preserve_success_then_later_failure(cached, monkeypatch, outcome_events):
    _, cache = cached
    module = 'industry-momentum'
    cache.refresh(module, background=False)
    first_file = cache.read_state()['modules'][module]['file']
    def fail(*args, **kwargs):
        raise ValueError('invalid source')
    monkeypatch.setattr(cache, 'copy_module', fail)
    cache.refresh(module, background=False)
    assert [e['outcome'] for e in outcome_events] == ['complete', 'failed']
    assert {e['trigger'] for e in outcome_events} == {'manual'}
    assert len({e['run_key'] for e in outcome_events}) == 2
    assert cache.read_state()['modules'][module]['file'] == first_file


def test_sync_outcomes_exclude_collector_owned_and_internal_refresh(cached, monkeypatch, outcome_events):
    _, cache = cached
    monkeypatch.setattr(cache, 'copy_module', lambda module, **kwargs: (module + '-test.sqlite', 1))
    for module in COLLECTOR_OWNED_MODULES:
        cache.refresh(module, background=False)
    cache.refresh('industry-momentum', background=False, reason='internal-collection')
    assert outcome_events == []


def test_scheduled_sync_outcomes_cover_only_database_modules(cached, monkeypatch, outcome_events):
    import app.core.cache as module
    _, cache = cached
    monkeypatch.setattr(module, 'automatic_collection_allowed', lambda _: True)
    monkeypatch.setattr(cache, 'copy_module', lambda key, **kwargs: (key + '-test.sqlite', 1))
    monkeypatch.setattr(cache, 'outcome_has_data', lambda key, entry: True)
    cache.refresh(background=False, reason='scheduled')
    assert {e['module'] for e in outcome_events} == set(DATABASE_SYNC_MODULES)
    assert {e['trigger'] for e in outcome_events} == {'scheduled'}
    assert {e['outcome'] for e in outcome_events} == {'complete'}


def test_cancelled_sync_records_cancelled_not_success(cached, monkeypatch, outcome_events):
    from app.core.sync_progress import report
    _, cache = cached
    def cancel(*args, **kwargs):
        assert cache.cancel(cache.read_state()['run_id'])
        report('check cancellation')
    monkeypatch.setattr(cache, 'copy_module', cancel)
    cache.refresh('industry-momentum', background=False)
    assert [e['outcome'] for e in outcome_events] == ['cancelled']


def test_stopped_running_sync_records_interruption_not_success(cached, monkeypatch, outcome_events):
    _, cache = cached
    def stop(*args, **kwargs):
        cache.stop_event.set()
        return None
    monkeypatch.setattr(cache, 'copy_with_retry', stop)
    cache.refresh('industry-momentum', background=False)
    assert [e['outcome'] for e in outcome_events] == ['interrupted']
    assert cache.read_state()['pending'] == ['industry-momentum']


def test_empty_sync_replaces_old_green_with_unknown(cached, session, outcome_events):
    _, cache = cached
    cache.refresh('industry-momentum', background=False)
    session.execute(IndustryMomentum.__table__.delete())
    session.commit()
    assert cache.refresh('industry-momentum', background=False, full=True)
    assert cache.read_state()['modules']['industry-momentum']['progress']['status'] == 'complete'
    assert [e['outcome'] for e in outcome_events] == ['complete', 'unknown']


@pytest.mark.parametrize('module,tables', [
    ('etf-momentum', ('etf_momentum_pool', 'etf_momentum_heat')),
    ('institution-ownership', ('ownership_stock', 'ownership_report', 'ownership_holders')),
])
def test_catalogue_only_snapshot_does_not_prove_research_data(cached, module, tables):
    import sqlite3
    _, cache = cached
    cache.root.mkdir(parents=True, exist_ok=True)
    filename = module + '-catalogue.sqlite'
    with sqlite3.connect(cache.root / filename) as db:
        for table in tables:
            db.execute('CREATE TABLE "' + table + '" (id INTEGER)')
        db.execute('INSERT INTO "' + tables[0] + '" VALUES(1)')
    entry = dict(file=filename, rows=1)
    assert not cache.outcome_has_data(module, entry)
    with sqlite3.connect(cache.root / filename) as db:
        db.execute('INSERT INTO "' + tables[-1] + '" VALUES(1)')
    assert cache.outcome_has_data(module, dict(entry, rows=2))


@pytest.mark.parametrize('module', sorted(DATABASE_SYNC_MODULES))
def test_zero_rows_never_proves_research_data(cached, module):
    _, cache = cached
    assert not cache.outcome_has_data(module, dict(file=module + '-missing.sqlite', rows=0))
