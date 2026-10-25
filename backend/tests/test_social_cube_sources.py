from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest

from app.services.social_cube_sources import (
    cube_message_parts, cube_symbol, fetch_cube_rebalances, normalize_rebalance,
)
from app.services.social_sources import MonitoringDeferred, PlatformIntervention, RateLimited


def record(identifier=10, **changes):
    value = {
        'id': identifier, 'status': 'success', 'category': 'user_rebalancing',
        'created_at': '2026-10-25T02:03:04+00:00',
        'updated_at': '2026-10-25T02:04:05+00:00', 'comment': '',
        'rebalancing_histories': [{
            'stock_symbol': 'SH688410', 'stock_name': '山外山', 'price': 9.78,
            'prev_weight': 0, 'prev_weight_adjusted': 0,
            'weight': 11.38, 'target_weight': 11.38, 'proactive': True,
        }],
    }
    value.update(changes)
    return value


@contextmanager
def mock_client(handler):
    original = httpx.Client
    with patch('app.services.social_cube_sources.httpx.Client',
               side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)):
        yield


@pytest.mark.parametrize('value,expected', [
    ('ZH679918', 'ZH679918'), (' zh679918 ', 'ZH679918'),
    ('https://www.xueqiu.com/P/ZH679918', 'ZH679918'),
    ('http://xueqiu.com/P/ZH679918/', 'ZH679918'),
    ('https://xueqiu.com/P/zh679918?utm_source=test#history', 'ZH679918'),
])
def test_cube_symbols_are_extracted_and_normalized(value, expected):
    assert cube_symbol(value) == expected


@pytest.mark.parametrize('value', [
    '', None, 679918, '679918', 'ZH12/path', 'ZH１２',
    'https://example.com/P/ZH679918', 'https://www.xueqiu.com.evil.test/P/ZH679918',
    'https://xueqiu.com@evil.test/P/ZH679918',
    'https://user:password@xueqiu.com/P/ZH679918',
    'https://www.xueqiu.com:443/P/ZH679918',
    'https://www.xueqiu.com:invalid/P/ZH679918',
    'file:///P/ZH679918', '//www.xueqiu.com/P/ZH679918',
    'https://www.xueqiu.com/u/679918', 'https://www.xueqiu.com/P/ZH679918/extra',
])
def test_cube_input_cannot_control_request_host_or_path(value):
    with pytest.raises(ValueError):
        cube_symbol(value)


def test_rebalance_normalizes_times_and_preserves_zero_and_unknown_weights():
    rows = [
        {'stock_symbol': 'SZ000792', 'stock_name': '盐湖股份', 'prev_weight': 11.4,
         'prev_weight_adjusted': 11.41, 'target_weight': 0, 'weight': 2, 'price': '26.53'},
        {'stock_symbol': 'ZERO', 'prev_weight_adjusted': 0, 'prev_weight': 8,
         'target_weight': 0, 'weight': 4},
        {'stock_symbol': 'NEW', 'prev_weight': 0, 'target_weight': 11.38},
        {'stock_symbol': 'PASSIVE', 'prev_weight': 2, 'target_weight': 3, 'proactive': False},
        {'stock_symbol': 'UNKNOWN', 'prev_weight': None, 'target_weight': 5, 'price': 'nan'},
        {'stock_symbol': 'NO_DATA', 'prev_weight': None, 'target_weight': None},
        {'stock_symbol': 'ADD', 'prev_weight_adjusted': 'NaN', 'prev_weight': 5, 'target_weight': 6},
        {'stock_symbol': 'CUT', 'prev_weight': 5, 'target_weight': 4},
    ]
    event = normalize_rebalance('ZH679918', record(rebalancing_histories=rows))
    assert event['time'] == '2026-10-25T10:03:04+08:00'
    assert event['updated_at'] == '2026-10-25T10:04:05+08:00'
    changes = event['changes']
    assert [change['symbol'] for change in changes] == ['SZ000792', 'NEW', 'UNKNOWN', 'ADD', 'CUT']
    assert changes[0]['before'] == 11.41 and changes[0]['after'] == 0
    assert changes[0]['action'] == '卖出' and changes[0]['price'] == 26.53
    assert changes[1]['before'] == 0 and changes[1]['action'] == '买入'
    assert changes[2]['before'] is None and changes[2]['price'] is None
    assert changes[2]['action'] == '调整'
    assert changes[3]['before'] == 5 and changes[3]['action'] == '加仓'
    assert changes[4]['action'] == '减仓'


