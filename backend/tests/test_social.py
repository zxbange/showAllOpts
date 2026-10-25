import time
from datetime import datetime
from unittest.mock import patch
from contextlib import contextmanager

from fastapi.testclient import TestClient
import httpx
import pytest

from app.core.config import Settings
from app.main import create_app
from app.services.social import SocialMonitor, CHINA, retention_cutoff, next_check_time
from app.services.social_sources import (normalize_post, message_parts, fetch_posts,
                                         PlatformIntervention, RateLimited, PostBatch, check_payload,
                                         fetch_xueqiu_author_name)


@contextmanager
def clock(value):
    fixed = datetime.fromisoformat(value)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    with patch('app.services.social.datetime', FixedDateTime):
        yield fixed


@pytest.fixture
def social_client(engine, tmp_path):
    settings = Settings(_env_file=None, log_dir=tmp_path, cache_enabled=False, social_worker_enabled=False)
    with clock('2026-10-25T12:00:00+08:00'):
        app = create_app(settings, engine)
        with TestClient(app) as client:
            yield client, app.state.social


def post(identifier='1', text='测试正文'):
    return {'id': identifier, 'name': '测试博主', 'time': '2026-10-25T12:00:00+08:00',
            'text': text, 'url': f'https://m.weibo.cn/detail/{identifier}'}


def add(service):
    service.add_author('weibo', '3962719063', '测试博主')
    return service.overview()['authors'][0]


def enable(service):
    service.update_config({'token': 'test-token', 'chat_id': '1234', 'enabled': True})


def test_all_management_endpoints_work_without_login(social_client):
    client, service = social_client
    endpoint = '/api/v1/social-monitor'
    assert client.get(endpoint + '/status').status_code == 200
    assert client.get(endpoint + '/posts').status_code == 200
    assert client.post(endpoint + '/authors', json={'platform': 'weibo', 'url': '123', 'name': '公开访问测试'}).status_code == 200
    author_id = service.overview()['authors'][0]['id']
    assert client.post(endpoint + '/config', json={'token': 'test-token', 'chat_id': '123', 'enabled': True}).status_code == 200
    assert client.post(endpoint + f'/authors/{author_id}', json={'enabled': False}).status_code == 200
    assert client.post(endpoint + '/platforms/weibo/resume', json={}).status_code == 200
    with patch.object(service, 'send_part') as send:
        assert client.post(endpoint + '/telegram-test', json={}).status_code == 200
        send.assert_called_once()
    assert client.post(endpoint + f'/authors/{author_id}/delete', json={}).status_code == 200
    for _ in range(12):
        assert client.get(endpoint + '/status').status_code == 200


def test_configuration_encrypted_and_never_echoed(social_client):
    client, service = social_client
    endpoint = '/api/v1/social-monitor/config'
    assert client.post(endpoint, json={'enabled': True}).status_code == 400
    add(service)
    response = client.post(endpoint, json={'token': 'secret-bot-token', 'chat_id': '123',
                                          'weibo_cookie': 'secret-cookie', 'enabled': True})
    assert response.status_code == 200
    assert response.json()['weibo_cookie_configured']
    assert 'secret-cookie' not in response.text and 'secret-bot-token' not in response.text
    raw = (service.root / 'monitor.sqlite3').read_bytes()
    assert b'secret-cookie' not in raw and b'secret-bot-token' not in raw
    # Restart uses the existing key and database.
    restarted = SocialMonitor(service.settings)
    with restarted.db() as db:
        assert restarted.decode(restarted.config(db)['token']) == 'secret-bot-token'


def test_clear_one_platform_cookie_preserves_other_settings(social_client):
    client, service = social_client
    service.update_config({'token': 'test-token', 'chat_id': '123',
                           'weibo_cookie': 'weibo-secret', 'xueqiu_cookie': 'xueqiu-secret'})
    response = client.post('/api/v1/social-monitor/config', json={'xueqiu_cookie': ''})
    assert response.status_code == 200
    status = response.json()
    assert not status['xueqiu_cookie_configured']
    assert status['weibo_cookie_configured'] and status['token_configured'] and status['chat_id_configured']
    with service.db() as db:
        config = service.config(db)
    assert service.decode(config['xueqiu_cookie']) == ''
    assert service.decode(config['weibo_cookie']) == 'weibo-secret'


def test_author_management_and_feed(social_client):
    client, service = social_client
    endpoint = '/api/v1/social-monitor'
    payload = {'platform': 'weibo', 'url': 'https://weibo.com/u/3962719063', 'name': '博主'}
    response = client.post(endpoint + '/authors', json=payload)
    assert response.status_code == 200
    author = response.json()['authors'][0]
    assert client.post(endpoint + '/authors', json=payload).status_code == 400
    assert client.post(endpoint + '/authors', json=dict(payload, url='https://example.com/123')).status_code == 400
    service.collect(author, [post()])
    feed = client.get(endpoint + '/posts?platform=weibo&author_id=' + str(author['id'])).json()
    assert feed['total'] == 1 and feed['items'][0]['payload']['text'] == '测试正文'
    assert client.get(endpoint + '/posts?platform=xueqiu').json()['total'] == 0
    assert client.post(endpoint + f"/authors/{author['id']}", json={'enabled': False}).status_code == 200
    assert client.post(endpoint + f"/authors/{author['id']}/delete", json={}).status_code == 200
    assert client.get(endpoint + '/posts').json()['total'] == 0


