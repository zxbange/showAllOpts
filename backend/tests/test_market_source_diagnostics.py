"""Offline source diagnostics must not weaken validation or expose inputs."""
import json
import logging
import socket
import ssl

import httpx
import pytest

from app.services.market_collection import CollectionCancelled, PacedHTTPClient
from app.services.market_diagnostics import record_transport_failure, source_failure_context, source_failure_details
from app.services.market_public import MarketDependencyError, PublicMarketProvider
from app.services.market_sina import SinaRateLimitError
from app.services.market_universe import ExchangeSecurityMaster, UniverseDataError
from test_market_collection import worker
from test_market_public import DAY, make_provider
from test_market_universe import ExchangeFixture, NOW, fetch


SECRET = 'https://private.invalid/?token=never-log-me\nFORGED LOG'
SOURCES = ('sse_current', 'sse_delisted', 'szse_current', 'szse_cdr',
           'szse_delisted', 'szse_listing_suspended')


def source_of(request):
    query = request.url.params
    if request.url.host == 'query.sse.com.cn':
        return 'sse_delisted' if query['COMPANY_STATUS'] == '3' else 'sse_current'
    return {('1110', 'tab1'): 'szse_current', ('1110', 'tab3'): 'szse_cdr',
            ('1793_ssgs', 'tab2'): 'szse_delisted',
            ('1793_ssgs', 'tab1'): 'szse_listing_suspended'}[query['CATALOGID'], query['TABKEY']]


@pytest.mark.parametrize('source_id', SOURCES)
@pytest.mark.parametrize('status', [403, 429, 503])
def test_each_catalogue_failure_retains_safe_source_status_and_stops(source_id, status):
    fixture, calls = ExchangeFixture(), []
    def response(request):
        calls.append(source_of(request))
        return httpx.Response(status, text=SECRET) if calls[-1] == source_id else fixture.serve(request)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(UniverseDataError) as caught:
            ExchangeSecurityMaster(client, now=lambda: NOW).fetch(DAY)
    error = caught.value
    assert error.source_diagnostics == {
        'source_id': source_id, 'stage': 'request',
        'code': 'rate_limited' if status in (403, 429) else 'http_error', 'status_code': status,
    }
    assert calls[-1] == source_id and calls.count(source_id) == 1
    assert 'never-log-me' not in source_failure_details(error)


@pytest.mark.parametrize('fault,source_id,stage,code', [
    ('json', 'sse_current', 'json_parse', 'invalid_json'),
    ('sse_page', 'sse_current', 'page_validation', 'invalid_page'),
    ('sse_date', 'sse_current', 'rows_validation', 'invalid_date'),
    ('sz_metadata', 'szse_current', 'metadata_validation', 'invalid_page'),
    ('sz_workbook', 'szse_current', 'workbook_parse', 'invalid_workbook'),
    ('sz_date', 'szse_current', 'rows_validation', 'invalid_date'),
    ('overlap', 'exchange_master', 'catalogue_validation', 'ambiguous_listing_intervals'),
])
def test_strict_validation_retains_original_stage_and_code(fault, source_id, stage, code):
    fixture = ExchangeFixture()
    if fault == 'sse_date':
        fixture.sse_current[0]['LIST_DATE'] = SECRET
    elif fault == 'sz_date':
        fixture.sz['1110', 'tab1'][1][0][2] = SECRET
    elif fault == 'overlap':
        fixture.sse_delisted[0]['A_STOCK_CODE'] = '600001'
    def response(request):
        if fault == 'json':
            return httpx.Response(200, text=SECRET)
        if fault == 'sse_page':
            return httpx.Response(200, json={'private': SECRET})
        if source_of(request) == 'szse_current':
            if fault == 'sz_metadata':
                return httpx.Response(200, json={'private': SECRET})
            if fault == 'sz_workbook' and request.url.params.get('SHOWTYPE'):
                return httpx.Response(200, text=SECRET)
        return fixture.serve(request)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(UniverseDataError) as caught:
            ExchangeSecurityMaster(client, now=lambda: NOW).fetch(DAY)
    assert caught.value.code == code
    assert caught.value.source_diagnostics == {
        'source_id': source_id, 'stage': stage, 'code': code, 'status_code': None,
    }
    assert 'never-log-me' not in source_failure_details(caught.value)


