"""Synthetic offline listing-directory contracts; never use real credentials."""
from copy import deepcopy
from datetime import date, datetime, timezone, timedelta
from hashlib import sha256
import gzip
import json

import httpx
from pydantic import SecretStr
import pytest

from app.services import market_asharehub_listing as source
from app.services.market_sina import SinaDataError, SinaRateLimitError

DAY = date(2026, 11, 9)
NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)
KEY = 'synthetic-listing-key'


def row(symbol='688827.SH', **changes):
    return dict(symbol=symbol, name='测试证券', exchange='SSE', list_status='L',
                list_date='20261103', delist_date=None) | changes


def adapter(handler=None, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler or (lambda r: httpx.Response(200, json=[row()]))))
    return source.AShareHubListingMetadata(client, SecretStr(KEY), now=lambda: NOW, **kwargs)


def test_proof_exact_identity_and_date():
    result = adapter().fetch('sh688827', DAY)
    assert result['ipo_date'] == '2026-11-03'
    assert result['request_secid'] == '688827.SH'
    assert source.validate_listing_evidence(result, 'sh688827', DAY) == result
    assert KEY not in json.dumps(result)


def test_bulk_shared_for_multiple_symbols_and_disk(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers['X-API-Key'] == KEY
        assert request.url == source.URL + '?list_status=L'
        assert request.extensions['timeout']['connect'] == 60
        return httpx.Response(200, json=[row(), row('600001.SH')])
    value = adapter(handler, cache_root=tmp_path)
    value.fetch('sh688827', DAY)
    value.fetch('sh600001', DAY)
    adapter(lambda r: pytest.fail('cache should avoid request'), cache_root=tmp_path).fetch('sh600001', DAY)
    assert len(calls) == 1
    assert KEY not in next(tmp_path.rglob('L.json')).read_text()


def test_next_capture_day_refreshes_directory(tmp_path):
    value = adapter(cache_root=tmp_path)
    value.fetch('sh688827', DAY)
    calls = []
    other = adapter(lambda r: calls.append(r) or httpx.Response(200, json=[row()]), cache_root=tmp_path)
    other._now = lambda: NOW + timedelta(days=1)
    other.fetch('sh688827', DAY)
    assert len(calls) == 1


def test_delisted_lazy_lookup():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[row()] if request.url.params['list_status'] == 'L' else [
            row('600001.SH', list_status='D', list_date='19990101', delist_date='20260101')])
    value = adapter(handler)
    assert value.fetch('sh600001', DAY)['ipo_date'] == '1999-01-01'
    value.fetch('sh600001', DAY)
    assert len(calls) == 2


def test_missing_is_not_historical_membership_evidence():
    def handler(request):
        return httpx.Response(200, json=[row()] if request.url.params['list_status'] == 'L' else [
            row('600001.SH', list_status='D', list_date='19990101', delist_date='20260101')])
    with pytest.raises(SinaDataError, match='没有') as result:
        adapter(handler).fetch('sz000001', DAY)
    assert result.value.code == 'listing_not_found'


@pytest.mark.parametrize('changes', [dict(list_date=None), dict(list_date='20260230'),
    dict(exchange='SZSE'), dict(list_status='P'), dict(symbol='688827.XSHG'), dict(name='')])
def test_invalid_catalogue_fails(changes):
    with pytest.raises(SinaDataError):
        adapter(lambda r: httpx.Response(200, json=[row(**changes)])).fetch('sh688827', DAY)


def test_duplicate_and_future_date_rejected():
    with pytest.raises(SinaDataError):
        adapter(lambda r: httpx.Response(200, json=[row(), row()])).fetch('sh688827', DAY)
    with pytest.raises(SinaDataError):
        adapter().fetch('sh688827', date(2026, 11, 2))


@pytest.mark.parametrize('status', [401, 403, 429])
def test_denial_never_retried(status):
    calls = []
    with pytest.raises(SinaRateLimitError):
        adapter(lambda r: calls.append(r) or httpx.Response(status)).fetch('sh688827', DAY)
    assert len(calls) == 1


def test_connect_failure_one_retry_then_success():
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectTimeout('synthetic')
        return httpx.Response(200, json=[row()])
    assert adapter(handler).fetch('sh688827', DAY)['verified'] is True
    assert len(calls) == 2


def test_connect_failure_does_not_poison_next_fetch():
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) <= 2:
            raise httpx.ConnectError('synthetic')
        return httpx.Response(200, json=[row()])
    value = adapter(handler)
    with pytest.raises(httpx.ConnectError):
        value.fetch('sh688827', DAY)
    assert value.fetch('sh688827', DAY)['verified'] is True
    assert len(calls) == 3


def test_redirect_not_followed():
    calls = []
    with pytest.raises(httpx.HTTPStatusError):
        adapter(lambda r: calls.append(r) or httpx.Response(302, headers={'Location': 'https://example.com/'})).fetch('sh688827', DAY)
    assert len(calls) == 1


def test_tampered_proof_rejected():
    proof = adapter().fetch('sh688827', DAY)
    for key, bad in [('ipo_date', '1999-01-01'), ('source_id', 'eastmoney_listing_metadata'),
                     ('verified', 1), ('evidence_version', True), ('source_digest', 'x')]:
        changed = proof | {key: bad}
        with pytest.raises(SinaDataError):
            source.validate_listing_evidence(changed, 'sh688827', DAY)


def test_corrupt_cache_rejected(tmp_path):
    adapter(cache_root=tmp_path).fetch('sh688827', DAY)
    path = next(tmp_path.rglob('L.json'))
    payload = json.loads(path.read_text()); payload['sha256'] = 'bad'
    path.write_text(json.dumps(payload))
    with pytest.raises(SinaDataError):
        adapter(lambda r: pytest.fail('no request on corrupt cache'), cache_root=tmp_path).fetch('sh688827', DAY)


def test_request_budget_checked_at_headers():
    clock = [0.0]
    def handler(request):
        clock[0] = 61
        return httpx.Response(200, json=[row()])
    with pytest.raises(SinaDataError) as result:
        adapter(handler, monotonic=lambda: clock[0]).fetch('sh688827', DAY)
    assert result.value.code == 'request_deadline'


def test_retired_namespace_is_not_normalized_to_current_symbol():
    def handler(request):
        return httpx.Response(200, json=[row()] if request.url.params['list_status'] == 'L' else [
            row('T600018.SH', list_status='D', list_date='20000719', delist_date='20061020')])
    with pytest.raises(SinaDataError) as error:
        adapter(handler).fetch('sh600018', DAY)
    assert error.value.code == 'listing_not_found'


def test_cancellation_not_retried():
    class Cancelled(Exception):
        stop_collection = True
    calls = []
    def handler(request):
        calls.append(request)
        raise Cancelled()
    with pytest.raises(Cancelled):
        adapter(handler).fetch('sh688827', DAY)
    assert len(calls) == 1


def test_oversized_body_rejected(monkeypatch):
    monkeypatch.setattr(source, 'MAX_BYTES', 20)
    with pytest.raises(SinaDataError):
        adapter().fetch('sh688827', DAY)