def test_baseline_new_posts_dedup_and_retry(social_client):
    _, service = social_client
    author = add(service)
    enable(service)
    service.collect(author, [post('1')])
    service.collect(author, [post('1'), post('2')])
    with service.db() as db:
        config = service.config(db)
    with patch.object(service, 'send_part', side_effect=RuntimeError('发送失败')):
        with pytest.raises(RuntimeError):
            service.deliver(config)
    assert service.overview()['counts'] == {'baseline': 1, 'pending': 1}
    with patch.object(service, 'send_part') as send, patch.object(service.stop_event, 'wait'):
        service.deliver(config)
        service.deliver(config)
        assert send.call_count == 1
    assert service.overview()['counts'] == {'baseline': 1, 'sent': 1}


def test_random_interval_and_fetch_failure_does_not_initialize(social_client):
    _, service = social_client
    author = add(service)
    enable(service)
    with clock('2026-10-25T12:00:00+08:00') as moment:
        with patch('app.services.social.fetch_posts', return_value=[post()]), patch('app.services.social.random.uniform', return_value=780) as jitter:
            service.cycle()
        jitter.assert_called_with(600, 960)
        assert service.overview()['interval_min'] == 600
        assert service.overview()['interval_max'] == 960
        with service.db() as db:
            assert service.config(db)['next_check'] == moment.timestamp() + 780
    service.add_author('xueqiu', '123', '雪球用户')
    with patch('app.services.social.fetch_posts', side_effect=RuntimeError('需要登录')):
        service.cycle()
    assert service.overview()['authors'][1]['initialized'] == 0
    assert service.overview()['authors'][1]['last_error'] == '需要登录'


def test_pause_and_resume_partial_message(social_client):
    _, service = social_client
    author = add(service)
    enable(service)
    service.collect(author, [])
    service.collect(author, [post(text='😀' * 4000)])
    with service.db() as db:
        db.execute('UPDATE posts SET next_part=1')
        config = service.config(db)
    service.change_author(author['id'], False)
    with patch.object(service, 'send_part') as send:
        service.deliver(config)
        send.assert_not_called()
    service.change_author(author['id'], True)
    with patch.object(service, 'send_part') as send, patch.object(service.stop_event, 'wait'):
        service.deliver(config)
        assert send.call_count == len(message_parts('weibo', post(text='😀' * 4000))) - 1
    assert service.overview()['counts']['sent'] == 1


def test_time_normalization_and_utf16_split():
    result = normalize_post('xueqiu', '123', {'id': 1, 'created_at': 0, 'text': '<p>正文<br>内容</p>'}, '博主')
    assert result['time'] == '1970-01-01T08:00:00+08:00'
    assert result['text'] == '正文\n内容'
    parts = message_parts('weibo', post(text='😀' * 4000))
    assert all(len(part.encode('utf-16-le')) // 2 <= 3500 for part in parts)


def test_telegram_errors_are_sanitized(social_client):
    _, service = social_client
    with patch('app.services.social.httpx.Client') as client:
        client.return_value.__enter__.return_value.post.side_effect = httpx.ConnectError('https://api.telegram.org/botSECRET/sendMessage')
        with pytest.raises(RuntimeError) as error:
            service.send_part('SECRET', '123', 'test')
        assert 'SECRET' not in str(error.value)


def test_weibo_nested_cards_long_text_and_foreign_recommendations():
    def handler(request):
        assert request.headers['cookie'] == 'own-cookie'
        if request.url.path.endswith('/extend'):
            return httpx.Response(200, json={'ok': 1, 'data': {'longTextContent': '<p>完整长文</p>'}})
        return httpx.Response(200, json={'ok': 1, 'data': {'cards': [{'card_group': [
            {'mblog': {'id': '2', 'user': {'id': 123}, 'created_at': 'Sun Oct 25 12:00:00 +0800 2026', 'text': '推荐'}},
            {'mblog': {'id': '1', 'user': {'id': 3962719063}, 'created_at': 'Sun Oct 25 12:00:00 +0800 2026', 'text': '摘要', 'isLongText': True}},
        ]}]}})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with patch('app.services.social_sources.httpx.Client', return_value=client):
        result = fetch_posts({'platform': 'weibo', 'uid': '3962719063', 'cookie': 'own-cookie'}, pages=1)
    assert len(result) == 1 and result[0]['text'] == '完整长文'
    assert result[0]['time'] == '2026-10-25T12:00:00+08:00'


def test_xueqiu_posts_and_login_challenge():
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        'statuses': [{'id': 10, 'user_id': 123, 'created_at': 0, 'text': '<p>雪球正文</p>'}]})))
    with patch('app.services.social_sources.httpx.Client', return_value=client):
        result = fetch_posts({'platform': 'xueqiu', 'uid': '123', 'cookie': 'own-cookie'}, pages=1)
    assert result[0]['text'] == '雪球正文' and result[0]['url'] == 'https://xueqiu.com/123/10'
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text='<html>登录验证</html>')))
    with patch('app.services.social_sources.httpx.Client', return_value=client):
        with pytest.raises(PlatformIntervention):
            fetch_posts({'platform': 'weibo', 'uid': '123', 'cookie': 'own-cookie'}, pages=1)


