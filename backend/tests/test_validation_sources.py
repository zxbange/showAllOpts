from contextlib import contextmanager
import json
import socket
import threading
from unittest.mock import Mock
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
import portalocker
import pytest
from sqlalchemy import create_engine
from pydantic import SecretStr
from app.core.config import Settings
from app.services.short_validation import ValidationJobs
from app.services.validation_sources import ValidationSources, SourceError
from app.api.routes.short_term import router
from test_short_validation import setup, create, finish

@pytest.fixture
def secure(tmp_path):
    settings=Settings(_env_file=None,log_dir=tmp_path,validation_admin_key='a'*40,
        validation_encryption_key=Fernet.generate_key().decode(),validation_allowed_targets=['db.example:3306','127.0.0.1:3306'])
    jobs=ValidationJobs(settings,create_engine('sqlite://'));jobs.sources=ValidationSources(jobs,settings)
    app=FastAPI();app.state.short_validation=jobs;app.include_router(router)
    with TestClient(app,base_url='https://testserver') as client:yield jobs,jobs.sources,settings,client

@pytest.fixture
def config():
    return dict(name='研究库',host='db.example',port=3306,database='private-db',username='private-user',password='unique-private-password',tls='verify',ca_pem='')

def auth(client):client.headers['Authorization']='Bearer '+'a'*40

def test_encryption_versions_and_no_echo(secure,config):
    jobs,sources,settings,client=secure;auth(client)
    response=client.post('/short-term/validation/sources/save',json=config)
    assert response.status_code==200,response.text
    first=response.json();assert 'password' not in first
    assert config['password'] not in response.text+client.get('/short-term/validation/sources').text
    raw=(jobs.root/'jobs.sqlite3').read_bytes()
    for key in ('name','host','database','username','password'):assert config[key].encode() not in raw
    second=client.post('/short-term/validation/sources/save',json={**config,'password':None,'host':'127.0.0.1','id':first['id'],'revision':1}).json()
    assert second['revision']==2 and sources.get(first['id'],1)==config
    assert sources.get(first['id'],2)['password']==config['password']
    assert client.post('/short-term/validation/sources/save',json={**config,'id':first['id'],'revision':1}).status_code==409
    with jobs.db() as db:
        one=db.execute('SELECT ciphertext FROM validation_sources WHERE revision=1').fetchone()[0]
        db.execute('UPDATE validation_sources SET ciphertext=? WHERE revision=2',(one,))
    with pytest.raises(SourceError,match='校验'):sources.get(first['id'],2)

def test_bad_keys_block_reads_and_all_writes(secure,config):
    _,sources,settings,_=secure;ref=sources.save(config)
    for key in (Fernet.generate_key().decode(),'','not-a-key'):
        settings.validation_encryption_key=SecretStr(key)
        with pytest.raises(SourceError):sources.get(ref['id'],ref['revision'])
        with pytest.raises(SourceError):sources.save(config)

def test_permissions_https_invalid_payload_no_echo(secure,config,monkeypatch):
    _,sources,_,client=secure;probe=Mock();monkeypatch.setattr(sources,'test',probe)
    for path in ('/sources/save','/sources/test','/jobs'):
        assert client.post('/short-term/validation'+path,json=config).status_code==403
    assert client.get('/short-term/validation/sources').status_code==403
    auth(client)
    assert client.post('http://testserver/short-term/validation/sources/test',json=config).status_code==403
    for data in ({**config,'port':config['password']},{**config,'password':{'secret':config['password']}}):
        result=client.post('/short-term/validation/sources/save',json=data)
        assert result.status_code==422 and config['password'] not in result.text
    assert not probe.called

def test_connection_failure_safe(secure,config,monkeypatch,caplog):
    _,sources,_,client=secure;auth(client)
    engine=Mock();monkeypatch.setattr(sources,'build_engine',lambda c:engine)
    import app.services.short_statistics as stats
    def failure(*a):raise RuntimeError(config['password']+' '+config['host'])
    monkeypatch.setattr(stats,'validate_schema',failure)
    response=client.post('/short-term/validation/sources/test',json=config)
    assert response.status_code==409 and config['password'] not in response.text+caplog.text
    engine.dispose.assert_called_once()

@pytest.mark.parametrize('address',['169.254.169.254','100.100.100.200','168.63.129.16','::ffff:169.254.169.254','0.0.0.0','224.0.0.1','fe80::1','fd00:ec2::254'])
def test_special_targets_denied(secure,config,monkeypatch,address):
    _,sources,_,_=secure
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:[(socket.AF_INET,socket.SOCK_STREAM,6,'',(address,3306))])
    with pytest.raises(SourceError):sources.allowed(config)

def test_allowlist_socket_pinning_and_no_tls_downgrade(secure,config,monkeypatch):
    _,sources,_,_=secure
    resolve=Mock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('10.1.2.3',3306))]);monkeypatch.setattr(socket,'getaddrinfo',resolve)
    with pytest.raises(SourceError):sources.allowed({**config,'host':'not-allowed.example'})
    assert not resolve.called
    import app.services.validation_sources as module
    conn=Mock();sock=Mock();conn._sock=object()
    connect=Mock(return_value=conn);monkeypatch.setattr(module.pymysql,'connect',connect)
    dial=Mock(return_value=sock);monkeypatch.setattr(module.socket,'create_connection',dial)
    engine=sources.build_engine({**config,'tls':'disabled'});assert engine.pool._creator() is conn
    dial.assert_called_once_with(('10.1.2.3',3306),timeout=8)
    assert connect.call_args.kwargs['host']=='db.example' and connect.call_args.kwargs['ssl_disabled']
    conn.connect.assert_called_once_with(sock=sock);engine.dispose()
    engine=sources.build_engine(config)
    with pytest.raises(SourceError):engine.pool._creator()
    assert not connect.call_args.kwargs['ssl_disabled']
    engine.dispose()

