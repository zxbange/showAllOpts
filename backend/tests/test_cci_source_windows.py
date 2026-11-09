"""Bounded public source requests, independent of recovery planning."""
import httpx
import pytest
from app.services.cci_source import CCIError, fetch_rows


def payload(grade, days):
    return {'total': len(days), 'rows': [
        {'kind': 'index', 'data': {'date': day, 'index_type': '日指数',
         'index_name': grade, 'unit': '元/吨', 'price': '1015', 'change': None}}
        for day in days]}


def test_range_preserves_dates_and_counts_end_date_targets():
    calls = []
    days = ['2026-11-06', '2026-11-09']
    def handle(request):
        params = request.url.params
        calls.append(params)
        assert params['date_from'] == days[0] and params['date_to'] == days[-1]
        assert not {'page', 'offset', 'limit'} & set(params)
        return httpx.Response(200, json=payload(params['index_name'], days))
    details = []
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        rows = fetch_rows(client, days[0], days[-1], diagnostics=details)
    assert len(calls) == 3 and len(rows) == 6
    assert {row['quoteDate'] for row in rows} == set(days)
    assert all(d['targetHits'] == 1 and d['observed'] == 2 for d in details)
    assert all(row['change'] is None for row in rows)


def test_single_grade_and_31_day_boundary():
    calls = []
    def handle(request):
        calls.append(request)
        assert request.url.params['index_name'] == 'CCI5000'
        return httpx.Response(200, json=payload('CCI5000', ['2026-10-01', '2026-10-31']))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        rows = fetch_rows(client, '2026-10-01', '2026-10-31', grades=('CCI5000',))
    assert len(calls) == 1 and len(rows) == 2


@pytest.mark.parametrize('start,end', [
    ('2026-10-01', '2026-11-01'), ('2026-11-02', '2026-11-01'),
    ('2025-12-31', '2026-01-02'), ('20261101', '2026-11-02'),
    ('2026-11-01', '20261102'), ('2026-02-30', '2026-03-01'),
    (None, '2026-11-02'), ('2026-11-01', None),
])
def test_invalid_range_rejected_without_network(start, end):
    def handle(request):
        pytest.fail('Invalid range must not request network')
    with httpx.Client(transport=httpx.MockTransport(handle)) as client, pytest.raises(CCIError):
        fetch_rows(client, start, end)


@pytest.mark.parametrize('grades', [(), ('CCI5500', 'CCI5500'), ('unknown',), 'CCI5500', None])
def test_invalid_grade_selection_rejected_without_network(grades):
    def handle(request):
        pytest.fail('Invalid grade selection must not request network')
    with httpx.Client(transport=httpx.MockTransport(handle)) as client, pytest.raises(CCIError):
        fetch_rows(client, '2026-11-01', '2026-11-02', grades=grades)


@pytest.mark.parametrize('mode', ['truncated', 'duplicate', 'out_of_range', 'invalid_date'])
def test_invalid_window_is_atomic_and_not_retried(mode):
    calls = []
    def handle(request):
        calls.append(request)
        body = payload('CCI5500', ['2026-11-02', '2026-11-03'])
        if mode == 'truncated':
            body['total'] = 10
        elif mode == 'duplicate':
            body['rows'][1] = body['rows'][0]
        else:
            body['rows'][1]['data']['date'] = '2026-11-04' if mode == 'out_of_range' else '2026-11-02T12:00:00'
        return httpx.Response(200, json=body)
    details = []
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert fetch_rows(client, '2026-11-02', '2026-11-03', grades=('CCI5500',), diagnostics=details) == []
    assert len(calls) == 1 and details[0]['errorType'] == 'CCIError'
    assert details[0]['observed'] == details[0]['targetHits'] == 0


def test_failed_grade_keeps_other_grade_successes():
    def handle(request):
        grade = request.url.params['index_name']
        return httpx.Response(429) if grade == 'CCI5000' else httpx.Response(200, json=payload(grade, ['2026-11-02']))
    details = []
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        rows = fetch_rows(client, '2026-11-01', '2026-11-03', diagnostics=details)
    assert {r['grade'] for r in rows} == {'CCI5500', 'CCI4500'}
    assert details[1]['error'] == 'HTTP 429'
    assert all(d['targetHits'] == 0 for d in details)


def test_stop_checked_during_stream():
    stopping = [False]
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{'
            stopping[0] = True
            yield b'"rows": [], "total": 0}'
    def handle(request):
        return httpx.Response(200, stream=Stream())
    details = []
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        rows = fetch_rows(client, '2026-11-01', '2026-11-03', grades=('CCI5500',),
                          should_stop=lambda: stopping[0], diagnostics=details)
    assert rows == [] and details[0]['errorType'] == 'CCIError'
    assert details[0]['returned'] is None


def test_stop_between_grades_preserves_validated_rows_and_reports_unrequested():
    import json
    stopping = [False]
    calls = []
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield json.dumps(payload('CCI5500', ['2026-11-02'])).encode()
            stopping[0] = True
    def handle(request):
        calls.append(request)
        return httpx.Response(200, stream=Stream())
    details = []
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        rows = fetch_rows(client, '2026-11-01', '2026-11-03',
                          should_stop=lambda: stopping[0], diagnostics=details)
    assert len(rows) == len(calls) == 1
    assert rows[0]['grade'] == 'CCI5500'
    assert [d['requested'] for d in details] == [True, False, False]
    assert details[0]['error'] is None
    assert all(d['error'] and d['returned'] is None and d['observed'] == 0 for d in details[1:])


def test_stop_before_first_grade_reports_unrequested_not_source_empty():
    def handle(request):
        pytest.fail('Stopped collector must not make a request')
    details = []
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert fetch_rows(client, '2026-11-01', '2026-11-03',
                          should_stop=lambda: True, diagnostics=details) == []
    assert len(details) == 3
    assert all(not d['requested'] and d['error'] and d['returned'] is None for d in details)