def test_current_xueqiu_timeline_schema_and_pagination():
    seen = []
    def handler(request):
        assert request.url.host == 'www.xueqiu.com'
        assert request.url.path == '/statuses/original/timeline.json'
        assert request.url.params['count'] == '10'
        assert request.headers['cookie'] == 'own-cookie'
        page = int(request.url.params['page'])
        seen.append(page)
        return httpx.Response(200, json={'list': [{
            'id': 11 - page, 'user_id': 123, 'created_at': 1792890000000,
            'description': '<p>当前接口正文</p>', 'title': '标题',
        }] if page == 1 else [], 'page': page, 'count': 10, 'maxPage': 1, 'total': 1})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with patch('app.services.social_sources.httpx.Client', return_value=client):
        result = fetch_posts({'platform': 'xueqiu', 'uid': '123', 'cookie': 'own-cookie',
                              '_initial': False, 'name': '博主备注'}, pages=3)
    assert seen == [1, 2]
    assert result.complete
    assert len(result) == 1
    assert result[0]['text'] == '标题\n当前接口正文'
    assert result[0]['name'] == '博主备注'
    assert result[0]['time'].startswith('2026-10-25T')


def test_daily_retention_boundary_pending_and_unknown_dates(social_client):
    _, service = social_client
    author = add(service)
    # Seed already persisted records, including ones an older version accepted.
    service.collect(author, [post(str(i)) for i in range(1, 6)])
    with service.db() as db:
        db.execute("UPDATE posts SET published_at='2024-10-24T23:59:59+08:00',status='pending' WHERE post_id='1'")
        db.execute("UPDATE posts SET published_at='2024-10-25T00:00:00+08:00' WHERE post_id='2'")
        db.execute("UPDATE posts SET published_at='未知',discovered_at='2024-10-24T12:00:00+08:00' WHERE post_id='3'")
        db.execute("UPDATE posts SET published_at='刚刚',discovered_at='2026-10-25T12:00:00+08:00' WHERE post_id='4'")
        # Same expired instant expressed in UTC.
        db.execute("UPDATE posts SET published_at='2024-10-24T15:59:59+00:00' WHERE post_id='5'")
    moment = datetime(2025, 10, 25, 10, tzinfo=CHINA)
    assert service.cleanup_posts(moment) == 3
    with service.db() as db:
        assert [row[0] for row in db.execute('SELECT post_id FROM posts ORDER BY id')] == ['2', '4']
    assert service.overview()['last_cleanup_date'] == '2025-10-25'
    assert service.overview()['last_cleanup_count'] == 3
    assert service.overview()['authors'][0]['uid'] == author['uid']
    restarted = SocialMonitor(service.settings)
    assert restarted.cleanup_posts(moment) == 0
    assert restarted.overview()['last_cleanup_count'] == 3
    assert restarted.cleanup_posts(datetime(2025, 10, 26, tzinfo=CHINA)) == 1


def test_cannot_start_without_enabled_author(social_client):
    client, service = social_client
    service.update_config({'token': 'test-token', 'chat_id': '123'})
    response = client.post('/api/v1/social-monitor/config', json={'enabled': True})
    assert response.status_code == 400
    assert '添加用户' in response.json()['detail']
    author = add(service)
    service.change_author(author['id'], False)
    assert client.post('/api/v1/social-monitor/config', json={'enabled': True}).status_code == 400
    service.change_author(author['id'], True)
    assert client.post('/api/v1/social-monitor/config', json={'enabled': True}).status_code == 200


def test_expired_posts_not_reinserted_or_notified(social_client):
    _, service = social_client
    author = add(service)
    service.collect(author, [])
    old = dict(post('99'), time='2024-10-24T23:59:59+08:00')
    with patch('app.services.social.now', return_value='2025-10-25T12:00:00+08:00'):
        service.collect(author, [old, dict(post('100'), time='2024-10-25T00:00:00+08:00')])
    items = service.posts(None, None, 1, 20)['items']
    assert len(items) == 1 and items[0]['post_id'] == '100'
    assert items[0]['status'] == 'pending'


def test_leap_day_retention():
    assert retention_cutoff(datetime(2024, 2, 29, 18, tzinfo=CHINA)) == datetime(2023, 2, 28, tzinfo=CHINA)


