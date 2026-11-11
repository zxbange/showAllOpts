"""Offline TDX diagnostics preserve validation, cache and stop semantics."""
import errno
import gzip
import json
import logging
from pathlib import Path
import socket
import ssl
from types import SimpleNamespace

import httpx
import pytest

from app.services import market_bulk
from app.services.market_bulk import TdxDailyArchives
from app.services.market_collection import CollectionCancelled, PacedHTTPClient
from app.services.market_diagnostics import (
    copy_source_failure, record_source_progress, source_failure_details,
)
from app.services.market_public import MarketDependencyError, PublicMarketProvider
from app.services.market_sina import SinaDataError, SinaRateLimitError
from test_market_bulk import daily, zipped
from test_market_collection import worker
from test_market_public import DAY, make_provider


SECRET = 'https://private.invalid/?token=never-log-me\nFORGED LOG'


def assert_safe(value):
    assert all(secret not in value for secret in ('private.invalid', 'never-log-me', 'FORGED LOG'))


def load_daily(source, day=DAY):
    provider = make_provider()
    provider.archives = source
    return provider._daily_rows(day)


def assert_progress(error, *, stage, code, status=None, cache='miss', size=0):
    facts = error.source_diagnostics
    assert facts['source_id'] == 'tdx_daily_archive'
    assert facts['stage'] == stage and facts['code'] == code
    assert facts['status_code'] == status
    assert facts['source_date'] == str(DAY) and facts['cache_state'] == cache
    assert facts['archive_bytes'] == size
    assert type(facts['elapsed_ms']) is int and 0 <= facts['elapsed_ms'] <= 604800000
    assert_safe(source_failure_details(error))


@pytest.mark.parametrize('status', [403, 429, 404, 500])
def test_http_failure_retains_status_without_consuming_body_or_changing_policy(tmp_path, status):
    class UnreadableBody(httpx.SyncByteStream):
        closed = False
        def __iter__(self):
            pytest.fail('HTTP failure bodies must never be consumed')
        def close(self):
            self.closed = True
    body, calls, invalidated = UnreadableBody(), [], []
    def response(request):
        calls.append(request)
        return httpx.Response(status, stream=body, headers={'retry-after': SECRET})
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        source = TdxDailyArchives(client, tmp_path)
        source.invalidate = invalidated.append
        provider = make_provider()
        provider.archives = source
        with pytest.raises(SinaRateLimitError if status in (403, 429) else MarketDependencyError) as caught:
            provider._daily_rows(DAY)
    assert len(calls) == 1 and invalidated == [] and body.closed
    assert not list(tmp_path.iterdir())
    error = caught.value
    assert error.code == ('rate_limited' if status in (403, 429) else 'source_unavailable')
    assert_progress(error, stage='request', code='rate_limited' if status in (403, 429) else 'http_error',
                    status=status)
    assert 'transport_category' not in error.source_diagnostics
    assert_safe(str(error))


@pytest.mark.parametrize('status', [403, 429])
def test_paced_header_denial_still_has_tdx_source_and_zero_bytes(tmp_path, status):
    collection, _, _, _ = worker(tmp_path / 'collection')
    calls = []
    def response(request):
        calls.append(request)
        return httpx.Response(status, text=SECRET)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        source = TdxDailyArchives(PacedHTTPClient(client, collection, lambda: None), tmp_path / 'cache')
        with pytest.raises(SinaRateLimitError) as caught:
            PublicMarketProvider._dependency_call('source_unavailable', source.fetch, DAY)
    assert_progress(caught.value, stage='request', code='rate_limited', status=status)
    assert len(calls) == 1 and not (tmp_path / 'cache').exists()


@pytest.mark.parametrize('kind,category', [
    (httpx.ConnectTimeout, 'connect_timeout'), (httpx.ReadTimeout, 'read_timeout'),
    (httpx.ConnectError, 'connect_error'), (httpx.RemoteProtocolError, 'protocol_error'),
])
def test_transport_failure_is_annotated_before_safe_public_wrapper(tmp_path, kind, category):
    calls, invalidated = [], []
    original = kind(SECRET)
    def response(request):
        calls.append(request)
        raise original
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        source = TdxDailyArchives(client, tmp_path)
        source.invalidate = invalidated.append
        provider = make_provider()
        provider.archives = source
        with pytest.raises(MarketDependencyError) as caught:
            provider._daily_rows(DAY)
    assert caught.value.code == 'source_unavailable'
    assert_progress(caught.value, stage='request', code='network_error')
    assert caught.value.source_diagnostics['transport_category'] == category
    assert len(calls) == 1 and invalidated == [] and not list(tmp_path.iterdir())
    assert caught.value.__suppress_context__ is True
    assert_safe(str(caught.value))


