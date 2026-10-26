"""Polling settings persist and take effect without interrupting an active round."""
from datetime import datetime, timedelta
from unittest.mock import call, patch

from fastapi.testclient import TestClient
import httpx
import pytest

from app.core.config import Settings
from app.main import create_app
from app.services.social import CHINA, SocialMonitor, next_check_time
from test_social import clock


BASE = '/api/v1/social-monitor'


@pytest.fixture
def polling_client(engine, tmp_path):
    settings = Settings(_env_file=None, log_dir=tmp_path, cache_enabled=False, social_worker_enabled=False)
    with clock('2026-10-26T12:00:00+08:00'):
        app = create_app(settings, engine)
        with TestClient(app) as client, \
                patch('app.services.social.fetch_posts', return_value=[]), \
                patch('app.services.social.fetch_cube_rebalances', return_value=[]), \
                patch.object(app.state.social, 'send_part') as send, \
                patch.object(app.state.social.stop_event, 'wait', return_value=False):
            yield client, app.state.social, send


def config(service):
    with service.db() as db:
        return service.config(db)


def enable(service):
    service.update_config({'enabled': True, 'token': 'private-test-token', 'chat_id': '123456',
                           'weibo_cookie': 'private-test-weibo', 'xueqiu_cookie': 'private-test-xueqiu'})


def test_legacy_defaults_and_saved_range_survive_restart(polling_client):
    client, service, send = polling_client
    with service.db() as db:
        legacy = service.config(db)
        legacy.pop('interval_min', None)
        legacy.pop('interval_max', None)
        service.write_config(db, legacy)
    default = client.get(BASE + '/status').json()
    assert (default['interval_min'], default['interval_max']) == (600, 960)
    saved = client.post(BASE + '/config', json={'interval_min': 300, 'interval_max': 720})
    assert saved.status_code == 200
    assert (saved.json()['interval_min'], saved.json()['interval_max']) == (300, 720)
    restarted = SocialMonitor(service.settings)
    assert (restarted.overview()['interval_min'], restarted.overview()['interval_max']) == (300, 720)
    persisted = config(restarted)
    assert (persisted['interval_min'], persisted['interval_max']) == (300, 720)
    assert not restarted.overview()['enabled']
    send.assert_not_called()


def test_partial_updates_merge_current_range_and_null_preserves_values(polling_client):
    client, service, _ = polling_client
    assert client.post(BASE + '/config', json={'interval_min': 60, 'interval_max': 120}).status_code == 200
    changed = client.post(BASE + '/config', json={'interval_max': 86400})
    assert changed.status_code == 200
    assert (changed.json()['interval_min'], changed.json()['interval_max']) == (60, 86400)
    changed = client.post(BASE + '/config', json={'interval_min': 86340})
    assert changed.status_code == 200
    assert (changed.json()['interval_min'], changed.json()['interval_max']) == (86340, 86400)
    ignored = client.post(BASE + '/config', json={'interval_min': None, 'interval_max': None})
    assert ignored.status_code == 200
    assert (ignored.json()['interval_min'], ignored.json()['interval_max']) == (86340, 86400)
    assert not config(service)['enabled']


@pytest.mark.parametrize('invalid', [
    {'interval_min': 0}, {'interval_min': 59}, {'interval_min': 61},
    {'interval_max': 86460}, {'interval_min': True}, {'interval_max': False},
    {'interval_min': '600'}, {'interval_min': 660.0},
    {'interval_min': 960}, {'interval_max': 600},
    {'interval_min': 1200, 'interval_max': 600},
])
def test_invalid_range_is_rejected_atomically_without_changing_credentials_or_queue(polling_client, invalid):
    client, service, send = polling_client
    service.add_author('weibo', '123', '测试博主')
    enable(service)
    with patch('app.services.social.random.uniform', return_value=780):
        assert service.scheduled_author('weibo') is not None
    before = config(service)
    response = client.post(BASE + '/config', json=dict(invalid, token='must-not-replace-token', enabled=False))
    assert response.status_code in (400, 422)
    assert config(service) == before
    send.assert_not_called()


@pytest.mark.parametrize('invalid', [
    {'interval_min': True}, {'interval_max': 960.0}, {'interval_min': 61},
    {'interval_max': 86460}, {'interval_min': 960},
])
def test_internal_updates_also_validate_ranges(polling_client, invalid):
    _, service, _ = polling_client
    before = config(service)
    with pytest.raises(ValueError):
        service.update_config(dict(invalid, chat_id='must-not-save'))
    assert config(service) == before


def test_changing_range_preserves_inflight_task_and_existing_round_until_next_redraw(polling_client):
    _, service, _ = polling_client
    for uid in ('300', '100', '200'):
        service.add_author('weibo', uid, uid)
    service.update_config({'interval_min': 180, 'interval_max': 360})
    enable(service)
    started = datetime(2026, 10, 26, 12, tzinfo=CHINA)
    with patch('app.services.social.random.uniform', side_effect=[240, 900]) as random_interval:
        first, identifier = service.scheduled_author('weibo')
        original_queue = config(service)['platform_queues']['weibo']
        protected = {key: config(service)[key] for key in ('token', 'chat_id', 'weibo_cookie', 'xueqiu_cookie')}
        service.update_config({'interval_min': 600, 'interval_max': 1200})
        updated = config(service)
        assert updated['platform_queues']['weibo'] == original_queue
        assert {key: updated[key] for key in protected} == protected
        with service.db() as db:
            assert service.active_author(db, first) is not None
        service.finish_scheduled_author('weibo', identifier)
        assert config(service)['platform_queues']['weibo']['next_check'] == started.timestamp() + 80
        for elapsed, uid in ((80, '100'), (160, '200')):
            with clock((started + timedelta(seconds=elapsed - 1)).isoformat()):
                assert service.scheduled_author('weibo') is None
            with clock((started + timedelta(seconds=elapsed)).isoformat()):
                author, existing_id = service.scheduled_author('weibo')
                assert author['uid'] == uid and existing_id == identifier
                service.finish_scheduled_author('weibo', existing_id)
        with clock((started + timedelta(seconds=239)).isoformat()):
            assert service.scheduled_author('weibo') is None
        with clock((started + timedelta(seconds=240)).isoformat()):
            author, next_id = service.scheduled_author('weibo')
            assert author['uid'] == '300' and next_id != identifier
            assert config(service)['platform_queues']['weibo']['interval'] == 900
            service.finish_scheduled_author('weibo', next_id)
            assert config(service)['platform_queues']['weibo']['next_check'] == started.timestamp() + 540
        assert random_interval.call_args_list == [call(180, 360), call(600, 1200)]


