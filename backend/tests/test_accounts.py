import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.services.accounts import Accounts, AccountError, COOKIE, digest, IDLE_SECONDS, SESSION_SECONDS
from account_helpers import PASSWORD, sign_in


@pytest.fixture
def site(engine, tmp_path):
    settings = Settings(_env_file=None, log_dir=tmp_path, cache_enabled=False,
                        social_worker_enabled=False, margin_worker_enabled=False)
    app = create_app(settings, engine)
    accounts = app.state.accounts
    admin = accounts.create('admin', PASSWORD, bootstrap=True)
    viewer = accounts.create('reader', PASSWORD, actor=admin['id'])
    with TestClient(app, base_url='https://site.example') as client:
        yield accounts, client, admin, viewer


@pytest.mark.parametrize('path', ['/api/v1/modules', '/api/v1/cache/status',
    '/api/v1/short-term/latest', '/api/v1/social-monitor/status',
    '/api/v1/short-term/validation/jobs', '/api/v1/social-monitor/browser/weibo/status'])
def test_anonymous_and_old_keys_cannot_access_any_data(site, path):
    _, client, _, _ = site
    assert client.get(path).status_code == 401
    assert client.get(path, headers={'Authorization': 'Bearer old-admin-key',
        'X-Browser-Login-Key': 'old-browser-key'}).status_code == 401
    assert client.get('/api/v1/health').status_code == 200
    assert client.get('/api/v1/short-term/validation/access').status_code == 404
    assert client.post('/api/v1/short-term/validation/pair', json={'code': 'old'}).status_code == 404


def test_hashes_salts_sessions_and_restart(site):
    accounts, client, admin, viewer = site
    response = sign_in(client)
    cookie = response.headers['set-cookie'].lower()
    assert all(flag in cookie for flag in ('httponly', 'secure', 'samesite=strict'))
    token = client.cookies.get(COOKIE)
    with accounts.db() as db:
        hashes = [r[0] for r in db.execute('SELECT password_hash FROM users')]
        assert hashes[0] != hashes[1]
        assert all(h.startswith('$argon2id$v=19$m=65536,t=3,p=1$') for h in hashes)
        assert db.execute('SELECT token_hash FROM sessions').fetchone()[0] == digest(token)
    assert PASSWORD.encode() not in accounts.path.read_bytes()
    assert token.encode() not in accounts.path.read_bytes()
    assert Accounts(accounts.settings).session(token)['user']['id'] == admin['id']
    assert response.headers['cache-control'] == 'no-store'
    assert 'password' not in client.get('/api/auth/users').text


def test_viewer_reads_but_cannot_operate_or_see_admin_secrets(site):
    _, client, _, _ = site
    sign_in(client, 'reader')
    assert client.get('/api/v1/social-monitor/status').status_code == 200
    for path in ['/api/auth/users', '/api/v1/short-term/validation/sources',
                 '/api/v1/short-term/validation/jobs', '/api/v1/social-monitor/browser/weibo/status']:
        assert client.get(path).status_code == 403
    for path in ['/api/auth/users', '/api/v1/cache/refresh', '/api/v1/social-monitor/config',
                 '/api/v1/short-term/validation/jobs']:
        assert client.post(path, json={}).status_code == 403


def test_http_fails_closed_and_forwarded_headers_do_not_bypass(site):
    accounts, client, _, _ = site
    url = 'http://site.example/api/auth/login'
    for extra in [{}, {'X-Forwarded-Proto': 'https'}, {'Forwarded': 'proto=https'}]:
        response = client.post(url, headers={'Origin': 'http://site.example', 'X-App-Client': 'browser', **extra},
                               json={'username': 'admin', 'password': PASSWORD})
        assert response.status_code == 403
        assert not response.cookies
    assert client.get('http://site.example/api/auth/session').json()['https_required']
    accounts.settings.auth_allow_http = True  # Explicit local-development escape hatch only.
    with TestClient(client.app, base_url='http://site.example') as local:
        assert sign_in(local).status_code == 200


