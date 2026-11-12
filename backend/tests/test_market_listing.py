from types import SimpleNamespace
import httpx
import pytest
from app.services.market_listing import ListingMetadataSources
from app.services.market_sina import SinaDataError, SinaRateLimitError

@pytest.mark.parametrize('error', [httpx.ConnectTimeout('secret'), SinaDataError('absent', code='listing_not_found')])
def test_fallback_only_on_missing_or_transient(error):
    def fail(*args): raise error
    source = ListingMetadataSources(SimpleNamespace(fetch=fail), SimpleNamespace(fetch=lambda s,d: {'symbol': s}))
    assert source.fetch('sh600000', '2026-11-09')['symbol'] == 'sh600000'

@pytest.mark.parametrize('error', [SinaRateLimitError(403), SinaRateLimitError(429),
    SinaDataError('bad date', code='dated_membership_gap'), SinaDataError('bad body', code='invalid_json')])
def test_denial_or_invalid_proof_never_tries_other_source(error):
    def fail(*args): raise error
    def forbidden(*args): pytest.fail('must not fall back')
    source = ListingMetadataSources(SimpleNamespace(fetch=fail), SimpleNamespace(fetch=forbidden))
    with pytest.raises(type(error)):
        source.fetch('sh600000', '2026-11-09')


def test_primary_result_reused_without_fallback():
    source = ListingMetadataSources(SimpleNamespace(fetch=lambda s,d: {'symbol': s}),
                                    SimpleNamespace(fetch=lambda *a: pytest.fail('fallback')))
    assert source.fetch('sh600000', '2026-11-09')['symbol'] == 'sh600000'


def test_batch_connection_failure_has_short_cooldown_not_permanent_poison():
    clock = [100.0]
    calls = []
    def fail(symbol, day):
        calls.append(symbol)
        raise httpx.ConnectTimeout('temporary')
    source = ListingMetadataSources(SimpleNamespace(fetch=fail),
        SimpleNamespace(fetch=lambda s,d: {'symbol': s}), monotonic=lambda: clock[0])
    assert source.fetch('sh600000', '2026-11-09')['symbol'] == 'sh600000'
    source.fetch('sh600001', '2026-11-09')
    assert len(calls) == 1
    clock[0] += 61
    source.fetch('sh600001', '2026-11-09')
    assert len(calls) == 2
