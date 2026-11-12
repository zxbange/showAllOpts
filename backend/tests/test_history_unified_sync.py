"""History really reaches page stores through shared daily/manual/full sync."""
from datetime import date, datetime, timedelta
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event

from app.core.cache import CHINA, HISTORY_MODULES, MODULES, SnapshotCache
from app.core.config import Settings
from app.core import history_sync
from app.main import create_app
from app.services import growth_dividend_import as growth, valuation_ruler_import as valuation
from app.services.valuation_ruler_model import PUBLIC_SOURCE, validate_day
from history_sync_helpers import seed_history_sources

DAY = date(2020, 1, 2)
MOMENT = datetime(2026, 11, 10, 22, tzinfo=CHINA)


@pytest.fixture
def setup(engine, tmp_path, monkeypatch):
    seed_history_sources(engine)
    settings = Settings(_env_file=None, cache_enabled=True, cache_auto_refresh=False,
                        log_dir=tmp_path/'log', growth_dividend_worker_enabled=False,
                        valuation_ruler_worker_enabled=False)
    monkeypatch.setattr('app.core.cache.now', lambda: MOMENT)
    cache = SnapshotCache(settings, engine)
    return settings, cache


def entry(cache, module):
    return next(item for item in cache.status()['modules'] if item['id'] == module)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def count(service):
    with service.db() as db:
        return db.execute('SELECT COUNT(*) FROM points').fetchone()[0]


@pytest.mark.parametrize('full', [False, True])
def test_global_api_sync_publishes_to_both_page_apis(setup, engine, full):
    settings, _ = setup
    with TestClient(create_app(settings, engine)) as client:
        cache = client.app.state.cache
        assert client.post('/api/v1/cache/refresh', json={'full': full}).status_code == 202
        cache.worker.join(timeout=10)
        assert not cache.worker.is_alive()
        for module in HISTORY_MODULES:
            assert entry(cache,module)['ready'] and entry(cache,module)['progress']['status'] == 'complete'
            assert client.post('/api/v1/'+module+'/sync-joinquant').status_code == 404
        # Disconnect the source. The existing actual API/service must still serve
        # the imported values, not merely a newly registered cache label/file.
        def disconnected(*args):
            pytest.fail('Page query attempted a source query')
        event.listen(engine, 'before_cursor_execute', disconnected)
        try:
            growth_page = client.get('/api/v1/growth-dividend/history').json()
            valuation_page = client.get('/api/v1/valuation-ruler/history').json()
            assert growth_page['latest']['chinext'] == 1000
            assert growth_page['latest']['dividend'] == 1001
            assert valuation_page['latest']['index_close'] == 4000
            assert valuation_page['benchmarks']['sample_days'] == 1
        finally:
            event.remove(engine, 'before_cursor_execute', disconnected)


def test_daily_schedule_includes_actual_history_imports(setup):
    _, cache = setup
    # Existing daily schedule semantics: no catch-up on startup, next due slot
    # imports both service modules without a second scheduler or separate action.
    cache.tick(MOMENT)
    assert all(not entry(cache,module)['ready'] for module in HISTORY_MODULES)
    cache.tick(MOMENT + timedelta(days=1))
    for module in HISTORY_MODULES:
        assert entry(cache,module)['ready']
        assert count(history_sync.service_for(cache,module)) > 0