def test_night_rest_and_seven_am_resume(social_client):
    _, service = social_client
    add(service)
    enable(service)
    with clock('2026-10-26T00:00:00+08:00'):
        with patch('app.services.social.fetch_posts') as fetch, patch.object(service, 'send_part') as send:
            service.cycle()
            fetch.assert_not_called()
            send.assert_not_called()
        assert service.overview()['night_rest']
        assert service.overview()['next_check'].startswith('2026-10-26T07:00:00')
    with clock('2026-10-26T07:00:00+08:00'):
        with patch('app.services.social.fetch_posts', return_value=[]) as fetch:
            service.cycle()
            fetch.assert_called_once()
    assert next_check_time(datetime(2026, 10, 25, 23, 59, tzinfo=CHINA), 180) == datetime(2026, 10, 26, 7, tzinfo=CHINA).timestamp()


def test_intervention_pauses_platform_and_alerts_once_across_restart(social_client):
    client, service = social_client
    add(service)
    service.add_author('weibo', '123', '第二位博主')
    service.add_author('xueqiu', '456', '雪球博主')
    enable(service)
    with clock('2026-10-25T12:00:00+08:00'), patch.object(service, 'send_part') as send:
        with patch('app.services.social.fetch_posts', side_effect=[PlatformIntervention('需要验证码'), []]) as fetch:
            service.cycle()
            assert fetch.call_count == 2  # Second Weibo author is not requested.
        state = service.overview()['platforms']['weibo']
        assert state['paused'] and state['alert_sent_at']
        service.pause_platform('weibo', '重复错误')
        service.send_alerts()
        restarted = SocialMonitor(service.settings)
        with patch.object(restarted, 'send_part') as other_send:
            restarted.send_alerts()
            other_send.assert_not_called()
        assert send.call_count == 1
        # Cookie updates and master enable must not silently clear a safety pause.
        service.update_config({'weibo_cookie': 'replacement', 'enabled': True})
        assert service.overview()['platforms']['weibo']['paused']
        assert client.post('/api/v1/social-monitor/platforms/weibo/resume', json={}).status_code == 200
        assert not service.overview()['platforms']['weibo']['paused']


def test_failed_alert_is_persisted_and_not_retried_each_second(social_client):
    _, service = social_client
    add(service)
    enable(service)
    service.pause_platform('weibo', '登录失效')
    with patch.object(service, 'send_part', side_effect=RuntimeError('网络错误')) as send:
        service.send_alerts()
        service.send_alerts()
        assert send.call_count == 1
    service.platform_state('weibo', alert_next_try=0)
    with patch.object(service, 'send_part') as send:
        service.send_alerts()
        service.send_alerts()
        assert send.call_count == 1


@pytest.mark.parametrize('status_code,payload,expected', [
    (401, {}, PlatformIntervention), (403, {}, PlatformIntervention),
    (302, {}, PlatformIntervention), (429, {}, RateLimited),
    (200, {'ok': 0, 'msg': '请先登录'}, PlatformIntervention),
    (200, {'error_code': '21327'}, PlatformIntervention),
    (200, {'ok': 0, 'msg': '需要验证码'}, PlatformIntervention),
    (200, {'error_description': 'access denied'}, PlatformIntervention),
    (400, {'error_code': '400016'}, PlatformIntervention),
    (400, {'msg': '请重新登录'}, PlatformIntervention),
])
def test_platform_response_classification(status_code, payload, expected):
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status_code, json=payload, headers={'Retry-After': '900'})))
    with patch('app.services.social_sources.httpx.Client', return_value=client):
        with pytest.raises(expected) as error:
            fetch_posts({'platform': 'weibo', 'uid': '123'}, pages=1)
    if expected is RateLimited:
        assert error.value.retry_after == 900


def test_rate_limit_defers_all_authors_on_same_platform(social_client):
    _, service = social_client
    add(service)
    service.add_author('weibo', '123', '另一位')
    enable(service)
    with clock('2026-10-25T12:00:00+08:00'), patch('app.services.social.fetch_posts', side_effect=RateLimited(900)) as fetch:
        service.cycle()
        service.cycle()
        assert fetch.call_count == 1
    assert service.overview()['platforms']['weibo']['retry_at'] > time.time() + 890
    assert not service.overview()['platforms']['weibo'].get('paused')


