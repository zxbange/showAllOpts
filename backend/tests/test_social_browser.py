import json
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from account_helpers import PASSWORD, sign_in
from app.services.social_browser import (BrowserLogin, allowed_url, authenticated_xueqiu,
                                         browser_error, LoginRestricted, MAX_PAGE_TRANSITIONS)


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
    with TestClient(create_app(settings, engine), base_url='https://testserver') as client:
        client.app.state.accounts.create('admin', PASSWORD, bootstrap=True)
        sign_in(client)
        yield client, client.app.state.social_browser


def test_browser_endpoints_require_account_and_reject_old_key(browser_client):
    client, service = browser_client
    assert client.get(BASE + '/weibo/status').json()['state'] == 'idle'
    assert client.get('/api/v1/social-monitor/status').json()['browser_login_available']
    client.cookies.clear()
    for method, suffix, body in [('GET', '/status', None), ('POST', '/start', {}), ('POST', '/logout', {}),
                                  ('POST', '/action', {'id': 'a' * 32, 'action': 'cancel'}),
                                  ('GET', '/image/' + 'a' * 32, None)]:
        response = client.request(method, BASE + '/weibo' + suffix, json=body,
                                  headers={'X-Browser-Login-Key': KEY})
        assert response.status_code == 401
    assert client.get('/api/v1/social-monitor/status').status_code == 401


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


def put_state(service, platform='weibo', **changes):
    state = dict(id='a' * 32, state='waiting', message='测试', expires_at=time.time() + 60,
                 image=True, revision=1)
    state.update(changes)
    service.save(platform, state)
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
    result = service.action('weibo', state['id'], 'cancel')
    assert result['state'] == 'cancelling' and not result['image']
    assert service.image('weibo', state['id']) is None
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
    page.is_closed.return_value = False
    page.url = 'https://weibo.com/login.php'
    page.goto.return_value.status = 200
    page.locator.return_value.inner_text.return_value = body
    page.screenshot.return_value = b'private-qr'
    context = MagicMock()
    context.browser.is_connected.return_value = True
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
    service.monitor.update_config({'weibo_cookie': 'SUB=test-session-cookie'}, verified_platform='weibo')
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


def test_normal_login_cannot_start_another_window_and_expiry_overrides_old_success(browser_client):
    _, service = browser_client
    service.monitor.update_config({'weibo_cookie': 'SUB=private'}, verified_platform='weibo')
    put_state(service, state='success', image=False)
    with patch('app.services.social_browser.threading.Thread') as thread:
        assert service.start('weibo')['session']['state'] == 'normal'
        thread.assert_not_called()
    with service.monitor.db() as db:
        cookie = service.monitor.config(db)['weibo_cookie']
    service.monitor.record_login_result('weibo', cookie, 'expired')
    assert service.status('weibo')['session']['state'] == 'expired'
    assert BrowserLogin(service.settings, service.monitor).status('weibo')['session']['state'] == 'expired'


def test_duplicate_start_returns_existing_job(browser_client):
    _, service = browser_client
    put_state(service)
    with patch('app.services.social_browser.threading.Thread') as thread:
        assert service.start('weibo')['id'] == 'a' * 32
        thread.assert_not_called()


def test_logout_clears_only_target_credentials_and_old_fetch_cannot_revive(browser_client):
    client, service = browser_client
    monitor = service.monitor
    monitor.update_config({'weibo_cookie': 'SUB=secret', 'xueqiu_cookie': 'other'}, verified_platform='weibo')
    with monitor.db() as db:
        old = monitor.config(db)['weibo_cookie']
    profile = service.profiles / 'weibo'
    profile.mkdir()
    (profile / 'cookies.enc').write_bytes(b'encrypted')
    (profile / 'old-browser-storage').write_bytes(b'old')
    put_state(service, state='success')
    response = client.post(BASE + '/weibo/logout', json={})
    assert response.status_code == 200
    assert response.json()['session']['state'] == 'logged_out'
    assert not profile.exists()
    assert not monitor.overview()['weibo_cookie_configured']
    assert monitor.overview()['xueqiu_cookie_configured']
    assert monitor.overview()['platforms']['weibo']['paused']
    assert monitor.record_login_result('weibo', old, 'normal') is False
    assert service.status('weibo')['session']['state'] == 'logged_out'
    assert client.post(BASE + '/weibo/logout', json={}).status_code == 200


