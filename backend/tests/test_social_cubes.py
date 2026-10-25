from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.services.social import SocialMonitor
from app.services.social_sources import MonitoringDeferred, PlatformIntervention, PostBatch, RateLimited


@contextmanager
def clock(value):
    moment = datetime.fromisoformat(value)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    with patch('app.services.social.datetime', FixedDateTime), \
            patch('app.services.social.time.time', return_value=moment.timestamp()):
        yield moment


@pytest.fixture
def cube_client(engine, tmp_path):
    settings = Settings(_env_file=None, log_dir=tmp_path, cache_enabled=False, social_worker_enabled=False)
    with clock('2026-10-26T12:00:00+08:00'):
        app = create_app(settings, engine)
        with TestClient(app) as client, \
                patch('app.services.social.fetch_posts', return_value=[]), \
                patch('app.services.social.fetch_cube_rebalances', return_value=PostBatch([])), \
                patch('app.services.social.random.uniform', return_value=780), \
                patch.object(app.state.social, 'send_part') as send, \
                patch.object(app.state.social.stop_event, 'wait', return_value=False):
            yield client, app.state.social, send


def add_cube(service, symbol='ZH679918', name='测试组合'):
    service.add_cube(symbol, name)
    return next(cube for cube in service.overview()['cubes'] if cube['symbol'] == symbol)


def enable(service):
    service.update_config({'token': 'fake-token', 'chat_id': '123',
                           'xueqiu_cookie': 'fake-cookie', 'enabled': True})


def event(identifier='101', status='success', time='2026-10-26T12:01:00+08:00', **changes):
    return dict({
        'id': identifier, 'status': status, 'category': 'user_rebalancing', 'time': time,
        'updated_at': time, 'comment': '调整持仓',
        'changes': [{'symbol': 'SH600000', 'name': '测试股票', 'price': 10.5,
                     'before': 0, 'after': 12.5, 'action': '买入'}],
        'url': 'https://www.xueqiu.com/P/ZH679918',
    }, **changes)


def rows(service, cube_id=None):
    return service.cube_rebalances(cube_id, 1, 100)['items']


def next_tick(service):
    stamp = service.overview()['next_check']
    assert stamp is not None
    return clock(stamp)


def seed_baseline(service, values=()):
    with patch('app.services.social.fetch_cube_rebalances', return_value=PostBatch(list(values))):
        service.cycle()


def test_cube_management_api_and_start_with_only_a_cube(cube_client):
    client, service, _ = cube_client
    base = '/api/v1/social-monitor'
    assert client.post(base + '/config', json={'token': 'fake-token', 'chat_id': '123',
                                              'enabled': True}).status_code == 400
    response = client.post(base + '/cubes', json={'url': 'https://www.xueqiu.com/P/ZH679918',
                                                 'name': '关注组合'})
    assert response.status_code == 200
    cube = response.json()['cubes'][0]
    assert cube['symbol'] == 'ZH679918' and cube['name'] == '关注组合'
    assert client.post(base + '/cubes', json={'url': 'ZH679918'}).status_code == 400
    assert client.post(base + '/cubes', json={'url': 'https://example.com/P/ZH123'}).status_code == 400
    assert client.post(base + '/config', json={'token': 'fake-token', 'chat_id': '123',
                                              'enabled': True}).status_code == 200
    assert service.overview()['authors'] == []
    assert client.get(base + '/cube-rebalances').json()['items'] == []
    assert client.post(base + f"/cubes/{cube['id']}", json={'enabled': False}).status_code == 200
    assert not service.overview()['cubes'][0]['enabled']
    assert client.post(base + f"/cubes/{cube['id']}", json={'enabled': True}).status_code == 200
    assert client.post(base + f"/cubes/{cube['id']}/delete", json={}).status_code == 200
    assert service.overview()['cubes'] == []
    assert client.post(base + f"/cubes/{cube['id']}/delete", json={}).status_code == 404


