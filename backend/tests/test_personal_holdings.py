from datetime import datetime, timedelta, timezone, date
from decimal import Decimal
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text

from app.core.config import Settings
from app.services import personal_holdings as module
from app.services.personal_holdings import PersonalHoldings, in_window
from app.services.holdings_calendar import trading_day
from app.services.holdings_snapshot import HoldingsError, normalize, changes
from app.api.routes.personal_holdings import router
from account_helpers import install_accounts, sign_in, PASSWORD


def row(code='000001', amt='100', strategy='策略一', name='平安银行'):
    return dict(code=code, amt=amt, strategy2=strategy, name=name)


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    settings=Settings(_env_file=None, log_dir=tmp_path/'log',auth_allow_http=True)
    monitor=PersonalHoldings(settings)
    now=[datetime(2026,11,2,1,25,tzinfo=timezone.utc)]
    monkeypatch.setattr(module,'clock',lambda:now[0])
    source=create_engine('sqlite:///'+str(tmp_path/'source.sqlite'))
    with source.begin() as connection:
        connection.execute(text('CREATE TABLE positions (strategy2 TEXT,name TEXT,amt TEXT,code TEXT)'))
    counts=[];closed=[]
    def engine(config):
        engine=create_engine(source.url,pool_size=1,max_overflow=0)
        event.listen(engine,'connect',lambda c,r:counts.append(c))
        event.listen(engine,'close',lambda c,r:closed.append(c))
        return engine
    monkeypatch.setattr(monitor.sources,'build_engine',engine)
    config=dict(host='db.test',port=3306,database='stocks',username='reader',password='test-secret-only',table='positions')
    monitor.configure(config)
    yield monitor,source,now,config,counts,closed
    monitor.stop();source.dispose()


def replace(source, rows):
    with source.begin() as db:
        db.execute(text('DELETE FROM positions'))
        for item in rows: db.execute(text('INSERT INTO positions VALUES(:strategy2,:name,:amt,:code)'),item)


def tick(fixture, rows=None):
    monitor,source,now,*_=fixture
    if rows is not None: replace(source,rows)
    now[0]+=timedelta(seconds=15)
    return monitor.poll()


def test_exact_normalization_and_strategy_keys():
    assert normalize([row(amt='100.10'),row(amt=Decimal('0.20'))])[0]['amt']=='100.3'
    assert len(normalize([row(),row(strategy='策略二',amt='20')]))==2
    assert normalize([row(amt=0)])==[]
    for invalid in [None,-1,'NaN','Infinity','0.0000001',True,'bad']:
        with pytest.raises(HoldingsError): normalize([row(amt=invalid)])
    for invalid in [None,1,'', ' '*3]:
        with pytest.raises(HoldingsError): normalize([row(code=invalid)])
    with pytest.raises(HoldingsError): normalize([row(),row(name='另一个名字')])
    assert changes(normalize([row()]),normalize([row(name='新名称')]))==[]
    assert [e['kind'] for e in changes(normalize([row()]),normalize([row(strategy='策略二')]))]==['clear','buy']


def test_baseline_changes_atomic_empty_and_long_connection(monitor):
    job,source,now,config,counts,closed=monitor
    assert not job.poll() and not counts  # saving never starts the source
    job.set_enabled(True)
    assert tick(monitor,[row(),row(strategy='策略二',amt='10')])
    assert job.history('2026-11-02')['events']==[]
    assert tick(monitor,[row(amt='150'),row(code='000002',name='万科',amt='20')])
    events=job.history('2026-11-02')['events']
    assert {e['kind'] for e in events}=={'add','buy','clear'}
    version=job.status()['version']
    assert tick(monitor) and job.status()['version']==version
    assert len(job.history('2026-11-02')['events'])==3
    assert tick(monitor,[]) and job.status()['rows']==[]
    assert len(job.history('2026-11-02')['events'])==5
    assert len(counts)==1 and not closed
    assert not job.connection.in_transaction()
    job.set_enabled(False);job.poll();assert len(closed)==1


def test_failure_incomplete_and_rollback_preserve_old_snapshot(monitor,monkeypatch):
    job,source,now,*_=monitor;job.set_enabled(True);tick(monitor,[row()])
    old=job.status()['rows']
    assert not tick(monitor,[row(amt=None)])
    assert job.status()['rows']==old and job.status()['error']
    assert not job.history('2026-11-02')['events']
    original=job.write
    def fail(db,state):
        if state['rows']!=old:raise sqlite3.OperationalError('secret-private-test')
        original(db,state)
    monkeypatch.setattr(job,'write',fail)
    assert not tick(monitor,[row(amt='150')])
    assert job.status()['rows']==old and not job.history('2026-11-02')['events']
    assert 'secret-private-test' not in job.status()['error']
    monkeypatch.setattr(job,'write',original);tick(monitor)
    assert len(job.history('2026-11-02')['events'])==1