def test_verified_scan_resumes_auth_pause_but_not_platform_restrictions(browser_client):
    _, service = browser_client
    service.monitor.pause_platform('weibo', '登录过期', 'login_expired')
    service.monitor.update_config({'weibo_cookie': 'SUB=new'}, verified_platform='weibo')
    assert not service.monitor.overview()['platforms']['weibo']['paused']
    service.monitor.pause_platform('weibo', '平台验证', 'captcha')
    service.monitor.update_config({'weibo_cookie': 'SUB=newer'}, verified_platform='weibo')
    assert service.monitor.overview()['platforms']['weibo']['paused']


def test_visitor_fetch_is_not_logged_in_and_changed_cookie_needs_check(browser_client):
    _, service = browser_client
    monitor = service.monitor
    monitor.record_login_result('weibo', '', 'normal')
    assert monitor.platform_login('weibo')['state'] == 'logged_out'
    monitor.update_config({'weibo_cookie': 'SUB=one'}, verified_platform='weibo')
    monitor.update_config({'weibo_cookie': 'SUB=two'})
    assert monitor.platform_login('weibo')['state'] == 'unknown'


def test_browser_always_uses_fresh_profile_without_restored_tabs(browser_client):
    _, service = browser_client
    playwright = MagicMock()
    context = playwright.chromium.launch_persistent_context.return_value
    context.cookies.return_value = []
    with service.browser_context(playwright, 'weibo'):
        first = playwright.chromium.launch_persistent_context.call_args.args[0]
    with service.browser_context(playwright, 'weibo'):
        second = playwright.chromium.launch_persistent_context.call_args.args[0]
    assert first != second and first != str(service.profiles / 'weibo')
    from pathlib import Path
    assert not Path(first).exists() and not Path(second).exists()


def test_logged_out_platform_does_not_restore_saved_cookies(browser_client):
    _, service = browser_client
    profile = service.profiles / 'weibo'
    profile.mkdir()
    service.write(profile / 'cookies.enc', b'[]')
    playwright = MagicMock()
    with service.browser_context(playwright, 'weibo') as context:
        context.add_cookies.assert_not_called()


@pytest.mark.parametrize('outcome,expected', [('success', 'normal'), ('network', 'normal'), ('expired', 'expired')])
def test_monitor_cycle_updates_login_without_treating_network_as_expiry(browser_client, outcome, expected):
    import httpx
    from app.services.social_sources import PlatformIntervention
    _, browser = browser_client
    monitor = browser.monitor
    monitor.add_author('weibo', '123', '测试')
    monitor.update_config({'weibo_cookie': 'SUB=test', 'telegram_enabled': False, 'enabled': True},
                          verified_platform='weibo' if outcome != 'success' else None)
    author = monitor.overview()['authors'][0]
    error = (httpx.ReadTimeout('network') if outcome == 'network' else
             PlatformIntervention('过期', 'login_expired') if outcome == 'expired' else None)
    with patch('app.services.social.night_time', return_value=False), \
            patch.object(monitor, 'scheduled_author', side_effect=[(author, 'queue'), None]), \
            patch.object(monitor, 'check_author', side_effect=error), \
            patch.object(monitor, 'deliver_notifications'), patch.object(monitor, 'send_alerts'):
        monitor.cycle()
    assert monitor.platform_login('weibo')['state'] == expected


@pytest.mark.parametrize('platform', ['weibo', 'xueqiu'])
def test_navigation_during_body_read_recovers_without_reloading(browser_client, platform):
    from playwright.sync_api import Error
    _, service = browser_client
    state = put_state(service, platform=platform)
    with fake_browser(service) as context, patch.object(service, 'verify', return_value='SUB=verified'), \
            patch.object(service.stopping, 'wait'):
        page = context.pages[0]
        page.locator.return_value.inner_text.side_effect = [
            Error('Execution context was destroyed, most likely because of a navigation'), '扫码登录',
        ]
        service.run(platform, state, MagicMock())
    assert service.status(platform)['state'] == 'success'
    assert page.locator.return_value.inner_text.call_count == 2
    page.goto.assert_called_once()


def test_closed_popup_switches_to_live_page_without_replaying_click(browser_client):
    from playwright.sync_api import Error
    _, service = browser_client
    state = put_state(service)
    service.action('weibo', state['id'], 'click', 100, 100)
    with fake_browser(service) as context, patch.object(service, 'verify', return_value='SUB=verified'), \
            patch.object(service.stopping, 'wait'):
        old = context.pages[0]
        replacement = MagicMock()
        replacement.is_closed.return_value = False
        replacement.url = 'https://weibo.com/'
        replacement.locator.return_value.inner_text.return_value = '首页'

        def close_popup(*args):
            old.is_closed.return_value = True
            context.pages = [replacement]
            raise Error('Target page, context or browser has been closed')

        old.mouse.click.side_effect = close_popup
        service.run('weibo', state, MagicMock())
    assert service.status('weibo')['state'] == 'success'
    old.mouse.click.assert_called_once_with(100, 100)
    replacement.mouse.click.assert_not_called()
    old.locator.assert_not_called()
    replacement.set_default_timeout.assert_called_once_with(10000)
    assert 'dialog' in [call.args[0] for call in replacement.on.call_args_list]


