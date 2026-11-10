"""Provider-contract tests use public response fixtures and offline HTTP transports."""
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.services import growth_dividend_source as source


def test_real_sina_calendar_codec_and_exchange_holidays():
    # Captured from CALENDAR_URL on 2026-11-10; a 508-byte compressed calendar,
    # independently checked against Sina/AKShare's original JavaScript decoder.
    payload = (Path(__file__).parent / 'fixtures/growth-dividend/sina-calendar.txt').read_text()
    days = source.decode_sina_trade_calendar(payload)
    assert (days[0], days[-1], len(days)) == (date(1990, 12, 19), date(2026, 12, 31), 8796)
    assert len([day for day in days if date(2018, 1, 1) <= day <= date(2026, 11, 10)]) == 2147
    assert [str(day) for day in days if date(2020, 1, 23) <= day <= date(2020, 2, 5)] == [
        '2020-01-23', '2020-02-03', '2020-02-04', '2020-02-05']
    assert [str(day) for day in days if date(2026, 2, 13) <= day <= date(2026, 2, 25)] == [
        '2026-02-13', '2026-02-24', '2026-02-25']


@pytest.mark.parametrize('payload', ['<html>unavailable</html>', 'AAAA', 'var datelist="LC/";', ''])
def test_invalid_or_truncated_calendar_fails_closed(payload):
    with pytest.raises(source.IndexSourceError):
        source.decode_sina_trade_calendar(payload)


def test_tencent_filters_ignored_start_and_uses_close_not_open():
    requests = []
    def provider(request):
        requests.append(request)
        return httpx.Response(200, json={'code': 0, 'data': {'sz399006': {'day': [
            ['2017-12-29', '1900', '1800'], ['2018-01-02', '1752.10', '1769.67'],
            ['2018-01-03', '1768', '1795'], ['2099-01-01', '999', '999'],
        ]}}})
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        rows = source.fetch_tencent(client, source.INDEXES[0], date(2018, 1, 2), date(2018, 1, 2))
    assert set(rows) == {date(2018, 1, 2)}
    assert rows[date(2018, 1, 2)].close == 1769.67
    assert requests[0].url.params['param'] == 'sz399006,day,2018-01-02,2018-01-02,5,'
    assert rows[date(2018, 1, 2)].source == '腾讯财经'


@pytest.mark.parametrize('close', ['NaN', 'Infinity', 0, -1, True, None])
def test_invalid_index_close_is_not_accepted(close):
    def provider(request):
        return httpx.Response(200, json={'code': 0, 'data': {'sh000922': {
            'day': [['2026-11-10', '5000', close]]}}})
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(source.IndexSourceError):
            source.fetch_tencent(client, source.INDEXES[1], date(2026, 11, 10), date(2026, 11, 10))


def test_partial_primary_is_supplemented_without_overwriting_primary_values():
    days = {date(2026, 11, 9), date(2026, 11, 10)}
    def provider(request):
        if request.url.host == 'proxy.finance.qq.com':
            return httpx.Response(200, json={'code': 0, 'data': {'sh000688': {
                'day': [['2026-11-09', '1200', '1260.00']]}}})
        return httpx.Response(200, json={'data': {'code': '000688', 'klines': [
            '2026-11-09,1200,1260.001,1300', '2026-11-10,1200,1265.02,1300']}})
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        rows = source.fetch_prices(client, source.INDEXES[2], min(days), max(days), expected_dates=days)
    assert set(rows) == days
    assert rows[min(days)].source == '腾讯财经'
    assert rows[max(days)].source == '东方财富'
    assert rows[max(days)].close == 1265.02


def test_stale_http_200_response_never_counts_as_complete():
    day = date(2026, 11, 10)
    def provider(request):
        if request.url.host == 'proxy.finance.qq.com':
            return httpx.Response(200, json={'code': 0, 'data': {'sh000922': {
                'day': [['2019-01-30', '4000', '4000']]}}})
        return httpx.Response(200, json={'data': {'code': '000922', 'klines': []}})
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        assert source.fetch_prices(client, source.INDEXES[1], day, day, expected_dates={day}) == {}


def test_mismatched_index_and_conflicting_dates_are_rejected():
    with pytest.raises(source.IndexSourceError):
        source._rows([['2026-11-10', '100', '200'], ['2026-11-10', '100', '201']],
                     date(2026, 11, 10), date(2026, 11, 10), 'test', 'https://example.test')
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={'data': {'code': '000001', 'klines': ['2026-11-10,10,20']}}))) as client:
        with pytest.raises(source.IndexSourceError):
            source.fetch_eastmoney(client, source.INDEXES[1], date(2026, 11, 10), date(2026, 11, 10))


def test_one_transient_tencent_failure_retries_before_fallback():
    calls = []
    day = date(2026, 11, 10)
    def provider(request):
        calls.append(request.url.host)
        if len(calls) == 1:
            raise httpx.ReadTimeout('temporary reset', request=request)
        return httpx.Response(200, json={'code': 0, 'data': {'sh000922': {
            'day': [[str(day), '5500', '5529.32']]}}})
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        rows = source.fetch_prices(client, source.INDEXES[1], day, day, expected_dates={day})
    assert rows[day].close == 5529.32
    assert calls == ['proxy.finance.qq.com'] * 2


def test_network_retries_are_bounded_and_observe_cancellation():
    calls = []
    day = date(2026, 11, 10)
    def provider(request):
        calls.append(request.url.host)
        return httpx.Response(503)
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(source.IndexSourceError):
            source.fetch_prices(client, source.INDEXES[1], day, day, expected_dates={day})
    assert calls == ['proxy.finance.qq.com', 'proxy.finance.qq.com', 'push2his.eastmoney.com']
    calls.clear()
    def cancelled():
        if calls:
            raise RuntimeError('cancelled fixture')
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(RuntimeError, match='cancelled fixture'):
            source.fetch_prices(client, source.INDEXES[1], day, day, expected_dates={day},
                                check_cancel=cancelled)
    assert calls == ['proxy.finance.qq.com']