def test_multiworker_and_restart_no_duplicate(monitor,monkeypatch):
    job,source,now,config,counts,closed=monitor;job.set_enabled(True);tick(monitor,[row()])
    other=PersonalHoldings(job.settings)
    monkeypatch.setattr(other.sources,'build_engine',job.sources.build_engine)
    replace(source,[row(amt='200')]);now[0]+=timedelta(seconds=15)
    assert not other.poll()
    job.stop();assert other.poll()
    assert len(other.history('2026-11-02')['events'])==1
    assert not other.poll()
    other.stop()


def test_source_change_baseline_and_stale_inflight_rejected(monitor,monkeypatch):
    job,source,now,config,*_=monitor;job.set_enabled(True);tick(monitor,[row()])
    original=job.read
    def read(connection,table):
        result=original(connection,table)
        job.configure(dict(config,password='different-test-secret'))
        return result
    monkeypatch.setattr(job,'read',read)
    assert not tick(monitor,[])
    assert not job.status()['enabled'] and not job.status()['baseline']
    assert not job.history('2026-11-02')['events']
    monkeypatch.setattr(job,'read',original);job.set_enabled(True);tick(monitor,[row(amt='200')])
    assert not job.history('2026-11-02')['events']


def test_pause_resume_keeps_baseline_and_inflight_fenced(monitor,monkeypatch):
    job,source,now,*_=monitor;job.set_enabled(True);tick(monitor,[row()]);original=job.read
    def read(connection,table):
        result=original(connection,table);job.set_enabled(False);job.set_enabled(True);return result
    monkeypatch.setattr(job,'read',read);assert not tick(monitor,[])
    assert job.status()['rows'] and not job.history('2026-11-02')['events']
    monkeypatch.setattr(job,'read',original);tick(monitor)
    assert job.history('2026-11-02')['events'][0]['kind']=='clear'


def test_idle_disconnect_reconnects_next_poll(monitor):
    job,source,now,config,counts,closed=monitor;job.set_enabled(True);tick(monitor,[row()])
    job.connection.invalidate();assert not tick(monitor,[row(amt='200')])
    assert len(counts)==1 and job.status()['rows'][0]['amt']=='100'
    assert not job.poll();assert tick(monitor)
    assert len(counts)==2 and len(job.history('2026-11-02')['events'])==1


@pytest.mark.parametrize('day,expected', [('2026-11-02',True),('2026-11-01',False),('2026-10-01',False),('2026-10-10',False),('2026-02-23',False),('2026-02-24',True),('2027-01-04',None),('2025-12-31',None)])
def test_calendar_coverage(day,expected):
    assert trading_day(date.fromisoformat(day)) is expected


def test_window_closes_releases_and_next_day_compares(monitor):
    job,source,now,config,counts,closed=monitor;job.set_enabled(True);tick(monitor,[row()])
    now[0]=datetime(2026,11,2,7,10,tzinfo=timezone.utc)
    assert not job.poll() and len(closed)==1
    replace(source,[]);now[0]=datetime(2026,11,3,1,24,59,tzinfo=timezone.utc)
    assert not job.poll()
    now[0]+=timedelta(seconds=1);assert job.poll()
    assert job.history('2026-11-03')['events'][0]['kind']=='clear'
    assert not job.history('2026-11-02')['events']
    now[0]=datetime(2027,1,4,2,tzinfo=timezone.utc);assert not job.poll()
    assert job.status()['calendar_error']
    assert in_window(datetime(2026,11,2,4,tzinfo=timezone.utc))  # lunch included


def test_configuration_encrypted_retain_password_no_probe_write(monitor):
    job,source,now,config,counts,closed=monitor
    assert 'password' not in job.configuration()
    assert b'test-secret-only' not in job.path.read_bytes()
    ref=job.state()['source'];job.configure(dict(config,password=None));assert job.state()['source']==ref
    with pytest.raises(HoldingsError):job.configure(dict(config,host='other.test',password=None))
    job.test(dict(config,password=None));assert job.state()['source']==ref and not job.status()['baseline']
    assert len(counts)==len(closed)==1