def test_initial_success_is_a_baseline_then_new_success_is_sent_once(cube_client):
    _, service, send = cube_client
    add_cube(service)
    enable(service)
    old = event('100', time='2026-10-25T10:00:00+08:00')
    seed_baseline(service, [old])
    assert service.overview()['cubes'][0]['initialized'] == 1
    assert rows(service)[0]['notify_status'] == 'baseline'
    send.assert_not_called()
    fresh = event('101')
    with patch('app.services.social.fetch_cube_rebalances', return_value=PostBatch([fresh, old])):
        with next_tick(service):
            service.cycle()
        with next_tick(service):
            service.cycle()
    assert send.call_count == 1
    assert sorted(row['notify_status'] for row in rows(service)) == ['baseline', 'sent']
    assert service.overview()['cube_counts']['sent'] == 1


def test_pending_record_can_become_success_even_when_seen_during_baseline(cube_client):
    _, service, send = cube_client
    add_cube(service)
    enable(service)
    seed_baseline(service, [event('101', status='pending')])
    assert rows(service)[0]['notify_status'] == 'waiting'
    send.assert_not_called()
    with patch('app.services.social.fetch_cube_rebalances', return_value=PostBatch([event('101')])):
        with next_tick(service):
            service.cycle()
        with next_tick(service):
            service.cycle()
    assert len(rows(service)) == 1
    assert rows(service)[0]['status'] == 'success'
    assert rows(service)[0]['notify_status'] == 'sent'
    assert send.call_count == 1


def test_multiple_new_rebalances_are_preserved_and_sent_oldest_first(cube_client):
    client, service, send = cube_client
    cube = add_cube(service)
    enable(service)
    seed_baseline(service)
    older = event('101', comment='第一笔调整')
    newer = event('102', time='2026-10-26T12:02:00+08:00', comment='第二笔调整')
    with patch('app.services.social.fetch_cube_rebalances', return_value=PostBatch([newer, older])):
        with next_tick(service):
            service.cycle()
    assert send.call_count == 2
    assert '第一笔调整' in send.call_args_list[0].args[2]
    assert '第二笔调整' in send.call_args_list[1].args[2]
    endpoint = '/api/v1/social-monitor/cube-rebalances'
    first = client.get(endpoint, params={'cube_id': cube['id'], 'page': 1, 'size': 1}).json()
    second = client.get(endpoint, params={'cube_id': cube['id'], 'page': 2, 'size': 1}).json()
    assert first['total'] == second['total'] == 2
    assert first['items'][0]['rebalance_id'] != second['items'][0]['rebalance_id']
    assert client.get(endpoint, params={'cube_id': cube['id'] + 999}).json()['total'] == 0


def test_failed_canceled_and_passive_weight_changes_are_not_success_notifications(cube_client):
    _, service, send = cube_client
    add_cube(service)
    enable(service)
    seed_baseline(service)
    values = [event('101', status='failed'), event('102', status='canceled'),
              event('103', status='pending'), event('104', changes=[]),
              event('105', category='system_rebalancing')]
    with patch('app.services.social.fetch_cube_rebalances', return_value=PostBatch(values)):
        with next_tick(service):
            service.cycle()
    send.assert_not_called()
    assert all(row['notify_status'] not in ('pending', 'sent') for row in rows(service))


@pytest.mark.parametrize('stamp,active', [
    ('2026-10-26T08:59:59+08:00', False),
    ('2026-10-26T09:00:00+08:00', True),
    ('2026-10-26T14:59:59+08:00', True),
    ('2026-10-26T15:00:00+08:00', False),
    ('2026-10-26T23:00:00+08:00', False),
    ('2026-10-26T01:00:00+00:00', True),
    ('2026-10-24T10:00:00+08:00', False),
    ('2026-10-25T10:00:00+08:00', False),
])
def test_cube_checks_only_run_in_beijing_nine_to_fifteen_window(cube_client, stamp, active):
    _, service, send = cube_client
    with clock(stamp):
        add_cube(service)
        enable(service)
        with patch('app.services.social.fetch_cube_rebalances', return_value=PostBatch([])) as fetch:
            service.cycle()
        assert bool(fetch.call_count) is active
        assert service.overview()['cube_monitoring_active'] is active
        if not active:
            due = datetime.fromisoformat(service.overview()['next_check'])
            assert due.hour == 9 and due.minute == 0
            if datetime.fromisoformat(stamp).weekday() >= 5:
                assert due.date().isoformat() == '2026-10-26'
    send.assert_not_called()