def test_incremental_scan_ignores_pinned_known_posts_and_reuses_long_text():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        page = request.url.params['page']
        def item(identifier, hour, pinned=False):
            return {'mblog': {'id': identifier, 'user': {'id': 123}, 'text': f'内容 {identifier}',
                             'created_at': f'Sun Oct 25 {hour}:00:00 +0800 2026', 'isTop': pinned,
                             'isLongText': identifier == '100'}}
        cards = [item('100', '00', True), item('202', '06'), item('201', '05')] if page == '1' else [item('200', '01'), item('100', '00')]
        return httpx.Response(200, json={'ok': 1, 'data': {'cards': cards}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with patch('app.services.social_sources.httpx.Client', return_value=client):
        posts = fetch_posts({'platform': 'weibo', 'uid': '123', '_known_ids': {'100'}, '_initial': False,
                             '_scan_since': '2026-10-25T00:30:00+08:00'}, pages=20)
    assert [post['id'] for post in posts] == ['200', '201', '202']
    assert len(requests) == 2  # No repeated long-text request for the known pinned post.
    assert posts.complete


def test_morning_backfill_preserves_cursor_and_pushes_night_posts(social_client):
    _, service = social_client
    author = add(service)
    enable(service)
    service.collect(author, [dict(post('100'), time='2026-10-24T23:50:00+08:00')])
    with service.db() as db:
        db.execute("UPDATE authors SET last_success_at='2026-10-24T23:55:00+08:00' WHERE id=?", (author['id'],))

    def first_scan(source, pages):
        assert pages == 20
        assert source['_scan_since'] == '2026-10-24T23:55:00+08:00'
        source['_on_page']([dict(post('202'), time='2026-10-25T06:00:00+08:00')], 2, False)
        raise httpx.ConnectError('temporary failure')

    with clock('2026-10-25T07:00:00+08:00'), patch.object(service, 'send_part'), patch.object(service.stop_event, 'wait'):
        with patch('app.services.social.fetch_posts', side_effect=first_scan):
            service.cycle()
        assert service.overview()['authors'][0]['scan_page'] == 2
        service.platform_state('weibo', retry_at=0)

        def resume_scan(source, pages):
            assert source['_scan_page'] == 2
            assert source['_scan_since'] == '2026-10-24T23:55:00+08:00'
            return PostBatch([dict(post('201'), time='2026-10-25T01:00:00+08:00')], complete=True)

        with patch('app.services.social.fetch_posts', side_effect=resume_scan):
            service.cycle()
    assert service.overview()['counts'] == {'baseline': 1, 'sent': 2}
    assert service.overview()['authors'][0]['scan_page'] == 1


def test_pending_messages_do_not_send_at_night(social_client):
    _, service = social_client
    author = add(service)
    enable(service)
    service.collect(author, [])
    service.collect(author, [post()])
    with service.db() as db:
        config = service.config(db)
    with clock('2026-10-26T06:59:59+08:00'), patch.object(service, 'send_part') as send:
        service.deliver(config)
        send.assert_not_called()
    assert service.overview()['counts']['pending'] == 1


def test_new_author_added_at_night_gets_night_posts_not_older_history(social_client):
    _, service = social_client
    with clock('2026-10-25T02:00:00+08:00'):
        add(service)
        enable(service)
    overnight = [dict(post('201'), time='2026-10-25T01:00:00+08:00'),
                 dict(post('100'), time='2026-10-24T23:30:00+08:00')]
    with clock('2026-10-25T07:00:00+08:00'), patch.object(service, 'send_part') as send, patch.object(service.stop_event, 'wait'):
        with patch('app.services.social.fetch_posts', return_value=overnight) as fetch:
            service.cycle()
        source = fetch.call_args.args[0]
        assert not source['_initial'] and source['_stop_before'] == '2026-10-25T00:00:00+08:00'
        assert send.call_count == 1
        assert '201' in send.call_args.args[2]
    assert service.overview()['counts'] == {'baseline': 1, 'sent': 1}


def test_deep_backfill_resumes_without_stopping_at_newly_saved_known_posts():
    requested, cursors = [], []
    def handler(request):
        page = int(request.url.params['page'])
        requested.append(page)
        values = {1: [('202', '06')], 2: [('201', '05'), ('200', '01')], 3: [('100', '00')]}
        return httpx.Response(200, json={'ok': 1, 'data': {'cards': [
            {'mblog': {'id': identifier, 'user': {'id': 123}, 'text': '内容',
                       'created_at': f'Sun Oct 25 {hour}:00:00 +0800 2026'}}
            for identifier, hour in values[page]]}})
    source = {'platform': 'weibo', 'uid': '123', '_known_ids': {'202', '201', '100'},
              '_initial': False, '_scan_since': '2026-10-25T00:30:00+08:00', '_scan_page': 2,
              '_on_page': lambda posts, next_page, complete: cursors.append((next_page, complete))}
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with patch('app.services.social_sources.httpx.Client', return_value=client):
        result = fetch_posts(source, pages=2)
    assert requested == [1, 2] and not result.complete
    assert [item['id'] for item in result] == ['200']
    assert cursors[-1] == (3, False)
    requested.clear()
    source['_scan_page'] = 3
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with patch('app.services.social_sources.httpx.Client', return_value=client):
        result = fetch_posts(source, pages=2)
    assert requested == [1, 3] and result.complete


def test_content_mentioning_captcha_is_not_a_platform_error():
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'ok': 1, 'data': {'cards': [
        {'mblog': {'id': '1', 'user': {'id': 123}, 'text': '今天讨论验证码和登录失效'}}]}})))
    with patch('app.services.social_sources.httpx.Client', return_value=client):
        assert fetch_posts({'platform': 'weibo', 'uid': '123'}, pages=1)[0]['text'] == '今天讨论验证码和登录失效'


