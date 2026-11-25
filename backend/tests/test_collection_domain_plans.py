"""Characterize domain policy without a network, plus existing worker seams."""

import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest

from app.repositories.etf_state_schema import initialize_etf_state
from app.services import etf_collection, market_collection, market_writer
from app.services.collection_writer import WriterFailure, writer_error_code
from app.services.etf_collection_plan import (
    ScopedSourceGuard,
    observation_source,
    plan_manual_observations,
)
from app.services.market_gap_plan import plan_market_gaps, transition_market_day
from app.services.market_methodology import JOINQUANT_HEAT_METHOD
from app.services.market_observation_rules import (
    SourceFailure,
    dated_snapshot,
    fresh_quote,
    price_verified,
    validate_hq_batch,
    validate_snapshot,
    verification_samples,
)

DAYS = tuple(date(2026, 11, n) for n in (13, 16, 17, 18, 19, 20, 23, 24))
CODE = '510300.XSHG'


class Calendar:
    days = DAYS

    @staticmethod
    def range(start, end):
        return [day for day in DAYS if start <= day <= end]


def observation(kind, day, status, code=CODE):
    return {'kind': kind, 'code': code, 'day': str(day), 'status': status}


def full_day(day):
    return [observation(kind, day, 'complete') for kind in ('shares', 'prices')]


def keys(days):
    return {(kind, CODE, day) for day in days for kind in ('shares', 'prices')}


def test_etf_known_earlier_hole_survives_later_complete_day_and_old_blocked_rows():
    old, baseline, missing, _, _, newer, target, _ = DAYS
    rows = [
        observation('prices', old, 'blocked_unverified_history'),
        *full_day(baseline),
        observation('shares', missing, 'complete'),
        observation('prices', missing, 'manual_only'),
        *full_day(newer),
        observation('shares', 'invalid-day', 'complete'),
        observation('shares', old, 'pending', code='untracked'),
    ]
    scope, required, unchecked = plan_manual_observations(rows, None, Calendar(), target, [CODE])
    assert scope == DAYS[2:-1]
    assert required == keys(DAYS[2:-2])
    assert unchecked == required - keys([newer]) - {('shares', CODE, missing)}


def test_etf_bootstrap_is_bounded_and_an_explicit_due_gap_floor_survives_complete_latest():
    target = DAYS[-2]
    scope, required, unchecked = plan_manual_observations([], None, Calendar(), target, [CODE])
    assert scope == DAYS[-4:-1]
    assert required == unchecked == keys(DAYS[-4:-2])
    floor = DAYS[1]
    scope, required, unchecked = plan_manual_observations(
        full_day(target), str(floor), Calendar(), target, [CODE]
    )
    assert scope == DAYS[1:-1]
    assert required == unchecked == keys(DAYS[1:-2])


def test_etf_facade_reads_only_due_observations_and_floor_from_existing_schema():
    end, future = DAYS[-2:]
    with sqlite3.connect(':memory:') as db:
        db.row_factory = sqlite3.Row
        initialize_etf_state(db)
        db.executemany(
            'INSERT INTO observations(kind,code,day,status) VALUES (?,?,?,?)',
            [('shares', CODE, str(day), 'complete') for day in (end, future)],
        )
        db.execute("INSERT INTO metadata VALUES ('manual_gap_floor',?)", (str(future),))
        db.execute(
            'INSERT INTO metadata VALUES (?,?)',
            ('manual_gap_floor_deferred:' + str(future), str(future)),
        )
        assert etf_collection.ETFCollection._manual_days(
            db, Calendar(), end, {CODE: 'known'}
        ) == plan_manual_observations(
            [observation('shares', end, 'complete')], None, Calendar(), end, [CODE]
        )