@pytest.mark.parametrize('status', [403, 429, 503])
def test_public_wrapper_preserves_diagnostics_without_invalidating_archive(status):
    provider, calls, invalidated = make_provider(), [], []
    def response(request):
        calls.append(request)
        return httpx.Response(status, text=SECRET)
    provider.archives.invalidate = invalidated.append
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        provider.master = ExchangeSecurityMaster(client, now=lambda: NOW)
        expected = SinaRateLimitError if status in (403, 429) else MarketDependencyError
        with pytest.raises(expected) as caught:
            provider.fetch_universe(DAY)
    assert caught.value.code == ('rate_limited' if status in (403, 429) else 'universe_incomplete')
    assert caught.value.source_diagnostics['source_id'] == 'sse_current'
    assert caught.value.source_diagnostics['status_code'] == status
    assert caught.value.source_diagnostics['code'] == ('rate_limited' if status in (403, 429) else 'http_error')
    assert len(calls) == 1 and invalidated == []


@pytest.mark.parametrize('status', [403, 429])
def test_paced_denial_is_annotated_before_propagation(tmp_path, status):
    collection, _, _, _ = worker(tmp_path)
    calls = []
    def response(request):
        calls.append(request)
        return httpx.Response(status, text=SECRET)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        paced = PacedHTTPClient(client, collection, lambda: None)
        with pytest.raises(SinaRateLimitError) as caught:
            PublicMarketProvider._dependency_call('universe_incomplete',
                ExchangeSecurityMaster(paced, now=lambda: NOW).fetch, DAY)
    assert caught.value.source_diagnostics == {
        'source_id': 'sse_current', 'stage': 'request', 'code': 'rate_limited', 'status_code': status,
    }
    assert len(calls) == 1


def test_cancellation_is_not_annotated_or_wrapped():
    cancellation = CollectionCancelled()
    def response(request):
        raise cancellation
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(CollectionCancelled) as caught:
            PublicMarketProvider._dependency_call('universe_incomplete',
                ExchangeSecurityMaster(client, now=lambda: NOW).fetch, DAY)
    assert caught.value is cancellation and not hasattr(cancellation, 'source_diagnostics')


@pytest.mark.parametrize('unsafe', [SECRET, {'private': SECRET}, ['request'], True])
def test_injected_attributes_cannot_escape_allowlists(unsafe):
    error = UniverseDataError(SECRET, code=unsafe, status_code=unsafe)
    error.source_diagnostics = {key: unsafe for key in (
        'source_id', 'stage', 'code', 'status_code', 'transport_category', 'underlying_kind', 'underlying_errno')}
    def fail():
        with source_failure_context(source_id='szse_current', stage='rows_validation'):
            raise error
    with pytest.raises(MarketDependencyError) as caught:
        PublicMarketProvider._dependency_call('universe_incomplete', fail)
    details = source_failure_details(caught.value)
    assert details == 'source_id=szse_current source_stage=rows_validation source_code=- http_status=-'


@pytest.mark.parametrize('status,public_code', [(503, 'universe_incomplete'),
                                               (403, 'source_forbidden'), (429, 'source_rate_limited')])
def test_collection_logs_source_failure_but_keeps_public_status_safe(tmp_path, caplog, status, public_code):
    collection, provider, writer, clock = worker(tmp_path)
    fixture, calls = ExchangeFixture(), []
    def response(request):
        calls.append(source_of(request))
        return httpx.Response(status, text=SECRET) if calls[-1] == 'szse_current' else fixture.serve(request)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        master = ExchangeSecurityMaster(client, now=lambda: NOW)
        provider.fetch_universe = lambda day: PublicMarketProvider._dependency_call('universe_incomplete', master.fetch, day)
        with caplog.at_level(logging.WARNING, logger='app.services.market_collection'):
            assert not collection.tick()
            if status in (403, 429):
                prior_calls = len(calls)
                assert not collection.tick(clock.advance(minutes=31))
                assert len(calls) == prior_calls
    assert collection.status()['runs'][0]['error'] == public_code
    assert writer.calls == []
    assert 'source_id=szse_current source_stage=request' in caplog.text
    assert f'http_status={status}' in caplog.text
    assert ('source_code=http_error' if status == 503 else 'source_code=rate_limited') in caplog.text
    exposed = caplog.text + json.dumps(collection.status())
    assert all(secret not in exposed for secret in ('never-log-me', 'FORGED LOG', 'private.invalid'))