@pytest.mark.parametrize('credential,expected', [(None, 'error'), ('SUB=verified', 'success')])
def test_all_pages_closed_never_reads_stale_page(browser_client, credential, expected):
    _, service = browser_client
    state = put_state(service)
    lease = MagicMock()
    with fake_browser(service) as context, patch.object(service, 'verify', return_value=credential) as verify:
        page = context.pages[0]

        def close_page(*args, **kwargs):
            page.is_closed.return_value = True
            context.pages = []
            return MagicMock(status=200)

        page.goto.side_effect = close_page
        service.run('weibo', state, lease)
    assert service.status('weibo')['state'] == expected
    if expected == 'error':
        assert service.status('weibo')['error_code'] == 'page_closed'
        assert service.status('weibo')['diagnostics']['page_count'] == 0
    page.locator.assert_not_called()
    context.new_page.assert_not_called()
    verify.assert_called_once_with('weibo', context, None)
    lease.release.assert_called_once()


def test_navigation_recovery_is_bounded(browser_client):
    from playwright.sync_api import Error
    _, service = browser_client
    state = put_state(service)
    with fake_browser(service) as context, patch.object(service, 'verify') as verify, \
            patch.object(service.stopping, 'wait'):
        page = context.pages[0]
        page.locator.return_value.inner_text.side_effect = Error('Execution context was destroyed')
        service.run('weibo', state, MagicMock())
    result = service.status('weibo')
    assert result['state'] == 'error' and result['error_code'] == 'page_navigating'
    assert result['stage'] == '读取登录画面'
    assert page.locator.return_value.inner_text.call_count == MAX_PAGE_TRANSITIONS + 1
    page.goto.assert_called_once()
    verify.assert_not_called()


@pytest.mark.parametrize('finish', ['cancel', 'expire', 'stop'])
def test_interrupted_navigation_recovery_never_saves_credentials(browser_client, finish):
    from playwright.sync_api import Error
    _, service = browser_client
    state = put_state(service)
    with fake_browser(service) as context, patch.object(service, 'verify') as verify:
        page = context.pages[0]
        page.locator.return_value.inner_text.side_effect = Error('Execution context was destroyed')

        def interrupt(*args):
            if finish == 'cancel':
                service.action('weibo', state['id'], 'cancel')
            elif finish == 'expire':
                state['expires_at'] = time.time() - 1
            else:
                service.stopping.set()

        with patch.object(service.stopping, 'wait', side_effect=interrupt):
            service.run('weibo', state, MagicMock())
    assert service.status('weibo')['state'] == ('cancelled' if finish == 'cancel' else 'expired')
    assert not service.monitor.overview()['weibo_cookie_configured']
    verify.assert_not_called()


@pytest.mark.parametrize('credential', [None, 'SUB=must-not-save'])
def test_cancel_during_closed_page_verification_wins(browser_client, credential):
    _, service = browser_client
    state = put_state(service)
    with fake_browser(service) as context:
        page = context.pages[0]
        page.is_closed.return_value = True

        def cancel(*args):
            service.action('weibo', state['id'], 'cancel')
            return credential

        with patch.object(service, 'verify', side_effect=cancel):
            service.run('weibo', state, MagicMock())
    assert service.status('weibo')['state'] == 'cancelled'
    assert not service.monitor.overview()['weibo_cookie_configured']


def test_browser_disconnect_is_terminal_and_diagnostics_precede_cleanup(browser_client):
    from playwright.sync_api import Error
    _, service = browser_client
    state = put_state(service)
    with fake_browser(service) as context, patch.object(service.stopping, 'wait') as wait:
        page = context.pages[0]

        def disconnect(*args, **kwargs):
            context.browser.is_connected.return_value = False
            page.is_closed.return_value = True
            for call in context.browser.on.call_args_list:
                if call.args[0] == 'disconnected':
                    call.args[1](context.browser)
            raise Error('Target page, context or browser has been closed')

        page.locator.return_value.inner_text.side_effect = disconnect
        service.run('weibo', state, MagicMock())
    result = service.status('weibo')
    assert result['error_code'] == 'browser_closed'
    assert result['diagnostics']['browser_connected'] is False
    assert result['diagnostics']['browser_disconnected'] is True
    assert result['diagnostics']['context_closed'] is False
    wait.assert_not_called()