@pytest.mark.parametrize('cause,kind,number', [
    (ssl.SSLCertVerificationError(1, SECRET), 'ssl_certificate_verification', 1),
    (socket.gaierror(-2, SECRET), 'dns_error', -2),
])
def test_tdx_canonical_underlying_cause_survives_wrapping(tmp_path, cause, kind, number):
    error = httpx.ConnectError(SECRET)
    error.__cause__ = cause
    def response(request):
        raise error
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(MarketDependencyError) as caught:
            PublicMarketProvider._dependency_call('source_unavailable', TdxDailyArchives(client, tmp_path).fetch, DAY)
    facts = caught.value.source_diagnostics
    assert facts['underlying_kind'] == kind and facts['underlying_errno'] == number
    assert_progress(caught.value, stage='request', code='network_error')


@pytest.mark.parametrize('kind,category', [(httpx.ReadTimeout, 'read_timeout'), (httpx.ReadError, 'read_error')])
def test_interrupted_body_records_partial_byte_count_and_closes(tmp_path, kind, category):
    class InterruptedBody(httpx.SyncByteStream):
        closed = False
        def __iter__(self):
            yield b'partial'
            raise kind(SECRET)
        def close(self):
            self.closed = True
    body, calls = InterruptedBody(), []
    def response(request):
        calls.append(request)
        return httpx.Response(200, stream=body)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(MarketDependencyError) as caught:
            PublicMarketProvider._dependency_call('source_unavailable', TdxDailyArchives(client, tmp_path).fetch, DAY)
    assert_progress(caught.value, stage='request', code='network_error', status=200, size=7)
    assert caught.value.source_diagnostics['transport_category'] == category
    assert body.closed and len(calls) == 1 and not list(tmp_path.iterdir())


@pytest.mark.parametrize('payload,stage', [(b'not zip', 'zip_parse'), (b'', 'zip_parse'),
    (zipped({'../unsafe': b'x'}), 'zip_parse'), (daily(date='261105'), 'records_parse'),
    (daily(close=float('nan')), 'records_parse'), (daily(seq=1), 'records_parse')])
def test_validation_failure_remains_invalid_archive_and_uncached(tmp_path, payload, stage):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=payload))) as client:
        with pytest.raises(MarketDependencyError) as caught:
            load_daily(TdxDailyArchives(client, tmp_path))
    assert caught.value.code == 'invalid_tdx_archive'
    assert_progress(caught.value, stage=stage, code='invalid_tdx_archive', size=len(payload))
    assert not list(tmp_path.iterdir())


def test_download_size_failure_retains_request_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(market_bulk, 'MAX_ZIP_BYTES', 4)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'12345'))) as client:
        with pytest.raises(SinaDataError) as caught:
            TdxDailyArchives(client, tmp_path).fetch(DAY)
    assert_progress(caught.value, stage='request', code='invalid_tdx_archive', status=200, size=5)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('oversized', [False, True])
def test_bad_cache_stops_without_network_and_preserves_bytes(tmp_path, monkeypatch, oversized):
    source = TdxDailyArchives(None, tmp_path)
    _, _, filename = source._cache_identity(DAY)
    payload = b'invalid cache'
    (tmp_path / filename).write_bytes(payload)
    if oversized:
        monkeypatch.setattr(market_bulk, 'MAX_ZIP_BYTES', len(payload) - 1)
    with pytest.raises(SinaDataError) as caught:
        source.fetch(DAY)
    assert_progress(caught.value, stage='cache_read' if oversized else 'zip_parse',
                    code='invalid_tdx_archive', cache='hit', size=len(payload))
    assert (tmp_path / filename).read_bytes() == payload