def test_dns_timeout_bounded(secure,config,monkeypatch):
    import app.services.validation_sources as module
    _,sources,_,_=secure;release=threading.Event()
    def slow(*a,**kw):release.wait(2);return []
    monkeypatch.setattr(socket,'getaddrinfo',slow);monkeypatch.setattr(module,'DNS_TIMEOUT',.01)
    try:
        with pytest.raises(SourceError,match='超时'):sources.allowed(config)
    finally:release.set()

def test_task_revision_binding_frozen_resume_without_key(setup,config,monkeypatch):
    jobs,settings,engine,calendar=setup
    settings.validation_encryption_key=SecretStr(Fernet.generate_key().decode());settings.validation_allowed_targets=['db.example:3306']
    jobs.sources=ValidationSources(jobs,settings);ref=jobs.sources.save(config)
    job=jobs.create(['oversold'],calendar[170].date(),None,None,source=ref)
    assert jobs.sources.save({**config,'database':'different'},ref['id'],1)['revision']==2
    seen=[]
    @contextmanager
    def source_engine(source):seen.append(jobs.sources.get(source['id'],source['revision'])['database']);yield engine
    monkeypatch.setattr(jobs.sources,'engine',source_engine);jobs.tick();jobs.tick()
    assert seen==['private-db','private-db'] and job['source']==ref
    settings.validation_encryption_key=SecretStr('');jobs.action(job['id'],'pause');jobs.action(job['id'],'retry')
    assert finish(jobs,job['id'])['status']=='completed' and len(seen)==2
    assert config['password'] not in json.dumps(jobs.get(job['id']))

def test_cancel_pending_terminal_restart_and_idempotency(setup,monkeypatch):
    jobs,settings,engine,calendar=setup;job=create(jobs,calendar);jobs.tick();jobs.tick()
    from app.services import short_statistics as stats
    original=stats.stock_samples
    def cancel(*a,**kw):
        first=jobs.action(job['id'],'cancel');assert first['status']=='cancelling' and 'cancelled_at' not in first
        assert jobs.action(job['id'],'cancel')['cancel_requested_at']==first['cancel_requested_at']
        with pytest.raises(ValueError):jobs.action(job['id'],'retry')
        return original(*a,**kw)
    monkeypatch.setattr(stats,'stock_samples',cancel);jobs.tick()
    result=jobs.get(job['id']);assert result['status']=='cancelled' and result['done']==0 and result['cancelled_at']
    again=ValidationJobs(settings,engine);assert not again.tick()
    assert again.action(job['id'],'cancel')==result
    with pytest.raises(ValueError):again.action(job['id'],'retry')

def test_cancel_queued_crash_recovery_preserves_completed(setup):
    jobs,settings,engine,calendar=setup;job=create(jobs,calendar)
    with portalocker.Lock(str(jobs.root/'worker.lock'),timeout=0):assert jobs.action(job['id'],'cancel')['status']=='cancelling'
    restarted=ValidationJobs(settings,engine);assert not restarted.tick()
    assert restarted.get(job['id'])['status']=='cancelled'
    fresh=restarted.create(['oversold'],calendar[170].date(),None,None,request_id='new');result=finish(restarted,fresh['id'])
    assert result['status']=='completed' and restarted.action(fresh['id'],'cancel')==result

def test_tls_required_rejects_server_before_authentication():
    import ssl,pymysql
    conn=pymysql.connect(host='db.example',user='test',password='not-sent',ssl=ssl.create_default_context(),defer_connect=True)
    conn.server_version='8.0.0';conn.server_capabilities=0;conn.write_packet=Mock()
    with pytest.raises(pymysql.err.OperationalError):conn._request_authentication()
    conn.write_packet.assert_not_called()


def test_default_source_is_frozen_and_new_settings_create_new_version(secure):
    jobs,sources,settings,_=secure
    first=jobs.create(['oversold'],__import__('datetime').date(2024,1,1),None,None)
    settings.db_password=SecretStr('changed-ephemeral-password')
    second=jobs.create(['oversold'],__import__('datetime').date(2024,1,1),None,None)
    assert first['source']['id']=='service' and first['source']['revision']==1
    assert second['source']['revision']==2 and second['id']!=first['id']
    assert sources.get('service',1)['password']!='changed-ephemeral-password'
    assert 'changed-ephemeral-password' not in json.dumps(second)

def test_test_connection_success_select_only(secure,setup,config,monkeypatch):
    from sqlalchemy import event
    _,sources,_,_=secure;_,_,engine,_=setup
    statements=[]
    event.listen(engine,'before_cursor_execute',lambda conn,cursor,sql,*args:statements.append(sql))
    monkeypatch.setattr(sources,'build_engine',lambda c:engine)
    assert sources.test(config)['ok']
    assert len(statements)==3 and all(s.startswith('SELECT') for s in statements)


def test_cancel_during_source_query_never_commits_input(setup):
    from sqlalchemy import event
    jobs,_,engine,calendar=setup;job=create(jobs,calendar);jobs.tick()
    def cancel(conn,cursor,sql,*args):
        if 'SELECT * FROM ultra_research_bar' in sql:
            assert jobs.action(job['id'],'cancel')['status']=='cancelling'
    event.listen(engine,'before_cursor_execute',cancel);jobs.tick()
    result=jobs.get(job['id']);assert result['status']=='cancelled' and result['captured']==0
    with jobs.db() as db:assert db.execute('SELECT count(*) FROM stocks').fetchone()[0]==0
