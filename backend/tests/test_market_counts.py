"""Official counts are strict date guards, not constituent-list evidence."""
from copy import deepcopy
from datetime import date, datetime

import httpx
import pytest

from app.services.market_counts import ExchangeDailyCounts, SSE_SQL_ID
from app.services.market_sina import SinaDataError, SinaRateLimitError


def sse(day='2026-11-06'):
    return {'actionErrors': [], 'fieldErrors': {}, 'sqlId': SSE_SQL_ID,
            'isPagination': 'false', 'result': [
                {'PRODUCT_CODE': code, 'LIST_NUM': count, 'TRADE_DATE': day.replace('-', '')}
                for code, count in [('01', '1700'), ('02', '41'), ('03', '618'),
                                    ('11', '0'), ('17', '2359')]]}


def szse(day='2026-11-06'):
    return [{'metadata': {'catalogid': '1803_sczm', 'tabkey': 'tab1',
                           'subname': day, 'pagesize': -1, 'pageno': 0,
                           'pagecount': 0, 'recordcount': 0},
             'error': None, 'data': [
                 {'lbmc': '股票', 'zqsl': '2942'},
                 {'lbmc': '&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;主板A股', 'zqsl': '1495'},
                 {'lbmc': '&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;主板B股', 'zqsl': '36'},
                 {'lbmc': '&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;创业板A股', 'zqsl': '1411'}]},
            {'metadata': {'catalogid': '1803_sczm', 'tabkey': 'tab2', 'subname': ''},
             'data': [], 'error': None}]


def fetch(first=None, second=None, *, day='2026-11-06'):
    calls = []
    def serve(request):
        calls.append(request)
        return httpx.Response(200, json=(first if first is not None else sse(str(day)))
                              if len(calls) == 1 else (second if second is not None else szse(str(day))))
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        with ExchangeDailyCounts(client) as source:
            result = source.fetch(day)
        assert not client.is_closed
    return result, calls


def test_verified_counts_exclude_b_and_are_not_membership_evidence():
    result, calls = fetch()
    assert result['date'] == '2026-11-06'
    assert result['counts'] == {'sse_main_a': 1700, 'sse_star': 618,
                                'szse_main_a': 1495, 'szse_chinext': 1411}
    assert result['verified'] is True
    assert result['evidence']['membership_verified'] is False
    assert calls[0].url.params['SEARCH_DATE'] == calls[1].url.params['txtQueryDate'] == result['date']
    for source, request in zip(('sse', 'szse'), calls):
        ev = result['evidence'][source]
        assert ev['source_url'] == str(request.url)
        assert len(ev['sha256']) == 64
    assert calls[0].headers['Referer'] == 'https://www.sse.com.cn/'


def test_requested_date_is_not_hardcoded():
    result, calls = fetch(day=date(2025, 3, 4))
    assert result['date'] == '2025-03-04'
    assert calls[0].url.params['SEARCH_DATE'] == '2025-03-04'
    assert calls[1].url.params['txtQueryDate'] == '2025-03-04'


@pytest.mark.parametrize('bad', [True, None, 1700.0, '', '-1', '1.1', '1e3', 'NaN', '1,70', '100001'])
def test_bad_count_rejected(bad):
    payload = sse()
    payload['result'][0]['LIST_NUM'] = bad
    with pytest.raises(SinaDataError, match='数量'):
        fetch(first=payload)


@pytest.mark.parametrize('mutation,code', [
    (lambda p: p['result'][0].update(TRADE_DATE='20261105'), 'count_date_mismatch'),
    (lambda p: p['result'].append(deepcopy(p['result'][0])), 'duplicate_category'),
    (lambda p: p['result'].pop(0), 'missing_category'),
    (lambda p: p.update(actionErrors=['unavailable']), 'invalid_report'),
    (lambda p: p.update(isPagination='true'), 'invalid_report'),
    (lambda p: p.update(sqlId='wrong'), 'invalid_report'),
])
def test_sse_fails_closed(mutation, code):
    payload = sse()
    mutation(payload)
    with pytest.raises(SinaDataError) as error:
        fetch(first=payload)
    assert error.value.code == code


@pytest.mark.parametrize('status', [301, 403, 429, 500])
@pytest.mark.parametrize('failing_call', [1, 2])
def test_error_stops_without_retry_redirect_or_fallback(status, failing_call):
    calls = []
    def serve(request):
        calls.append(request)
        if len(calls) == failing_call:
            return httpx.Response(status, headers={'Retry-After': '120', 'Location': 'https://alternate.test/'})
        return httpx.Response(200, json=sse())
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        with pytest.raises(SinaDataError) as error:
            ExchangeDailyCounts(client).fetch('2026-11-06')
    assert len(calls) == failing_call
    assert error.value.status_code == status
    if status in (403, 429):
        assert isinstance(error.value, SinaRateLimitError)
        assert error.value.retry_after == '120'


