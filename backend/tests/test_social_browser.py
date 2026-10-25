import json
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.services.social_browser import BrowserLogin, allowed_url, authenticated_xueqiu, browser_error, LoginRestricted


@pytest.mark.parametrize('message,code', [
    ("Executable doesn't exist at /private/path", 'browser_missing'),
    ('Failed to move to new namespace: Operation not permitted', 'sandbox_denied'),
    ('error while loading shared libraries: libexample.so', 'system_dependencies_missing'),
    ('Permission denied', 'permission_denied'),
    ('ProcessSingleton profile appears to be in use', 'profile_busy'),
    ('net::ERR_NAME_NOT_RESOLVED https://secret.example/', 'network_failed'),
])
def test_browser_error_is_specific_without_raw_exception(message, code):
    result, reason = browser_error(RuntimeError(message + ' SUB=secret-cookie'))
    assert result == code
    assert 'secret-cookie' not in reason
    assert '/private/path' not in reason


KEY = 'unit-test-management-key-123456789'
BASE = '/api/v1/social-monitor/browser'


@pytest.fixture
def browser_client(engine, tmp_path):
    settings = Settings(_env_file=None, log_dir=tmp_path, cache_enabled=False,
                        social_worker_enabled=False, social_browser_login_key=KEY)
    with TestClient(create_app(settings, engine)) as client:
        yield client, client.app.state.social_browser


def test_browser_endpoints_require_key_but_old_monitor_remains_public(browser_client):
    client, service = browser_client
    assert client.get('/api/v1/social-monitor/status').status_code == 200
    assert client.get('/api/v1/social-monitor/status').json()['browser_login_available']
    for method, suffix, body in [('GET', '/status', None), ('POST', '/start', {}),
                                  ('POST', '/action', {'id': 'a' * 32, 'action': 'cancel'}),
                                  ('GET', '/image/' + 'a' * 32, None)]:
        response = client.request(method, BASE + '/weibo' + suffix, json=body)
        assert response.status_code == 403
        assert client.request(method, BASE + '/weibo' + suffix, json=body,
                              headers={'X-Browser-Login-Key': 'wrong'}).status_code == 403
    response = client.get(BASE + '/weibo/status', headers={'X-Browser-Login-Key': KEY})
    assert response.json()['state'] == 'idle'
    assert response.headers['cache-control'] == 'no-store'
    assert KEY not in response.text


def test_unconfigured_login_is_disabled(client):
    assert client.get(BASE + '/weibo/status').status_code == 503


def test_only_official_origins_allowed():
    assert allowed_url('weibo', 'https://passport.weibo.com/sso/signin')
    assert allowed_url('xueqiu', 'https://open.weixin.qq.com/connect/qrconnect')
    for url in ('http://xueqiu.com/', 'https://xueqiu.com.evil.example/',
                'https://evilxueqiu.com/', 'https://127.0.0.1/', 'file:///etc/passwd',
                'https://xueqiu.com:8000/'):
        assert not allowed_url('xueqiu', url)


def test_xueqiu_visitor_cookies_are_not_login():
    user = {'id': 123, 'screen_name': '测试'}
    cookies = [{'name': 'u', 'value': '123'}, {'name': 'xq_a_token', 'value': 'access'}]
    assert not authenticated_xueqiu(cookies, user)
    cookies.append({'name': 'xq_r_token', 'value': 'refresh'})
    assert not authenticated_xueqiu(cookies, {'id': 456, 'screen_name': '其他博主'})
    assert not authenticated_xueqiu(cookies, None)
    assert authenticated_xueqiu(cookies, user)


def put_state(service, **changes):
    state = dict(id='a' * 32, state='waiting', message='测试', expires_at=time.time() + 60,
                 image=True, revision=1)
    state.update(changes)
    service.save('weibo', state)
    return state


