"""Delivery follows event time, including resumed scans and mixed event types."""
from contextlib import contextmanager
from datetime import datetime
import json
from unittest.mock import patch

import httpx
import pytest

from app.core.config import Settings
from app.services.social import SocialMonitor
from app.services.social_cube_sources import cube_message_parts
from app.services.social_sources import PostBatch, message_parts


@contextmanager
def clock(value):
    fixed = datetime.fromisoformat(value)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    with patch('app.services.social.datetime', FixedDateTime), \
            patch('app.services.social.time.time', return_value=fixed.timestamp()):
        yield


@pytest.fixture
def monitor(tmp_path):
    settings = Settings(_env_file=None, log_dir=tmp_path, social_worker_enabled=False)
    with clock('2026-10-26T09:00:00+08:00'):
        service = SocialMonitor(settings)
        service.add_author('weibo', '123', 'Test author')
        service.add_cube('ZH123', 'Test cube')
        service.update_config({'token': 'test-token', 'chat_id': '123', 'enabled': True})
        author = service.overview()['authors'][0]
        service.collect(author, [])
        with service.db() as db:
            db.execute("UPDATE cubes SET initialized=1,baseline_at='2026-10-26T09:00:00+08:00'")
    with clock('2026-10-26T12:45:00+08:00'), \
            patch.object(service, 'send_part') as send, \
            patch.object(service.stop_event, 'wait', return_value=False):
        yield service, send


def config(service):
    with service.db() as db:
        return service.config(db)


def post(identifier, time, text):
    return {'id': str(identifier), 'name': 'Test author', 'time': time,
            'text': text, 'url': f'https://m.weibo.cn/detail/{identifier}'}


def rebalance(identifier, time, comment):
    return {'id': str(identifier), 'status': 'success', 'category': 'user_rebalancing',
            'time': time, 'updated_at': time, 'comment': comment,
            'changes': [{'symbol': 'SH600000', 'name': 'Test stock', 'price': 10,
                         'before': 0, 'after': 10, 'action': '买入'}],
            'url': 'https://www.xueqiu.com/P/ZH123'}


def test_latest_page_is_saved_first_but_posts_are_sent_by_publication_time(monitor):
    service, send = monitor
    author = service.overview()['authors'][0]
    # Deliberately make API order, local IDs, and platform IDs differ from time order.
    newest = post('100', '2026-10-26T11:30:00+08:00', 'newest-post')
    middle = post('300', '2026-10-26T11:00:00+08:00', 'middle-post')
    oldest = post('200', '2026-10-26T10:30:00+08:00', 'oldest-post')

    def fetch(source, pages):
        source['_on_page']([newest, middle], 2, False)
        source['_on_page']([oldest], 1, True)
        return PostBatch([newest, oldest, middle])

    with patch('app.services.social.fetch_posts', side_effect=fetch):
        service.check_author(author, config(service))
    service.deliver(config(service))
    assert send.call_count == 3
    assert ['oldest-post', 'middle-post', 'newest-post'] == [
        next(label for label in ('oldest-post', 'middle-post', 'newest-post')
             if label in call.args[2]) for call in send.call_args_list]


def test_publication_offsets_and_unknown_time_use_actual_instants(monitor):
    service, send = monitor
    author = service.overview()['authors'][0]
    service.collect(author, [
        post('1', '2026-10-26T02:20:00+00:00', 'later-utc-post'),
        post('2', '2026-10-26T10:05:00+08:00', 'earlier-china-post'),
        post('3', 'unknown', 'unknown-time-post'),
    ])
    service.deliver(config(service))
    assert send.call_count == 3
    assert 'earlier-china-post' in send.call_args_list[0].args[2]
    assert 'later-utc-post' in send.call_args_list[1].args[2]
    assert 'unknown-time-post' in send.call_args_list[2].args[2]