def test_existing_author_monitoring_still_runs_at_seven_before_cubes_open(cube_client):
    _, service, _ = cube_client
    service.add_author('weibo', '11', '微博测试')
    service.add_author('xueqiu', '22', '雪球测试')
    add_cube(service)
    enable(service)
    requested = []
    with clock('2026-10-26T07:00:00+08:00'), \
            patch('app.services.social.fetch_posts', side_effect=lambda author, pages: requested.append(author['platform']) or []), \
            patch('app.services.social.fetch_cube_rebalances', return_value=PostBatch([])) as cubes:
        service.cycle()
    assert requested == ['weibo', 'xueqiu']
    cubes.assert_not_called()


def test_cube_request_guard_stops_pagination_at_fifteen(cube_client):
    _, service, send = cube_client
    add_cube(service)
    enable(service)
    seed_baseline(service)

    def crossing_close(source, pages):
        assert not source['_should_stop']()
        with clock('2026-10-26T15:00:00+08:00'):
            assert source['_should_stop']()
        raise MonitoringDeferred('组合监控时间已结束')

    with clock('2026-10-26T14:59:59+08:00'), \
            patch('app.services.social.fetch_cube_rebalances', side_effect=crossing_close):
        service.cycle()
    send.assert_not_called()
    assert rows(service) == []


def test_cube_and_xueqiu_author_share_one_spread_platform_queue(cube_client):
    _, service, _ = cube_client
    service.add_author('xueqiu', '22', '雪球测试')
    add_cube(service)
    enable(service)
    requested = []
    with patch('app.services.social.fetch_posts', side_effect=lambda author, pages: requested.append('author') or []), \
            patch('app.services.social.fetch_cube_rebalances', side_effect=lambda cube, pages: requested.append('cube') or PostBatch([])):
        service.cycle()
        assert requested == ['author']
        service.cycle()
        assert requested == ['author']
        with next_tick(service):
            service.cycle()
        assert requested == ['author', 'cube']
        with next_tick(service):
            service.cycle()
    assert requested == ['author', 'cube', 'author']


def test_page_failure_keeps_scan_cursor_and_restart_resumes_without_duplicate_notifications(cube_client):
    _, service, send = cube_client
    add_cube(service)
    enable(service)
    seed_baseline(service)
    original_success = service.overview()['cubes'][0]['last_success_at']

    def failed_page(source, pages):
        source['_on_page']([event('101')], 2, False)
        raise httpx.ConnectError('private-platform-response')

    with patch('app.services.social.fetch_cube_rebalances', side_effect=failed_page):
        with next_tick(service):
            service.cycle()
    cube = service.overview()['cubes'][0]
    assert cube['scan_page'] == 2 and cube['last_success_at'] == original_success
    assert 'private-platform-response' not in cube['last_error']
    assert send.call_count == 1
    service.platform_state('xueqiu', retry_at=0)
    restarted = SocialMonitor(service.settings)

    def resumed_page(source, pages):
        assert source['_scan_page'] == 2
        return PostBatch([event('101'), event('102', comment='继续补抓')])

    with patch('app.services.social.fetch_cube_rebalances', side_effect=resumed_page), \
            patch.object(restarted, 'send_part') as resumed_send, \
            patch.object(restarted.stop_event, 'wait', return_value=False):
        with next_tick(restarted):
            restarted.cycle()
    assert len(rows(restarted)) == 2
    assert resumed_send.call_count == 1 and '继续补抓' in resumed_send.call_args.args[2]
    assert restarted.overview()['cubes'][0]['scan_page'] == 1


