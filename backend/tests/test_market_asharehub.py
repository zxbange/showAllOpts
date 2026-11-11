"""Offline contract and integration tests. All API keys and quotes are synthetic."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
import logging

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.logging import StandardFormatter, logging_config
from app.services import market_asharehub as source
from app.services.market_asharehub import AShareHubDailyArchives, SOURCE_ID, URL
from app.services.market_bulk import TdxDailyArchives
from app.services.market_collection import MarketCollection
from app.services.market_diagnostics import source_failure_details
from app.services.market_public import PublicMarketProvider
from app.services.market_sina import (
    MarketDependencyError, SinaDataError, SinaRateLimitError, aggregate_crowding, aggregate_heat,
)
from test_market_dated_integration import make_dated, SYMBOLS
from test_market_public import DAYS, DAY

KEY = 'synthetic-asharehub-test-key'


def row(**changes):
    return dict(symbol='600001.SH', trade_date=DAY.strftime('%Y%m%d'), close=11.81,
                vol=1640276.39, amount=1927307.59, ma_qfq_20=999, close_qfq=999, **changes)


def archive(handler=None, *, cache_root=None):
    handler = handler or (lambda request: httpx.Response(200, json=[row()]))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return AShareHubDailyArchives(client, SecretStr(KEY), cache_root)


def test_exact_units_and_only_raw_values_are_used():
    result = archive().fetch(DAY)
    value = result['rows'][0]
    assert value == dict(symbol='sh600001', date=str(DAY), close='11.81',
                         volume='164027639', amount='1927307590')
    assert result['source_id'] == SOURCE_ID and result['publication_complete'] is None
    assert 'ma_qfq_20' not in json.dumps(result) and KEY not in json.dumps(result)


@pytest.mark.parametrize('field,value', [
    ('symbol', '600001.XSHG'), ('symbol', '600001.sh'), ('symbol', None),
    ('trade_date', '20260101'), ('trade_date', int(DAY.strftime('%Y%m%d'))),
    ('close', 0), ('close', -1), ('close', True), ('close', None), ('close', 'NaN'),
    ('vol', -1), ('vol', True), ('vol', '0.001'), ('amount', -1), ('amount', None),
    ('amount', 0), ('vol', '1e100'),
])
def test_invalid_date_identity_or_number_fails_closed(field, value):
    item = row(); item[field] = value
    with pytest.raises(SinaDataError) as error:
        AShareHubDailyArchives.parse(json.dumps([item]).encode(), DAY)
    assert error.value.code == 'snapshot_invalid'


@pytest.mark.parametrize('payload', [[], {}, [None], [row(), row()], {'data': [row()]},
                                   {'error': 'credential should not be echoed'}])
def test_empty_envelope_error_and_duplicate_payloads_rejected(payload):
    with pytest.raises(SinaDataError):
        archive(lambda r: httpx.Response(200, json=payload)).fetch(DAY)


def test_zero_observation_does_not_certify_suspension():
    item = row(); item.update(vol=0, amount=0)
    result = AShareHubDailyArchives.parse(json.dumps([item]).encode(), DAY)
    assert 'paused' not in result['rows'][0]


def test_only_exact_fixed_host_receives_header_and_redirects_disabled():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url == URL + '?trade_date=' + DAY.strftime('%Y%m%d')
        assert request.headers['X-API-Key'] == KEY
        return httpx.Response(302, headers={'Location': 'https://other.example/leak'})
    with pytest.raises(httpx.HTTPStatusError):
        archive(handler).fetch(DAY)
    assert len(calls) == 1


@pytest.mark.parametrize('status', [401, 403, 429])
def test_access_limits_stop_without_retry_fallback_or_error_body(status, tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=KEY)
    with pytest.raises(SinaRateLimitError) as error:
        archive(handler, cache_root=tmp_path).fetch(DAY)
    assert len(calls) == 1 and error.value.status_code == (403 if status == 401 else status)
    assert KEY not in str(error.value) + source_failure_details(error.value)
    assert not list(tmp_path.rglob('*.json'))


def test_shared_dependency_preserves_auth_failure_without_source_switch():
    provider, _ = make_dated()
    provider.archives = archive(lambda r: httpx.Response(403))
    with pytest.raises(SinaRateLimitError):
        provider.fetch_universe(DAY)


def test_download_size_deadline_and_cancellation(monkeypatch):
    monkeypatch.setattr(source, 'MAX_RESPONSE_BYTES', 5)
    with pytest.raises(SinaDataError):
        archive().fetch(DAY)
    monkeypatch.setattr(source, 'MAX_RESPONSE_BYTES', 64 * 1024 * 1024)
    monkeypatch.setattr(source, 'DOWNLOAD_BUDGET', 0)
    with pytest.raises(SinaDataError) as error:
        archive().fetch(DAY)
    assert error.value.code == 'request_deadline'
    from app.services.market_collection import CollectionCancelled
    adapter = archive(lambda r: pytest.fail('Cancelled before request'))
    adapter.client.check = lambda: (_ for _ in ()).throw(CollectionCancelled())
    with pytest.raises(CollectionCancelled):
        adapter.fetch(DAY)


def test_compact_cache_reused_by_new_instance_and_contains_no_key(tmp_path):
    first = archive(cache_root=tmp_path).fetch(DAY)
    second = archive(lambda r: pytest.fail('Must reuse cached daily response'), cache_root=tmp_path).fetch(DAY)
    assert first == second
    saved = list(tmp_path.rglob('*.json'))
    assert len(saved) == 1 and KEY not in saved[0].read_text()
    assert 'ma_qfq_20' not in saved[0].read_text()
    assert saved[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('mutation', ['hash', 'date', 'source', 'duplicate', 'url', 'number'])
def test_corrupt_cache_rejected_even_with_updated_hash(tmp_path, mutation):
    adapter = archive(cache_root=tmp_path); adapter.fetch(DAY)
    path = next(tmp_path.rglob('*.json')); payload = json.loads(path.read_bytes())
    result = payload['result']
    if mutation == 'hash': payload['sha256'] = '0' * 64
    elif mutation == 'date': result['date'] = '2020-01-01'
    elif mutation == 'source': result['source_id'] = 'tdx_daily_archive'
    elif mutation == 'duplicate': result['rows'].append(deepcopy(result['rows'][0]))
    elif mutation == 'url': result['source_url'] = 'https://other.example/'
    else: result['rows'][0]['amount'] = 'NaN'
    if mutation != 'hash': payload['sha256'] = sha256(source._encoded(result)).hexdigest()
    path.write_bytes(source._encoded(payload))
    with pytest.raises(SinaDataError):
        archive(lambda r: pytest.fail('Corrupt cache never refetches in this attempt'), cache_root=tmp_path).fetch(DAY)
    assert adapter.invalidate(DAY) is True and adapter.invalidate(DAY) is False


def test_cache_symlink_not_followed(tmp_path):
    target = tmp_path / 'do-not-read'; target.write_text('not cache')
    adapter = archive(cache_root=tmp_path)
    adapter.cache_root.mkdir()
    (adapter.cache_root / adapter._filename(DAY)).symlink_to(target)
    with pytest.raises(OSError):
        adapter.fetch(DAY)
    assert target.read_text() == 'not cache'


def configured_provider(*, cache_root=None, missing=(), paused=None):
    original, calls = make_dated(paused=paused)
    old_fetch = original.archives.fetch
    def handler(request):
        target = date.fromisoformat(str(request.url.params['trade_date'])[:4] + '-' +
                                   str(request.url.params['trade_date'])[4:6] + '-' +
                                   str(request.url.params['trade_date'])[6:])
        rows = []
        for raw in old_fetch(target)['rows']:
            if (raw['symbol'], target) in missing:
                continue
            rows.append(dict(symbol=raw['symbol'][2:] + '.' + raw['symbol'][:2].upper(),
                trade_date=target.strftime('%Y%m%d'), close=raw['close'],
                vol=str(Decimal(raw['volume']) / 100), amount=str(Decimal(raw['amount']) / 1000),
                ma_qfq_20=99999, close_qfq=99999))
        # BJ remains outside the independently selected SSE/SZSE pool.
        rows.append(dict(symbol='920001.BJ', trade_date=target.strftime('%Y%m%d'), close=10, vol=1, amount=1))
        return httpx.Response(200, json=rows)
    provider = PublicMarketProvider(httpx.Client(transport=httpx.MockTransport(handler)),
        cache_root=cache_root, asharehub_api_key=SecretStr(KEY), master=original.master,
        counts=original.counts, suspensions=original.suspensions, calendar_loader=original.calendar_loader,
        actions=original.actions, listing_metadata=original.listing_metadata)
    provider._prior_close = original._prior_close
    return provider, calls


def test_original_twenty_market_days_formula_pool_and_crowding_remain_identical(tmp_path):
    baseline, _ = make_dated()
    expected = baseline.fetch_universe(DAY)
    provider, calls = configured_provider(cache_root=tmp_path)
    snapshot = provider.fetch_universe(DAY)
    assert [r['symbol'] for r in snapshot['rows']] == SYMBOLS
    assert aggregate_crowding(snapshot) == aggregate_crowding(expected)
    quotes = [provider.fetch_quote(r['symbol'], DAY, snapshot=r) for r in snapshot['rows']]
    reference = [baseline.fetch_quote(r['symbol'], DAY, snapshot=r) for r in expected['rows']]
    assert aggregate_heat(snapshot, quotes) == aggregate_heat(expected, reference)
    assert quotes[0]['ma20'] == '19.5'
    assert SOURCE_ID in quotes[0]['evidence']['sources']
    assert len(calls['archive']) == len(calls['master']) == len(calls['count']) == 20
    assert snapshot['universe_evidence']['source_archive_evidence']['source_id'] == SOURCE_ID
    assert provider.snapshot_compatible(snapshot)
    assert snapshot['rows'][0]['source_observation']['source_url'].startswith(URL)
    assert len(snapshot['rows'][0]['source_observation']['source_digest']) == 64


def test_missing_stock_is_not_dropped_or_assumed_paused():
    provider, _ = configured_provider(missing=[(SYMBOLS[0], DAY)])
    with pytest.raises(SinaDataError) as error:
        provider.fetch_universe(DAY)
    assert error.value.code == 'paused_unknown'


def test_confirmed_paused_stock_stays_in_crowding_pool_not_heat_divisor():
    provider, _ = configured_provider(paused={SYMBOLS[0]: [DAY]})
    snapshot = provider.fetch_universe(DAY)
    assert len(snapshot['rows']) == 100
    result = aggregate_crowding(snapshot)
    assert result['publishable'] and result['universe_count'] == 100 and result['top_count'] == 5
    quote = provider.fetch_quote(SYMBOLS[0], DAY, snapshot=snapshot['rows'][0])
    assert quote['status'] == 'paused' and not quote['eligible']


def test_unknown_historical_pause_still_blocks_heat_but_not_valid_target_crowding():
    provider, _ = configured_provider(missing=[(SYMBOLS[0], DAYS[5])])
    snapshot = provider.fetch_universe(DAY)
    assert aggregate_crowding(snapshot)['publishable']
    with pytest.raises(MarketDependencyError):
        provider.fetch_quote(SYMBOLS[0], DAY, snapshot=snapshot['rows'][0])


def test_source_specific_audit_cache_and_old_snapshot_are_not_reused(tmp_path):
    old, _ = make_dated(cache_root=tmp_path); old_snapshot = old.fetch_universe(DAY)
    new, calls = configured_provider(cache_root=tmp_path)
    assert not new.snapshot_compatible(old_snapshot)
    current = new.fetch_universe(DAY)
    assert calls['master'] == [DAY] and current['universe_evidence']['source_archive_evidence']['source_id'] == SOURCE_ID
    assert not old.snapshot_compatible(current)
    assert old._audit_path(DAY) != new._audit_path(DAY)


def test_missing_key_preserves_existing_source_and_status_has_no_secret(tmp_path):
    for value, expected in [('', TdxDailyArchives), (KEY, AShareHubDailyArchives)]:
        settings = Settings(_env_file=None, log_dir=tmp_path / ('key' if value else 'none'), asharehub_api_key=value)
        owner = MarketCollection(settings)
        provider = owner.provider_factory(httpx.Client())
        assert isinstance(provider.archives, expected)
        state = owner.status()
        assert state['raw_price_source'] == (SOURCE_ID if value else 'tdx_daily_archive')
        assert KEY not in json.dumps(state) + json.dumps(owner.configuration.public())
        assert KEY not in repr(settings)


def test_logging_redacts_configured_key_even_without_a_field_label(tmp_path):
    settings = Settings(_env_file=None, log_dir=tmp_path, asharehub_api_key=KEY)
    formatter = StandardFormatter(logging_config(settings)['formatters']['standard']['secrets'])
    record = logging.LogRecord('test', logging.INFO, __file__, 1, 'value %s', (KEY,), None)
    assert KEY not in formatter.format(record)


def test_precise_unit_conversion_is_exact_not_default_context_rounded():
    item = row(); item['amount'] = '123456789012345678901.123456789'
    result = AShareHubDailyArchives.parse(json.dumps([item]).encode(), DAY)
    assert result['rows'][0]['amount'] == '123456789012345678901123.456789'