def test_public_wrapper_retains_original_validation_code():
    fixture = ExchangeFixture()
    fixture.omit_tab = True
    with pytest.raises(MarketDependencyError) as caught:
        PublicMarketProvider._dependency_call('universe_incomplete', fetch, fixture)
    assert caught.value.code == 'universe_incomplete'
    assert caught.value.source_diagnostics == {
        'source_id': 'szse_current', 'stage': 'metadata_validation', 'code': 'invalid_page', 'status_code': None,
    }


@pytest.mark.parametrize('kind,category', [
    (httpx.ConnectTimeout, 'connect_timeout'), (httpx.ReadTimeout, 'read_timeout'),
    (httpx.WriteTimeout, 'write_timeout'), (httpx.PoolTimeout, 'pool_timeout'),
    (httpx.ConnectError, 'connect_error'), (httpx.ProxyError, 'proxy_error'),
    (httpx.LocalProtocolError, 'protocol_error'), (httpx.RemoteProtocolError, 'protocol_error'),
    (httpx.ReadError, 'read_error'), (httpx.WriteError, 'write_error'),
    (httpx.DecodingError, 'decoding_error'), (httpx.HTTPError, 'other_httpx'),
])
def test_transport_category_survives_wrapping_without_retry_or_raw_message(kind, category):
    provider, calls, invalidated = make_provider(), [], []
    provider.archives.invalidate = invalidated.append
    def response(request):
        calls.append(request)
        raise kind(SECRET)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        provider.master = ExchangeSecurityMaster(client, now=lambda: NOW)
        with pytest.raises(MarketDependencyError) as caught:
            provider.fetch_universe(DAY)
    assert caught.value.code == 'universe_incomplete'
    assert caught.value.source_diagnostics == {
        'source_id': 'sse_current', 'stage': 'request', 'code': 'network_error', 'status_code': None,
        'transport_category': category,
    }
    assert len(calls) == 1 and invalidated == []
    assert f'transport_category={category}' in source_failure_details(caught.value)
    assert all(secret not in source_failure_details(caught.value)
               for secret in ('never-log-me', 'FORGED LOG', 'private.invalid'))


@pytest.mark.parametrize('kind,underlying,number', [
    (ssl.SSLCertVerificationError, 'ssl_certificate_verification', 1),
    (socket.gaierror, 'dns_error', -2),
])
@pytest.mark.parametrize('link', ['__cause__', '__context__'])
def test_canonical_tls_or_dns_chain_retains_only_type_category_and_errno(kind, underlying, number, link):
    low_level, middle = kind(number, SECRET), RuntimeError(SECRET)
    setattr(middle, link, low_level)
    transport = httpx.ConnectError(SECRET)
    setattr(transport, link, middle)
    def response(request):
        raise transport
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(MarketDependencyError) as caught:
            PublicMarketProvider._dependency_call('universe_incomplete',
                ExchangeSecurityMaster(client, now=lambda: NOW).fetch, DAY)
    assert caught.value.source_diagnostics == {
        'source_id': 'sse_current', 'stage': 'request', 'code': 'network_error', 'status_code': None,
        'transport_category': 'connect_error', 'underlying_kind': underlying, 'underlying_errno': number,
    }
    details = source_failure_details(caught.value)
    assert f'underlying_kind={underlying} underlying_errno={number}' in details
    assert all(secret not in details for secret in ('never-log-me', 'FORGED LOG', 'private.invalid'))


def test_transport_text_and_impostor_class_names_are_not_cause_evidence():
    impostor = type('SSLCertVerificationError', (Exception,), {})(SECRET)
    transport = httpx.ConnectError('certificate verify failed DNS socket.gaierror ' + SECRET)
    transport.__cause__ = impostor
    wrapped = record_transport_failure(transport, UniverseDataError('safe', code='network_error'))
    assert wrapped.source_diagnostics['transport_category'] == 'connect_error'
    assert 'underlying_kind' not in wrapped.source_diagnostics
    assert 'never-log-me' not in source_failure_details(wrapped)