def test_incomplete_author_scan_waits_for_older_pages_without_blocking_other_authors(monitor):
    service, send = monitor
    author = service.overview()['authors'][0]
    newer = post('201', '2026-10-26T11:30:00+08:00', 'newer-resumed-post')
    older = post('101', '2026-10-26T10:00:00+08:00', 'older-resumed-post')

    def failed_scan(source, pages):
        source['_on_page']([newer], 2, False)
        raise httpx.ConnectError('simulated interrupted page')

    with patch('app.services.social.fetch_posts', side_effect=failed_scan), \
            pytest.raises(httpx.ConnectError):
        service.check_author(author, config(service))
    service.deliver(config(service))
    send.assert_not_called()

    service.add_author('weibo', '456', 'Other author')
    other = service.overview()['authors'][1]
    service.collect(other, [], baseline=False)
    service.collect(other, [post('401', '2026-10-26T11:00:00+08:00', 'independent-post')])
    service.deliver_notifications(config(service))
    send.assert_called_once()
    assert 'independent-post' in send.call_args.args[2]

    restarted = SocialMonitor(service.settings)
    resumed_author = restarted.overview()['authors'][0]

    def resume(source, pages):
        assert source['_scan_page'] == 2
        source['_on_page']([older], 1, True)
        return PostBatch([older])

    with patch('app.services.social.fetch_posts', side_effect=resume), \
            patch.object(restarted, 'send_part') as resumed_send, \
            patch.object(restarted.stop_event, 'wait', return_value=False):
        restarted.check_author(resumed_author, config(restarted))
        restarted.deliver_notifications(config(restarted))
    assert resumed_send.call_count == 2
    assert 'older-resumed-post' in resumed_send.call_args_list[0].args[2]
    assert 'newer-resumed-post' in resumed_send.call_args_list[1].args[2]


def test_incomplete_cube_scan_waits_across_restart_until_older_events_arrive(monitor):
    service, send = monitor
    cube = service.overview()['cubes'][0]
    newer = rebalance('202', '2026-10-26T11:30:00+08:00', 'newer-resumed-cube')
    older = rebalance('101', '2026-10-26T10:00:00+08:00', 'older-resumed-cube')

    def interrupted(source, pages):
        source['_on_page']([newer], 2, False)
        raise httpx.ConnectError('simulated interrupted cube page')

    with pytest.raises(httpx.ConnectError):
        service.check_cube(cube, config(service), interrupted)
    service.deliver_cubes(config(service))
    service.deliver_notifications(config(service))
    send.assert_not_called()

    restarted = SocialMonitor(service.settings)

    def resume(source, pages):
        assert source['_scan_page'] == 2
        source['_on_page']([older], 1, True)
        return PostBatch([older])

    with patch.object(restarted, 'send_part') as resumed_send, \
            patch.object(restarted.stop_event, 'wait', return_value=False):
        restarted.check_cube(restarted.overview()['cubes'][0], config(restarted), resume)
        restarted.deliver_notifications(config(restarted))
    assert resumed_send.call_count == 2
    assert 'older-resumed-cube' in resumed_send.call_args_list[0].args[2]
    assert 'newer-resumed-cube' in resumed_send.call_args_list[1].args[2]


def test_pending_blog_and_cube_notifications_share_one_time_order(monitor):
    service, send = monitor
    author = service.overview()['authors'][0]
    cube = service.overview()['cubes'][0]
    service.collect(author, [
        post('1', '2026-10-26T11:30:00+08:00', 'last-blog'),
        post('2', '2026-10-26T10:30:00+08:00', 'first-blog'),
    ])
    service.collect_cube(cube, [rebalance('1', '2026-10-26T03:00:00+00:00', 'middle-cube')])
    service.deliver_notifications(config(service))
    assert send.call_count == 3
    for call, label in zip(send.call_args_list, ('first-blog', 'middle-cube', 'last-blog')):
        assert label in call.args[2]


