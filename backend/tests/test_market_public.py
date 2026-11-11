from datetime import date, timedelta
from types import SimpleNamespace
from decimal import Decimal
import httpx
import pytest
from app.services.market_public import MarketDependencyError, PublicMarketProvider, parse_factors
from app.services.market_sina import SinaDataError, aggregate_heat

DAYS = [date(2026, 10, 12) + timedelta(days=n) for n in range(26)
        if (date(2026, 10, 12) + timedelta(days=n)).weekday() < 5]
DAY = DAYS[-1]


def member(symbol='sh689123', **extra):
    return dict(symbol=symbol, name='Future CDR', security_type='cdr',
                listed_from='2020-01-01', listed_until=None, **extra)


def make_provider(*, members=None, missing=(), pauses=None, prices=None, master_verified=True, actions=()):
    members = members or [member()]
    prices = prices or list(range(10, 30))
    def daily(day):
        rows = [dict(symbol=m['symbol'], name=m['name'], date=str(day),
                     close=str(prices[DAYS.index(day)]), amount='100', volume='10')
                for m in members if (m['symbol'], day) not in missing]
        return {'date': str(day), 'verified': True, 'rows': rows, 'source_digest': 'a' * 64}
    master = SimpleNamespace(fetch=lambda day: {'date': str(day), 'rows': members,
        'expected_count': len(members), 'evidence': {'membership_verified': master_verified}})
    source = SimpleNamespace(fetch=daily)
    pauses = pauses or {}
    halt = SimpleNamespace(fetch=lambda start, end: {'intervals': pauses})
    action_source = SimpleNamespace(fetch=lambda: {'verified': True, 'coverage_through': str(DAY), 'rows': list(actions)})
    return PublicMarketProvider(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        master=master, archives=source, actions=action_source, suspensions=halt,
        calendar_loader=lambda client: DAYS)


def test_dynamic_cdr_and_no_action_window_use_same_original_formula():
    provider = make_provider()
    snapshot = provider.fetch_universe(DAY)
    assert snapshot['rows'][0]['symbol'] == 'sh689123'
    quote = provider.fetch_quote('sh689123', DAY, snapshot=snapshot['rows'][0])
    result = aggregate_heat(snapshot, [quote])
    assert result['publishable'] and result['eligible_count'] == 1
    assert result['above_count'] == 1 and result['ratio'] == 1
    assert quote['ma20'] == '19.5'


def test_incomplete_historical_membership_blocks_snapshot():
    provider = make_provider(master_verified=False)
    with pytest.raises(SinaDataError, match='上市区间') as error:
        provider.fetch_universe(DAY)
    assert error.value.code == 'universe_coverage_unverified'


def test_missing_trade_is_not_assumed_suspended():
    provider = make_provider(missing=[('sh689123', DAYS[5])])
    snapshot = provider.fetch_universe(DAY)
    quote = provider.fetch_quote('sh689123', DAY, snapshot=snapshot['rows'][0])
    assert quote['error'] == 'paused_unknown'
    assert not aggregate_heat(snapshot, [quote])['publishable']


def test_confirmed_pause_forward_fills_only_that_session():
    provider = make_provider(missing=[('sh689123', DAYS[5])], pauses={'sh689123': [
        {'start': str(DAYS[5]), 'end': str(DAYS[5]), 'full_day': True}]})
    snapshot = provider.fetch_universe(DAY)
    quote = provider.fetch_quote('sh689123', DAY, snapshot=snapshot['rows'][0])
    assert quote['status'] == 'current'
    assert Decimal(quote['ma20']) == Decimal('19.45')


def test_intraday_halt_does_not_authorize_forward_fill():
    provider = make_provider(missing=[('sh689123', DAYS[5])], pauses={'sh689123': [
        {'start': str(DAYS[5]), 'end': str(DAYS[5]), 'full_day': False}]})
    snapshot = provider.fetch_universe(DAY)
    assert provider.fetch_quote('sh689123', DAY, snapshot=snapshot['rows'][0])['error'] == 'paused_unknown'


def test_confirmed_current_pause_stays_in_universe_but_not_heat_divisor():
    provider = make_provider(members=[member(), member('sz001234')],
        missing=[('sh689123', DAY)], pauses={'sh689123': [
            {'start': str(DAY), 'end': str(DAY), 'full_day': True}]})
    provider._prior_close = lambda symbol, day: (DAYS[-2], Decimal('28'))
    snapshot = provider.fetch_universe(DAY)
    quotes = [provider.fetch_quote(row['symbol'], DAY, snapshot=row) for row in snapshot['rows']]
    result = aggregate_heat(snapshot, quotes)
    assert result['universe_count'] == 2 and result['eligible_count'] == 1
    assert result['paused_count'] == 1 and result['publishable']


