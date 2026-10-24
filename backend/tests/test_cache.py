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
         '/market-crowding/history', '/market-sentiment/history', '/microcap-temperature/history', '/etf-shares/history',
         '/earnings-gap/stocks?period=2026-09-30&search=测试&sort=profit_yoy',
         '/earnings-gap/stocks/600001.XSHG?period=2026-09-30',
         '/microcap-breadth/history', '/dividend-selection/latest', '/high-growth/latest', '/etf-momentum/latest']


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
    selects = [s for s in statements if s.lstrip().startswith('SELECT')]
    assert len(selects) == sum(len(models) for _, models in MODULES.values())
    # Optional ownership journal is detected with read-only SQLite table introspection.
    assert all(s.lstrip().startswith(('SELECT', 'PRAGMA main.table_info(', 'PRAGMA temp.table_info('))
               for s in statements)
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
    assert cache.read_state()['modules'] == {}
    cache.refresh(background=False)
    first = cache.read_state()['modules']['industry-momentum']['file']
    SnapshotCache(settings,engine).tick(morning)
    assert cache.read_state()['modules']['industry-momentum']['file'] == first
    assert settings.cache_refresh_time == '20:00'
    cache.tick(morning.replace(hour=18))
    cache.tick(morning.replace(hour=19, minute=59))
    assert cache.read_state()['modules']['industry-momentum']['file'] == first
    evening = morning.replace(hour=20)
    monkeypatch.setattr('app.core.cache.now', lambda: evening)
    cache.tick(evening)
    second = cache.read_state()['modules']['industry-momentum']['file']
    assert second != first
    cache.tick(evening+timedelta(seconds=30))
    assert cache.read_state()['modules']['industry-momentum']['file'] == second
    assert latest_slot(morning,'20:00').isoformat() == '2026-10-17T20:00:00+08:00'


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
    assert state['pending'] == ['dividend-selection']
    assert state['modules']['dividend-selection']['file'] == dividend_before
    cache.refresh('dividend-selection', background=False)
    state = cache.read_state()
    assert state['pending'] == []
    assert state['modules']['industry-momentum']['file'] == previous
    assert state['modules']['dividend-selection']['file'] != dividend_before


def test_missing_module_waits_for_manual_or_next_schedule(cached, monkeypatch):
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
    assert calls == []
    cache.tick(clock+timedelta(minutes=1))
    cache.tick(clock+timedelta(minutes=5))
    assert calls == []
    cache.tick(clock.replace(hour=20))
    assert len(calls) == len(MODULES)
    cache.tick(clock.replace(hour=20, minute=5))
    assert len(calls) == len(MODULES)


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
    clock = datetime(2026,10,18,22,tzinfo=CHINA)
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
    running, queued = list(MODULES)[:2]
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
    assert len(calls) == len(MODULES) - 1
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