@pytest.mark.parametrize('where', ['body', 'verify', 'screenshot'])
def test_unknown_errors_are_terminal_and_structured_logs_never_include_secrets(browser_client, where):
    _, service = browser_client
    state = put_state(service)
    error = RuntimeError('private-cookie secret-token https://user:password@host/private-user?code=secret#secret')
    with fake_browser(service) as context, patch.object(service, 'verify', return_value=None) as verify, \
            patch('app.services.social_browser.logger') as log, patch.object(service.stopping, 'wait') as wait:
        page = context.pages[0]
        target = {'body': page.locator.return_value.inner_text, 'verify': verify, 'screenshot': page.screenshot}[where]
        target.side_effect = error
        service.run('weibo', state, MagicMock())
    result = service.status('weibo')
    assert result['state'] == 'error'
    assert result['stage'] == {'body': '读取登录画面', 'verify': '验证平台登录状态', 'screenshot': '截取登录画面'}[where]
    assert result['diagnostics']['error_type'] == 'RuntimeError'
    assert result['diagnostics']['page_closed'] is False
    assert result['diagnostics']['browser_connected'] is True
    output = json.dumps(result) + str(log.mock_calls)
    for secret in ('private-cookie', 'secret-token', 'password', 'private-user', '?code=', '#secret'):
        assert secret not in output
    target.assert_called_once()
    wait.assert_not_called()


def test_screenshot_transition_recovers_without_repeating_login_request(browser_client):
    from playwright.sync_api import Error
    _, service = browser_client
    state = put_state(service)
    with fake_browser(service) as context, patch.object(service, 'verify', side_effect=[None, 'SUB=verified']), \
            patch.object(service.stopping, 'wait'):
        page = context.pages[0]
        page.screenshot.side_effect = Error('Cannot find context with specified id')
        service.run('weibo', state, MagicMock())
    assert service.status('weibo')['state'] == 'success'
    page.goto.assert_called_once()
    page.screenshot.assert_called_once_with(type='png', timeout=15000)


@pytest.mark.parametrize('status', [401, 403, 418, 429, 567])
def test_weibo_verification_restriction_is_terminal(browser_client, status):
    _, service = browser_client
    state = put_state(service)
    with fake_browser(service) as context, patch.object(service.stopping, 'wait') as wait:
        context.cookies.return_value = [{'name': 'SUB', 'value': 'private-cookie'}]
        context.request.get.return_value.status = status
        service.run('weibo', state, MagicMock())
    result = service.status('weibo')
    assert result['state'] == 'blocked' and result['http_status'] == status
    context.request.get.assert_called_once()
    wait.assert_not_called()
    assert not service.monitor.overview()['weibo_cookie_configured']


def test_cleanup_failure_does_not_mask_primary_error_or_cancel(browser_client):
    _, service = browser_client
    playwright = MagicMock()
    context = playwright.chromium.launch_persistent_context.return_value
    context.pages = []
    context.browser.is_connected.return_value = False
    context.close.side_effect = RuntimeError('secret-close-error')
    with patch('app.services.social_browser.logger') as log:
        with pytest.raises(ValueError, match='primary-error'):
            with service.browser_context(playwright, 'weibo'):
                raise ValueError('primary-error')
        with service.browser_context(playwright, 'weibo'):
            pass
    assert context.close.call_count == 2
    assert 'secret-close-error' not in str(log.mock_calls)


def test_snapshot_failure_does_not_undo_verified_credentials(browser_client):
    _, service = browser_client
    service.monitor.update_config({'weibo_cookie': 'SUB=verified'}, verified_platform='weibo')
    playwright = MagicMock()
    context = playwright.chromium.launch_persistent_context.return_value
    context.pages = []
    context.browser.is_connected.return_value = False
    context.cookies.side_effect = RuntimeError('snapshot-secret')
    with patch('app.services.social_browser.logger') as log:
        with service.browser_context(playwright, 'weibo'):
            pass
    assert service.monitor.platform_login('weibo')['state'] == 'normal'
    context.close.assert_called_once()
    assert 'snapshot-secret' not in str(log.mock_calls)