def test_missing_optional_fields_do_not_invent_a_timestamp_or_holdings():
    event = normalize_rebalance('ZH1', {'id': 1})
    assert event['time'] == event['updated_at'] == ''
    assert event['changes'] == [] and event['comment'] == ''
    assert event['status'] == 'unknown'
    epoch = normalize_rebalance('ZH1', record(created_at=0, updated_at=1700000000000))
    assert epoch['time'] == '1970-01-01T08:00:00+08:00'
    assert epoch['updated_at'] == '2023-11-15T06:13:20+08:00'
    invalid = normalize_rebalance('ZH1', record(created_at='not-a-date', updated_at=float('inf')))
    assert invalid['time'] == invalid['updated_at'] == ''


@pytest.mark.parametrize('value', [
    {}, {'id': None}, {'id': True}, {'id': 'secret-value'},
    record(rebalancing_histories={}), record(rebalancing_histories=[None]),
])
def test_malformed_records_raise_safe_errors(value):
    with pytest.raises(RuntimeError) as captured:
        normalize_rebalance('ZH1', value)
    assert 'secret-value' not in str(captured.value)


def test_initial_check_reads_only_latest_page_and_emits_the_baseline():
    requests, cursors = [], []

    def handler(request):
        requests.append(request)
        assert request.url.host == 'www.xueqiu.com'
        assert request.url.path == '/cubes/rebalancing/history.json'
        assert dict(request.url.params) == {'cube_symbol': 'ZH679918', 'count': '20', 'page': '1'}
        assert request.headers['Cookie'] == 'private-test-cookie'
        assert request.headers['Referer'] == 'https://www.xueqiu.com/P/ZH679918'
        return httpx.Response(200, json={'list': [record(10), record(9)], 'totalCount': 200})

    with mock_client(handler):
        result = fetch_cube_rebalances({'symbol': 'ZH679918', 'cookie': 'private-test-cookie',
                                       '_on_page': lambda *args: cursors.append(args)})
    assert len(requests) == 1 and result.complete
    assert [event['id'] for event in result] == ['9', '10']
    assert cursors[0][1:] == (1, True)


def test_pending_record_is_revisited_past_a_known_success_boundary():
    requested, callbacks, waits = [], [], []
    values = {1: [record(10), record(9)], 2: [record(8)],
              3: [record(7, status='success')], 4: []}

    def handler(request):
        page = int(request.url.params['page'])
        requested.append(page)
        return httpx.Response(200, json={'list': values[page]})

    with mock_client(handler):
        result = fetch_cube_rebalances({
            'symbol': 'ZH1', '_initial': False, '_known_ids': {'10', '9', '7'},
            '_pending_ids': {'7'}, '_on_page': lambda *args: callbacks.append(args),
            '_wait': lambda delay: waits.append(delay) or False,
        })
    assert requested == [1, 2, 3, 4] and result.complete
    assert all(1 <= delay <= 3 for delay in waits) and len(waits) == 3
    assert any(event['id'] == '7' and event['status'] == 'success'
               for events, _, _ in callbacks for event in events)
    assert callbacks[-1][1:] == (1, True)


def test_resume_covers_new_front_pages_before_jumping_to_the_deep_cursor():
    requested, callbacks = [], []
    values = {1: [record(14), record(13)], 2: [record(12), record(11)],
              3: [record(10)], 5: [record(7), record(6)]}

    def handler(request):
        page = int(request.url.params['page'])
        requested.append(page)
        return httpx.Response(200, json={'list': values[page]})

    with mock_client(handler):
        result = fetch_cube_rebalances({
            'symbol': 'ZH1', '_initial': False, '_scan_page': 5,
            '_known_ids': {'10', '9', '7', '6'}, '_pending_ids': {'7'},
            '_on_page': lambda *args: callbacks.append(args),
        })
    assert requested == [1, 2, 3, 5] and result.complete
    assert callbacks[2][1:] == (5, False)
    assert {event['id'] for event in result} >= {'14', '13', '12', '11', '7'}


def test_page_budget_retains_progress_and_empty_history_is_valid():
    requests, callbacks = [], []

    def handler(request):
        page = int(request.url.params['page'])
        requests.append(page)
        return httpx.Response(200, json={'list': [record(100 - page)]})

    with mock_client(handler):
        result = fetch_cube_rebalances({'symbol': 'ZH1', '_initial': False,
                                       '_on_page': lambda *args: callbacks.append(args)}, pages=2)
    assert requests == [1, 2] and not result.complete
    assert callbacks[-1][1:] == (3, False)
    with mock_client(lambda _: httpx.Response(200, json={'list': []})):
        result = fetch_cube_rebalances({'symbol': 'ZH1', '_initial': False, '_pending_ids': {'1'}})
    assert result.complete and result == []