@pytest.mark.parametrize('day', ['2026-2-01', '2026-02-30', datetime(2026, 11, 6), None])
def test_invalid_date_does_not_make_request(day):
    def serve(request):
        pytest.fail('Invalid date reached network')
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        with pytest.raises(SinaDataError):
            ExchangeDailyCounts(client).fetch(day)


def test_configurable_endpoints_and_owned_client():
    calls = []
    def serve(request):
        calls.append(request)
        return httpx.Response(200, json=sse() if len(calls) == 1 else szse())
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        ExchangeDailyCounts(client, sse_url='https://sse.test/count',
                            szse_url='https://szse.test/count').fetch('2026-11-06')
    assert [r.url.host for r in calls] == ['sse.test', 'szse.test']
    source = ExchangeDailyCounts()
    source.close()
    assert source.client.is_closed


@pytest.mark.parametrize('failing_call', [1, 2])
def test_invalid_json_and_network_errors_fail_closed(failing_call):
    for failure, expected in [('json', 'invalid_json'), ('network', 'network_error')]:
        calls = []
        def serve(request):
            calls.append(request)
            if len(calls) == failing_call:
                if failure == 'network':
                    raise httpx.ReadTimeout('timeout', request=request)
                return httpx.Response(200, text='<html>not JSON</html>')
            return httpx.Response(200, json=sse())
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            with pytest.raises(SinaDataError) as error:
                ExchangeDailyCounts(client).fetch('2026-11-06')
        assert error.value.code == expected
        assert len(calls) == failing_call


@pytest.mark.parametrize('mutation,code', [
    (lambda p: p[0]['metadata'].update(subname='2026-11-05'), 'count_date_mismatch'),
    (lambda p: p[0]['metadata'].update(subname='2026-11-06 2026-11-05'), 'count_date_mismatch'),
    (lambda p: p[0]['metadata'].update(pagesize=20), 'invalid_report'),
    (lambda p: p[0]['data'].append(deepcopy(p[0]['data'][1])), 'duplicate_category'),
    (lambda p: p[0]['data'].pop(1), 'missing_category'),
    (lambda p: p[0]['data'][1].update(zqsl=True), 'invalid_count'),
    (lambda p: p[0].update(error='unavailable'), 'invalid_report'),
    (lambda p: p.append(deepcopy(p[0])), 'invalid_report'),
    (lambda p: p.pop(0), 'invalid_report'),
])
def test_szse_fails_closed(mutation, code):
    payload = szse()
    mutation(payload)
    with pytest.raises(SinaDataError) as error:
        fetch(second=payload)
    assert error.value.code == code


def test_szse_entity_normalization_and_comma_formatted_count():
    payload = szse()
    payload[0]['data'][1].update(lbmc=' \u00a0主板A股 ', zqsl='1,495')
    result, _ = fetch(second=payload)
    assert result['counts']['szse_main_a'] == 1495


def test_szse_duplicate_normalized_category_rejected():
    payload = szse()
    payload[0]['data'].append({'lbmc': '主板A股', 'zqsl': '1495'})
    with pytest.raises(SinaDataError) as error:
        fetch(second=payload)
    assert error.value.code == 'duplicate_category'


@pytest.mark.parametrize('zero', [0, '0'])
def test_explicit_zero_category_is_valid(zero):
    first, second = sse('2018-01-02'), szse('2018-01-02')
    first['result'][2]['LIST_NUM'] = zero
    second[0]['data'][3]['zqsl'] = zero
    result, _ = fetch(first=first, second=second, day='2018-01-02')
    assert result['counts']['sse_star'] == 0
    assert result['counts']['szse_chinext'] == 0
    assert result['verified'] is True


def test_absent_pre_launch_star_category_is_not_inferred_zero():
    payload = sse('2018-01-02')
    payload['result'].pop(2)
    with pytest.raises(SinaDataError) as error:
        fetch(first=payload, day='2018-01-02')
    assert error.value.code == 'missing_category'


@pytest.mark.parametrize('label', ['中小板', '中小板A股', '&nbsp;中小企业板'])
def test_separate_sme_board_requires_verified_historical_grouping(label):
    payload = szse('2018-01-02')
    payload[0]['data'].append({'lbmc': label, 'zqsl': '900'})
    with pytest.raises(SinaDataError) as error:
        fetch(second=payload, day='2018-01-02')
    assert error.value.code == 'unsupported_historical_schema'