@pytest.mark.parametrize('module', list(HISTORY_MODULES))
@pytest.mark.parametrize('full', [False, True])
def test_append_preserves_public_history_and_conflicts_and_old_snapshots(setup, engine, module, full):
    _, cache = setup
    service = history_sync.service_for(cache,module)
    public_day = date(2019,12,31)
    if module == 'growth-dividend':
        with service.db() as db:
            db.execute('INSERT INTO points VALUES (?,?,?,?,?,?)',('399006',str(public_day),999,'public','url','stamp'))
            db.execute('INSERT INTO points VALUES (?,?,?,?,?,?)',('399006',str(DAY),888,'public','url','stamp'))
    else:
        members = {f'{i:06d}.XSHE':'10' for i in range(300)}
        with service.db() as db:
            for day in (public_day, DAY):
                service.publish_day(db,validate_day(day,'999',members,PUBLIC_SOURCE),members,{'public':True})
    cache.refresh(module,background=False)
    first = cache.snapshot_path(module,cache.read_state()['modules'][module])
    before = digest(first)
    # Source deletion must never erase valid local/public history on full sync.
    with engine.begin() as db:
        if module == 'growth-dividend':
            db.execute(growth.PRICES.delete().where(growth.PRICES.c.index_code=='000922.XSHG'))
        else:
            db.execute(valuation.CALENDAR.insert(),dict(trade_date=date(2019,12,30),
                       source=valuation.CALENDAR_SOURCE,updated_at=datetime(2020,1,2)))
    cache.refresh(module,background=False,full=full)
    assert digest(first) == before  # Published snapshots are immutable.
    assert entry(cache,module)['ready'] and not entry(cache,module)['error']
    assert '冲突' in entry(cache,module)['sync_note'] and '缺失' in entry(cache,module)['sync_note']
    with service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM comparisons').fetchone()[0] > 0
        if module == 'growth-dividend':
            assert db.execute('SELECT close FROM points WHERE code=? AND trade_date=?',('399006',str(DAY))).fetchone()[0] == 888
            assert db.execute('SELECT source FROM points WHERE trade_date=?',(str(public_day),)).fetchone()[0] == 'public'
            assert db.execute('SELECT close FROM points WHERE code=? AND trade_date=?',('000922',str(DAY))).fetchone()[0] == 1001
        else:
            assert json.loads(db.execute('SELECT payload FROM points WHERE trade_date=?',(str(DAY),)).fetchone()[0])['index_close'] == '999.00000000'
            assert db.execute('SELECT COUNT(*) FROM points').fetchone()[0] == 2
            assert db.execute('SELECT trade_date FROM failures').fetchone()[0] == '2019-12-30'


@pytest.mark.parametrize('module', list(HISTORY_MODULES))
@pytest.mark.parametrize('failure', ['invalid_source','cancel','replace'])
def test_failed_or_cancelled_sync_preserves_page_and_snapshot(setup, engine, monkeypatch, module, failure):
    _, cache = setup
    cache.refresh(module,background=False)
    service = history_sync.service_for(cache,module)
    page = service.root/'history.sqlite3'
    old_page = digest(page)
    previous = dict(cache.read_state()['modules'][module])
    snapshot = cache.snapshot_path(module,previous)
    old_snapshot = digest(snapshot)
    if failure == 'invalid_source':
        with engine.begin() as db:
            table = growth.PRICES if module == 'growth-dividend' else valuation.MEMBER
            db.execute(table.update().values(source='bad'))
    elif failure == 'cancel':
        real = history_sync.shutil.copyfile
        def cancel_after_staging(source,target):
            result = real(source,target)
            assert cache.cancel(cache.read_state()['run_id'],module)
            return result
        monkeypatch.setattr(history_sync.shutil,'copyfile',cancel_after_staging)
    else:
        real = history_sync.os.replace
        def fail_replace(source,target):
            if str(target) == str(page):
                raise OSError('simulated publication failure')
            return real(source,target)
        monkeypatch.setattr(history_sync.os,'replace',fail_replace)
    cache.refresh(module,background=False,full=True)
    after = cache.read_state()['modules'][module]
    assert after['file'] == previous['file'] and after['updated_at'] == previous['updated_at']
    assert digest(page) == old_page and digest(snapshot) == old_snapshot
    assert after['progress']['status'] == ('cancelled' if failure=='cancel' else 'failed')
    assert not service.running() and not cache.refreshing()
    assert not list(service.root.glob('.cache-sync-*'))
    assert len(list(cache.root.glob(module+'-*.sqlite'))) == 1


@pytest.mark.parametrize('module', list(HISTORY_MODULES))
def test_public_collection_lock_blocks_shared_import(setup,module):
    _, cache = setup
    service = history_sync.service_for(cache,module)
    lock = service.acquire()
    try:
        cache.refresh(module,background=False)
        assert entry(cache,module)['error'] and not entry(cache,module)['ready']
        assert count(service)==0
    finally:
        lock.release()
    cache.refresh(module,background=False)
    assert entry(cache,module)['ready']