@pytest.mark.parametrize('response,exception', [
    (httpx.Response(401), PlatformIntervention),
    (httpx.Response(403), PlatformIntervention),
    (httpx.Response(418), PlatformIntervention),
    (httpx.Response(302, headers={'Location': 'https://example.com/secret'}), PlatformIntervention),
    (httpx.Response(429, headers={'Retry-After': '600'}), RateLimited),
    (httpx.Response(200, json={'message': '请重新登录 secret-cookie'}), PlatformIntervention),
    (httpx.Response(200, text='<html>验证码</html>'), PlatformIntervention),
    (httpx.Response(200, text='<html>ordinary html</html>'), PlatformIntervention),
    (httpx.Response(200, json={'list': {}}), RuntimeError),
    (httpx.Response(200, json={'list': [None]}), RuntimeError),
])
def test_platform_restrictions_and_bad_responses_stop_without_retries(response, exception):
    requested = []

    def handler(request):
        requested.append(request)
        return response

    with mock_client(handler), pytest.raises(exception) as captured:
        fetch_cube_rebalances({'symbol': 'ZH1'})
    assert len(requested) == 1
    assert 'secret-cookie' not in str(captured.value) and 'example.com' not in str(captured.value)


def test_comment_mentioning_login_or_captcha_is_ordinary_content():
    with mock_client(lambda _: httpx.Response(200, json={'list': [
            record(comment='<p>今天讨论验证码和登录过期</p>')]})):
        result = fetch_cube_rebalances({'symbol': 'ZH1'})
    assert result[0]['comment'] == '今天讨论验证码和登录过期'


def test_request_guard_stops_before_first_request_and_before_followup_page():
    requested, callbacks = [], []

    def handler(request):
        requested.append(request)
        return httpx.Response(200, json={'list': [record()]})

    with mock_client(handler), pytest.raises(MonitoringDeferred):
        fetch_cube_rebalances({'symbol': 'ZH1', '_should_stop': lambda: True})
    assert requested == []
    with mock_client(handler), pytest.raises(MonitoringDeferred):
        fetch_cube_rebalances({'symbol': 'ZH1', '_initial': False, '_wait': lambda _: True,
                               '_on_page': lambda *args: callbacks.append(args)})
    assert len(requested) == 1 and callbacks[0][1:] == (2, False)


