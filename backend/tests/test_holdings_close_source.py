from datetime import date, datetime
import json

import httpx
import pytest

from app.services import holdings_close_source as source


DAY = date(2026, 11, 23)
TOKEN = 'test-token-must-not-leak'
FIELDS = ['ts_code', 'trade_date', 'close']


def use_transport(monkeypatch, handler):
    real_client = httpx.Client

    def build_client(**kwargs):
        assert kwargs['follow_redirects'] is False
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(source.httpx, 'Client', build_client)


def payload(rows):
    return {'code': 0, 'data': {'fields': FIELDS, 'items': rows}}


def assert_unavailable():
    with pytest.raises(source.HoldingsCloseUnavailable) as failure:
        source.fetch_daily_closes(DAY, TOKEN)
    assert str(failure.value) == '当日未复权收盘价暂不可用。'
    assert failure.value.__context__ is None
    assert failure.value.__cause__ is None


def test_requests_bulk_date_only_and_preserves_exact_close(monkeypatch):
    def handler(request):
        assert request.method == 'POST'
        assert str(request.url) == 'https://api.tushare.pro'
        assert json.loads(request.content) == {
            'api_name': 'daily', 'token': TOKEN, 'params': {'trade_date': '20261123'},
            'fields': 'ts_code,trade_date,close',
        }
        return httpx.Response(200, content=b'''{"code":0,"data":{"fields":["close",
          "ts_code","trade_date"],"items":[[12.10000001,"000001.SZ","20261123"],
          [9.50000,"600000.SH","20261123"],["3.250","920001.BJ","20261123"]]}}''')

    use_transport(monkeypatch, handler)
    assert source.fetch_daily_closes(DAY, TOKEN) == {
        '000001.SZ': '12.10000001', '600000.SH': '9.5', '920001.BJ': '3.25',
    }


@pytest.mark.parametrize('rows', [
    [],
    [['000001.SZ', '20261120', 12]],
    [['000001.SZ', 20261123, 12]],
    [['000001.sz', '20261123', 12]],
    [['000001.HK', '20261123', 12]],
    [['000001.SZ', '20261123', 0]],
    [['000001.SZ', '20261123', -8]],
    [['000001.SZ', '20261123', 0.000000001]],
    [['000001.SZ', '20261123', 1.000000001]],
    [['000001.SZ', '20261123', 100000000]],
    [['000001.SZ', '20261123', True]],
    [['000001.SZ', '20261123', float('inf')]],
    [['000001.SZ', '20261123', None]],
    [['000001.SZ', '20261123', '12; secret']],
    [['000001.SZ', '20261123']],
    [['000001.SZ', '20261123', 12], ['000001.SZ', '20261123', 12]],
])
def test_not_yet_published_or_invalid_rows_reject_entire_response(monkeypatch, rows):
    use_transport(monkeypatch, lambda request: httpx.Response(
        200, content=json.dumps(payload(rows)).encode(),
    ))
    assert_unavailable()


def test_possible_pagination_truncation_is_not_silently_accepted(monkeypatch):
    rows = [[f'{i:06d}.SZ', '20261123', 1] for i in range(6000)]
    result = payload(rows)
    use_transport(monkeypatch, lambda request: httpx.Response(200, json=result))
    assert_unavailable()
    rows.pop()
    assert len(source.fetch_daily_closes(DAY, TOKEN)) == 5999
    result['data']['has_more'] = True
    assert_unavailable()


@pytest.mark.parametrize('status, raw', [
    (302, b'redirect'),
    (429, b'rate limit'),
    (500, b'private server diagnostic'),
    (200, b'not JSON'),
    (200, b'{"code":-1,"msg":"test-token-must-not-leak"}'),
    (200, b'{"code":0,"code":1}'),
    (200, b'{"code":false,"data":{"fields":[],"items":[]}}'),
    (200, b'{"code":0,"data":{"fields":["ts_code","trade_date","adj_close"],"items":[["000001.SZ","20261123",12]]}}'),
])
def test_provider_failure_is_safe_and_not_retried(monkeypatch, status, raw):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(status, headers={'location': 'https://redirect.invalid'}, content=raw)

    use_transport(monkeypatch, handler)
    assert_unavailable()
    assert calls == ['api.tushare.pro']


def test_transport_exception_is_safe(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout(TOKEN, request=request)

    use_transport(monkeypatch, handler)
    assert_unavailable()


def test_oversized_body(monkeypatch):
    monkeypatch.setattr(source, '_RESPONSE_CAP', 16)
    use_transport(monkeypatch, lambda request: httpx.Response(200, content=b'x' * 17))
    assert_unavailable()


def test_missing_config_or_non_date_never_contacts_provider(monkeypatch):
    def handler(request):
        pytest.fail('Invalid input contacted provider')

    use_transport(monkeypatch, handler)
    for day, token in [(DAY, ' '), (datetime(2026, 11, 23, 15, 5), TOKEN)]:
        with pytest.raises(source.HoldingsCloseUnavailable):
            source.fetch_daily_closes(day, token)
