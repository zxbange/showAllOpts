"""Dual-channel notification behavior; all transports are mocked."""
import base64
from contextlib import contextmanager
from datetime import datetime
import hashlib
import hmac
import json
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
import pytest

from app.core.config import Settings
from app.main import create_app
from app.services.social import SocialMonitor
from app.services import social_notifications as notifications


WEBHOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/00000000-0000-0000-0000-000000000001'
SECRET = 'test-signing-secret-never-publish'
API = '/api/v1/social-monitor'


@contextmanager
def clock():
    moment = datetime.fromisoformat('2026-10-26T12:00:00+08:00')
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)
    with patch('app.services.social.datetime', Fixed), patch('app.services.social.time.time', return_value=moment.timestamp()):
        yield moment


@pytest.fixture
def channels(engine, tmp_path):
    settings = Settings(_env_file=None, log_dir=tmp_path, social_worker_enabled=False, cache_enabled=False)
    with clock():
        app = create_app(settings, engine)
        with TestClient(app) as client:
            service = app.state.social
            service.add_author('weibo', '123', '测试博主')
            with patch.object(service, 'send_part') as telegram, \
                    patch('app.services.social_notifications.send_feishu') as feishu, \
                    patch.object(service.stop_event, 'wait', return_value=False):
                yield client, service, telegram, feishu


def enable(service):
    service.update_config(dict(token='fake-telegram-token', chat_id='123', feishu_enabled=True,
                               feishu_webhook=WEBHOOK, feishu_secret=SECRET, enabled=True))
    author = service.overview()['authors'][0]
    service.collect(author, [])
    return author


def post(identifier='1', text='测试正文'):
    return dict(id=identifier, name='测试博主', text=text, time='2026-10-26T11:00:00+08:00',
                url='https://m.weibo.cn/detail/' + identifier)


def config(service):
    with service.db() as db:
        return service.config(db)


