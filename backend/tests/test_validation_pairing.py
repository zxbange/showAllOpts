"""Validation regression using unified accounts, replacing retired one-time pairing."""
from pathlib import Path
import socket
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from app.api.routes.short_term import router
from app.core.config import Settings
from app.services.short_validation import ValidationJobs
from app.services.validation_sources import ValidationSources
from app.services.validation_keys import POSIX_KEY_STORAGE
from account_helpers import install_accounts, sign_in
from test_validation_sources import config

BASE = '/api/v1/short-term/validation'

@pytest.fixture
def paired(tmp_path):
    # Name retained for deletion regression imports; authentication uses real accounts.
    if not POSIX_KEY_STORAGE:
        pytest.skip('Automatic database encryption key storage requires POSIX')
    settings = Settings(_env_file=None, log_dir=tmp_path, validation_encryption_key='', validation_allowed_targets=[])
    jobs = ValidationJobs(settings, create_engine('sqlite://'))
    jobs.sources = ValidationSources(jobs, settings)
    app = FastAPI(); app.state.short_validation = jobs
    app.include_router(router, prefix='/api/v1')
    accounts = install_accounts(app, settings)
    with TestClient(app, base_url='https://site.example') as client:
        yield jobs, settings, accounts, client

def login(access, client):
    return '', sign_in(client)

@pytest.mark.parametrize('horizons', [None, [7, 120]])
def test_no_config_pair_save_start_logout(paired, config, monkeypatch, horizons):
    jobs, settings, access, client = paired
    assert client.get(BASE+'/sources').status_code in (401,403)
    code, response = login(access,client)
    assert response.status_code == 200
    assert 'httponly' in response.headers['set-cookie'].lower()
    assert 'samesite=strict' in response.headers['set-cookie'].lower()
    ref = client.post(BASE+'/sources/save',json=config)
    assert ref.status_code == 200,ref.text
    assert config['password'] not in ref.text
    assert config['password'].encode() not in (jobs.root/'jobs.sqlite3').read_bytes()
    assert (settings.log_path/'short-validation-secrets/source.key').exists()
    calls=[]
    def create(*args, horizons):
        calls.append((args, horizons))
        return {'id':'job1','status':'queued','source':args[-1], 'horizons':horizons}
    monkeypatch.setattr(jobs,'create',create)
    body={'methods':['bukou','oversold'],'start':'2024-01-01','source':{'id':ref.json()['id'],'revision':1}}
    if horizons is not None:
        body['horizons'] = horizons
    assert client.post(BASE+'/jobs',json=body).status_code == 200
    assert calls[0][0][0] == ['bukou','oversold']
    assert calls[0][0][-1] == body['source']
    assert calls[0][1] == (horizons if horizons is not None else [1, 2, 3])
    assert client.post('/api/auth/logout',json={}).status_code == 200
    assert client.get(BASE+'/jobs').status_code == 401



def test_all_job_and_source_operations_require_accounts(paired, config):
    _, _, _, client = paired
    for path, body in [('/jobs', {'methods':['bukou']}), ('/jobs/abc/pause', {}),
                       ('/jobs/abc/retry', {}), ('/jobs/abc/cancel', {}), ('/sources/save', config), ('/sources/test', config)]:
        assert client.post(BASE+path, json=body).status_code == 401
    for path in ['/jobs', '/sources']:
        assert client.get(BASE+path).status_code == 401
    for path in ['/access', '/pair', '/logout']:
        assert client.post(BASE+path, json={}).status_code == 404


def test_docker_packages_account_console_entry():
    dockerfile = Path(__file__).resolve().parents[1]/'Dockerfile'
    assert 'COPY run.py docker_entrypoint.py manage_users.py ./' in dockerfile.read_text()


def test_source_probe_limits_and_target_restrictions_use_account(paired, config, monkeypatch):
    jobs, settings, _, client = paired
    sign_in(client)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('169.254.169.254', 3306))])
    assert client.post(BASE+'/sources/test', json=config).status_code == 409
    monkeypatch.setattr(jobs.sources, 'test', lambda config: {'ok': True, 'message': 'ok'})
    for _ in range(3):
        assert client.post(BASE+'/sources/test', json=config).status_code == 200
    assert client.post(BASE+'/sources/test', json=config).status_code == 429
    settings.validation_allowed_targets = ['operator-db.example:3306']
    assert client.post(BASE+'/sources/save', json=config).status_code == 409
