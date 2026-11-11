"""Affirmative event intervals are not an all-symbol historical pause oracle."""
from datetime import datetime, timezone

import httpx
import pytest

from app.services.market_suspensions import PublicSuspensions, SuspensionDataError


NOW = datetime(2026, 11, 11, 9, tzinfo=timezone.utc)


def event(code='002623', *, start='2026-11-03 09:30:00', end='2026-11-09 15:00:00',
          kind='连续停牌', predicted=None, exchange='SZ'):
    return {'SECURITY_CODE': code, 'SECUCODE': f'{code}.{exchange}',
            'SECURITY_NAME_ABBR': '测试停牌', 'SUSPEND_START_TIME': start,
            'SUSPEND_START_DATE': start[:10] + ' 00:00:00', 'SUSPEND_END_TIME': end,
            'SUSPEND_EXPIRE': kind, 'PREDICT_RESUME_DATE': predicted}


def result(rows):
    return {'success': True, 'result': {'count': len(rows), 'pages': (len(rows) + 499) // 500,
                                       'data': rows}}


def fetch(rows, *, start='2026-11-06', end='2026-11-06'):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=result(rows)))) as client:
        with PublicSuspensions(client, now=lambda: NOW) as source:
            data = source.fetch(start, end)
        assert not client.is_closed
        return data


def test_target_intersection_removes_future_events_returned_by_date_filter():
    rows = [event(), event('688798', start='2026-11-10 09:30:00', end=None, exchange='SH')]
    data = fetch(rows)
    assert set(data['intervals']) == {'sz002623'}
    interval = data['intervals']['sz002623'][0]
    assert interval['start'] == interval['end'] == '2026-11-06'
    assert interval['paused'] == 1
    assert interval['evidence']['raw'] == rows[0]
    assert data['evidence']['raw_count'] == 2
    assert data['evidence']['absence_means_unpaused'] is False
    assert data['evidence']['coverage_from'] is None


def test_intraday_start_and_end_days_are_excluded_but_middle_days_are_full():
    data = fetch([event(start='2026-11-03 10:45:00', end='2026-11-09 10:00:00')],
                 start='2026-11-03', end='2026-11-09')
    interval = data['intervals']['sz002623'][0]
    assert interval['start'] == '2026-11-04'
    assert interval['end'] == '2026-11-08'
    assert interval['end_inclusive'] is True
    assert data['evidence']['calendar_verified'] is False  # Caller filters weekend dates.


@pytest.mark.parametrize('end,kind', [('2026-11-06 10:55:00', '盘中停牌'),
                                    (None, '盘中停牌'), (None, '停牌一天')])
def test_intraday_or_unknown_end_is_not_full_day_evidence(end, kind):
    data = fetch([event(start='2026-11-06 10:45:00', end=end, kind=kind)])
    assert data['intervals'] == {}


def test_open_continuous_pause_is_bounded_by_capture_and_predictions_not_extended():
    data = fetch([event(start='2026-11-03 09:30:00', end=None,
                        predicted='2026-11-10 00:00:00')], start='2026-11-03', end='2026-11-11')
    assert data['intervals']['sz002623'][0]['end'] == '2026-11-09'
    data = fetch([event(start='2026-11-03 09:30:00', end=None)],
                 start='2026-11-03', end='2026-11-11')
    assert data['intervals']['sz002623'][0]['end'] == '2026-11-11'


def test_zero_records_means_unknown_not_no_suspensions_and_bse_outside_scope():
    assert fetch([])['intervals'] == {}
    assert fetch([event('920001', exchange='BJ')])['intervals'] == {}


@pytest.mark.parametrize('change,code', [
    ({'SUSPEND_START_TIME': '2026-11-03'}, 'invalid_suspension_time'),
    ({'SUSPEND_START_DATE': '2026-11-04 00:00:00'}, 'invalid_suspension_interval'),
    ({'SUSPEND_END_TIME': '2026-11-02 15:00:00'}, 'invalid_suspension_interval'),
    ({'SECUCODE': '000001.SH'}, 'invalid_suspension_symbol'),
])
def test_contradictory_or_missing_evidence_fails_closed(change, code):
    row = event()
    row.update(change)
    with pytest.raises(SuspensionDataError) as error:
        fetch([row])
    assert error.value.code == code


def test_duplicate_events_fail_instead_of_hiding_bad_pagination():
    with pytest.raises(SuspensionDataError) as error:
        fetch([event(), event()])
    assert error.value.code == 'duplicate_event'


@pytest.mark.parametrize('status', [403, 429])
def test_access_denial_stops_without_retry(status):
    calls = []
    def denied(request):
        calls.append(request)
        return httpx.Response(status)
    with httpx.Client(transport=httpx.MockTransport(denied)) as client:
        with pytest.raises(SuspensionDataError) as error:
            PublicSuspensions(client, now=lambda: NOW).fetch('2026-11-06', '2026-11-06')
    assert len(calls) == 1
    assert error.value.is_rate_limited


@pytest.mark.parametrize('report', [
    {'success': False, 'result': None},
    {'success': True, 'result': {'count': 1, 'pages': 1, 'data': []}},
    {'success': True, 'result': {'count': 1, 'pages': 2, 'data': [event()]}},
])
def test_incomplete_report_cannot_be_treated_as_zero_pauses(report):
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=report))) as client:
        with pytest.raises(SuspensionDataError):
            PublicSuspensions(client, now=lambda: NOW).fetch('2026-11-06', '2026-11-06')


def test_multi_page_report_counts_and_rows_are_checked():
    rows = [event(f'{number:06}') for number in range(501)]
    calls = []
    def serve(request):
        page = int(request.url.params['pageNumber'])
        calls.append(page)
        return httpx.Response(200, json={'success': True, 'result': {
            'count': 501, 'pages': 2, 'data': rows[(page-1)*500:page*500]}})
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        data = PublicSuspensions(client, now=lambda: NOW).fetch('2026-11-06', '2026-11-06')
    assert calls == [1, 2]
    assert len(data['intervals']) == 501


def test_future_query_never_contacts_provider():
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda req: calls.append(req))) as client:
        with pytest.raises(SuspensionDataError):
            PublicSuspensions(client, now=lambda: NOW).fetch('2026-11-06', '2026-11-12')
    assert not calls


def test_before_close_does_not_assert_a_full_day_has_already_been_paused():
    rows = [event(start='2026-11-11 09:30:00', end=None)]
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=result(rows)))) as client:
        source = PublicSuspensions(client, now=lambda: datetime(2026, 11, 11, 2, tzinfo=timezone.utc))
        data = source.fetch('2026-11-11', '2026-11-11')
    assert data['intervals'] == {}
    assert data['evidence']['latest_full_day_evidence_date'] == '2026-11-10'