def test_admin_only_all_endpoints_csrf_no_password_echo(monitor):
    job,*_=monitor;app=FastAPI();app.state.personal_holdings=job;install_accounts(app,job.settings)
    app.include_router(router,prefix='/api/v1');client=TestClient(app)
    paths=['status','configuration','history?day=2026-11-02']
    for path in paths:assert client.get('/api/v1/personal-holdings/'+path).status_code==401
    app.state.accounts.create('viewer',PASSWORD,'viewer',app.state.accounts.list()[0]['id']);sign_in(client,'viewer')
    for path in paths:assert client.get('/api/v1/personal-holdings/'+path).status_code==403
    for path in ['start','stop','configuration','test','sync']:
        assert client.post('/api/v1/personal-holdings/'+path,json={}).status_code==403
    sign_in(client);assert client.get('/api/v1/personal-holdings/status').status_code==200
    assert 'test-secret-only' not in client.get('/api/v1/personal-holdings/configuration').text
    response=client.post('/api/v1/personal-holdings/configuration',json={'password':'secret-raw-invalid'})
    assert response.status_code==422 and 'secret-raw-invalid' not in response.text
    assert client.post('/api/v1/personal-holdings/sync',json={'request_id':'a'*32}).status_code==200
    client.headers['X-CSRF-Token']='bad';assert client.post('/api/v1/personal-holdings/start',json={}).status_code==403
    assert client.post('/api/v1/personal-holdings/sync',json={'request_id':'b'*32}).status_code==403
    sign_in(client);client.headers['Origin']='https://evil.test'
    assert client.post('/api/v1/personal-holdings/start',json={}).status_code==403
    client.close()


def test_row_limit_and_failed_midstream_are_not_empty(monkeypatch):
    from app.services import holdings_snapshot
    monkeypatch.setattr(holdings_snapshot,'MAX_ROWS',1)
    with pytest.raises(HoldingsError):normalize([row(),row()])
    def broken():
        yield row()
        raise RuntimeError('mock interrupted read')
    with pytest.raises(RuntimeError):normalize(broken())


def test_secret_key_mismatch_cannot_read_or_rewrite(monitor):
    from cryptography.fernet import Fernet
    from pydantic import SecretStr
    from app.services.database_sources import SourceError
    job,*_=monitor
    job.settings.validation_encryption_key=SecretStr(Fernet.generate_key().decode())
    with pytest.raises(SourceError):job.configuration()


def test_history_paginates_and_retains_previous_source_events(monitor):
    job,source,now,config,*_=monitor;job.set_enabled(True);tick(monitor,[row()])
    for qty in ['200','300','400']:tick(monitor,[row(amt=qty)])
    first=job.history('2026-11-02',limit=2)
    rest=job.history('2026-11-02',before=first['next_before'],limit=2)
    assert len(first['events'])==2 and len(rest['events'])==1
    job.configure(dict(config,password='second-source-secret'))
    assert len(job.history('2026-11-02')['events'])==3


def test_cross_process_leader_lock(monitor):
    import multiprocessing
    import portalocker
    job,*_=monitor;job.set_enabled(True);tick(monitor,[row()])
    context=multiprocessing.get_context('fork')
    parent,child=context.Pipe()
    def probe():
        try:
            with portalocker.Lock(str(job.root/'worker.lock'),timeout=0,flags=portalocker.LOCK_EX|portalocker.LOCK_NB):child.send(True)
        except portalocker.exceptions.LockException:child.send(False)
        finally:child.close()
    process=context.Process(target=probe);process.start();assert parent.poll(5);assert parent.recv() is False
    process.join(5);assert process.exitcode==0


def test_target_policy_and_table_injection_are_rejected(monitor,monkeypatch):
    from app.services import database_sources
    from app.services.database_sources import SourceError
    job,source,now,config,*_=monitor
    with pytest.raises(HoldingsError):job.configure(dict(config,table='positions; DROP TABLE positions'))
    monkeypatch.setattr(database_sources,'resolve_addresses',lambda *_:[(None,None,None,None,('169.254.169.254',3306))])
    with pytest.raises(SourceError):job.sources.allowed(dict(config,host='metadata.example'))
    job.settings.validation_allowed_targets=['approved.test:3306']
    with pytest.raises(SourceError):job.configure(config)


def test_concurrent_equivalent_configuration_is_idempotent(monitor):
    from concurrent.futures import ThreadPoolExecutor
    job,source,now,config,*_=monitor
    other=PersonalHoldings(job.settings)
    before=job.state()['source']
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda instance:instance.configure(dict(config,password=None)),[job,other]))
    assert job.state()['source']==before
    other.stop()


def test_probe_error_does_not_echo_driver_secret(monitor,monkeypatch):
    from sqlalchemy.exc import OperationalError
    job,*_=monitor;app=FastAPI();app.state.personal_holdings=job;install_accounts(app,job.settings)
    app.include_router(router,prefix='/api/v1');client=TestClient(app);sign_in(client)
    def fail(_):raise OperationalError('private query',{'password':'never-echo'},Exception('never-echo'))
    monkeypatch.setattr(job,'test',fail)
    result=client.post('/api/v1/personal-holdings/test',json=dict(host='mock.test',port=3306,database='mock',username='reader',table='positions',password='never-echo'))
    assert result.status_code==503 and 'never-echo' not in result.text and 'private query' not in result.text
    client.close()