def deliveries(service, table='posts'):
    with service.db() as db:
        return [dict(row, delivery=json.loads(row['delivery'] or '{}')) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY id')]


def test_new_install_feishu_only_and_all_off_can_still_monitor(channels):
    client, service, telegram, feishu = channels
    # Save new credentials before touching the default, unconfigured Telegram switch.
    assert client.post(API+'/config', json=dict(feishu_webhook=WEBHOOK, feishu_secret=SECRET)).status_code == 200
    assert client.post(API+'/config', json=dict(telegram_enabled=False)).status_code == 200
    assert client.post(API+'/config', json=dict(feishu_enabled=True, enabled=True)).status_code == 200
    author = service.overview()['authors'][0]
    service.collect(author, [])
    service.collect(author, [post()])
    service.deliver_notifications(config(service))
    assert feishu.call_count == 1 and telegram.call_count == 0
    service.update_config(dict(feishu_enabled=False, feishu_webhook='', feishu_secret=''))
    assert service.overview()['enabled'] is True
    service.collect(author, [post('2')])
    assert deliveries(service)[-1]['status'] == 'muted'


def test_telegram_failure_does_not_block_feishu_queue_and_retry_is_independent(channels):
    _, service, telegram, feishu = channels
    author = enable(service)
    service.collect(author, [post('1'), post('2')])
    telegram.side_effect = RuntimeError('Telegram unavailable')
    with pytest.raises(RuntimeError):
        service.deliver_notifications(config(service))
    assert telegram.call_count == 1 and feishu.call_count == 2
    assert all(row['delivery']['feishu']['status'] == 'sent' for row in deliveries(service))
    telegram.side_effect = None
    telegram.reset_mock()
    feishu.reset_mock()
    service.deliver_notifications(config(service))
    service.deliver_notifications(config(service))
    assert telegram.call_count == 2 and feishu.call_count == 0
    assert all(row['status'] == 'sent' for row in deliveries(service))


def test_feishu_failure_does_not_block_telegram_and_retries_remaining_parts(channels):
    _, service, telegram, feishu = channels
    author = enable(service)
    service.collect(author, [post('1', 'x'*8000), post('2')])
    feishu.side_effect = [None, notifications.NotificationError('飞书暂时失败')]
    with pytest.raises(RuntimeError):
        service.deliver_notifications(config(service))
    rows = deliveries(service)
    assert rows[0]['delivery']['feishu']['next_part'] == 1
    assert all(row['delivery']['telegram']['status'] == 'sent' for row in rows)
    sent_telegram = telegram.call_count
    feishu.side_effect = None
    feishu.reset_mock()
    service.deliver_notifications(config(service))
    assert telegram.call_count == sent_telegram
    assert feishu.call_count == 3  # Remaining two parts, plus the next event.
    assert all(row['status'] == 'sent' for row in deliveries(service))


def test_channel_switches_do_not_replay_history_or_muted_events(channels):
    _, service, telegram, feishu = channels
    author = enable(service)
    service.collect(author, [post('1')])
    service.deliver_notifications(config(service))
    service.update_config(dict(telegram_enabled=False, feishu_enabled=False))
    service.collect(author, [post('2')])
    service.update_config(dict(telegram_enabled=True, feishu_enabled=True))
    service.deliver_notifications(config(service))
    assert telegram.call_count == feishu.call_count == 1
    assert deliveries(service)[1]['status'] == 'muted'
    service.collect(author, [post('3')])
    service.deliver_notifications(config(service))
    assert telegram.call_count == feishu.call_count == 2


def test_disabling_one_pending_channel_preserves_the_other(channels):
    _, service, telegram, feishu = channels
    author = enable(service)
    service.collect(author, [post()])
    service.update_config(dict(telegram_enabled=False))
    service.deliver_notifications(config(service))
    row = deliveries(service)[0]
    assert row['delivery']['telegram']['status'] == 'skipped'
    assert row['delivery']['feishu']['status'] == 'sent'
    assert feishu.call_count == 1 and telegram.call_count == 0
    service.update_config(dict(telegram_enabled=True))
    service.deliver_notifications(config(service))
    assert telegram.call_count == 0


def test_close_channel_mid_message_stops_its_remaining_parts(channels):
    _, service, telegram, feishu = channels
    author = enable(service)
    service.collect(author, [post(text='x'*8000)])
    telegram.side_effect = lambda *args: service.update_config({'telegram_enabled': False})
    service.deliver_notifications(config(service))
    assert telegram.call_count == 1 and feishu.call_count == 3
    assert deliveries(service)[0]['delivery']['telegram']['status'] == 'skipped'


def test_enabling_feishu_does_not_attach_it_to_existing_telegram_pending(channels):
    _, service, telegram, feishu = channels
    service.update_config(dict(token='fake', chat_id='123', enabled=True))
    author = service.overview()['authors'][0]
    service.collect(author, [])
    service.collect(author, [post()])
    service.update_config(dict(feishu_enabled=True, feishu_webhook=WEBHOOK, feishu_secret=SECRET))
    service.deliver_notifications(config(service))
    assert telegram.call_count == 1 and feishu.call_count == 0


def test_legacy_pending_cursor_migrates_only_to_telegram(channels):
    _, service, telegram, feishu = channels
    author = enable(service)
    service.collect(author, [post('1', 'x'*8000), post('2')])
    with service.db() as db:
        db.execute("UPDATE posts SET delivery=NULL,next_part=1 WHERE post_id='1'")
        db.execute("UPDATE posts SET delivery=NULL,status='sent' WHERE post_id='2'")
        old = service.config(db)
        for key in ('telegram_enabled', 'feishu_enabled', 'feishu_webhook', 'feishu_secret'):
            old.pop(key, None)
        service.write_config(db, old)
    restarted = SocialMonitor(service.settings)
    assert restarted.overview()['telegram_enabled'] and not restarted.overview()['feishu_enabled']
    with patch.object(restarted, 'send_part') as resumed, patch.object(restarted.stop_event, 'wait', return_value=False):
        restarted.deliver_notifications(config(restarted))
    assert resumed.call_count == 2
    assert deliveries(restarted)[0]['delivery'] == {'telegram': {'next_part': 3, 'status': 'sent', 'last_error': None}}
    assert feishu.call_count == telegram.call_count == 0


def test_paused_platform_alert_uses_both_channels_and_independent_retry(channels):
    _, service, telegram, feishu = channels
    enable(service)
    service.pause_platform('weibo', '需要人工登录')
    telegram.side_effect = RuntimeError('Telegram unavailable')
    service.send_alerts()
    service.send_alerts()
    assert telegram.call_count == feishu.call_count == 1
    state = service.overview()['platforms']['weibo']
    assert state['alert_delivery']['feishu']['status'] == 'sent'
    service.platform_state('weibo', alert_next_try=0)
    telegram.side_effect = None
    service.send_alerts()
    assert telegram.call_count == 2 and feishu.call_count == 1
    assert service.overview()['platforms']['weibo']['alert_sent_at']


def test_cube_events_have_independent_channel_cursors(channels):
    _, service, telegram, feishu = channels
    enable(service)
    service.add_cube('ZH123', '测试组合')
    with service.db() as db:
        db.execute("UPDATE cubes SET initialized=1,baseline_at='2026-10-26T09:00:00+08:00'")
    cube = service.overview()['cubes'][0]
    event = dict(id='1', status='success', category='user_rebalancing', time='2026-10-26T11:00:00+08:00',
                 comment='调仓', changes=[dict(symbol='SH600000', name='测试', before=0, after=10, price=10)],
                 url='https://www.xueqiu.com/P/ZH123')
    service.collect_cube(cube, [event])
    telegram.side_effect = RuntimeError('Telegram unavailable')
    with pytest.raises(RuntimeError):
        service.deliver_notifications(config(service))
    assert feishu.call_count == 1
    assert deliveries(service, 'cube_rebalances')[0]['delivery']['feishu']['status'] == 'sent'
    telegram.side_effect = None
    service.deliver_notifications(config(service))
    assert telegram.call_count == 2 and feishu.call_count == 1


def test_saved_credentials_encrypted_and_validation_errors_do_not_echo_inputs(channels):
    client, service, _, _ = channels
    response = client.post(API+'/config', json=dict(feishu_webhook=WEBHOOK, feishu_secret=SECRET))
    assert response.status_code == 200 and response.json()['feishu_secret_configured']
    assert SECRET not in response.text and WEBHOOK not in response.text
    raw = (service.root/'monitor.sqlite3').read_bytes()
    assert SECRET.encode() not in raw and WEBHOOK.encode() not in raw
    for payload in [dict(feishu_secret=SECRET*100), dict(feishu_webhook=WEBHOOK+'?secret='+SECRET),
                    dict(feishu_secret={'value': SECRET}), dict(feishu_webhook=WEBHOOK*100)]:
        failed = client.post(API+'/config', json=payload)
        assert failed.status_code in (400, 422)
        assert SECRET not in failed.text and WEBHOOK not in failed.text


def test_test_endpoints_honor_independent_switches(channels):
    client, service, telegram, feishu = channels
    enable(service)
    assert client.post(API+'/feishu-test').status_code == 200
    assert client.post(API+'/telegram-test').status_code == 200
    assert telegram.call_count == feishu.call_count == 1
    service.update_config(dict(feishu_enabled=False, telegram_enabled=False))
    assert client.post(API+'/feishu-test').status_code == 400
    assert client.post(API+'/telegram-test').status_code == 400
    assert telegram.call_count == feishu.call_count == 1


def test_feishu_signature_matches_official_algorithm():
    stamp = '1599360473'
    body = notifications.feishu_body('消息', 'sign-secret', timestamp=stamp)
    expected = base64.b64encode(hmac.new((stamp+'\nsign-secret').encode(), b'', hashlib.sha256).digest()).decode()
    assert body['timestamp'] == stamp and body['sign'] == expected
    assert 'sign-secret' not in json.dumps(body)


def test_feishu_message_splitting_accounts_for_utf8_and_json_escaping():
    text = '中😀"\n\t\\' * 10000
    parts = notifications.feishu_parts([text])
    assert ''.join(parts) == text
    assert len(parts) > 1
    for part in parts:
        body = json.dumps(notifications.feishu_body(part, SECRET), ensure_ascii=False).encode()
        assert len(body) < 20 * 1024


@pytest.mark.parametrize('url', [
    'http://open.feishu.cn/open-apis/bot/v2/hook/000000000000000000',
    'https://evil.invalid/open-apis/bot/v2/hook/000000000000000000',
    'https://open.feishu.cn@evil.invalid/open-apis/bot/v2/hook/000000000000000000',
    WEBHOOK+'?forward=elsewhere', 'https://open.feishu.cn/internal/admin',
])
def test_feishu_webhook_rejects_foreign_or_unsafe_urls(url):
    with patch('app.services.social_notifications.httpx.Client') as client:
        with pytest.raises(ValueError):
            notifications.send_feishu(url, SECRET, 'test')
        client.assert_not_called()


@pytest.mark.parametrize('status,payload,retry_after', [
    (200, {'code': 0}, 0), (200, {'code': 19021, 'msg': SECRET}, 0),
    (200, {'code': 11232}, 60), (429, {'code': 0}, 60), (302, {'code': 0}, 0),
    (500, {'code': 0}, 0), (200, {'code': False}, 0),
])
def test_feishu_response_code_http_status_and_limit_errors(status, payload, retry_after):
    observed = []
    def handler(request):
        observed.append(request)
        return httpx.Response(status, json=payload, headers={'location': 'https://evil.invalid'})
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    with patch('app.services.social_notifications.httpx.Client', return_value=client):
        if status == 200 and type(payload['code']) is int and payload['code'] == 0:
            notifications.send_feishu(WEBHOOK, SECRET, '测试')
        else:
            with pytest.raises(notifications.NotificationError) as error:
                notifications.send_feishu(WEBHOOK, SECRET, '测试')
            assert error.value.retry_after == retry_after
            assert WEBHOOK not in str(error.value) and SECRET not in str(error.value)
    assert len(observed) == 1


def test_feishu_network_error_does_not_expose_webhook():
    with patch('app.services.social_notifications.httpx.Client') as client:
        client.return_value.__enter__.return_value.post.side_effect = httpx.ConnectError(WEBHOOK)
        with pytest.raises(RuntimeError) as error:
            notifications.send_feishu(WEBHOOK, SECRET, 'test')
    assert WEBHOOK not in str(error.value)


def test_feishu_rate_limit_is_persisted_without_blocking_telegram(channels):
    _, service, telegram, feishu = channels
    author = enable(service)
    service.collect(author, [post('1'), post('2')])
    feishu.side_effect = notifications.NotificationError('飞书限流', retry_after=60)
    with pytest.raises(RuntimeError):
        service.deliver_notifications(config(service))
    assert telegram.call_count == 2 and feishu.call_count == 1
    with pytest.raises(RuntimeError):
        service.deliver_notifications(config(service))
    assert telegram.call_count == 2 and feishu.call_count == 1
    assert config(service)['notification_retry_at']['feishu'] > service.current_moment().timestamp()


@pytest.mark.parametrize('kind,direct', [('post', False), ('post', True), ('cube', False), ('cube', True)])
def test_more_than_100_failed_channel_events_cannot_starve_the_other(channels, kind, direct):
    _, service, telegram, feishu = channels
    author = enable(service)
    if kind == 'post':
        service.collect(author, [post(str(index)) for index in range(1, 111)])
        deliver = service.deliver if direct else service.deliver_notifications
    else:
        service.add_cube('ZH123', '测试组合')
        with service.db() as db:
            db.execute("UPDATE cubes SET initialized=1,baseline_at='2026-10-26T09:00:00+08:00'")
        cube = service.overview()['cubes'][0]
        events = [dict(id=str(index), status='success', category='user_rebalancing',
                       time='2026-10-26T11:00:00+08:00', comment='调仓',
                       changes=[dict(symbol='SH600000', name='测试', before=0, after=10, price=10)],
                       url='https://www.xueqiu.com/P/ZH123') for index in range(1, 111)]
        service.collect_cube(cube, events)
        deliver = service.deliver_cubes if direct else service.deliver_notifications
    telegram.side_effect = RuntimeError('Telegram unavailable')
    for _ in range(2):
        with pytest.raises(RuntimeError):
            deliver(config(service))
    assert telegram.call_count == 2
    assert feishu.call_count == 110
    rows = deliveries(service, 'posts' if kind == 'post' else 'cube_rebalances')
    assert all(row['delivery']['feishu']['status'] == 'sent' for row in rows)


def test_closing_successful_channel_does_not_hide_other_channel_error(channels):
    _, service, telegram, feishu = channels
    author = enable(service)
    service.collect(author, [post()])
    telegram.side_effect = RuntimeError('Telegram unavailable')
    with pytest.raises(RuntimeError):
        service.deliver_notifications(config(service))
    service.update_config({'feishu_enabled': False})
    assert deliveries(service)[0]['last_error'] == 'Telegram unavailable'


def test_resuming_platform_during_first_alert_cancels_second_channel_alert(channels):
    _, service, telegram, feishu = channels
    enable(service)
    service.pause_platform('weibo', '需要人工登录')
    telegram.side_effect = lambda *args: service.resume_platform('weibo')
    service.send_alerts()
    assert telegram.call_count == 1 and feishu.call_count == 0
    assert service.overview()['platforms']['weibo']['paused'] is False


def test_feishu_send_spacing_is_persisted_for_worker_and_test_calls(channels):
    _, service, _, feishu = channels
    enable(service)
    service.test_feishu()
    reserved = config(service)['notification_next_send']['feishu']
    assert reserved == pytest.approx(service.current_moment().timestamp()+.65)
    service.test_feishu()
    assert feishu.call_count == 2
    assert any(.64 <= call.args[0] <= .66 for call in service.stop_event.wait.call_args_list)


def test_oversized_or_non_json_feishu_response_never_leaks_input():
    with patch('app.services.social_notifications.httpx.Client') as client:
        with pytest.raises(notifications.NotificationError, match='20 KB'):
            notifications.send_feishu(WEBHOOK, SECRET, '中'*20000)
        client.assert_not_called()
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=WEBHOOK+SECRET)))
    with patch('app.services.social_notifications.httpx.Client', return_value=client):
        with pytest.raises(notifications.NotificationError) as error:
            notifications.send_feishu(WEBHOOK, SECRET, 'test')
    assert WEBHOOK not in str(error.value) and SECRET not in str(error.value)


def test_non_json_http_429_still_honors_retry_after():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        429, text='rate limited', headers={'Retry-After': '120'})))
    with patch('app.services.social_notifications.httpx.Client', return_value=client):
        with pytest.raises(notifications.NotificationError) as error:
            notifications.send_feishu(WEBHOOK, SECRET, 'test')
    assert error.value.retry_after == 120