def test_existing_database_schema_upgrades_without_losing_authors(social_client):
    _, service = social_client
    service.update_config({'token': 'existing-token'})
    with service.db() as db:
        db.execute('DROP TABLE posts')
        db.execute('DROP TABLE authors')
        db.execute('CREATE TABLE authors (id INTEGER PRIMARY KEY, platform TEXT NOT NULL,uid TEXT NOT NULL,name TEXT NOT NULL,'
                   'enabled INTEGER NOT NULL DEFAULT 1,initialized INTEGER NOT NULL DEFAULT 0,last_check TEXT,last_error TEXT,UNIQUE(platform,uid))')
        db.execute("INSERT INTO authors(platform,uid,name) VALUES('weibo','123','原有博主')")
    restarted = SocialMonitor(service.settings)
    author = restarted.overview()['authors'][0]
    assert author['name'] == '原有博主' and author['scan_page'] == 1
    assert author['name_checked_at'] is None
    with restarted.db() as db:
        assert restarted.decode(restarted.config(db)['token']) == 'existing-token'


def test_long_retry_after_is_not_shortened():
    assert RateLimited(172800).retry_after == 172800


def test_user_pause_during_request_does_not_advance_backfill_cursor(social_client):
    _, service = social_client
    author = add(service)
    enable(service)

    def incoming_page(source, pages):
        service.change_author(author['id'], False)
        source['_on_page']([post()], 2, False)

    with patch('app.services.social.fetch_posts', side_effect=incoming_page):
        service.cycle()
    assert service.overview()['authors'][0]['scan_page'] == 1
    assert not service.overview()['platforms'].get('weibo', {}).get('paused')
    assert service.posts(None, None, 1, 20)['total'] == 0


def test_keyword_search_filters_all_saved_posts_with_pagination(social_client):
    client, service = social_client
    author = add(service)
    service.collect(author, [post('1', '新能源 STOCK'), post('2', '新能源第二条'), post('3', '其他行业'), post('4', '收益 10%_增长')])
    endpoint = '/api/v1/social-monitor/posts'
    first = client.get(endpoint, params={'keyword': '新能源', 'size': 1}).json()
    second = client.get(endpoint, params={'keyword': '新能源', 'size': 1, 'page': 2}).json()
    assert first['total'] == second['total'] == 2
    assert first['items'][0]['post_id'] != second['items'][0]['post_id']
    assert client.get(endpoint, params={'keyword': 'stock'}).json()['total'] == 1
    assert client.get(endpoint, params={'keyword': '%_'}).json()['total'] == 1
    assert client.get(endpoint, params={'keyword': "' OR 1=1 --"}).json()['total'] == 0
    assert client.get(endpoint, params={'keyword': '新能源', 'platform': 'xueqiu'}).json()['total'] == 0
    assert client.get(endpoint, params={'keyword': '   '}).json()['total'] == 4


def test_author_names_persist_and_existing_post_names_are_backfilled(social_client):
    _, service = social_client
    service.add_author('weibo', '123', '自定义备注')
    author = service.overview()['authors'][0]
    service.collect(author, [dict(post(), name='平台真实昵称')])
    assert service.overview()['authors'][0]['screen_name'] == '平台真实昵称'
    assert service.overview()['authors'][0]['name'] == '自定义备注'
    service.update_author_name(author['id'], '改名后的昵称')
    assert service.posts(None, None, 1, 20)['items'][0]['author_name'] == '改名后的昵称'
    with service.db() as db:
        db.execute('UPDATE authors SET screen_name=NULL')
    restarted = SocialMonitor(service.settings)
    assert restarted.overview()['authors'][0]['screen_name'] == '平台真实昵称'


def test_known_posts_refresh_author_name_without_long_text_request():
    names = []
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'ok': 1, 'data': {'cards': [
        {'mblog': {'id': '1', 'user': {'id': 123, 'screen_name': '最新昵称'}, 'isLongText': True, 'text': '摘要'}}]}})))
    with patch('app.services.social_sources.httpx.Client', return_value=client):
        assert not fetch_posts({'platform': 'weibo', 'uid': '123', '_known_ids': {'1'}, '_on_author_name': names.append}, pages=1)
    assert names == ['最新昵称']


@pytest.mark.parametrize('payload,code', [
    ({'msg': '请输入验证码'}, 'captcha_required'),
    ({'msg': '请重新登录 token=must-not-be-returned'}, 'login_expired'),
    ({'error_code': '21327'}, 'login_expired'),
    ({'error_description': 'access denied'}, 'access_denied'),
])
def test_distinct_pause_reasons_do_not_echo_platform_secrets(payload, code):
    with pytest.raises(PlatformIntervention) as captured:
        check_payload(payload)
    assert captured.value.reason_code == code
    assert 'must-not-be-returned' not in str(captured.value)