def test_legacy_namespace_and_source_change_are_safe(setup, engine, tmp_path):
    settings, cache = setup
    schema = [(key,[(m.__tablename__,[(c.name,str(c.type)) for c in m.__table__.columns]) for m in models])
              for key,(_,models) in MODULES.items() if key!='market-sentiment' and key not in HISTORY_MODULES]
    url=engine.url
    legacy=[url.drivername,url.host,url.port,url.database,url.username,schema,'provider-results-v2']
    assert cache.root.name == hashlib.sha256(json.dumps(legacy,ensure_ascii=True).encode()).hexdigest()[:20]
    for module in HISTORY_MODULES:
        cache.refresh(module,background=False)
    other_engine=create_engine('sqlite:///'+str(tmp_path/'changed-source.sqlite'))
    try:
        other=SnapshotCache(settings,other_engine)
        assert other.root!=cache.root
        assert all(not entry(other,module)['ready'] for module in HISTORY_MODULES)
        # Old public/local history remains available, but never claims that the
        # changed source has synchronized until its own validated import succeeds.
        for module in HISTORY_MODULES:
            service=history_sync.service_for(other,module)
            before=digest(service.root/'history.sqlite3')
            other.refresh(module,background=False)
            assert not entry(other,module)['ready'] and entry(other,module)['error']
            assert digest(service.root/'history.sqlite3')==before
        # Even copying stale state/files into a new namespace cannot forge ready.
        cache.source=other_engine
        assert all(not entry(cache,module)['ready'] for module in HISTORY_MODULES)
    finally:
        other_engine.dispose()


def test_unified_sync_admin_csrf_and_viewer_permissions(setup,engine):
    from account_helpers import PASSWORD,sign_in
    settings,_=setup
    with TestClient(create_app(settings,engine)) as client:
        csrf=client.headers.pop('X-CSRF-Token')
        assert client.post('/api/v1/cache/refresh',json={'module':'growth-dividend'}).status_code==403
        client.headers['X-CSRF-Token']=csrf
        accounts=client.app.state.accounts
        accounts.create('viewer',PASSWORD,actor=accounts.list()[0]['id'])
        sign_in(client,'viewer')
        assert client.get('/api/v1/cache/status').status_code==200
        assert client.post('/api/v1/cache/refresh',json={'module':'valuation-ruler','full':True}).status_code==403
        client.cookies.clear()
        assert client.get('/api/v1/cache/status').status_code==401


@pytest.mark.parametrize('module', list(HISTORY_MODULES))
@pytest.mark.parametrize('full',[False,True])
def test_recovered_older_dates_append_and_open_readers_stay_stable(setup,engine,module,full):
    _,cache=setup
    cache.refresh(module,background=False)
    service=history_sync.service_for(cache,module)
    original_count=count(service)
    # A MAX(date) cursor would miss this recovered gap.
    seed_history_sources(engine,day=date(2019,12,30))
    with service.db() as opened_before:
        assert opened_before.execute('SELECT COUNT(*) FROM points').fetchone()[0]==original_count
        cache.refresh(module,background=False,full=full)
        assert opened_before.execute('SELECT COUNT(*) FROM points').fetchone()[0]==original_count
    assert count(service)==2*original_count
    assert not entry(cache,module)['error']


@pytest.mark.parametrize('module', list(HISTORY_MODULES))
def test_cancel_after_atomic_publication_does_not_report_false_rollback(setup,monkeypatch,module):
    _,cache=setup
    service=history_sync.service_for(cache,module)
    real=history_sync.os.replace
    def replace_and_cancel(source,target):
        result=real(source,target)
        if str(target)==str(service.root/'history.sqlite3'):
            assert cache.cancel(cache.read_state()['run_id'],module)
        return result
    monkeypatch.setattr(history_sync.os,'replace',replace_and_cancel)
    cache.refresh(module,background=False)
    assert count(service)>0
    assert entry(cache,module)['progress']['status']=='complete'
    assert entry(cache,module)['ready']