def test_telegram_failure_stays_pending_and_retries_without_recreating_event(cube_client):
    _, service, send = cube_client
    add_cube(service)
    enable(service)
    seed_baseline(service)
    with patch('app.services.social.fetch_cube_rebalances', return_value=PostBatch([event()])), \
            patch.object(service, 'send_part', side_effect=RuntimeError('模拟发送失败')):
        with next_tick(service):
            service.cycle()
    assert rows(service)[0]['notify_status'] == 'pending'
    with next_tick(service):
        service.cycle()
    assert len(rows(service)) == 1
    assert rows(service)[0]['notify_status'] == 'sent'
    send.assert_called_once()


@pytest.mark.parametrize('refreshed_status', ['success', 'pending'])
def test_long_cube_notification_resumes_after_the_last_confirmed_part(cube_client, refreshed_status):
    _, service, send = cube_client
    add_cube(service)
    enable(service)
    seed_baseline(service)
    long_event = event(comment='分段消息' * 1500)
    with patch('app.services.social.fetch_cube_rebalances', return_value=PostBatch([long_event])), \
            patch.object(service, 'send_part', side_effect=[None, RuntimeError('第二段发送失败')]) as first_send:
        with next_tick(service):
            service.cycle()
    assert first_send.call_count == 2
    with service.db() as db:
        assert db.execute('SELECT next_part FROM cube_rebalances').fetchone()[0] == 1
    assert rows(service)[0]['notify_status'] == 'pending'
    incomplete_update = event(status=refreshed_status, changes=[], comment='接口暂缺原调仓明细')
    with patch('app.services.social.fetch_cube_rebalances', return_value=PostBatch([incomplete_update])):
        with next_tick(service):
            service.cycle()
    assert rows(service)[0]['notify_status'] == 'sent'
    assert rows(service)[0]['payload'] == long_event
    assert send.call_count >= 1
    assert send.call_args_list[0].args[2] == first_send.call_args_list[1].args[2]
    assert all(call.args[2] != first_send.call_args_list[0].args[2] for call in send.call_args_list)


def test_pending_cube_notifications_wait_until_next_morning_after_fifteen(cube_client):
    _, service, send = cube_client
    add_cube(service)
    enable(service)
    seed_baseline(service)
    with clock('2026-10-26T14:59:00+08:00'), \
            patch('app.services.social.fetch_cube_rebalances', return_value=PostBatch([event()])), \
            patch.object(service, 'send_part', side_effect=RuntimeError('模拟发送失败')):
        service.cycle()
    assert rows(service)[0]['notify_status'] == 'pending'
    with clock('2026-10-26T15:00:00+08:00'), \
            patch('app.services.social.fetch_cube_rebalances') as fetch:
        service.cycle()
    fetch.assert_not_called()
    send.assert_not_called()
    with clock('2026-10-27T09:00:00+08:00'):
        service.cycle()
    send.assert_called_once()
    assert rows(service)[0]['notify_status'] == 'sent'


def test_friday_after_close_and_weekend_changes_are_sent_in_order_on_monday(cube_client):
    _, service, send = cube_client
    baseline = event('100', time='2026-10-23T14:00:00+08:00')
    with clock('2026-10-23T14:40:00+08:00'):
        add_cube(service)
        enable(service)
        seed_baseline(service, [baseline])
    changes = [event('101', time='2026-10-23T16:00:00+08:00', comment='周五盘后调整'),
               event('102', time='2026-10-24T11:00:00+08:00', comment='周六调整'),
               event('103', time='2026-10-25T13:00:00+08:00', comment='周日调整')]
    with patch('app.services.social.fetch_cube_rebalances', return_value=PostBatch(changes[::-1] + [baseline])) as fetch:
        for stamp in ('2026-10-23T15:01:00+08:00', '2026-10-24T11:30:00+08:00',
                      '2026-10-25T14:59:59+08:00'):
            with clock(stamp):
                service.cycle()
        fetch.assert_not_called()
        send.assert_not_called()
        with clock('2026-10-26T09:00:00+08:00'):
            service.cycle()
    fetch.assert_called_once()
    assert send.call_count == 3
    for call, comment in zip(send.call_args_list, ('周五盘后调整', '周六调整', '周日调整')):
        assert comment in call.args[2]
    assert sorted(row['notify_status'] for row in rows(service)) == ['baseline', 'sent', 'sent', 'sent']