def test_pause_details_are_exposed_in_status_and_survive_restart(social_client):
    client, service = social_client
    add(service)
    enable(service)
    failure = PlatformIntervention('平台拒绝访问（HTTP 403），请检查账号权限', 'access_denied', 403)
    with patch('app.services.social.fetch_posts', side_effect=failure), patch.object(service, 'send_part'):
        service.cycle()
    state = client.get('/api/v1/social-monitor/status').json()['platforms']['weibo']
    assert state['reason'] == str(failure)
    assert state['reason_code'] == 'access_denied' and state['http_status'] == 403
    assert state['paused_at']
    restarted = SocialMonitor(service.settings)
    assert restarted.overview()['platforms']['weibo']['reason'] == str(failure)
    restarted.resume_platform('weibo')
    assert restarted.overview()['platforms']['weibo']['reason'] is None


@pytest.mark.parametrize('failure,expected', [
    (httpx.ConnectError('secret-cookie-value'), '无法连接平台'),
    (httpx.ReadTimeout('secret-cookie-value'), '平台请求超时'),
])
def test_network_backoff_keeps_a_safe_visible_reason(social_client, failure, expected):
    _, service = social_client
    add(service)
    enable(service)
    with patch('app.services.social.fetch_posts', side_effect=failure):
        service.cycle()
    state = service.overview()['platforms']['weibo']
    assert expected in state['reason'] and 'secret-cookie-value' not in state['reason']
    assert state['reason_code'] == 'fetch_failed' and state['retry_at']
    service.platform_state('weibo', retry_at=0)
    with patch('app.services.social.fetch_posts', return_value=[]):
        service.cycle()
    assert service.overview()['platforms']['weibo']['reason'] is None


@pytest.mark.parametrize('profile,expected', [
    ({'id': 123, 'screen_name': '  雪球真实昵称  '}, '雪球真实昵称'),
    ({'id': '123', 'screen_name': '雪球真实昵称'}, '雪球真实昵称'),
    ({'id': 456, 'screen_name': '其他博主'}, None),
    ({'id': 123, 'screen_name': '123'}, None),
    ({'id': 123, 'screen_name': '   '}, None),
])
def test_current_xueqiu_profile_endpoint_checks_target_identity(profile, expected):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == 'GET'
        assert request.url.host == 'www.xueqiu.com'
        assert request.url.path == '/user/show.json'
        assert dict(request.url.params) == {'id': '123'}
        return httpx.Response(200, json=profile)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_xueqiu_author_name(client, '123') == expected
    assert len(requests) == 1


def test_xueqiu_profile_biography_is_content_not_a_login_error():
    profile = {'id': 123, 'screen_name': '雪球昵称',
               'description': '讨论验证码、登录失效和 captcha / login required 的文章作者'}
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=profile))) as client:
        assert fetch_xueqiu_author_name(client, '123') == '雪球昵称'