@pytest.mark.parametrize('module', list(HISTORY_MODULES))
def test_service_shutdown_cancels_cleanly_without_starting_another_job(setup,module):
    _,cache=setup
    service=history_sync.service_for(cache,module)
    service.stop_event.set()
    cache.refresh(module,background=False)
    assert entry(cache,module)['progress']['status']=='cancelled'
    assert not entry(cache,module)['ready']
    assert not cache.refreshing() and not service.running()


@pytest.mark.parametrize('module', list(HISTORY_MODULES))
def test_cache_receipt_never_claims_ready_for_missing_or_other_source_page_store(setup,module):
    _,cache=setup
    cache.refresh(module,background=False)
    service=history_sync.service_for(cache,module)
    assert entry(cache,module)['ready']
    with service.db() as db:
        db.execute("UPDATE history_cache_source_v1 SET identity='another-source'")
    assert not entry(cache,module)['ready']
    cache.refresh(module,background=False)
    assert entry(cache,module)['ready']
    (service.root/'history.sqlite3').unlink()
    assert not entry(cache,module)['ready']


@pytest.mark.parametrize('module',list(HISTORY_MODULES))
@pytest.mark.parametrize('outcome',['complete','missing','conflict'])
def test_history_sync_preserves_public_status_and_success_time(setup,engine,module,outcome):
    _,cache=setup
    service=history_sync.service_for(cache,module)
    public_state=dict(run_id='a'*32,active=False,stage='公开采集之前失败',phase='failed',
        error='保留的公开采集错误',last_success_at='2019-12-30T20:00:00+08:00',
        last_attempt_at='2019-12-31T20:00:00+08:00',updated_at='2019-12-31T20:01:00+08:00',
        completed_days=7,conflict_days=9,processed=7,total=10,percent=70,
        calendar_through='2019-12-31',comparison_error='之前的对照错误')
    if outcome=='missing':
        with engine.begin() as db:
            if module=='growth-dividend':
                db.execute(growth.PRICES.delete().where(growth.PRICES.c.index_code=='000688.XSHG'))
            else:
                db.execute(valuation.CALENDAR.insert(),dict(trade_date=date(2019,12,30),
                    source=valuation.CALENDAR_SOURCE,updated_at=datetime(2020,1,2)))
    elif outcome=='conflict':
        with service.db() as db:
            if module=='growth-dividend':
                db.execute('INSERT INTO points VALUES (?,?,?,?,?,?)',
                    ('399006',str(DAY),888,'public','url','stamp'))
            else:
                members={f'{i:06d}.XSHE':'10' for i in range(300)}
                service.publish_day(db,validate_day(DAY,'999',members,PUBLIC_SOURCE),members,{'public':True})
    with service.db() as db:
        db.execute('UPDATE state SET payload=? WHERE id=1',(json.dumps(public_state),))
    cache.refresh(module,background=False)
    after=service.read_state()
    assert all(after[key]==value for key,value in public_state.items())
    status=service.status()
    assert status['last_success_at']==public_state['last_success_at']
    assert status['error']==public_state['error']
    assert entry(cache,module)['progress']['status']=='complete'
    assert entry(cache,module)['updated_at']==MOMENT.isoformat()
    if module=='growth-dividend':
        assert after['sync_summary']['imported_at']==MOMENT.isoformat()
        assert bool(after['sync_summary']['conflicts'])==(outcome=='conflict')
        assert bool(sum(value['missing'] for value in after['sync_summary']['indices'].values()))==(outcome=='missing')
    else:
        assert after['joinquant_import_at']==MOMENT.isoformat()
        assert bool(after['joinquant_import_warning'])==(outcome!='complete')
        assert status['joinquant_conflicts']==int(outcome=='conflict')  # Not public conflict_days=9.
        assert status['joinquant_imported']==int(outcome!='conflict')