def test_etf_scoped_route_rate_limit_and_archive_stay_independent():
    shares, closes = ScopedSourceGuard(), ScopedSourceGuard()
    guards = {'sse_scoped_shares': shares, 'szse_dated_close': closes}
    old, today = DAYS[-2:]
    assert observation_source('shares', CODE, old, today, guards) == (
        'sse_scoped_shares',
        {'scoped_sse': True},
        shares,
    )
    assert observation_source('prices', '159919.XSHE', old, today, guards)[2] is closes
    assert observation_source('prices', '159919.XSHE', today, today, guards) == (
        'exchanges',
        {},
        None,
    )
    closes.observe({}, today)
    closes.observe({}, old)  # An older archive boundary cannot lower the known one.
    assert closes.skip_reason(old) == 'archive' and closes.skip_reason(today) is None
    assert shares.skip_reason(old) is None
    shares.observe({'http_status': 429}, None)
    assert shares.skip_reason(today) == 'rate_limit' and closes.skip_reason(today) is None


def test_shared_writer_error_compatibility_does_not_import_one_collector_into_another():
    assert etf_collection.WriterFailure is market_writer.WriterFailure is WriterFailure
    assert etf_collection.writer_error_code is market_writer.writer_error_code is writer_error_code
    assert writer_error_code(SimpleNamespace(orig=SimpleNamespace(database_error_number=2013))) == (
        'writer_connection_failed'
    )
    assert (
        writer_error_code(RuntimeError(1045, 'private driver text')) == 'writer_permission_denied'
    )
    assert writer_error_code(RuntimeError('unclassified driver text')) == 'writer_write_failed'
    assert str(WriterFailure('writer_write_failed')) == 'writer_write_failed'


def test_market_scope_distinguishes_source_max_from_a_verified_gap_and_manual_only():
    calendar = [str(day) for day in DAYS]
    manual_only = {calendar[2]}
    existing = {'heat': {calendar[5]}, 'crowding': {calendar[4]}}
    saved = {'heat': {calendar[2], calendar[3], calendar[7]}, 'crowding': set()}
    bounded = calendar[:-1]
    automated = plan_market_gaps(
        bounded, existing, saved, manual_only, cutoff=bounded[-1], manual=False
    )
    manual = plan_market_gaps(
        bounded, existing, saved, manual_only, cutoff=bounded[-1], manual=True
    )
    assert automated.target == manual.target == calendar[6]
    assert automated.needed['heat'] == {calendar[3], calendar[6], calendar[7]}
    assert automated.planned == {calendar[3], calendar[4], calendar[5], calendar[6]}
    assert manual.planned == automated.planned | manual_only
    assert automated.verified == set()
    assert calendar[7] not in manual.planned and calendar[1] not in manual.planned
    with pytest.raises(SourceFailure, match='calendar_incomplete'):
        plan_market_gaps(
            [calendar[-1]], existing, saved, manual_only, cutoff=calendar[-1], manual=True
        )


def _day_state(*, heat='pending', crowding='pending', status='partial'):
    return {
        'heat_status': heat,
        'crowding_status': crowding,
        'status': status,
        'error': 'quote_unavailable',
        'next_attempt_at': 'old-retry',
        'updated_at': 'old-stamp',
    }


def test_market_metric_transition_keeps_unenumerated_success_and_retry_state():
    day = str(DAYS[-2])
    empty = {'heat': set(), 'crowding': set()}
    needed = dict(empty, crowding={day})
    update = transition_market_day(
        day, _day_state(heat='written', crowding='blocked'), empty, needed, 'now'
    )
    assert (update.heat_status, update.crowding_status, update.status) == (
        'written',
        'blocked',
        'partial',
    )
    assert update.gap_operations == {'heat': 'clear', 'crowding': 'remember'}
    assert (update.error, update.next_attempt_at, update.updated_at) == (
        'quote_unavailable',
        'old-retry',
        'old-stamp',
    )
    reopened = transition_market_day(
        day, _day_state(heat='written', crowding='written', status='complete'), empty, needed, 'now'
    )
    assert (reopened.heat_status, reopened.crowding_status, reopened.status) == (
        'written',
        'pending',
        'pending',
    )
    assert (reopened.error, reopened.next_attempt_at, reopened.updated_at) == (None, None, 'now')
    outside = transition_market_day(day, _day_state(), empty, empty, 'now')
    assert outside.status == 'superseded' and not outside.gap_operations
    complete = transition_market_day(
        day, _day_state(), {'heat': {day}, 'crowding': {day}}, empty, 'now'
    )
    assert complete.status == 'complete' and complete.gap_operations == {
        'heat': 'clear',
        'crowding': 'clear',
    }