@pytest.mark.parametrize('lookup_due,expected_name', [(True, '资料中的昵称'), (False, '缓存中的昵称')])
def test_xueqiu_posts_without_user_use_profile_or_cached_name(lookup_due, expected_name):
    paths, names = [], []

    def handler(request):
        paths.append(request.url.path)
        assert request.headers['cookie'] == 'test-cookie'
        if request.url.path == '/user/show.json':
            return httpx.Response(200, json={'id': 123, 'screen_name': '资料中的昵称'})
        assert request.url.path == '/statuses/original/timeline.json'
        return httpx.Response(200, json={'list': [
            {'id': 10, 'user_id': 123, 'created_at': 1792890000000, 'text': '没有 user 字段的正文'}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = {'platform': 'xueqiu', 'uid': '123', 'cookie': 'test-cookie',
              'name': '人工备注', 'screen_name': '缓存中的昵称', '_profile_lookup_due': lookup_due,
              '_on_author_name': names.append}
    with patch('app.services.social_sources.httpx.Client', return_value=client):
        result = fetch_posts(source, pages=1)
    assert result[0]['name'] == expected_name
    assert result[0]['text'] == '没有 user 字段的正文'
    assert names == (['资料中的昵称'] if lookup_due else [])
    assert paths == (['/user/show.json'] if lookup_due else []) + ['/statuses/original/timeline.json']


@pytest.mark.parametrize('lookup_due', [True, False])
def test_known_xueqiu_posts_still_refresh_author_name(lookup_due):
    names = []

    def handler(request):
        if request.url.path == '/user/show.json':
            return httpx.Response(200, json={'id': 123, 'screen_name': '最新昵称'})
        item = {'id': 10, 'user_id': 123, 'created_at': 1792890000000, 'text': '已入库正文'}
        if not lookup_due:
            item['user'] = {'id': 123, 'screen_name': '最新昵称'}
        return httpx.Response(200, json={'list': [item]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with patch('app.services.social_sources.httpx.Client', return_value=client):
        result = fetch_posts({'platform': 'xueqiu', 'uid': '123', 'cookie': 'test-cookie',
                              '_known_ids': {'10'}, '_profile_lookup_due': lookup_due,
                              '_on_author_name': names.append}, pages=1)
    assert result == [] and result.complete
    assert names == ['最新昵称']


@pytest.mark.parametrize('status,payload,reason', [
    (403, {}, 'access_denied'),
    (200, {'error_code': 'captcha', 'error_description': 'captcha required'}, 'captcha_required'),
    (500, {'message': '需要验证码'}, 'captcha_required'),
])
def test_xueqiu_profile_intervention_stops_before_timeline(status, payload, reason):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(status, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with patch('app.services.social_sources.httpx.Client', return_value=client):
        with pytest.raises(PlatformIntervention) as captured:
            fetch_posts({'platform': 'xueqiu', 'uid': '123', 'cookie': 'test-cookie',
                         '_profile_lookup_due': True}, pages=1)
    assert captured.value.reason_code == reason
    assert paths == ['/user/show.json']


@pytest.mark.parametrize('failure', ['http_500', 'connect', 'timeout'])
def test_temporary_xueqiu_profile_failure_preserves_readable_posts(failure):
    paths, names = [], []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == '/user/show.json':
            if failure == 'connect':
                raise httpx.ConnectError('temporary connection failure', request=request)
            if failure == 'timeout':
                raise httpx.ReadTimeout('temporary timeout', request=request)
            return httpx.Response(500, json={'message': 'temporary service failure'})
        return httpx.Response(200, json={'list': [{'id': 10, 'user_id': 123, 'text': '正常正文'}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with patch('app.services.social_sources.httpx.Client', return_value=client):
        result = fetch_posts({'platform': 'xueqiu', 'uid': '123', 'cookie': 'test-cookie',
                              'screen_name': '缓存昵称', '_profile_lookup_due': True,
                              '_on_author_name': names.append}, pages=1)
    assert result[0]['text'] == '正常正文' and result[0]['name'] == '缓存昵称'
    assert names == []
    assert paths == ['/user/show.json', '/statuses/original/timeline.json']


def test_xueqiu_profile_name_cache_lasts_24_hours_across_restart(social_client):
    _, service = social_client
    service.add_author('xueqiu', '123', '人工备注')
    service.update_config({'token': 'test-token', 'chat_id': '123', 'xueqiu_cookie': 'test-cookie', 'enabled': True})
    paths = []
    original_client = httpx.Client

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == '/user/show.json':
            assert dict(request.url.params) == {'id': '123'}
            return httpx.Response(200, json={'id': 123, 'screen_name': '持久化昵称'})
        return httpx.Response(200, json={'list': []})

    def client_factory(**kwargs):
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    with patch('app.services.social_sources.httpx.Client', side_effect=client_factory), \
            patch.object(service.stop_event, 'wait', return_value=False):
        service.cycle()
    author = service.overview()['authors'][0]
    assert author['screen_name'] == '持久化昵称'
    assert author['name_checked_at'] == '2026-10-25T12:00:00+08:00'

    restarted = SocialMonitor(service.settings)
    with patch('app.services.social_sources.httpx.Client', side_effect=client_factory), \
            patch.object(restarted.stop_event, 'wait', return_value=False):
        with clock('2026-10-25T12:15:00+08:00'):
            restarted.cycle()
        with clock('2026-10-26T11:59:59+08:00'):
            restarted.cycle()
        assert paths.count('/user/show.json') == 1
        with clock('2026-10-26T12:00:00+08:00'):
            restarted.cycle()
    assert paths.count('/user/show.json') == 2
    assert paths.count('/statuses/original/timeline.json') == 4
    assert restarted.overview()['authors'][0]['name_checked_at'] == '2026-10-26T12:00:00+08:00'


def test_author_name_refresh_repairs_uid_payloads_without_overwriting_history_or_remarks(social_client):
    _, service = social_client
    service.add_author('xueqiu', '123', '人工备注')
    author = service.overview()['authors'][0]
    missing_name = post('4')
    missing_name.pop('name')
    service.collect(author, [dict(post('1'), name='123'), dict(post('2'), name=''),
                             dict(post('3'), name='以前的有效昵称'), missing_name])
    service.update_author_name(author['id'], '  当前真实昵称  ')
    refreshed = service.overview()['authors'][0]
    assert refreshed['screen_name'] == '当前真实昵称'
    assert refreshed['name'] == '人工备注'
    assert refreshed['name_checked_at'] == '2026-10-25T12:00:00+08:00'
    rows = {row['post_id']: row for row in service.posts(None, None, 1, 20)['items']}
    assert {identifier: row['payload']['name'] for identifier, row in rows.items()} == {
        '1': '当前真实昵称', '2': '当前真实昵称', '3': '以前的有效昵称', '4': '当前真实昵称'}
    assert all(row['author_name'] == '当前真实昵称' for row in rows.values())
    for invalid_name in ('123', '', '   ', None):
        service.update_author_name(author['id'], invalid_name)
    assert service.overview()['authors'][0]['screen_name'] == '当前真实昵称'
