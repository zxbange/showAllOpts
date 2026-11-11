"""Offline source diagnostics must not weaken validation or expose inputs."""
import json
import logging

import httpx
import pytest

from app.services.market_collection import CollectionCancelled, PacedHTTPClient
from app.services.market_diagnostics import source_failure_context, source_failure_details
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
    error.source_diagnostics = {key: unsafe for key in ('source_id', 'stage', 'code', 'status_code')}
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
