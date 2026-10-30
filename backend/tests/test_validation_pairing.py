from concurrent.futures import ThreadPoolExecutor
import json
import socket
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
import pytest
from sqlalchemy import create_engine
from app.api.routes.short_term import router
from app.core.config import Settings
from app.services.short_validation import ValidationJobs
from app.services.validation_sources import ValidationSources
from app.services.validation_access import ValidationAccess, AccessError, COOKIE, digest, PAIR_TTL, SESSION_TTL
from test_validation_sources import config

BASE = '/api/v1/short-term/validation'

@pytest.fixture
def paired(tmp_path):
    settings = Settings(_env_file=None, log_dir=tmp_path, validation_admin_key='', validation_encryption_key='', validation_allowed_targets=[])
    jobs = ValidationJobs(settings, create_engine('sqlite://'))
    jobs.sources = ValidationSources(jobs, settings)
    app = FastAPI(); app.state.short_validation = jobs
    app.include_router(router, prefix='/api/v1')
    with TestClient(app, base_url='http://site.example') as client:
        yield jobs, settings, ValidationAccess(settings), client


def login(access, client, confirmed=True):
    code = access.issue('http://site.example')
    client.headers.update({'Origin':'http://site.example', 'X-Validation-Client':'browser'})
    response = client.post(BASE+'/pair', json={'code':code,'http_confirmed':confirmed})
    if response.status_code == 200:
        client.headers['X-Validation-CSRF'] = response.json()['csrf_token']
    return code, response


def test_no_config_pair_save_start_logout(paired, config, monkeypatch):
    jobs, settings, access, client = paired
    assert client.get(BASE+'/access').json()['mode'] == 'pair'
    assert not access.root.exists()
    assert client.get(BASE+'/sources').status_code in (401,403)
    code, response = login(access,client)
    assert response.status_code == 200
    assert 'httponly' in response.headers['set-cookie'].lower()
    assert 'samesite=strict' in response.headers['set-cookie'].lower()
    assert code not in (access.root/'access.sqlite3').read_bytes().decode(errors='ignore')
    assert client.get(BASE+'/access').json()['authenticated'] is True
    ref = client.post(BASE+'/sources/save',json=config)
    assert ref.status_code == 200,ref.text
    assert config['password'] not in ref.text
    assert config['password'].encode() not in (jobs.root/'jobs.sqlite3').read_bytes()
    assert (settings.log_path/'short-validation-secrets/source.key').exists()
    calls=[]
    def create(*args):
        calls.append(args)
        return {'id':'job1','status':'queued','source':args[-1]}
    monkeypatch.setattr(jobs,'create',create)
    body={'methods':['bukou','xuli'],'start':'2024-01-01','source':{'id':ref.json()['id'],'revision':1}}
    assert client.post(BASE+'/jobs',json=body).status_code == 200
    assert calls[0][0] == ['bukou','xuli']
    assert calls[0][-1] == body['source']
    assert client.post(BASE+'/jobs',json={'methods':['bukou']}).status_code == 422
    assert client.post(BASE+'/logout',json={}).status_code == 200
    assert client.get(BASE+'/jobs').status_code == 401


def test_http_requires_confirmation_and_legacy_cannot_be_bypassed(paired):
    _, settings, access, client = paired
    code, response = login(access,client,False)
    assert response.status_code == 403
    response = client.post(BASE+'/pair',json={'code':code,'http_confirmed':True})
    assert response.status_code == 200
    settings.validation_admin_key = SecretStr('old-key'*6)
    with pytest.raises(AccessError,match='旧管理'):
        access.issue('http://site.example')
    assert client.post(BASE+'/pair',json={'code':code,'http_confirmed':True}).status_code == 409
    assert client.get(BASE+'/jobs').status_code == 403
    assert client.get(BASE+'/access').json()['mode'] == 'legacy'


def test_replay_limits_expiration_and_concurrent_consumption(paired, monkeypatch):
    _, _, access, _ = paired
    code = access.issue('http://site.example')
    def consume(_):
        try:return access.pair(code,'http://site.example',True)
        except AccessError:return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens=list(pool.map(consume,range(8)))
    assert sum(token is not None for token in tokens) == 1
    code=access.issue('http://site.example')
    for _ in range(10):
        with pytest.raises(AccessError):access.pair('wrong','http://site.example',True)
    with pytest.raises(AccessError):access.pair(code,'http://site.example',True)
    code=access.issue('http://site.example')
    import time
    current=time.time()
    monkeypatch.setattr('app.services.validation_access.time.time',lambda:current+PAIR_TTL+1)
    with pytest.raises(AccessError):access.pair(code,'http://site.example',True)


def test_origin_csrf_and_session_restart(paired):
    _, settings, access, client=paired
    code, response=login(access,client)
    csrf=response.json()['csrf_token']
    token=client.cookies.get(COOKIE)
    assert ValidationAccess(settings).session(token,'http://site.example')['csrf_token']==csrf
    for headers in ({'Origin':'http://evil.example'}, {'Origin':'null'}, {'Sec-Fetch-Site':'same-site'}, {'Sec-Fetch-Site':'cross-site'}):
        assert client.get(BASE+'/access',headers=headers).status_code == 403
        assert client.post(BASE+'/logout',json={},headers=headers).status_code == 403
    assert client.post(BASE+'/logout',json={},headers={'X-Validation-CSRF':'wrong'}).status_code==403
    client.headers.pop('Origin')
    assert client.post(BASE+'/logout',json={}).status_code==403
    assert client.get(BASE+'/jobs').status_code==200
    client.headers.pop('X-Validation-Client')
    assert client.get(BASE+'/jobs').status_code==403
    assert ValidationAccess(settings).session(token,'http://site.example')
    with pytest.raises(AccessError):access.session(token,'https://site.example')
    access.issue('http://site.example',revoke=True)
    with pytest.raises(AccessError):access.session(token,'http://site.example')