def snapshot_row(symbol='sh600000', amount='13', volume='100', close='10', day=str(DAYS[-2])):
    return dict(symbol=symbol, amount=amount, volume=volume, close=close, date=day)


def test_market_verification_rules_preserve_exact_rounding_and_reject_duplicates_or_mixed_days():
    day, prior = str(DAYS[-2]), str(DAYS[-3])
    row = snapshot_row()
    snapshot = dict(
        date=day,
        expected_count=1,
        rows=[row],
        source_has_trading_date=True,
        dated_snapshot_verified=True,
        date_verified_by='sina_hq',
    )
    validate_snapshot(snapshot, day)
    assert dated_snapshot(snapshot, day)
    assert not dated_snapshot(dict(snapshot, rows=[dict(row, date=prior)]), day)
    with pytest.raises(SourceFailure, match='snapshot_invalid'):
        validate_snapshot(dict(snapshot, rows=[row, row], expected_count=2), day)
    with pytest.raises(SourceFailure, match='snapshot_invalid'):
        validate_snapshot(dict(snapshot, rows=[dict(row, amount='0')]), day)
    validate_hq_batch(day, {row['symbol']: row}, [dict(row, amount='12.5')])
    with pytest.raises(SourceFailure, match='snapshot_invalid'):
        validate_hq_batch(day, {row['symbol']: row}, [dict(row, amount='12.499')])
    with pytest.raises(SourceFailure, match='snapshot_invalid'):
        validate_hq_batch(day, {row['symbol']: row}, [row, row])


def test_market_quote_freshness_preserves_current_paused_and_no_trade_contracts():
    day, prior = str(DAYS[-2]), str(DAYS[-3])
    row = snapshot_row()
    quote = dict(row, day=day, status='current')
    assert fresh_quote(quote, row, day) and price_verified(quote, row, day)
    assert not price_verified(dict(quote, date=prior), row, day)
    paused_row = dict(row, amount='0', volume='0')
    paused = dict(
        paused_row,
        day=day,
        status='paused',
        paused=1,
        verified=True,
        methodology=JOINQUANT_HEAT_METHOD,
    )
    assert price_verified(paused, paused_row, day)
    assert not price_verified(dict(paused, verified=False), paused_row, day)
    assert price_verified(dict(paused, status='no_trade', date=prior), paused_row, day)
    assert not price_verified(dict(paused, status='no_trade'), paused_row, day)


def test_market_samples_select_each_required_market_deterministically_and_class_hooks_remain():
    day = str(DAYS[-2])
    a, b = snapshot_row('sh600000'), snapshot_row('sh600001')
    c, d = snapshot_row('sz000001'), snapshot_row('bj830001')
    assert verification_samples([a, b, c, d]) == [b, c, d]
    assert verification_samples([a, b, c]) is None

    class Override(market_collection.MarketCollection):
        @staticmethod
        def _fresh_quote(quote, snapshot_row, day):
            return True

        @staticmethod
        def _validate_snapshot(snapshot, day):
            raise SourceFailure('snapshot_invalid')

        @staticmethod
        def _quote_method(quote):
            return JOINQUANT_HEAT_METHOD

    assert Override._price_verified({}, {}, day)
    assert not Override._dated_snapshot({}, day)
    assert Override._verified_heat_quote({'verified': True})