def test_cache_read_error_has_stage_and_does_not_expose_path(tmp_path, monkeypatch):
    source = TdxDailyArchives(None, tmp_path)
    _, _, filename = source._cache_identity(DAY)
    payload = daily()
    (tmp_path / filename).write_bytes(payload)
    def fail(path):
        raise PermissionError(errno.EACCES, SECRET)
    monkeypatch.setattr(Path, 'read_bytes', fail)
    with pytest.raises(MarketDependencyError) as caught:
        load_daily(source)
    assert_progress(caught.value, stage='cache_read', code=None, cache='hit', size=len(payload))
    assert caught.value.code == 'source_unavailable'
    assert caught.value.source_diagnostics['os_errno'] == errno.EACCES
    assert 'transport_category' not in caught.value.source_diagnostics


def test_cache_write_error_keeps_valid_bytes_unpublished(tmp_path, monkeypatch):
    payload = daily()
    def fail(*args):
        raise OSError(errno.ENOSPC, SECRET)
    monkeypatch.setattr(market_bulk.os, 'replace', fail)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=payload))) as client:
        with pytest.raises(MarketDependencyError) as caught:
            load_daily(TdxDailyArchives(client, tmp_path))
    assert_progress(caught.value, stage='cache_write', code=None, size=len(payload))
    assert caught.value.code == 'source_unavailable' and not list(tmp_path.iterdir())
    assert caught.value.source_diagnostics['os_errno'] == errno.ENOSPC
    assert 'transport_category' not in caught.value.source_diagnostics