def test_first_weekend_start_keeps_older_baseline_but_notifies_post_start_changes(cube_client):
    _, service, send = cube_client
    with clock('2026-10-24T10:00:00+08:00'):
        add_cube(service)
        enable(service)
        with patch('app.services.social.fetch_cube_rebalances') as fetch:
            service.cycle()
        fetch.assert_not_called()
    old = event('100', time='2026-10-23T14:00:00+08:00', comment='添加前旧调仓')
    new_saturday = event('101', time='2026-10-24T10:30:00+08:00', comment='启用后周六调仓')
    new_sunday = event('102', time='2026-10-25T12:00:00+08:00', comment='启用后周日调仓')
    with clock('2026-10-25T14:00:00+08:00'), patch('app.services.social.fetch_cube_rebalances') as fetch:
        service.cycle()
        fetch.assert_not_called()
    with clock('2026-10-26T09:00:00+08:00'), \
            patch('app.services.social.fetch_cube_rebalances',
                  return_value=PostBatch([new_sunday, new_saturday, old])):
        service.cycle()
    assert service.overview()['cubes'][0]['initialized'] == 1
    assert send.call_count == 2
    assert '启用后周六调仓' in send.call_args_list[0].args[2]
    assert '启用后周日调仓' in send.call_args_list[1].args[2]
    statuses = {row['rebalance_id']: row['notify_status'] for row in rows(service)}
    assert statuses == {'100': 'baseline', '101': 'sent', '102': 'sent'}


def test_initial_closed_window_start_includes_a_success_in_the_same_second(cube_client):
    _, service, send = cube_client
    started = '2026-10-23T15:00:00+08:00'
    with clock(started):
        add_cube(service)
        enable(service)
        with patch('app.services.social.fetch_cube_rebalances') as fetch:
            service.cycle()
        fetch.assert_not_called()
    old = event('100', time='2026-10-23T14:59:59+08:00', comment='启用前旧调仓')
    same_second = event('101', time=started, comment='启用同秒的新调仓')
    with clock('2026-10-26T09:00:00+08:00'), \
            patch('app.services.social.fetch_cube_rebalances', return_value=PostBatch([same_second, old])):
        service.cycle()
    send.assert_called_once()
    assert '启用同秒的新调仓' in send.call_args.args[2]
    assert {row['rebalance_id']: row['notify_status'] for row in rows(service)} == {
        '100': 'baseline', '101': 'sent',
    }