def cross_process_login_worker(log_dir, queue):
    """Fresh interpreter, same on-disk state and encryption key, no browser/network."""
    import os
    import portalocker
    from app.services.social import SocialMonitor
    settings = Settings(_env_file=None, log_dir=log_dir, cache_enabled=False, social_worker_enabled=False)
    service = BrowserLogin(settings, SocialMonitor(settings))
    state = service.status('weibo')
    image = service.image('weibo', state['id'])
    existing = service.start('weibo')
    locked = False
    try:
        with portalocker.Lock(str(service.path('weibo', 'lock')), timeout=0):
            pass
    except portalocker.exceptions.LockException:
        locked = True
    cancelled = service.action('weibo', state['id'], 'cancel')
    queue.put((os.getpid(), state['id'], existing['id'], image, locked, cancelled['state']))


def test_actual_processes_share_session_lock_image_and_cancel(browser_client):
    import multiprocessing
    import os
    import portalocker
    _, service = browser_client
    state = put_state(service)
    service.write(service.path('weibo', 'image'), b'unit-test-qr')
    spawn = multiprocessing.get_context('spawn')
    queue = spawn.Queue()
    process = spawn.Process(target=cross_process_login_worker, args=(str(service.settings.log_path), queue))
    with portalocker.Lock(str(service.path('weibo', 'lock')), timeout=0):
        process.start()
        try:
            pid, job, existing, image, locked, status = queue.get(timeout=30)
            process.join(timeout=30)
            assert process.exitcode == 0 and pid != os.getpid()
            assert job == existing == state['id'] and image == b'unit-test-qr'
            assert locked and status == 'cancelling'
            assert service.command('weibo')['action'] == 'cancel'
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
            queue.close()


@pytest.mark.parametrize('event,expected_code', [('crash', 'page_crashed'), ('close', 'browser_closed')])
def test_failure_event_flags_are_captured_before_cleanup(browser_client, event, expected_code):
    from playwright.sync_api import Error
    _, service = browser_client
    state = put_state(service)
    with fake_browser(service) as context, patch.object(service.stopping, 'wait') as wait:
        page = context.pages[0]
        source = page if event == 'crash' else context

        def fail(*args, **kwargs):
            for call in source.on.call_args_list:
                if call.args[0] == event:
                    call.args[1](source)
            raise Error('Page crashed' if event == 'crash' else 'Target page, context or browser has been closed')

        page.locator.return_value.inner_text.side_effect = fail
        service.run('weibo', state, MagicMock())
    result = service.status('weibo')
    assert result['error_code'] == expected_code
    assert result['diagnostics']['page_crashed' if event == 'crash' else 'context_closed'] is True
    wait.assert_not_called()


def test_xueqiu_without_live_pages_does_not_attempt_page_based_verification(browser_client):
    _, service = browser_client
    state = put_state(service, platform='xueqiu')
    with fake_browser(service) as context, patch.object(service, 'verify') as verify:
        context.pages[0].is_closed.return_value = True
        service.run('xueqiu', state, MagicMock())
    assert service.status('xueqiu')['error_code'] == 'page_closed'
    verify.assert_not_called()
    context.pages[0].locator.assert_not_called()


@pytest.mark.parametrize('response', [403, 'navigation'])
def test_reload_is_not_replayed_on_restriction_or_transition(browser_client, response):
    from playwright.sync_api import Error
    _, service = browser_client
    state = put_state(service)
    service.action('weibo', state['id'], 'reload')
    with fake_browser(service) as context, patch.object(service, 'verify') as verify, \
            patch.object(service.stopping, 'wait') as wait:
        page = context.pages[0]
        page.goto.side_effect = [MagicMock(status=200),
                                MagicMock(status=response) if response == 403 else Error('Execution context was destroyed')]
        service.run('weibo', state, MagicMock())
    assert service.status('weibo')['state'] == ('blocked' if response == 403 else 'error')
    assert page.goto.call_count == 2
    verify.assert_not_called()
    wait.assert_not_called()


@pytest.mark.parametrize('failure', ['snapshot', 'close'])
def test_run_success_is_preserved_when_cleanup_fails(browser_client, failure):
    _, service = browser_client
    state = put_state(service)
    real_context = service.browser_context
    with fake_browser(service) as context, patch.object(service, 'browser_context', real_context), \
            patch('playwright.sync_api.sync_playwright') as start, \
            patch.object(service, 'verify', return_value='SUB=verified'):
        start.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = context
        if failure == 'snapshot':
            context.cookies.side_effect = RuntimeError('private-snapshot-error')
        else:
            context.close.side_effect = RuntimeError('private-close-error')
        service.run('weibo', state, MagicMock())
    assert service.status('weibo')['state'] == 'success'
    assert service.monitor.platform_login('weibo')['state'] == 'normal'
    context.close.assert_called_once()