def test_restart_retains_saved_range_and_queue_position(polling_client):
    _, service, _ = polling_client
    for uid in ('11', '12'):
        service.add_author('weibo', uid, uid)
    service.update_config({'interval_min': 120, 'interval_max': 240})
    enable(service)
    with patch('app.services.social.random.uniform', return_value=180):
        _, identifier = service.scheduled_author('weibo')
    service.finish_scheduled_author('weibo', identifier)
    original = config(service)['platform_queues']['weibo']
    restarted = SocialMonitor(service.settings)
    assert config(restarted)['platform_queues']['weibo'] == original
    with clock('2026-10-26T12:01:29+08:00'), patch('app.services.social.random.uniform') as random_interval:
        assert restarted.scheduled_author('weibo') is None
        random_interval.assert_not_called()
    with clock('2026-10-26T12:01:30+08:00'), patch('app.services.social.random.uniform') as random_interval:
        author, resumed_id = restarted.scheduled_author('weibo')
        assert author['uid'] == '12' and resumed_id == identifier
        random_interval.assert_not_called()


def test_xueqiu_author_and_cube_share_configured_round_and_random_redraw(polling_client):
    _, service, _ = polling_client
    service.add_author('xueqiu', '123', '测试博主')
    service.add_cube('ZH1', '测试组合')
    service.update_config({'interval_min': 60, 'interval_max': 120})
    enable(service)
    with patch('app.services.social.random.uniform', side_effect=[90, 120]) as random_interval:
        author, identifier = service.scheduled_author('xueqiu')
        assert author['_kind'] == 'author'
        assert len(config(service)['platform_queues']['xueqiu']['author_ids']) == 2
        service.finish_scheduled_author('xueqiu', identifier)
        with clock('2026-10-26T12:00:44+08:00'):
            assert service.scheduled_author('xueqiu') is None
        with clock('2026-10-26T12:00:45+08:00'):
            cube, existing_id = service.scheduled_author('xueqiu')
            assert cube['_kind'] == 'cube' and cube['symbol'] == 'ZH1' and existing_id == identifier
            service.finish_scheduled_author('xueqiu', existing_id)
        with clock('2026-10-26T12:01:30+08:00'):
            next_author, next_id = service.scheduled_author('xueqiu')
            assert next_author['_kind'] == 'author' and next_id != identifier
            assert config(service)['platform_queues']['xueqiu']['interval'] == 120
        assert random_interval.call_args_list == [call(60, 120), call(60, 120)]


@pytest.mark.parametrize('moment,next_open', [
    ('2026-10-24T12:00:00+08:00', '2026-10-26T09:00:00+08:00'),
    ('2026-10-26T08:59:59+08:00', '2026-10-26T09:00:00+08:00'),
    ('2026-10-26T15:00:00+08:00', '2026-10-27T09:00:00+08:00'),
])
def test_custom_range_keeps_cube_weekday_window(polling_client, moment, next_open):
    _, service, send = polling_client
    with clock(moment):
        service.add_cube('ZH1', '测试组合')
        service.update_config({'interval_min': 60, 'interval_max': 120})
        enable(service)
        assert service.scheduled_author('xueqiu') is None
        service.save_next_check()
        assert datetime.fromisoformat(service.overview()['next_check']) == datetime.fromisoformat(next_open)
    send.assert_not_called()


def test_custom_range_does_not_reduce_network_failure_backoff(polling_client):
    _, service, _ = polling_client
    service.add_author('weibo', '123', '测试博主')
    service.update_config({'interval_min': 60, 'interval_max': 120})
    enable(service)
    started = datetime(2026, 10, 26, 12, tzinfo=CHINA)
    with patch('app.services.social.fetch_posts', side_effect=httpx.ConnectError('fake-network-error')), \
            patch('app.services.social.random.uniform', side_effect=[90, 700]) as random_interval:
        service.cycle()
    assert random_interval.call_args_list == [call(60, 120), call(600, 960)]
    state = service.overview()['platforms']['weibo']
    assert state['reason_code'] == 'fetch_failed'
    assert state['retry_at'] == started.timestamp() + 700
    assert config(service)['platform_queues']['weibo']['interval'] == 90


@pytest.mark.parametrize('moment,delay,expected', [
    ('2026-10-26T23:00:00+08:00', 12 * 3600, '2026-10-27T11:00:00+08:00'),
    ('2026-10-26T23:59:00+08:00', 180, '2026-10-27T07:00:00+08:00'),
    ('2026-10-26T12:00:00+08:00', 24 * 3600, '2026-10-27T12:00:00+08:00'),
])
def test_long_ranges_preserve_next_day_time_and_still_skip_night(moment, delay, expected):
    stamp = next_check_time(datetime.fromisoformat(moment), delay)
    assert datetime.fromtimestamp(stamp, CHINA) == datetime.fromisoformat(expected)