def test_first_closed_window_scan_integrates_source_pagination_storage_and_notifications(cube_client):
    from app.services.social_cube_sources import fetch_cube_rebalances as real_fetch

    _, service, send = cube_client
    with clock('2026-10-24T12:00:00+08:00'):
        add_cube(service)
        enable(service)
        service.cycle()
    requested = []
    original_client = httpx.Client

    def raw_record(identifier, stamp):
        return {
            'id': identifier, 'status': 'success', 'category': 'user_rebalancing',
            'created_at': stamp, 'updated_at': stamp, 'comment': f'组合第{identifier}笔',
            'rebalancing_histories': [{'stock_symbol': 'SH600000', 'stock_name': '测试股票',
                                      'prev_weight': 0, 'target_weight': 12.5,
                                      'price': 10.5, 'proactive': True}],
        }

    begin = datetime.fromisoformat('2026-10-25T22:30:00+08:00')
    latest_page = [raw_record(102 + offset, (begin + timedelta(minutes=30 * offset)).isoformat())
                   for offset in range(20)][::-1]
    older_page = [raw_record(101, '2026-10-24T13:00:00+08:00'),
                  raw_record(100, '2026-10-24T11:00:00+08:00')]

    def handler(request):
        assert request.url.host == 'www.xueqiu.com'
        assert request.url.path == '/cubes/rebalancing/history.json'
        assert request.url.params['cube_symbol'] == 'ZH679918'
        page = int(request.url.params['page'])
        requested.append(page)
        assert page in (1, 2)
        return httpx.Response(200, json={'list': latest_page if page == 1 else older_page,
                                         'count': 20, 'page': page, 'totalCount': 22, 'maxPage': 2})

    def client_factory(**kwargs):
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    with patch('app.services.social.fetch_cube_rebalances', side_effect=real_fetch), \
            patch('app.services.social_cube_sources.httpx.Client', side_effect=client_factory):
        with clock('2026-10-26T09:00:00+08:00'):
            service.cycle()
        assert requested == [1, 2]
        assert len(rows(service)) == 22
        assert send.call_count == 21
        assert service.overview()['cubes'][0]['initialized'] == 1
        assert service.overview()['cubes'][0]['scan_page'] == 1
        statuses = {row['rebalance_id']: row['notify_status'] for row in rows(service)}
        assert statuses['100'] == 'baseline'
        assert sum(status == 'sent' for status in statuses.values()) == 21
        with next_tick(service):
            service.cycle()
    assert requested == [1, 2, 1]
    assert send.call_count == 21
    assert len(rows(service)) == 22


@pytest.mark.parametrize('failure', [PlatformIntervention('平台要求验证码'), RateLimited(1800)])
def test_xueqiu_restriction_on_cube_holds_author_tasks_but_weibo_continues(cube_client, failure):
    _, service, _ = cube_client
    add_cube(service)
    enable(service)
    with patch('app.services.social.fetch_cube_rebalances', side_effect=failure):
        service.cycle()
    state = service.overview()['platforms']['xueqiu']
    assert state.get('paused') or state.get('retry_at')
    service.add_author('xueqiu', '22', '雪球测试')
    service.add_author('weibo', '33', '微博测试')
    requested = []
    with patch('app.services.social.fetch_posts', side_effect=lambda author, pages: requested.append(author['platform']) or []), \
            patch('app.services.social.fetch_cube_rebalances') as fetch:
        service.cycle()
    assert requested == ['weibo']
    fetch.assert_not_called()


def test_deleting_cube_cascades_history_and_old_request_cannot_pollute_replacement(cube_client):
    _, service, send = cube_client
    old_cube = add_cube(service)
    enable(service)
    seed_baseline(service, [event('100')])
    assert len(rows(service)) == 1

    def stale_response(source, pages):
        service.change_cube(old_cube['id'], delete=True)
        add_cube(service, 'ZH222222', '新组合')
        with pytest.raises(MonitoringDeferred):
            source['_on_page']([event('101')], 2, False)
        return PostBatch([event('101')])

    with patch('app.services.social.fetch_cube_rebalances', side_effect=stale_response):
        with next_tick(service):
            service.cycle()
    assert rows(service) == []
    replacement = service.overview()['cubes'][0]
    assert replacement['symbol'] == 'ZH222222' and replacement['initialized'] == 0
    assert replacement['scan_page'] == 1 and replacement['last_success_at'] is None
    send.assert_not_called()


def test_cube_schema_upgrade_preserves_existing_authors_and_encrypted_configuration(cube_client):
    _, service, _ = cube_client
    service.add_author('weibo', '11', '原博主')
    service.update_config({'token': 'existing-token', 'xueqiu_cookie': 'existing-cookie'})
    with service.db() as db:
        db.execute('DROP TABLE cube_rebalances')
        db.execute('DROP TABLE cubes')
    restarted = SocialMonitor(service.settings)
    status = restarted.overview()
    assert status['authors'][0]['name'] == '原博主'
    assert status['cubes'] == []
    assert status['token_configured'] and status['xueqiu_cookie_configured']
    with restarted.db() as db:
        assert restarted.decode(restarted.config(db)['token']) == 'existing-token'
        assert restarted.decode(restarted.config(db)['xueqiu_cookie']) == 'existing-cookie'
