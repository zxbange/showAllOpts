"""Bounded full-sweep retries, durable gaps and common-dependency aborts."""
from collections import Counter
from copy import deepcopy
from datetime import date, timedelta

import pytest

from app.services.etf_collection import WriterFailure
from app.services.market_collection import CollectionCancelled, CollectionInterrupted
from app.services.market_public import MarketDependencyError
from app.services.market_sina import SinaRateLimitError
from test_market_collection import DAY, EVENING, N, historical_provider, worker
from test_market_public import DAYS, DAY as PUBLIC_DAY, make_provider, member


FRIDAY = EVENING.replace(day=13)
DATES = [date(2026, 11, day) for day in (10, 11, 12, 13)]
BAD = ('sh600003', DATES[0])


def planned_worker(tmp_path, *, manual=False):
    collection, provider, writer, clock = worker(tmp_path, moment=FRIDAY)
    historical_provider(provider)
    writer.dates = {'heat': {'2026-11-09'}, 'crowding': {'2026-11-09'}}
    if manual:
        collection.request_run('retry-full-sweep')
    return collection, provider, writer, clock


def failing_quote(provider, *, failures, bad=BAD):
    original = provider.fetch_quote
    seen = Counter()
    def quote(symbol, day, **kwargs):
        result = original(symbol, day, **kwargs)
        seen[(symbol, day)] += 1
        if (symbol, day) == bad and seen[(symbol, day)] <= failures:
            return result | {'status': 'transient_failure', 'error': 'adjustment_unavailable'}
        return result
    provider.fetch_quote = quote
    return seen


@pytest.mark.parametrize('manual', [False, True])
@pytest.mark.parametrize('failures', [1, 2, 3, 4])
def test_entire_first_sweep_precedes_one_two_or_three_failed_only_retry_passes(tmp_path, manual, failures):
    collection, provider, writer, _ = planned_worker(tmp_path, manual=manual)
    seen = failing_quote(provider, failures=failures)
    assert collection.tick()  # Later dates succeed even when the first remains failed.
    assert provider.universe_calls == DATES
    assert all(count == 1 for pair, count in seen.items() if pair != BAD)
    assert seen[BAD] == min(failures + 1, 4)
    first = [(symbol, day) for symbol, day, _ in provider.quote_calls[:N * len(DATES)]]
    assert len(first) == len(set(first)) == N * len(DATES)
    assert [(symbol, day) for symbol, day, _ in provider.quote_calls[N * len(DATES):]] == [BAD] * min(failures, 3)
    assert {day for day, heat, _, _ in writer.calls if heat} == (set(DATES) if failures < 4 else set(DATES[1:]))
    writes = Counter((day, kind) for day, heat, crowding, _ in writer.calls
                     for kind, value in (('heat', heat), ('crowding', crowding)) if value)
    assert max(writes.values()) == 1
    assert collection.status()['runs'][0]['status'] == ('complete' if failures < 4 else 'partial')
    assert bool(collection._meta('gap:heat:2026-11-10')) == (failures == 4)
    assert collection._meta('gap:crowding:2026-11-10') is None


@pytest.mark.parametrize('manual', [False, True])
def test_exhausted_gap_survives_max_advance_polling_restart_and_next_manual_run(tmp_path, manual):
    collection, provider, writer, clock = planned_worker(tmp_path, manual=manual)
    failing_quote(provider, failures=99)
    assert collection.tick()
    assert max(writer.dates['heat']) == '2026-11-13'
    calls = len(provider.quote_calls)
    for minutes in (1, 5, 30, 60):
        assert not collection.tick(clock.advance(minutes=minutes))
        assert len(provider.quote_calls) == calls
    restarted, resumed, committed, later = worker(tmp_path, moment=clock())
    historical_provider(resumed)
    committed.dates = deepcopy(writer.dates)
    # Real production source-date reads expose only MAX, not all stored dates.
    restarted.source_dates_loader = lambda owner: {kind: {max(values)} for kind, values in committed.dates.items()}
    assert not restarted.tick()
    assert resumed.quote_calls == []
    restarted.request_run('new-manual-budget')
    assert restarted.tick()
    assert [(symbol, day) for symbol, day, _ in resumed.quote_calls] == [BAD]
    assert resumed.universe_calls == []
    assert len(committed.calls) == 1 and committed.calls[0][1] and committed.calls[0][2] is None
    assert restarted._day('2026-11-10')['status'] == 'complete'
    assert restarted._meta('gap:heat:2026-11-10') is None
    assert restarted.status()['gaps'] == []