@pytest.mark.parametrize('unsafe', [SECRET, True, 2**80, {'private': SECRET}])
def test_underlying_errno_is_bounded_integer_not_arbitrary_data(unsafe):
    cause = socket.gaierror(-2, SECRET)
    cause.errno = unsafe
    transport = httpx.ConnectError(SECRET)
    transport.__cause__ = cause
    wrapped = record_transport_failure(transport, UniverseDataError('safe', code='network_error'))
    assert wrapped.source_diagnostics['underlying_kind'] == 'dns_error'
    assert 'underlying_errno' not in wrapped.source_diagnostics
    assert 'never-log-me' not in source_failure_details(wrapped)


@pytest.mark.parametrize('mode', ['suppressed', 'cycle', 'deep', 'explicit_cause'])
def test_transport_cause_scan_is_bounded_and_honors_explicit_cause(mode):
    transport = httpx.ConnectError(SECRET)
    if mode == 'suppressed':
        transport.__context__ = socket.gaierror(-2, SECRET)
        transport.__suppress_context__ = True
    elif mode == 'cycle':
        transport.__cause__ = RuntimeError(SECRET)
        transport.__cause__.__cause__ = transport
    elif mode == 'deep':
        current = transport
        for _ in range(12):
            current.__cause__ = RuntimeError(SECRET)
            current = current.__cause__
        current.__cause__ = socket.gaierror(-2, SECRET)
    else:
        transport.__context__ = socket.gaierror(-2, SECRET)
        transport.__cause__ = RuntimeError(SECRET)
    wrapped = record_transport_failure(transport, UniverseDataError('safe', code='network_error'))
    assert wrapped.source_diagnostics['transport_category'] == 'connect_error'
    assert 'underlying_kind' not in wrapped.source_diagnostics


@pytest.mark.parametrize('context', [
    {'transport_category': SECRET, 'underlying_kind': 'dns_error', 'underlying_errno': -2},
    {'transport_category': 'connect_error', 'underlying_kind': SECRET, 'underlying_errno': -2},
    {'transport_category': 'connect_error', 'underlying_kind': 'dns_error', 'underlying_errno': SECRET},
])
def test_transport_fields_are_revalidated_when_rendered(context):
    error = UniverseDataError(SECRET)
    error.source_diagnostics = context
    details = source_failure_details(error)
    assert all(secret not in details for secret in ('never-log-me', 'FORGED LOG', 'private.invalid'))
    if context['transport_category'] == SECRET:
        assert 'transport_category=' not in details and 'underlying_' not in details
    elif context['underlying_kind'] == SECRET:
        assert 'underlying_' not in details
    else:
        assert 'underlying_kind=dns_error' in details and 'underlying_errno=' not in details


def test_collection_transport_log_reaches_operator_without_leaking_cause_text(tmp_path, caplog):
    collection, provider, writer, _ = worker(tmp_path)
    calls = []
    def response(request):
        calls.append(request)
        error = httpx.ConnectError(SECRET, request=request)
        error.__cause__ = ssl.SSLCertVerificationError(1, SECRET)
        raise error
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        master = ExchangeSecurityMaster(client, now=lambda: NOW)
        provider.fetch_universe = lambda day: PublicMarketProvider._dependency_call('universe_incomplete', master.fetch, day)
        with caplog.at_level(logging.WARNING, logger='app.services.market_collection'):
            assert not collection.tick()
    assert len(calls) == 1 and writer.calls == []
    assert collection.status()['runs'][0]['error'] == 'universe_incomplete'
    assert 'source_id=sse_current source_stage=request source_code=network_error http_status=-' in caplog.text
    assert 'transport_category=connect_error underlying_kind=ssl_certificate_verification underlying_errno=1' in caplog.text
    exposed = caplog.text + json.dumps(collection.status())
    assert all(secret not in exposed for secret in ('never-log-me', 'FORGED LOG', 'private.invalid'))