def test_shared_state_encryption_expiry_and_commands(browser_client):
    client, service = browser_client
    state = put_state(service)
    service.write(service.path('weibo', 'image'), b'private-qr-bytes')
    restarted = BrowserLogin(service.settings, service.monitor)
    assert restarted.active('weibo')
    assert restarted.image('weibo', state['id']) == b'private-qr-bytes'
    assert b'private-qr-bytes' not in service.path('weibo', 'image').read_bytes()
    restarted.action('weibo', state['id'], 'click', 100, 100)
    with pytest.raises(ValueError):
        service.action('weibo', state['id'], 'click', 200, 100)
    service.action('weibo', state['id'], 'cancel')
    assert service.command('weibo')['action'] == 'cancel'
    assert service.command('weibo') is None
    with pytest.raises(ValueError):
        service.action('weibo', 'b' * 32, 'reload')
    with pytest.raises(ValueError):
        service.action('weibo', state['id'], 'click', 1100, 100)
    put_state(service, expires_at=time.time() - 1)
    assert not restarted.active('weibo')
    assert restarted.image('weibo', state['id']) is None
    response = client.get(BASE + '/weibo/image/' + state['id'], headers={'X-Browser-Login-Key': KEY})
    assert response.status_code == 410


@contextmanager
def fake_browser(service, body='扫码登录'):
    page = MagicMock()
    page.url = 'https://weibo.com/login.php'
    page.locator.return_value.inner_text.return_value = body
    page.screenshot.return_value = b'private-qr'
    context = MagicMock()
    context.pages = [page]
    context.cookies.return_value = []
    @contextmanager
    def fake_context(*args):
        yield context
    with patch('playwright.sync_api.sync_playwright'), patch.object(service, 'browser_context', fake_context):
        yield context


def test_verified_credentials_saved_encrypted_without_enabling_or_resuming(browser_client):
    _, service = browser_client
    state = put_state(service)
    lease = MagicMock()
    service.monitor.pause_platform('weibo', '测试登录过期')
    with fake_browser(service), patch.object(service, 'verify', return_value='SUB=private-session'):
        service.run('weibo', state, lease)
    assert service.status('weibo')['state'] == 'success'
    assert not service.monitor.overview()['enabled']
    assert service.monitor.overview()['platforms']['weibo']['paused']
    assert service.monitor.overview()['weibo_cookie_configured']
    with service.monitor.db() as db:
        assert service.monitor.decode(service.monitor.config(db)['weibo_cookie']) == 'SUB=private-session'
    assert b'private-session' not in (service.monitor.root / 'monitor.sqlite3').read_bytes()
    assert 'private-session' not in json.dumps(service.status('weibo'))
    assert not service.path('weibo', 'image').exists()
    lease.release.assert_called_once()


def test_cancel_does_not_save_credentials(browser_client):
    _, service = browser_client
    state = put_state(service)
    service.action('weibo', state['id'], 'cancel')
    with fake_browser(service), patch.object(service, 'verify') as verify:
        service.run('weibo', state, MagicMock())
    verify.assert_not_called()
    assert service.status('weibo')['state'] == 'cancelled'
    assert not service.monitor.overview()['weibo_cookie_configured']


def test_platform_block_is_terminal_no_repeated_requests(browser_client):
    _, service = browser_client
    state = put_state(service)
    with fake_browser(service, '请求异常已被安全策略拦截'), patch.object(service, 'verify') as verify:
        service.run('weibo', state, MagicMock())
    verify.assert_not_called()
    assert service.status('weibo')['state'] == 'blocked'
    assert not service.path('weibo', 'image').exists()


def test_login_page_http_denial_explains_status(browser_client):
    _, service = browser_client
    state = put_state(service)
    with fake_browser(service) as context, patch.object(service, 'verify') as verify:
        context.pages[0].goto.return_value.status = 403
        service.run('weibo', state, MagicMock())
    verify.assert_not_called()
    assert service.status('weibo')['state'] == 'blocked'
    assert '403' in service.status('weibo')['message']


def test_browser_errors_do_not_echo_secrets(browser_client):
    _, service = browser_client
    state = put_state(service)
    with patch('playwright.sync_api.sync_playwright'), patch.object(service, 'browser_context', side_effect=RuntimeError('SUB=secret-token https://user:password@host/')):
        service.run('weibo', state, MagicMock())
    result = service.status('weibo')
    assert result['state'] == 'error'
    assert 'secret-token' not in json.dumps(result)
    assert 'password' not in json.dumps(result)


def test_one_window_per_platform_across_workers(browser_client):
    _, service = browser_client
    second = BrowserLogin(service.settings, service.monitor)
    import portalocker
    with portalocker.Lock(str(service.path('weibo', 'lock')), timeout=0):
        with pytest.raises(ValueError, match='已有登录窗口'):
            second.start('weibo')


