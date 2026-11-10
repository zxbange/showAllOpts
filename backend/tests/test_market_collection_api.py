from types import SimpleNamespace

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import pytest

from account_helpers import install_accounts, sign_in, PASSWORD
from app.api.routes.accounts import require_user
from app.api.routes.market_collection import router
from app.core.config import Settings


@pytest.fixture
def api(tmp_path):
    app = FastAPI()
    settings = Settings(_env_file=None, log_dir=tmp_path, auth_allow_http=True)
    accounts = install_accounts(app, settings)
    calls = []
    configuration = dict(revision=0, username='', password_set=False, enabled=True,
                         host='127.0.0.1', port=3306, database='showallopts', source='none', schedule=['18:00'])
    app.state.market_collection = SimpleNamespace(
        configuration=SimpleNamespace(public=lambda: configuration),
        status=lambda: dict(status='idle', configured=False),
        configure=lambda body: calls.append(('save', body)) or configuration | {'revision': 1, 'password_set': True},
        test_configuration=lambda body: calls.append(('test', body)) or {'ok': True, 'permissions_verified': False},
        request_run=lambda request_id: calls.append(('run', request_id)) or {'id': request_id, 'status': 'queued'},
    )
    app.include_router(router, prefix='/api/v1', dependencies=[Depends(require_user)])
    return app, accounts, calls


def body(**changes):
    return dict(revision=0, username='writer', password='private-payload-must-not-echo', enabled=True) | changes


def test_https_admin_configuration_and_run_contract(api):
    app, accounts, calls = api
    with TestClient(app, base_url='https://testserver') as client:
        response = client.get('/api/v1/market-collection/configuration')
        assert response.status_code == 200 and response.json()['transport_secure']
        assert 'password' not in response.json()
        response = client.post('/api/v1/market-collection/configuration', json=body())
        assert response.status_code == 200 and 'private-payload' not in response.text
        assert calls[-1][1]['password'] == 'private-payload-must-not-echo'
        response = client.post('/api/v1/market-collection/run', json={'request_id': 'request-market-0001'})
        assert response.status_code == 202 and response.json()['status'] == 'queued'
        assert client.post('/api/v1/market-collection/test', json=body()).json()['permissions_verified'] is False


def test_http_configuration_is_rejected_without_mutation_or_echo(api):
    app, accounts, calls = api
    with TestClient(app, base_url='http://testserver') as client:
        response = client.post('/api/v1/market-collection/configuration', json=body())
        assert response.status_code == 403 and 'HTTPS' in response.text
        assert not calls and 'private-payload' not in response.text


@pytest.mark.parametrize('invalid', [body(revision=-1), body(enabled='true'), body(extra_secret='never-echo'), body(password='x' * 9000)])
def test_validation_does_not_return_secret_request_values(api, invalid):
    app, accounts, calls = api
    with TestClient(app, base_url='https://testserver') as client:
        response = client.post('/api/v1/market-collection/configuration', json=invalid)
        assert response.status_code == 422
        assert 'private-payload' not in response.text and 'never-echo' not in response.text
        assert not calls


def test_viewer_reads_status_but_cannot_configure_or_run(api):
    app, accounts, calls = api
    with TestClient(app, base_url='https://testserver') as client:
        actor = client.get('/api/auth/session').json()['user']['id']
        accounts.create('viewer', PASSWORD, actor=actor)
        sign_in(client, 'viewer')
        assert client.get('/api/v1/market-collection/status').status_code == 200
        assert client.get('/api/v1/market-collection/configuration').status_code == 403
        assert client.post('/api/v1/market-collection/run', json={'request_id': 'request-market-0001'}).status_code == 403
        assert not calls


def test_authentication_and_csrf_are_required(api):
    app, accounts, calls = api
    with TestClient(app, base_url='https://testserver') as client:
        del client.headers['X-CSRF-Token']
        assert client.post('/api/v1/market-collection/run', json={'request_id': 'request-market-0001'}).status_code == 403
        client.cookies.clear()
        assert client.get('/api/v1/market-collection/status').status_code == 401
        assert not calls