def test_next_scheduled_slot_gets_new_budget_but_morning_poll_does_not(tmp_path):
    collection, provider, writer, clock = planned_worker(tmp_path)
    seen = failing_quote(provider, failures=4)
    assert collection.tick()
    assert seen[BAD] == 4
    with collection.connect() as db:
        runs_before = db.execute('SELECT COUNT(*) FROM manual_runs').fetchone()[0]
    reads = []
    collection.source_dates_loader = lambda owner: reads.append(1) or writer.dates
    clock.value = FRIDAY + timedelta(days=1, hours=-8)
    assert not collection.tick()
    assert seen[BAD] == 4
    assert not collection.tick(clock.advance(seconds=30))
    assert reads == []
    with collection.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM manual_runs').fetchone()[0] == runs_before
    clock.value = FRIDAY + timedelta(days=1)
    assert collection.tick()
    assert seen[BAD] == 5
    assert provider.universe_calls == DATES
    assert collection.status()['gaps'] == []


def test_untracked_old_holes_not_reopened_but_planned_bootstrap_failure_is_retained(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    historical_provider(provider)
    provider.failures = {BAD[0]}
    assert not collection.tick()
    with collection.connect() as db:
        db.execute("INSERT INTO days(trade_date,status,created_at,updated_at) VALUES ('2026-11-06','partial',?,?)",
                   (clock().isoformat(), clock().isoformat()))
    provider.failures.clear()
    clock.advance(days=1)
    assert collection.tick()
    assert collection._day('2026-11-06')['status'] == 'superseded'
    assert collection._day(DAY)['status'] == 'complete'
    assert writer.dates['heat'] == {'2026-11-10', '2026-11-11'}


def test_metric_specific_transient_write_retries_after_other_dates_without_source_refetch(tmp_path):
    collection, provider, writer, _ = planned_worker(tmp_path)
    attempts = []
    def write(owner, day, heat, crowding, snapshot):
        attempts.append((day, bool(heat), bool(crowding)))
        if day == DAY and crowding and sum(d == day and c for d, _, c in attempts) < 4:
            raise WriterFailure('writer_write_failed')
        return writer(owner, day, heat, crowding, snapshot)
    collection.writer = write
    assert collection.tick()
    crowding_attempts = [day for day, _, crowding in attempts if crowding]
    assert crowding_attempts == DATES + [DAY] * 3
    assert [day for day, heat, _ in attempts if heat] == DATES
    assert len(provider.quote_calls) == N * len(DATES)
    assert provider.universe_calls == DATES
    assert collection.status()['gaps'] == []


def test_day_local_snapshot_failure_is_skipped_before_retry(tmp_path):
    collection, provider, writer, _ = planned_worker(tmp_path)
    original = provider.fetch_universe
    calls = []
    def universe(day):
        calls.append(day)
        if day == DAY and calls.count(day) <= 2:
            raise RuntimeError('temporary day-specific input failure')
        return original(day)
    provider.fetch_universe = universe
    assert collection.tick()
    assert calls == DATES + [DAY, DAY]
    assert collection.status()['gaps'] == []


@pytest.mark.parametrize('error', [CollectionInterrupted(), CollectionCancelled(), SinaRateLimitError(403), SinaRateLimitError(429)])
def test_stop_signal_on_first_retry_stops_before_any_further_item(tmp_path, error):
    collection, provider, writer, _ = planned_worker(tmp_path)
    seen = failing_quote(provider, failures=99)
    original = provider.fetch_quote
    def quote(symbol, day, **kwargs):
        if (symbol, day) == BAD and seen[BAD] == 1:
            raise error
        return original(symbol, day, **kwargs)
    provider.fetch_quote = quote
    assert not collection.tick()
    assert len(provider.quote_calls) == N * len(DATES)
    assert seen[BAD] == 1
    assert collection.status()['runs'][0]['status'] == 'paused'
    assert collection._day(DAY)['crowding_status'] == 'written'
    assert collection._meta('gap:heat:2026-11-10')


@pytest.mark.parametrize('status', [403, 429])
def test_manual_requests_cannot_clear_a_source_access_cooldown(tmp_path, status):
    collection, provider, writer, clock = planned_worker(tmp_path)
    provider.exceptions[BAD[0]] = SinaRateLimitError(status)
    assert not collection.tick()
    before = (len(provider.quote_calls), len(provider.universe_calls))
    provider.exceptions.clear()
    collection.request_run('during-source-cooldown')
    assert not collection.tick(clock.advance(hours=1))
    assert (len(provider.quote_calls), len(provider.universe_calls)) == before
    assert collection.status()['runs'][0]['error'] == ('source_forbidden' if status == 403 else 'source_rate_limited')
    clock.value = FRIDAY + timedelta(days=1)
    collection.request_run('after-source-cooldown')
    assert collection.tick()
    assert collection.status()['runs'][0]['status'] == 'complete'


def test_shared_dependency_abort_does_not_scan_all_stocks_or_retry_in_same_execution(tmp_path):
    collection, provider, writer, clock = planned_worker(tmp_path)
    provider.exceptions['bj600020'] = MarketDependencyError('calendar_incomplete')
    assert not collection.tick()
    assert provider.universe_calls == [DAY]
    assert len(provider.quote_calls) == 1
    assert collection.status()['runs'][0]['error'] == 'calendar_incomplete'
    assert collection._day(DAY)['crowding_status'] == 'written'
    for _ in range(3):
        assert not collection.tick(clock.advance(minutes=30))
    assert len(provider.quote_calls) == 4
    assert not collection.tick(clock.advance(minutes=30))
    assert len(provider.quote_calls) == 4
    assert collection._meta('gap:heat:2026-11-13')


def test_calendar_failures_have_bounded_shared_budget_without_any_stock_requests(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    calls = []
    def calendar(client):
        calls.append(1)
        raise RuntimeError('calendar unavailable')
    collection.calendar_loader = calendar
    for _ in range(4):
        assert not collection.tick()
        clock.advance(minutes=30)
    assert not collection.tick()
    assert len(calls) == 4
    assert provider.quote_calls == provider.universe_calls == []


@pytest.mark.parametrize('dependency', ['calendar', 'master', 'archive', 'suspensions', 'actions'])
def test_public_provider_promotes_shared_failures_instead_of_per_stock_results(dependency):
    provider = make_provider(members=[member(), member('sz001234')])
    calls = []
    def fail(*args):
        calls.append(args)
        raise RuntimeError('shared outage')
    if dependency == 'calendar':
        provider.calendar_loader = fail
        with pytest.raises(MarketDependencyError):
            provider.fetch_universe(PUBLIC_DAY)
    elif dependency == 'master':
        provider.master.fetch = fail
        with pytest.raises(MarketDependencyError):
            provider.fetch_universe(PUBLIC_DAY)
    else:
        snapshot = provider.fetch_universe(PUBLIC_DAY)
        if dependency == 'archive':
            provider.archives.fetch = fail
        elif dependency == 'actions':
            provider.actions.fetch = fail
        else:
            provider.suspensions.fetch = fail
            provider._daily[DAYS[0]] = {}
        with pytest.raises(MarketDependencyError):
            provider.fetch_quote(snapshot['rows'][0]['symbol'], PUBLIC_DAY, snapshot=snapshot['rows'][0])
    assert len(calls) == 1


@pytest.mark.parametrize('dependency', ['calendar', 'archive'])
def test_malformed_shared_payload_remains_a_dependency_failure(dependency):
    provider = make_provider()
    snapshot = provider.fetch_universe(PUBLIC_DAY)
    calls = []
    if dependency == 'calendar':
        provider._calendar_days = None
        provider.calendar_loader = lambda client: calls.append(1) or ['bad-date']
    else:
        provider.archives.fetch = lambda day: calls.append(day) or {'date': str(day), 'verified': True}
    with pytest.raises(MarketDependencyError):
        provider.fetch_quote(snapshot['rows'][0]['symbol'], PUBLIC_DAY, snapshot=snapshot['rows'][0])
    assert len(calls) == 1


@pytest.mark.parametrize('error_type', ['ConnectError', 'ConnectTimeout', 'ReadTimeout'])
def test_connection_setup_failure_stops_but_response_timeout_is_item_local(error_type):
    import httpx
    provider = make_provider()
    snapshot = provider.fetch_universe(PUBLIC_DAY)
    provider.actions = None
    calls = []
    def failed(request):
        calls.append(request)
        raise getattr(httpx, error_type)('source failure', request=request)
    provider.client = httpx.Client(transport=httpx.MockTransport(failed))
    row = snapshot['rows'][0]
    if error_type == 'ReadTimeout':
        quote = provider.fetch_quote(row['symbol'], PUBLIC_DAY, snapshot=row)
        assert quote['status'] == 'transient_failure' and quote['error'] == 'network_error'
    else:
        with pytest.raises(MarketDependencyError):
            provider.fetch_quote(row['symbol'], PUBLIC_DAY, snapshot=row)
    assert len(calls) == 1


@pytest.mark.parametrize('failure', [RuntimeError('master outage'), SinaRateLimitError(403), SinaRateLimitError(429)])
def test_master_failures_do_not_invalidate_unrelated_saved_archive(failure):
    provider = make_provider()
    invalidated = []
    provider.archives.invalidate = invalidated.append
    def failed(day):
        raise failure
    provider.master.fetch = failed
    with pytest.raises((MarketDependencyError, SinaRateLimitError)):
        provider.fetch_universe(PUBLIC_DAY)
    assert invalidated == []


def test_rejected_archive_is_invalidated_at_its_own_date():
    from app.services.market_sina import SinaDataError
    provider = make_provider()
    invalidated = []
    provider.archives.invalidate = invalidated.append
    def failed(day):
        raise SinaDataError('invalid archive', code='invalid_tdx_archive')
    provider.archives.fetch = failed
    with pytest.raises(MarketDependencyError, match='公共'):
        provider.fetch_universe(PUBLIC_DAY)
    assert invalidated == [PUBLIC_DAY]


def morning_gap_worker(tmp_path):
    collection, provider, writer, clock = worker(tmp_path, moment=EVENING.replace(day=11, hour=13))
    historical_provider(provider)
    # The source boundary must be within calendar coverage, not before it.
    collection.calendar_loader = lambda client: [date(2026, 11, day) for day in (5, 6, 9, 10, 11)]
    writer.dates = {'heat': {'2026-11-05'}, 'crowding': {'2026-11-05'}}
    collection.request_run('morning-gap-repair')
    return collection, provider, writer, clock


def test_nov11_before_1830_repairs_6_9_10_but_never_today(tmp_path):
    collection, provider, writer, _ = morning_gap_worker(tmp_path)
    assert collection.tick()
    expected = [date(2026, 11, day) for day in (6, 9, 10)]
    assert provider.universe_calls == expected
    assert {day for day, heat, _, _ in writer.calls if heat} == set(expected)
    assert {day for day, _, crowding, _ in writer.calls if crowding} == set(expected)
    assert all(collection._day(day)['status'] == 'complete' for day in expected)
    assert collection._day('2026-11-11') is None


def test_shared_master_failure_at_9_preserves_6_and_leaves_10_unattempted(tmp_path):
    collection, provider, writer, _ = morning_gap_worker(tmp_path)
    original, attempted = provider.fetch_universe, []
    def universe(day):
        attempted.append(day)
        if day == date(2026, 11, 9):
            raise MarketDependencyError('universe_incomplete')
        return original(day)
    provider.fetch_universe = universe
    assert not collection.tick()
    assert attempted == [date(2026, 11, day) for day in (6, 9)]
    assert writer.dates == {kind: {'2026-11-05', '2026-11-06'} for kind in ('heat', 'crowding')}
    assert collection._day('2026-11-06')['status'] == 'complete'
    assert collection._day('2026-11-09')['error'] == 'universe_incomplete'
    assert collection._day('2026-11-10')['attempts'] == 0
    assert collection._day('2026-11-11') is None
    assert collection.status()['runs'][0]['error'] == 'universe_incomplete'


def test_cached_calendar_fallback_still_uses_shared_dependency_budget(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    assert collection.tick()
    clock.advance(days=1)
    collection._put_meta(calendar_checked_at=(clock() - timedelta(days=8)).isoformat())
    calls = []
    def failed(client):
        calls.append(1)
        raise ValueError('calendar failure')
    collection.calendar_loader = failed
    for _ in range(6):
        collection.tick()
        clock.advance(minutes=30)
    assert len(calls) <= 4
    assert collection._meta('dependency-budget:scheduled:2026-11-11') is not None


def test_manual_dependency_failures_also_consume_current_automatic_slot(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    calls = []
    def failed(client):
        calls.append(1)
        raise RuntimeError('calendar unavailable')
    collection.calendar_loader = failed
    for request in range(4):
        collection.request_run(f'manual-calendar-{request}')
        assert not collection.tick()
    assert len(calls) == 4
    assert collection._meta('dependency-budget:scheduled:2026-11-10') == '4'
    assert not collection.tick(clock.advance(minutes=30))
    assert len(calls) == 4
    # A later explicit request still has its own budget.
    collection.request_run('manual-calendar-explicit-new')
    assert not collection.tick()
    assert len(calls) == 5