def test_notification_is_plain_text_detailed_and_split_by_utf16_units():
    event = normalize_rebalance('ZH679918', record(comment='<p>调仓备注</p>'))
    event['url'] = 'https://untrusted.example/ignored'
    parts = cube_message_parts({'symbol': 'ZH679918', 'name': '测试组合'}, event)
    text = ''.join(parts)
    assert '【雪球组合调仓】测试组合 ZH679918' in text
    assert '状态：调仓成功' in text and '北京时间' in text
    assert '买入 山外山 SH688410：0% → 11.38%' in text
    assert '组合记录价：9.78' in text and '调仓说明：调仓备注' in text
    assert '原文：https://www.xueqiu.com/P/ZH679918' in text and 'untrusted.example' not in text
    event['comment'] = '😀' * 4000
    long_parts = cube_message_parts({'symbol': 'ZH679918'}, event)
    assert len(long_parts) > 1
    assert all(len(part.encode('utf-16-le')) // 2 <= 3500 for part in long_parts)
    assert ''.join(long_parts).count('😀') == 4000


def test_notification_does_not_fabricate_unknown_values_or_success_status():
    event = normalize_rebalance('ZH1', record(status='pending', created_at=None,
        rebalancing_histories=[{'stock_symbol': 'TEST', 'target_weight': 2}]))
    text = ''.join(cube_message_parts({'symbol': 'ZH1'}, event))
    assert '调仓时间：未知' in text and '状态：等待完成' in text
    assert '未知 → 2%' in text and '调仓成功' not in text


def test_first_allowed_window_reads_twenty_one_post_start_events_across_two_pages():
    requested, callbacks = [], []
    since = '2026-10-24T10:00:00+08:00'
    fresh = [record(identifier, created_at=since, updated_at=since)
             for identifier in range(121, 100, -1)]
    old = record(100, created_at='2026-10-24T09:59:59+08:00',
                 updated_at='2026-10-24T09:59:59+08:00')

    def handler(request):
        page = int(request.url.params['page'])
        requested.append(page)
        return httpx.Response(200, json={'list': fresh[:20] if page == 1 else [fresh[20], old]})

    with mock_client(handler):
        result = fetch_cube_rebalances({
            'symbol': 'ZH1', '_initial': True, '_notify_since': since,
            '_on_page': lambda *args: callbacks.append(args),
        })
    assert requested == [1, 2] and result.complete
    assert len([event for event in result if int(event['id']) >= 101]) == 21
    assert '101' in {event['id'] for event in result}  # Same-second events stay in the new range.
    assert callbacks[0][1:] == (2, False) and callbacks[1][1:] == (1, True)
    assert '100' in {event['id'] for event in result}  # Storage decides baseline versus notification.


def test_initial_scan_budget_resumes_after_restart_and_covers_new_front_pages():
    since = '2026-10-24T10:00:00+08:00'
    requested, callbacks = [], []
    original = {1: [record(50)], 2: [record(40)], 3: [record(30)], 4: [record(20)]}

    def first_request(request):
        page = int(request.url.params['page'])
        requested.append(page)
        return httpx.Response(200, json={'list': original[page]})

    with mock_client(first_request):
        first = fetch_cube_rebalances({
            'symbol': 'ZH1', '_initial': True, '_notify_since': since,
            '_on_page': lambda *args: callbacks.append(args),
        }, pages=4)
    assert requested == [1, 2, 3, 4] and not first.complete
    assert callbacks[-1][1:] == (5, False)
    requested.clear()
    callbacks.clear()
    older = record(1, created_at='2026-10-24T09:00:00+08:00',
                   updated_at='2026-10-24T09:00:00+08:00')
    shifted = {1: [record(70)], 2: [record(60)], 3: [record(50)],
               5: [record(30)], 6: [record(20)], 7: [record(10), older]}

    def resumed_request(request):
        page = int(request.url.params['page'])
        requested.append(page)
        return httpx.Response(200, json={'list': shifted[page]})

    # These are the fields persisted by a failed/incomplete first initialization.
    with mock_client(resumed_request):
        resumed = fetch_cube_rebalances({
            'symbol': 'ZH1', '_initial': True, '_notify_since': since,
            '_scan_page': 5, '_known_ids': {event['id'] for event in first},
            '_on_page': lambda *args: callbacks.append(args),
        })
    assert requested == [1, 2, 3, 5, 6, 7] and resumed.complete
    assert {'70', '60', '10'} <= {event['id'] for event in resumed}
    assert callbacks[2][1:] == (5, False)
    assert callbacks[3][1:] == (6, False)  # A known deep page is not the time baseline either.
    assert callbacks[-1][1:] == (1, True)


def test_initial_time_boundary_uses_updated_time_and_unknown_dates_do_not_end_scan():
    requested, callbacks = [], []
    since = '2026-10-24T10:00:00+08:00'
    values = {
        1: [record(5, created_at='2026-10-23T10:00:00+08:00',
                   updated_at='2026-10-24T10:01:00+08:00')],
        2: [record(4, created_at=None, updated_at=None)],
        3: [record(3, created_at='2026-10-24T10:00:00+08:00', updated_at=None)],
        4: [record(2, created_at='2026-10-24T09:59:59+08:00', updated_at=None)],
    }

    def handler(request):
        page = int(request.url.params['page'])
        requested.append(page)
        return httpx.Response(200, json={'list': values[page]})

    with mock_client(handler):
        result = fetch_cube_rebalances({
            'symbol': 'ZH1', '_initial': True, '_notify_since': since,
            '_on_page': lambda *args: callbacks.append(args),
        })
    assert requested == [1, 2, 3, 4] and result.complete
    assert all(not complete for _, _, complete in callbacks[:-1])


def test_initial_unknown_dates_keep_page_budget_and_cursor_instead_of_completing():
    requested, callbacks = [], []

    def handler(request):
        page = int(request.url.params['page'])
        requested.append(page)
        return httpx.Response(200, json={'list': [record(100 - page, created_at=None, updated_at=None)]})

    with mock_client(handler):
        result = fetch_cube_rebalances({
            'symbol': 'ZH1', '_initial': True, '_notify_since': '2026-10-24T10:00:00+08:00',
            '_on_page': lambda *args: callbacks.append(args),
        }, pages=2)
    assert requested == [1, 2] and not result.complete
    assert callbacks[-1][1:] == (3, False)


def test_initial_time_boundary_does_not_hide_an_unchecked_pending_record():
    requested = []
    values = {
        1: [record(10, created_at='2026-10-24T09:00:00+08:00', updated_at=None)],
        2: [record(9, status='success', created_at='2026-10-24T08:00:00+08:00', updated_at=None)],
    }

    def handler(request):
        page = int(request.url.params['page'])
        requested.append(page)
        return httpx.Response(200, json={'list': values[page]})

    with mock_client(handler):
        result = fetch_cube_rebalances({
            'symbol': 'ZH1', '_initial': True, '_notify_since': '2026-10-24T10:00:00+08:00',
            '_known_ids': {'10', '9'}, '_pending_ids': {'9'},
        })
    assert requested == [1, 2] and result.complete
    assert next(event for event in result if event['id'] == '9')['status'] == 'success'