def test_login_temporarily_defers_monitor_requests(browser_client):
    _, service = browser_client
    service.monitor.add_author('weibo', '123', '测试')
    service.monitor.update_config({'enabled': True, 'token': 'test-token', 'chat_id': '123'})
    put_state(service)
    with patch('app.services.social.night_time', return_value=False), patch('app.services.social.fetch_posts') as fetch:
        service.monitor.cycle()
    fetch.assert_not_called()


def test_weibo_requires_confirmed_mobile_identity(browser_client):
    _, service = browser_client
    context = MagicMock()
    context.cookies.return_value = [{'name': 'SUB', 'value': 'private-cookie'}]
    context.request.get.return_value.status = 200
    context.request.get.return_value.json.return_value = {'data': {'login': False}}
    assert service.verify('weibo', context, MagicMock()) is None
    context.request.get.return_value.json.return_value = {'data': {'login': True, 'uid': '123'}}
    assert service.verify('weibo', context, MagicMock()) == 'SUB=private-cookie'


def test_profile_is_closed_on_cancel_or_exception(browser_client):
    _, service = browser_client
    playwright = MagicMock()
    context = playwright.chromium.launch_persistent_context.return_value
    context.cookies.return_value = []
    with pytest.raises(RuntimeError):
        with service.browser_context(playwright, 'weibo'):
            raise RuntimeError('simulated failure')
    context.close.assert_called_once()


def test_session_cookie_snapshot_restored_with_original_expiry(browser_client):
    _, service = browser_client
    playwright = MagicMock()
    context = playwright.chromium.launch_persistent_context.return_value
    cookies = [{'name': 'SUB', 'value': 'test-session-cookie', 'domain': '.weibo.cn',
                'path': '/', 'expires': -1, 'httpOnly': True, 'secure': True, 'sameSite': 'Lax'}]
    context.cookies.return_value = cookies
    with service.browser_context(playwright, 'weibo'):
        pass
    snapshot = service.profiles / 'weibo' / 'cookies.enc'
    assert b'test-session-cookie' not in snapshot.read_bytes()
    restarted = BrowserLogin(service.settings, service.monitor)
    with restarted.browser_context(playwright, 'weibo'):
        context.add_cookies.assert_called_once_with(cookies)


def test_browser_window_mode_is_configurable(browser_client):
    _, service = browser_client
    service.settings.social_browser_headless = False
    playwright = MagicMock()
    context = playwright.chromium.launch_persistent_context.return_value
    context.cookies.return_value = []
    with service.browser_context(playwright, 'xueqiu'):
        pass
    assert playwright.chromium.launch_persistent_context.call_args.kwargs['headless'] is False


@pytest.mark.parametrize('account_id,expected', [(123, True), (456, False)])
def test_xueqiu_verifies_current_browser_account_identity(browser_client, account_id, expected):
    _, service = browser_client
    context, page = MagicMock(), MagicMock()
    context.cookies.return_value = [
        {'name': 'xq_a_token', 'value': 'private-access'},
        {'name': 'xq_r_token', 'value': 'private-refresh'},
        {'name': 'u', 'value': '123'},
    ]
    page.url = 'https://www.xueqiu.com/'
    page.evaluate.return_value = {'status': 200, 'user': {'id': account_id, 'screen_name': '测试'}}
    assert bool(service.verify('xueqiu', context, page)) is expected
    context.cookies.assert_called_with(['https://www.xueqiu.com/'])
    context.request.get.assert_not_called()


def test_xueqiu_identity_restriction_stops_instead_of_retrying(browser_client):
    _, service = browser_client
    context, page = MagicMock(), MagicMock()
    context.cookies.return_value = [{'name': 'xq_a_token', 'value': 'access'},
                                    {'name': 'xq_r_token', 'value': 'refresh'}, {'name': 'u', 'value': '123'}]
    page.url = 'https://www.xueqiu.com/'
    page.evaluate.return_value = {'status': 567, 'user': None}
    with pytest.raises(LoginRestricted):
        service.verify('xueqiu', context, page)


def test_missing_display_is_actionable():
    code, message = browser_error(RuntimeError('Missing X server or $DISPLAY'))
    assert code == 'display_unavailable' and 'Xvfb' in message