@pytest.mark.parametrize('module',list(HISTORY_MODULES))
def test_page_in_another_process_identifies_shared_sync_and_cannot_cancel_old_run(setup,monkeypatch,module):
    import subprocess
    import sys
    from pathlib import Path
    _,cache=setup
    service=history_sync.service_for(cache,module)
    with service.db() as db:
        db.execute('UPDATE state SET payload=? WHERE id=1',
            (json.dumps(dict(run_id='a'*32,active=False,phase='complete',stage='之前公开采集完成')),))
    importer=growth if module=='growth-dividend' else valuation
    read_source=importer.read_source
    observations=[]
    def inspect_ownership(*args,**kwargs):
        script='''
import json,sys
from app.core.config import Settings
from app.services.growth_dividend import GrowthDividend
from app.services.valuation_ruler import ValuationRuler
settings=Settings(_env_file=None,log_dir=sys.argv[1],growth_dividend_worker_enabled=False,valuation_ruler_worker_enabled=False)
service=(GrowthDividend if sys.argv[2]=='growth-dividend' else ValuationRuler)(settings)
print(json.dumps({'status':service.status(),'cancel':service.cancel(),'refresh':service.refresh(background=False)}))
'''
        result=subprocess.run([sys.executable,'-c',script,str(cache.settings.log_path),module],
            cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,check=True,timeout=15)
        observed=json.loads(result.stdout)
        assert observed['status']['database_syncing'] is True
        assert observed['status']['sync_message']==history_sync.SYNC_MESSAGE
        if module=='growth-dividend':
            assert observed['status']['running'] is False
        else:
            assert observed['status']['status']!='running'
        assert observed['cancel'] is False and observed['refresh'] is False
        assert not (service.root/('cancel-'+'a'*32)).exists()
        observations.append(observed)
        return read_source(*args,**kwargs)
    monkeypatch.setattr(importer,'read_source',inspect_ownership)
    cache.refresh(module,background=False)
    assert observations and entry(cache,module)['progress']['status']=='complete'
    assert service.status()['database_syncing'] is False
    assert not (service.root/history_sync.OWNER_FILE).exists()


@pytest.mark.parametrize('module',list(HISTORY_MODULES))
def test_stale_database_owner_never_hides_next_public_collection(setup,module):
    _,cache=setup
    service=history_sync.service_for(cache,module)
    marker=service.root/history_sync.OWNER_FILE
    marker.write_text('interrupted shared synchronization')
    lock=service.acquire()
    assert lock is not None and not marker.exists()
    try:
        with service.db() as db:
            db.execute('UPDATE state SET payload=? WHERE id=1',
                (json.dumps(dict(run_id='b'*32,active=True,phase='public')),))
        status=service.status()
        assert not status['database_syncing']
        assert status['running'] if module=='growth-dividend' else status['status']=='running'
        assert service.cancel()
        assert (service.root/('cancel-'+'b'*32)).exists()
    finally:
        lock.release()
        (service.root/('cancel-'+'b'*32)).unlink(missing_ok=True)


@pytest.mark.parametrize('module',list(HISTORY_MODULES))
@pytest.mark.parametrize('old_active',[False,True])
def test_database_owner_covers_caller_entry_and_unlock_boundaries(setup,monkeypatch,module,old_active):
    from types import SimpleNamespace
    _,cache=setup
    service=history_sync.service_for(cache,module)
    with service.db() as db:
        db.execute('UPDATE state SET payload=? WHERE id=1',
            (json.dumps(dict(run_id='c'*32,active=old_active,phase='public')),))
    acquire=service.acquire
    checked=[]
    def assert_database_owner(boundary):
        status=service.status()
        assert status['database_syncing']
        assert not service.cancel()
        assert not (service.root/('cancel-'+'c'*32)).exists()
        checked.append(boundary)
    def instrumented_acquire(*,for_history=False):
        lock=acquire(for_history=for_history)
        if lock is None or not for_history:
            return lock
        assert_database_owner('after-acquire')
        def release():
            assert_database_owner('before-unlock')
            lock.release()
        return SimpleNamespace(release=release)
    monkeypatch.setattr(service,'acquire',instrumented_acquire)
    cache.refresh(module,background=False)
    assert checked==['after-acquire','before-unlock']
    assert entry(cache,module)['progress']['status']=='complete'
    assert not service.status()['database_syncing']