def test_interrupted_configuration_publication_does_not_trap_active_version(monitor,monkeypatch):
    job,source,now,config,*_=monitor
    old=job.state()['source'];original=job.write
    def broken(db,state):raise sqlite3.OperationalError('mock publish failure')
    monkeypatch.setattr(job,'write',broken)
    with pytest.raises(sqlite3.OperationalError):job.configure(dict(config,password='orphan-secret'))
    assert job.state()['source']==old
    monkeypatch.setattr(job,'write',original)
    job.configure(dict(config,password=None));assert job.state()['source']==old
    job.configure(dict(config,password='new-intended-secret'))
    assert job.state()['source']['revision']==3
    assert job.sources.get(job.state()['source']['id'],3)['password']=='new-intended-secret'


@pytest.mark.parametrize('moment', [datetime(2026,11,2,12,tzinfo=timezone.utc), datetime(2026,11,1,2,tzinfo=timezone.utc), datetime(2027,1,4,2,tzinfo=timezone.utc)])
def test_manual_outside_window_idempotent_and_persistent(monitor, moment):
    job,source,now,config,counts,closed=monitor
    now[0]=moment
    replace(source,[row()])
    assert not job.poll()
    before=job.state()['next_poll']
    job.request_sync('a'*32);job.request_sync('b'*32)
    assert job.poll()
    assert not job.status()['enabled'] and job.status()['baseline']
    assert job.status()['manual']['status']=='completed'
    assert job.state()['next_poll']==before and len(counts)==len(closed)==1
    day=moment.astimezone(module.SHANGHAI).date().isoformat()
    assert not job.history(day)['events']
    job.request_sync('a'*32);job.request_sync('b'*32)
    assert not job.poll() and len(counts)==1
    replace(source,[row(amt='200')]);job.request_sync('c'*32)
    restarted=PersonalHoldings(job.settings)
    restarted.sources.build_engine=job.sources.build_engine
    try:
        assert restarted.poll()
        assert restarted.history(day)['events'][0]['kind']=='add'
    finally: restarted.stop()


def test_manual_failure_and_config_fence(monitor, monkeypatch):
    job,source,now,config,*_=monitor
    replace(source,[row()]);job.request_sync('a'*32);assert job.poll()
    now[0]+=timedelta(hours=10)
    job.request_sync('b'*32)
    original=job.read
    def failed(*args): raise RuntimeError('private-secret-must-not-appear')
    monkeypatch.setattr(job,'read',failed)
    assert not job.poll()
    assert job.status()['manual']['status']=='failed'
    assert job.status()['rows'] and not job.history('2026-11-02')['events']
    assert 'private-secret' not in str(job.status())
    def changed(*args):
        result=original(*args)
        job.configure(dict(config,table='changed',password='new-test-secret'))
        return result
    monkeypatch.setattr(job,'read',changed)
    job.request_sync('c'*32);assert not job.poll()
    assert job.status()['manual']['status']=='cancelled'
    assert not job.status()['baseline']


def test_manual_automatic_leader_no_overlap(monitor):
    job,source,now,config,counts,closed=monitor
    job.set_enabled(True);replace(source,[row()]);assert job.poll()
    next_poll=job.state()['next_poll']
    other=PersonalHoldings(job.settings)
    other.sources.build_engine=job.sources.build_engine
    try:
        other.request_sync('a'*32)
        assert not other.poll()
        replace(source,[row(amt='150')]);assert job.poll()
        assert job.state()['next_poll']==next_poll and job.state()['enabled']
        assert len(counts)==1 and not closed
        assert len(job.history('2026-11-02')['events'])==1
        assert not other.poll()
    finally: other.stop()


def test_manual_empty_snapshot_and_stop_fence(monitor, monkeypatch):
    job,source,now,*_=monitor
    job.set_enabled(True);replace(source,[row()]);assert job.poll()
    now[0]+=timedelta(hours=10)
    job.request_sync('a'*32);replace(source,[]);assert job.poll()
    assert job.status()['enabled'] and not job.status()['rows']
    assert job.history('2026-11-02')['events'][0]['kind']=='clear'
    original=job.read
    def stop_during_read(*args):
        result=original(*args);job.set_enabled(False);return result
    monkeypatch.setattr(job,'read',stop_during_read)
    replace(source,[row()]);job.request_sync('b'*32)
    assert not job.poll() and not job.status()['rows']
    assert job.status()['manual']['status']=='cancelled'
    assert len(job.history('2026-11-02')['events'])==1


def test_manual_requires_saved_configuration(tmp_path):
    job=PersonalHoldings(Settings(_env_file=None,log_dir=tmp_path/'log'))
    with pytest.raises(HoldingsError,match='保存'):job.request_sync('a'*32)
    assert not job.status()['configured']