def test_failed_multipart_notification_finishes_before_later_messages(monitor):
    service, _ = monitor
    author = service.overview()['authors'][0]
    cube = service.overview()['cubes'][0]
    long_post = post('1', '2026-10-26T10:00:00+08:00', 'old-long-post ' + 'x' * 8000)
    later_post = post('2', '2026-10-26T10:01:00+08:00', 'last-short-post')
    parts = message_parts('weibo', long_post)
    service.collect(author, [later_post, long_post])
    service.collect_cube(cube, [rebalance('1', '2026-10-26T10:00:30+08:00', 'middle-cube')])
    with patch.object(service, 'send_part', side_effect=[None, RuntimeError('simulated send failure')]) as first_send:
        with pytest.raises(RuntimeError, match='simulated send failure'):
            service.deliver_notifications(config(service))
    assert [call.args[2] for call in first_send.call_args_list] == parts[:2]
    with service.db() as db:
        first = db.execute("SELECT status,next_part FROM posts WHERE post_id='1'").fetchone()
        assert tuple(first) == ('pending', 1)
        assert db.execute("SELECT status FROM posts WHERE post_id='2'").fetchone()[0] == 'pending'
        assert db.execute('SELECT notify_status FROM cube_rebalances').fetchone()[0] == 'pending'
    with patch.object(service, 'send_part') as retried:
        service.deliver_notifications(config(service))
    sent = [call.args[2] for call in retried.call_args_list]
    assert sent[:len(parts) - 1] == parts[1:]
    assert len(sent) == len(parts) + 1
    assert 'middle-cube' in sent[-2]
    assert 'last-short-post' in sent[-1]


def test_cube_multipart_retry_keeps_original_metadata_and_part_boundaries(monitor):
    service, _ = monitor
    cube = service.overview()['cubes'][0]
    service.update_cube_metadata(cube, {'cube_name': 'Original portfolio',
                                        'owner_name': 'Original manager', 'owner_id': '456'})
    original_cube = service.overview()['cubes'][0]
    long_event = rebalance('1', '2026-10-26T10:00:00+08:00', 'original-long-event ' + 'x' * 10000)
    original_parts = cube_message_parts(original_cube, long_event)
    assert len(original_parts) > 2
    service.collect_cube(original_cube, [long_event])
    with patch.object(service, 'send_part', side_effect=[None, RuntimeError('interrupted second part')]) as initial:
        with pytest.raises(RuntimeError, match='interrupted second part'):
            service.deliver_notifications(config(service))
    assert [call.args[2] for call in initial.call_args_list] == original_parts[:2]
    with service.db() as db:
        row = db.execute('SELECT payload,next_part,notify_status FROM cube_rebalances').fetchone()
        assert row['next_part'] == 1 and row['notify_status'] == 'pending'
        snapshot = json.loads(row['payload'])['_notification_cube']
        assert snapshot['cube_name'] == 'Original portfolio'
        assert snapshot['owner_name'] == 'Original manager'

    # Refresh both names before retry; their different lengths would shift every
    # following part if the heading were rebuilt from the current cube row.
    service.update_cube_metadata(cube, {'cube_name': 'New portfolio ' + 'n' * 80,
                                        'owner_name': 'New manager ' + 'm' * 80, 'owner_id': '456'})
    changed_cube = service.overview()['cubes'][0]
    assert cube_message_parts(changed_cube, long_event) != original_parts
    service.collect_cube(changed_cube, [long_event])

    restarted = SocialMonitor(service.settings)
    with patch.object(restarted, 'send_part') as retried, \
            patch.object(restarted.stop_event, 'wait', return_value=False):
        restarted.deliver_notifications(config(restarted))
    assert [call.args[2] for call in retried.call_args_list] == original_parts[1:]
    with restarted.db() as db:
        assert db.execute('SELECT notify_status FROM cube_rebalances').fetchone()[0] == 'sent'