def test_success_logs_bounded_progress_without_source_url_or_headers(tmp_path, caplog, monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(market_bulk, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    payload, calls = daily(), []
    def response(request):
        calls.append(request)
        clock.now = 101.25
        return httpx.Response(200, content=payload, headers={'x-private': SECRET})
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        source = TdxDailyArchives(client, tmp_path, base_url='https://private.invalid/private-token')
        with caplog.at_level(logging.INFO, logger='app.services.market_bulk'):
            first, second = source.fetch(DAY), source.fetch(DAY)
    assert first == second and first['publication_complete'] is None
    assert first['verified'] is True and len(first['rows']) == 2 and len(calls) == 1
    assert f'archive_bytes={len(payload)} records=2 elapsed_ms=1250' in caplog.text
    assert 'source_id=tdx_daily_archive source_date=2026-11-06 cache_state=miss' in caplog.text
    assert 'source_id=tdx_daily_archive source_date=2026-11-06 cache_state=hit' in caplog.text
    assert_safe(caplog.text)
    assert 'private-token' not in caplog.text and source._cache_identity(DAY)[2] not in caplog.text


@pytest.mark.parametrize('at_body', [False, True])
def test_cancellation_is_identical_unannotated_and_uncached(tmp_path, at_body):
    original = CollectionCancelled()
    class CancelledBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b'prefix'
            raise original
    def response(request):
        if at_body:
            return httpx.Response(200, stream=CancelledBody())
        raise original
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(CollectionCancelled) as caught:
            load_daily(TdxDailyArchives(client, tmp_path))
    assert caught.value is original and not hasattr(original, 'source_diagnostics')
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('unsafe', [SECRET, True, -1, 2**80, {'private': SECRET}])
def test_progress_fields_are_revalidated_when_copied_and_rendered(unsafe):
    error = SinaDataError(SECRET)
    error.source_diagnostics = {key: unsafe for key in ('source_date', 'cache_state', 'archive_bytes', 'elapsed_ms')}
    wrapped = copy_source_failure(error, MarketDependencyError('source_unavailable'))
    assert source_failure_details(wrapped) == 'source_id=- source_stage=- source_code=- http_status=-'
    record_source_progress(wrapped, source_date=unsafe, cache_state=unsafe, archive_bytes=unsafe, elapsed_ms=unsafe)
    assert source_failure_details(wrapped) == 'source_id=- source_stage=- source_code=- http_status=-'


@pytest.mark.parametrize('invalid_date', ['2026-11-31', '20261106', '2026-11-06\n', 'x' * 10000])
def test_progress_date_must_be_canonical(invalid_date):
    error = SinaDataError('safe')
    record_source_progress(error, source_date=invalid_date)
    assert 'source_date=' not in source_failure_details(error)


def test_collection_log_exposes_tdx_facts_but_never_raw_failure(tmp_path, caplog):
    collection, provider, writer, _ = worker(tmp_path / 'collection')
    calls = []
    def response(request):
        calls.append(request)
        raise httpx.ReadTimeout(SECRET)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        source = TdxDailyArchives(client, tmp_path / 'cache')
        provider.fetch_universe = lambda day: load_daily(source, day)
        with caplog.at_level(logging.WARNING, logger='app.services.market_collection'):
            assert not collection.tick()
    assert len(calls) == 1 and writer.calls == []
    assert collection.status()['runs'][0]['error'] == 'source_unavailable'
    assert 'source_id=tdx_daily_archive source_stage=request source_code=network_error http_status=-' in caplog.text
    assert 'transport_category=read_timeout source_date=2026-11-10 cache_state=miss archive_bytes=0 elapsed_ms=' in caplog.text
    assert_safe(caplog.text + json.dumps(collection.status()))


@pytest.mark.parametrize('unsafe', [SECRET, True, 2**80, {'private': SECRET}])
def test_os_errno_is_revalidated(unsafe):
    error = SinaDataError(SECRET)
    error.source_diagnostics = {'os_errno': unsafe}
    assert 'os_errno' not in source_failure_details(copy_source_failure(error, MarketDependencyError('source_unavailable')))


def test_longer_timeout_is_daily_only_and_shared_client_is_unchanged(tmp_path):
    requests = []
    payload = daily()
    def response(request):
        requests.append(request)
        return httpx.Response(200, content=payload)
    original = httpx.Timeout(7.0, connect=3.0)
    with httpx.Client(transport=httpx.MockTransport(response), timeout=original) as client:
        source = TdxDailyArchives(client, tmp_path)
        first = load_daily(source)
        assert load_daily(source) == first
        with pytest.raises(SinaDataError):
            market_bulk.TdxCorporateActions(client, lookup_table=[0] * 1042).fetch()
        assert client.timeout == original
    assert len(requests) == 2  # the second daily read uses validated cache
    assert requests[0].extensions['timeout'] == {'connect': 8., 'read': 120., 'write': 20., 'pool': 20.}
    assert requests[1].extensions['timeout'] == {'connect': 3., 'read': 7., 'write': 7., 'pool': 7.}
    assert requests[0].headers.get('range') is None


def test_slow_daily_stream_completes_with_low_frequency_progress_and_valid_cache(tmp_path, caplog, monkeypatch):
    clock = SimpleNamespace(now=0.)
    monkeypatch.setattr(market_bulk, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    payload, calls = daily(), []
    times = [31., 32., 60., 61., 99., 210.]
    class SlowBody(httpx.SyncByteStream):
        closed = False
        def __iter__(self):
            for i, moment in enumerate(times):
                clock.now = moment
                yield payload[i * len(payload) // len(times):(i + 1) * len(payload) // len(times)]
        def close(self):
            self.closed = True
    body = SlowBody()
    def response(request):
        calls.append(request)
        return httpx.Response(200, stream=body)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        source = TdxDailyArchives(client, tmp_path)
        with caplog.at_level(logging.INFO, logger='app.services.market_bulk'):
            result = load_daily(source)
            assert load_daily(source) == result
    assert body.closed and len(calls) == 1 and len(result) == 2
    progress = [record.message for record in caplog.records if 'Market source archive progress' in record.message]
    elapsed = [int(message.rsplit('elapsed_ms=', 1)[1]) for message in progress]
    assert elapsed == [31000, 61000, 99000, 210000]
    assert all(right - left >= 30000 for left, right in zip(elapsed, elapsed[1:]))
    assert all('source_date=2026-11-06' in message and 'received_bytes=' in message for message in progress)
    assert 'Market source archive complete' in caplog.text
    assert len(list(tmp_path.glob('*.zip'))) == 1


@pytest.mark.parametrize('last_read', [600., 715.])
def test_download_budget_stops_after_in_flight_read_without_retry_or_invalidating(tmp_path, monkeypatch, last_read):
    clock = SimpleNamespace(now=0.)
    monkeypatch.setattr(market_bulk, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    calls, invalidated = [], []
    class SlowBody(httpx.SyncByteStream):
        closed = False
        def __iter__(self):
            for moment in (100., 200., 300., 400., 500., 595., last_read):
                clock.now = moment
                yield b'piece'
            pytest.fail('No further body reads after the budget is exhausted')
        def close(self):
            self.closed = True
    body = SlowBody()
    def response(request):
        calls.append(request)
        return httpx.Response(200, stream=body)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        source = TdxDailyArchives(client, tmp_path)
        source.invalidate = invalidated.append
        with pytest.raises(MarketDependencyError) as caught:
            load_daily(source)
    assert caught.value.code == 'source_unavailable'
    assert_progress(caught.value, stage='request', code='request_deadline', status=200, size=30)
    # This is deliberately not asserted as an exact 600-second hard timeout.
    assert caught.value.source_diagnostics['elapsed_ms'] == int(last_read * 1000)
    assert body.closed and len(calls) == 1 and invalidated == [] and not list(tmp_path.iterdir())


def test_budget_checks_compressed_raw_chunks_even_when_decoding_yields_nothing(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=0.)
    monkeypatch.setattr(market_bulk, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    # A partial gzip header has wire bytes but no decoded ZIP bytes.
    header = gzip.compress(daily())[:6]
    class DripBody(httpx.SyncByteStream):
        closed = False
        def __iter__(self):
            for i, moment in enumerate((100., 200., 300., 400., 500., 601.)):
                clock.now = moment
                yield header[i:i + 1]
            pytest.fail('Compressed trickles must not evade the budget')
        def close(self):
            self.closed = True
    body = DripBody()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, stream=body, headers={'content-encoding': 'gzip'}))) as client:
        with pytest.raises(MarketDependencyError) as caught:
            load_daily(TdxDailyArchives(client, tmp_path))
    assert_progress(caught.value, stage='request', code='request_deadline', status=200, size=0)
    assert body.closed and not list(tmp_path.iterdir())


def test_streaming_content_decoding_and_paced_cancellation_are_preserved(tmp_path):
    collection, _, _, _ = worker(tmp_path / 'collection')
    payload = gzip.compress(daily())
    class CompressedBody(httpx.SyncByteStream):
        def __iter__(self):
            yield payload[:10]
            yield payload[10:]
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, stream=CompressedBody(), headers={'content-encoding': 'gzip'}))) as client:
        checks = []
        paced = PacedHTTPClient(client, collection, lambda: checks.append(True))
        result = load_daily(TdxDailyArchives(paced, tmp_path / 'cache'))
    assert len(result) == 2 and len(checks) >= 7


