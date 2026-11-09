"""Resume offset feeds without dropping new pages or half-committing checkpoints."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import sqlite3
from unittest.mock import patch

import httpx
import pytest

from app.core.config import Settings
from app.services.social import SocialMonitor
from app.services.social_sources import fetch_posts, MonitoringDeferred, normalize_post
from test_social import clock


CLIENT = httpx.Client
SINCE = '2026-10-25T09:00:00+08:00'


def item(identifier, platform='weibo', **changes):
    moment = datetime.fromisoformat(SINCE) + timedelta(minutes=int(identifier))
    value = {'id': str(identifier), 'text': f'post-{identifier}',
             'user': {'id': 123}, 'created_at': (
                 moment.strftime('%a %b %d %H:%M:%S %z %Y') if platform == 'weibo'
                 else int(moment.timestamp() * 1000))}
    return dict(value, **changes)


@contextmanager
def feed_client(values, requested, platform='weibo', before_request=None):
    def handler(request):
        page = int(request.url.params['page'])
        requested.append(page)
        if before_request:
            before_request(page)
        rows = values.get(page, [])
        if isinstance(rows, Exception):
            raise rows
        payload = {'ok': 1, 'data': {'cards': [{'mblog': row} for row in rows]}} if platform == 'weibo' else {'list': rows}
        return httpx.Response(200, json=payload)

    with patch('app.services.social_sources.httpx.Client',
               side_effect=lambda **kwargs: CLIENT(transport=httpx.MockTransport(handler), **kwargs)):
        yield


def source(platform='weibo', **changes):
    return dict({'platform': platform, 'uid': '123', 'cookie': 'test-cookie',
                 '_initial': False, '_scan_since': SINCE, '_scan_page': 5,
                 '_known_ids': {'50', '40', '30', '20', '0'}}, **changes)


@pytest.mark.parametrize('platform', ['weibo', 'xueqiu'])
def test_resume_walks_all_new_front_pages_before_old_cursor(platform):
    values = {page: [item(identifier, platform)] for page, identifier in
              enumerate([70, 60, 50, 40, 30, 20, 10, 0], 1)}
    requested, checkpoints = [], []
    with feed_client(values, requested, platform):
        result = fetch_posts(source(platform, _on_page=lambda *args: checkpoints.append(args)), pages=20)
    assert requested == [1, 2, 3, 5, 6, 7, 8]
    assert {row['id'] for row in result} == {'70', '60', '10'}
    assert result.complete
    assert [entry[1:] for entry in checkpoints[:3]] == [(2, False), (3, False), (5, False)]
    assert checkpoints[-1][1:] == (1, True)


@pytest.mark.parametrize('first_budget', [1, 2])
def test_another_interruption_before_overlap_saves_only_contiguous_front(first_budget):
    values = {page: [item(identifier)] for page, identifier in
              enumerate([70, 60, 50, 40, 30, 20, 10, 0], 1)}
    requested, checkpoints = [], []
    initial_source = source(_on_page=lambda *args: checkpoints.append(args))
    with feed_client(values, requested):
        first = fetch_posts(initial_source, pages=first_budget)
    assert not first.complete
    assert checkpoints[-1][1:] == (first_budget + 1, False)
    assert requested == list(range(1, first_budget + 1))
    requested.clear()
    # Newly saved front rows become known on restart. Keeping cursor 5 would
    # let them skip the still-unread rest of the new front pages.
    restarted = dict(initial_source, _scan_page=checkpoints[-1][1],
                     _known_ids=initial_source['_known_ids'] | {row['id'] for row in first})
    with feed_client(values, requested):
        second = fetch_posts(restarted, pages=20)
    assert second.complete
    assert {'70', '60', '10'} <= {row['id'] for row in [*first, *second]}
    assert first_budget + 1 in requested


@pytest.mark.parametrize('special', [
    item(0, isTop=True),
    item(0, user={'id': 999}),
    item(0, isTop=True, created_at='Sun Oct 25 08:00:00 +0800 2025'),
])
def test_pinned_and_foreign_known_rows_do_not_skip_new_pages(special):
    requested = []
    values = {1: [special, item(70)], 2: [item(60)], 3: [item(50)], 5: [item(0)]}
    with feed_client(values, requested):
        result = fetch_posts(source(_retention_cutoff='2025-10-26T00:00:00+08:00'), pages=20)
    assert requested == [1, 2, 3, 5]
    assert {row['id'] for row in result} == {'70', '60'}
    assert result.complete


@pytest.mark.parametrize('changes', [
    {'_known_ids': {'0'}},
    {'_known_ids': set(), '_stop_before': '2026-10-25T09:01:00+08:00'},
    {'_known_ids': set(), '_retention_cutoff': '2026-10-25T09:01:00+08:00'},
])
def test_real_boundaries_finish_after_resume_is_reached(changes):
    requested, checkpoints = [], []
    values = {1: [item(20)], 2: [item(10)], 3: [item(0)]}
    with feed_client(values, requested):
        result = fetch_posts(source(_scan_page=2, _on_page=lambda *args: checkpoints.append(args), **changes), pages=20)
    assert requested == [1, 2, 3]
    assert result.complete and checkpoints[-1][1:] == (1, True)


def test_empty_history_finishes_even_before_old_cursor():
    requested, checkpoints = [], []
    with feed_client({}, requested):
        result = fetch_posts(source(_on_page=lambda *args: checkpoints.append(args)), pages=20)
    assert requested == [1] and result.complete
    assert checkpoints == [([], 1, True)]


def test_initial_interrupted_scan_does_not_finish_before_catching_saved_prefix():
    requested, checkpoints = [], []
    with feed_client({1: [item(70)], 2: [item(60)]}, requested):
        result = fetch_posts(source(_initial=True, _on_page=lambda *args: checkpoints.append(args)), pages=2)
    assert not result.complete and checkpoints[-1][1:] == (3, False)


@pytest.fixture
def monitor(tmp_path):
    with clock('2026-10-25T12:00:00+08:00'):
        service = SocialMonitor(Settings(_env_file=None, log_dir=tmp_path, social_worker_enabled=False))
        service.add_author('weibo', '123', 'Test author')
        service.update_config({'enabled': True, 'token': 'test-token', 'chat_id': '123'})
        service.collect(service.overview()['authors'][0], [normalize_post('weibo', '123', item(0), '')])
        with service.db() as db:
            db.execute('UPDATE authors SET last_success_at=?', (SINCE,))
        with patch.object(service.stop_event, 'wait', return_value=False):
            yield service


def check(service):
    with service.db() as db:
        config = service.config(db)
    service.check_author(service.overview()['authors'][0], config)


def saved_ids(service):
    with service.db() as db:
        return {row[0] for row in db.execute('SELECT post_id FROM posts')}


def interrupt_at_deep_cursor(service):
    requested = []
    values = {page: [item(identifier)] for page, identifier in enumerate([50, 40, 30, 20], 1)}
    values[5] = httpx.ConnectError('interrupted deep page')
    with feed_client(values, requested), pytest.raises(httpx.ConnectError):
        check(service)
    assert requested == [1, 2, 3, 4, 5]
    author = service.overview()['authors'][0]
    assert author['scan_page'] == 5 and author['scan_since'] == SINCE
    assert author['last_success_at'] == SINCE
    return author


def test_repeated_restart_preserves_all_posts_and_delays_delivery_until_complete(monitor):
    interrupt_at_deep_cursor(monitor)
    requested = []
    values = {page: [item(identifier)] for page, identifier in
              enumerate([70, 60, 50, 40, 30, 20, 10, 0], 1)}
    interrupted = dict(values)
    interrupted[2] = httpx.ReadTimeout('interrupted new front')
    with feed_client(interrupted, requested), pytest.raises(httpx.ReadTimeout):
        check(monitor)
    assert requested == [1, 2]
    assert monitor.overview()['authors'][0]['scan_page'] == 2
    assert saved_ids(monitor) == {'70', '50', '40', '30', '20', '0'}

    restarted = SocialMonitor(monitor.settings)
    with restarted.db() as db:
        config = restarted.config(db)
    with patch.object(restarted, 'send_part') as send:
        restarted.deliver_notifications(config)
        send.assert_not_called()
    requested.clear()

    def database_is_unlocked(_page):
        # Another process must be able to pause authors or save unrelated work
        # during network I/O, without waiting for SQLite's write lock.
        with sqlite3.connect(restarted.root / 'monitor.sqlite3', timeout=0) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE authors SET name=name')

    with patch.object(restarted.stop_event, 'wait', return_value=False), \
            feed_client(values, requested, before_request=database_is_unlocked):
        check(restarted)
    assert requested == list(range(1, 9))
    assert saved_ids(restarted) == {'70', '60', '50', '40', '30', '20', '10', '0'}
    author = restarted.overview()['authors'][0]
    assert author['scan_page'] == 1 and author['scan_since'] is None
    assert author['last_success_at'] != SINCE
    with patch.object(restarted, 'send_part') as send, \
            patch.object(restarted.stop_event, 'wait', return_value=False):
        restarted.deliver_notifications(config)
        restarted.deliver_notifications(config)
    assert send.call_count == 7
    assert [int(call.args[2].rsplit('/', 1)[-1]) for call in send.call_args_list] == [10, 20, 30, 40, 50, 60, 70]


def test_failed_cursor_write_rolls_back_page_posts_and_survives_restart(monitor):
    interrupt_at_deep_cursor(monitor)
    with monitor.db() as db:
        db.execute("CREATE TRIGGER reject_checkpoint BEFORE UPDATE OF scan_page ON authors "
                   "BEGIN SELECT RAISE(ABORT, 'checkpoint unavailable'); END")
    requested = []
    with feed_client({1: [item(70)]}, requested), pytest.raises(sqlite3.IntegrityError, match='checkpoint unavailable'):
        check(monitor)
    assert requested == [1]
    assert saved_ids(monitor) == {'50', '40', '30', '20', '0'}
    restarted = SocialMonitor(monitor.settings)
    assert restarted.overview()['authors'][0]['scan_page'] == 5
    assert restarted.overview()['authors'][0]['scan_since'] == SINCE
    with restarted.db() as db:
        db.execute('DROP TRIGGER reject_checkpoint')
    values = {page: [item(identifier)] for page, identifier in
              enumerate([70, 60, 50, 40, 30, 20, 10, 0], 1)}
    with patch.object(restarted.stop_event, 'wait', return_value=False), feed_client(values, []):
        check(restarted)
    assert saved_ids(restarted) == {'70', '60', '50', '40', '30', '20', '10', '0'}


def test_pause_during_resumed_page_leaves_posts_and_checkpoint_unchanged(monitor):
    author = interrupt_at_deep_cursor(monitor)
    with feed_client({1: [item(70)]}, [], before_request=lambda _page: monitor.change_author(author['id'], False)), \
            pytest.raises(MonitoringDeferred):
        check(monitor)
    assert saved_ids(monitor) == {'50', '40', '30', '20', '0'}
    author = monitor.overview()['authors'][0]
    assert author['scan_page'] == 5 and author['scan_since'] == SINCE