def test_login_csrf_and_mutation_csrf_origin(site):
    _, client, _, _ = site
    for origin in ('', 'null', 'https://evil.example'):
        assert client.post('/api/auth/login', headers={'Origin': origin, 'X-App-Client': 'browser'},
                           json={'username': 'admin', 'password': PASSWORD}).status_code == 403
    sign_in(client)
    for headers in ({'X-CSRF-Token': ''}, {'Origin': 'https://evil.example'},
                    {'Sec-Fetch-Site': 'cross-site'}, {'X-App-Client': ''}):
        assert client.post('/api/auth/logout', headers=headers).status_code == 403
        assert client.post('/api/v1/social-monitor/config', json={'enabled': False}, headers=headers).status_code == 403
    assert client.post('/api/auth/logout').status_code == 200
    assert client.get('/api/v1/cache/status').status_code == 401


def test_disable_reset_and_role_change_revoke_all_sessions(site):
    accounts, client, admin, viewer = site
    for update in ({'enabled': False}, {'enabled': True}, {'role': 'admin'}, {'password': PASSWORD + '-new'}):
        token = accounts.login('reader', PASSWORD, 'test') if update != {'enabled': True} else None
        sign_in(client)
        response = client.post('/api/auth/users/' + viewer['id'], json=update)
        assert response.status_code == 200, response.text
        if token:
            with pytest.raises(AccountError): accounts.session(token)
    assert accounts.login('reader', PASSWORD + '-new', 'test')
    assert client.post('/api/auth/users/' + admin['id'], json={'enabled': False}).status_code == 200
    assert client.get('/api/auth/users').status_code == 401


def test_last_admin_and_bootstrap_are_guarded_even_under_concurrency(site):
    accounts, _, admin, _ = site
    with pytest.raises(AccountError): accounts.create('hacker', PASSWORD, bootstrap=True)
    with pytest.raises(AccountError): accounts.create('hacker', PASSWORD)
    other = accounts.create('second', PASSWORD, 'admin', admin['id'])
    def disable(ident):
        try: accounts.update(ident, ident, enabled=False); return True
        except AccountError: return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(disable, [admin['id'], other['id']])) == 1
    assert sum(u['enabled'] and u['role'] == 'admin' for u in accounts.list()) == 1


def test_password_change_logout_expiry_and_console_recovery(site, monkeypatch):
    accounts, client, _, _ = site
    sign_in(client, 'reader')
    token = client.cookies.get(COOKIE)
    assert client.post('/api/auth/password', json={'old_password': 'wrong', 'new_password': PASSWORD + '-new'}).status_code == 400
    assert client.post('/api/auth/password', json={'old_password': PASSWORD, 'new_password': PASSWORD + '-new'}).status_code == 200
    with pytest.raises(AccountError): accounts.session(token)
    accounts.reset_from_console('reader', PASSWORD)
    token = accounts.login('reader', PASSWORD, 'local')
    now = time.time()
    monkeypatch.setattr('app.services.accounts.time.time', lambda: now + IDLE_SECONDS + 1)
    with pytest.raises(AccountError): accounts.session(token)
    monkeypatch.setattr('app.services.accounts.time.time', lambda: now + SESSION_SECONDS + 1)
    with pytest.raises(AccountError): accounts.session(token)


def test_login_limits_survive_restarts_and_do_not_expose_user_existence(site):
    accounts, client, _, _ = site
    for name in ('admin', 'missing'):
        for _ in range(10):
            with pytest.raises(AccountError, match='用户名或密码错误'): accounts.login(name, 'wrong', 'local')
        with pytest.raises(AccountError) as error: Accounts(accounts.settings).login(name, PASSWORD, 'local')
        assert error.value.status == 429
    assert client.post('/api/auth/login', headers={'Origin': 'https://site.example', 'X-App-Client': 'browser'},
                       json={'username': 'admin', 'password': {'secret': PASSWORD}}).status_code == 422
    assert PASSWORD not in client.post('/api/auth/login', json={'password': PASSWORD}).text


def test_admin_user_management_and_existing_password_does_not_echo(site):
    _, client, _, _ = site
    sign_in(client)
    response = client.post('/api/auth/users', json={'username': 'new-user', 'password': PASSWORD})
    assert response.status_code == 200
    assert response.json()['role'] == 'viewer' and PASSWORD not in response.text
    assert client.post('/api/auth/users', json={'username': 'new-user', 'password': PASSWORD}).status_code == 409
    assert client.post('/api/auth/users', json={'username': 'short', 'password': 'short'}).status_code == 400
    assert client.post('/api/auth/register', json={'username': 'public', 'password': PASSWORD}).status_code == 404