def test_paced_cancellation_during_raw_body_remains_unannotated(tmp_path):
    collection, _, _, _ = worker(tmp_path / 'collection')
    cancellation = CollectionCancelled()
    state = SimpleNamespace(cancelled=False)
    class CancelBody(httpx.SyncByteStream):
        closed = False
        def __iter__(self):
            state.cancelled = True
            yield b'partial'
            pytest.fail('Paced cancellation must stop before another raw read')
        def close(self):
            self.closed = True
    body = CancelBody()
    def check():
        if state.cancelled:
            raise cancellation
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=body))) as client:
        source = TdxDailyArchives(PacedHTTPClient(client, collection, check), tmp_path / 'cache')
        with pytest.raises(CollectionCancelled) as caught:
            load_daily(source)
    assert caught.value is cancellation and not hasattr(cancellation, 'source_diagnostics')
    assert body.closed and not (tmp_path / 'cache').exists()


def test_corrupt_cache_is_quarantined_only_after_provider_validation(tmp_path):
    source = TdxDailyArchives(None, tmp_path)
    _, _, filename = source._cache_identity(DAY)
    payload = b'corrupt archive'
    (tmp_path / filename).write_bytes(payload)
    with pytest.raises(MarketDependencyError) as caught:
        load_daily(source)
    assert caught.value.code == 'invalid_tdx_archive'
    assert_progress(caught.value, stage='zip_parse', code='invalid_tdx_archive', cache='hit', size=len(payload))
    assert not (tmp_path / filename).exists()
    audits = list(tmp_path.glob('*.rejected-*.zip'))
    assert len(audits) == 1 and audits[0].read_bytes() == payload