def test_listing_under_twenty_excluded_without_requesting_adjustment():
    m = member(); m['listed_from'] = str(DAYS[4])
    provider = make_provider(members=[m])
    provider.actions.fetch = lambda: pytest.fail('IPO exclusion does not need action inference')
    snapshot = provider.fetch_universe(DAY)
    quote = provider.fetch_quote(m['symbol'], DAY, snapshot=snapshot['rows'][0])
    assert quote['status'] == 'insufficient_history' and quote['history_count'] == 16


def test_delist_date_is_exclusive():
    m = member(); m.update(listed_until=str(DAY), listed_until_exclusive=True)
    provider = make_provider(members=[m])
    with pytest.raises(SinaDataError, match='上市区间'):
        provider.fetch_universe(DAY)


@pytest.mark.parametrize('payload', [
    'var sh689123qfq={"total":0,"data":[]}',
    'var sh689123qfq={"total":2,"data":[{"d":"2020-01-01","f":"1"}]}',
    'var otherqfq={"total":1,"data":[{"d":"2020-01-01","f":"1"}]}',
    'var sh689123qfq={"total":1,"data":[{"d":"2020-01-01","f":"0"}]}',
    'alert(1);var sh689123qfq={"total":1,"data":[{"d":"2020-01-01","f":"1"}]}',
])
def test_empty_or_invalid_factors_are_not_identity(payload):
    with pytest.raises(SinaDataError):
        parse_factors(payload, 'sh689123')


def test_valid_factor_assignment_uses_decimal_and_known_event_dates():
    factors = parse_factors('var sh689123qfq={"total":2,"data":['
        '{"d":"2026-10-20","f":"2"},{"d":"2020-01-01","f":"1"}]}; /* comment */', 'sh689123')
    assert factors == [(date(2020,1,1), Decimal(1)), (date(2026,10,20), Decimal(2))]


def test_known_action_requires_corresponding_factor():
    provider = make_provider(actions=[{'symbol': 'sh689123', 'date': str(DAYS[8]), 'category': 1}])
    provider._factors['sh689123'] = [(date(2020,1,1), Decimal('1'))]
    snapshot = provider.fetch_universe(DAY)
    quote = provider.fetch_quote('sh689123', DAY, snapshot=snapshot['rows'][0])
    assert quote['error'] == 'adjustment_unavailable'


def test_unverified_corporate_source_cannot_certify_no_events():
    provider = make_provider()
    provider.actions.fetch = lambda: {'verified': False, 'coverage_through': str(DAY), 'rows': []}
    snapshot = provider.fetch_universe(DAY)
    with pytest.raises(MarketDependencyError) as error:
        provider.fetch_quote('sh689123', DAY, snapshot=snapshot['rows'][0])
    assert error.value.code == 'adjustment_unavailable'


def test_real_suspension_adapter_contract_is_accepted():
    from datetime import datetime, timezone
    from app.services.market_suspensions import PublicSuspensions
    from test_market_suspensions import event, result
    event_row = event('689123', exchange='SH', start='2026-10-01 09:30:00', end=None)
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=result([event_row]))))
    provider = make_provider(missing=[('sh689123', DAY)])
    provider.suspensions = PublicSuspensions(client, now=lambda: datetime(2026,11,11,9,tzinfo=timezone.utc))
    provider._prior_close = lambda symbol, before: (DAYS[0] - timedelta(days=1), Decimal('28'))
    snapshot = provider.fetch_universe(DAY)
    quote = provider.fetch_quote('sh689123', DAY, snapshot=snapshot['rows'][0])
    assert quote['status'] == 'paused' and quote['error'] is None


def test_default_factor_path_never_uses_unverified_no_event_inference():
    provider = make_provider()
    provider.actions = None
    provider._factors['sh689123'] = [(date(2020,1,1), Decimal('2'))]
    snapshot = provider.fetch_universe(DAY)
    quote = provider.fetch_quote('sh689123', DAY, snapshot=snapshot['rows'][0])
    assert quote['ma20'] == '9.75'
    assert quote['qfq_close'] == '14.5'


def test_invalid_factor_count_is_not_truncated_to_integer():
    with pytest.raises(SinaDataError):
        parse_factors('var sh689123qfq={"total":1.5,"data":[{"d":"2020-01-01","f":"1"}]}','sh689123')


def test_changing_point_in_time_universe_invalidates_filtered_memory_cache():
    provider = make_provider(members=[member(), member('sz001234')])
    provider.prepare_snapshot({'rows': [member()]})
    assert set(provider._daily_rows(DAY)) == {'sh689123'}
    provider.prepare_snapshot({'rows': [member(), member('sz001234')]})
    assert set(provider._daily_rows(DAY)) == {'sh689123', 'sz001234'}


def test_missing_unexplained_window_bar_invalidates_archive_for_later_retry():
    provider = make_provider(missing=[('sh689123', DAYS[5])])
    invalidated = []
    provider.archives.invalidate = invalidated.append
    snapshot = provider.fetch_universe(DAY)
    quote = provider.fetch_quote('sh689123', DAY, snapshot=snapshot['rows'][0])
    assert quote['error'] == 'paused_unknown'
    assert invalidated == [DAYS[5]]
    assert DAYS[5] not in provider._daily