def test_cookie_expiry_and_secure_https(paired,monkeypatch):
    _, _, access, client=paired
    _, response=login(access,client)
    import time
    current=time.time()
    monkeypatch.setattr('app.services.validation_access.time.time',lambda:current+SESSION_TTL+1)
    assert client.get(BASE+'/jobs').status_code==401
    monkeypatch.undo()
    code=access.issue('https://site.example')
    response=client.post('https://site.example'+BASE+'/pair',headers={'Origin':'https://site.example'},json={'code':code})
    assert response.status_code==200
    assert 'secure' in response.headers['set-cookie'].lower()


def test_bound_origin_and_invalid_payload_never_echo(paired):
    _, _, access, client=paired
    code=access.issue('https://site.example')
    client.headers.update({'Origin':'http://site.example','X-Validation-Client':'browser'})
    assert client.post(BASE+'/pair',json={'code':code,'http_confirmed':True}).status_code==401
    result=client.post(BASE+'/pair',json={'code':{'secret':code}})
    assert result.status_code==422 and code not in result.text
    assert client.post(BASE+'/pair',json={'code':code,'http_confirmed':True},headers={'X-Forwarded-Proto':'https'}).status_code==401


def test_authenticated_targets_and_probe_limits(paired,config,monkeypatch):
    jobs, settings, access, client=paired
    _, response=login(access,client)
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('169.254.169.254',3306))])
    assert client.post(BASE+'/sources/test',json=config).status_code==409
    monkeypatch.setattr(jobs.sources,'test',lambda config:{'ok':True,'message':'ok'})
    for _ in range(4):assert client.post(BASE+'/sources/test',json=config).status_code==200
    assert client.post(BASE+'/sources/test',json=config).status_code==429
    settings.validation_allowed_targets=['operator-db.example:3306']
    assert client.post(BASE+'/sources/save',json=config).status_code==409


def test_all_job_and_source_operations_require_access(paired,config):
    _, _, _, client=paired
    for path,body in [('/jobs',{'methods':['bukou']}),('/jobs/abc/pause',{}),('/jobs/abc/retry',{}),('/jobs/abc/cancel',{}),('/sources/save',config),('/sources/test',config)]:
        assert client.post(BASE+path,json=body).status_code in (401,403)
    for path in ['/jobs','/sources']:
        assert client.get(BASE+path).status_code in (401,403)


def test_proxy_stripped_port_requires_console_authorized_exact_origin(paired):
    _, _, access, client = paired
    origin = 'http://site.example:8081'
    code = access.issue(origin)
    client.headers.update({'Origin':origin, 'X-Validation-Origin':origin, 'X-Validation-Client':'browser'})
    # ASGI sees Host without external port, as in the existing trusted Nginx proxy.
    response = client.post(BASE+'/pair',json={'code':code,'http_confirmed':True})
    assert response.status_code == 200
    client.headers['X-Validation-CSRF'] = response.json()['csrf_token']
    client.headers.pop('Origin')
    assert client.get(BASE+'/access').json()['authenticated'] is True
    assert client.get(BASE+'/jobs').status_code == 200
    assert client.get(BASE+'/jobs',headers={'X-Validation-Origin':'http://site.example:9999'}).status_code == 401
    assert client.get(BASE+'/jobs',headers={'X-Validation-Origin':'http://evil.example:8081'}).status_code == 403
    assert client.post(BASE+'/logout',json={},headers={'Origin':'http://site.example:9999'}).status_code == 403
    assert client.get(BASE+'/jobs',headers={'X-Validation-Origin':'https://site.example:8081','X-Forwarded-Proto':'https'}).status_code == 403
    assert client.post(BASE+'/logout',json={},headers={'Origin':origin}).status_code == 200


def pair_in_process(arguments):
    directory, code = arguments
    settings = Settings(_env_file=None,log_dir=directory,validation_admin_key='')
    try:
        return ValidationAccess(settings).pair(code,'http://site.example',True)
    except AccessError:
        return None


def test_pair_consumption_across_processes(paired):
    from concurrent.futures import ProcessPoolExecutor
    _, settings, access, _ = paired
    first = access.issue('http://site.example')
    code = access.issue('http://site.example')
    with pytest.raises(AccessError):access.pair(first,'http://site.example',True)
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(pair_in_process, [(str(settings.log_path),code)]*8))
    assert sum(item is not None for item in results) == 1
    with access.db() as db:
        row = db.execute('SELECT hash FROM sessions').fetchone()
    assert row['hash'] == digest(next(item for item in results if item is not None))


def test_docker_packages_console_entry():
    from pathlib import Path
    dockerfile = Path(__file__).resolve().parents[1]/'Dockerfile'
    assert 'COPY run.py docker_entrypoint.py validation_pair.py ./' in dockerfile.read_text()