def empty_factor_fallback(provider, *, adjust=None, key='qfqday', omit=None, volume='10'):
    provider.actions = None
    calls = []
    def request(req):
        calls.append(req)
        if req.url.path.endswith('/qfq.js'):
            return httpx.Response(200, text='var sh689123qfq={"total":0,"data":[]}')
        rows = []
        for i, day in enumerate(DAYS):
            if day == omit:
                continue
            close = Decimal(10 + i)
            if adjust:
                close = adjust(i, close)
            rows.append([str(day), '10', f'{close:.3f}', '30', '10', volume])
        return httpx.Response(200, json={'code': 0, 'data': {'sh689123': {key: rows}}})
    provider.client = httpx.Client(transport=httpx.MockTransport(request))
    return calls


def test_empty_factors_allow_only_complete_dual_source_identity_far_from_ma():
    provider = make_provider()
    calls = empty_factor_fallback(provider)
    snapshot = provider.fetch_universe(DAY)
    quote = provider.fetch_quote('sh689123', DAY, snapshot=snapshot['rows'][0])
    assert quote['status'] == 'current' and quote['above'] is True
    evidence = quote['evidence']['dual_source_window']
    assert evidence['difference_error_bound'] == '0.0019'
    assert evidence['all_scenarios_joinquant_equivalence'] is False
    assert len(calls) == 2


@pytest.mark.parametrize('options', [
    {'key': 'day'}, {'omit': DAYS[4]}, {'volume': '100'},
    {'adjust': lambda i, close: close - Decimal('0.01') if i < 10 else close},
])
def test_restricted_identity_rejects_raw_key_missing_dates_unit_ambiguity_or_adjustment(options):
    provider = make_provider()
    empty_factor_fallback(provider, **options)
    snapshot = provider.fetch_universe(DAY)
    assert provider.fetch_quote('sh689123', DAY, snapshot=snapshot['rows'][0])['error'] == 'adjustment_unavailable'


def test_identity_quantization_boundary_remains_pending():
    provider = make_provider(prices=[Decimal('10')] * 20)
    empty_factor_fallback(provider, adjust=lambda i, close: Decimal('10'))
    snapshot = provider.fetch_universe(DAY)
    quote = provider.fetch_quote('sh689123', DAY, snapshot=snapshot['rows'][0])
    assert quote['error'] == 'adjustment_precision_unverified'


def test_empty_factor_fallback_does_not_continue_after_access_denial():
    from app.services.market_sina import SinaRateLimitError
    provider = make_provider()
    provider.actions = None
    calls = []
    def request(req):
        calls.append(req)
        return httpx.Response(403)
    provider.client = httpx.Client(transport=httpx.MockTransport(request))
    snapshot = provider.fetch_universe(DAY)
    with pytest.raises(SinaRateLimitError):
        provider.fetch_quote('sh689123', DAY, snapshot=snapshot['rows'][0])
    assert len(calls) == 1


def test_restricted_evidence_api_access_denial_stops_after_two_requests():
    from app.services.market_sina import SinaRateLimitError
    provider = make_provider()
    provider.actions = None
    calls = []
    def request(req):
        calls.append(req)
        if req.url.path.endswith('/qfq.js'):
            return httpx.Response(200, text='var sh689123qfq={"total":0,"data":[]}')
        return httpx.Response(429)
    provider.client = httpx.Client(transport=httpx.MockTransport(request))
    snapshot = provider.fetch_universe(DAY)
    with pytest.raises(SinaRateLimitError):
        provider.fetch_quote('sh689123', DAY, snapshot=snapshot['rows'][0])
    assert len(calls) == 2


def test_historical_provider_audits_actual_identities_and_counts_before_acceptance():
    provider = make_provider()
    m = member(); m['board'] = '8'
    provider.master.fetch = lambda day: {'date': str(day), 'rows': [m], 'expected_count': 1,
        'evidence': {'membership_verified': False, 'source_counts_verified': True,
                     'listing_dates_verified': True, 'current_catalogue_verified': True,
                     'snapshot_date': '2026-11-11'}}
    provider.counts = SimpleNamespace(fetch=lambda day: {'date': str(day), 'verified': True,
        'counts': {'sse_main_a': 0, 'sse_star': 1, 'szse_main_a': 0, 'szse_chinext': 0}})
    snapshot = provider.fetch_universe(DAY)
    assert snapshot['universe_evidence']['historical_reconstruction_verified'] is True
    assert snapshot['universe_evidence']['joinquant_universe_directly_verified'] is False
    assert provider.fetch_quote(m['symbol'], DAY, snapshot=snapshot['rows'][0])['status'] == 'current'
