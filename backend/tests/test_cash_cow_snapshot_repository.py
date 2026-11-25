"""Contract tests for the cash-cow persistence boundary."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.cache_errors import SnapshotDataError
from app.models.cash_cow import CashCowSnapshot
from app.repositories import cash_cow_snapshot as repository
from app.services.cash_cow_import import (
    PUBLIC_SOURCE,
    RULE_VERSION,
    validate_snapshot,
    write_public,
)
from app.services.cash_cow_rules import PERIODS, evaluate_year, finish_stock


def publication_payload():
    """A complete synthetic result with exactly representable JSON numerics."""
    result = {}
    for period in PERIODS:
        years = []
        for year in range(2026 - period, 2026):
            outcome, annual = evaluate_year(
                year,
                {'cash_dividend': 50},
                {'adjusted_profit': 80, 'net_profit': 100, 'operating_cashflow': 100},
            )
            assert outcome == 'eligible'
            years.append(annual)
        dividends = {annual['year']: 0.5 for annual in years}
        prices = {
            annual['year']: {'year_average_close': 10, 'price_date': f'{annual["year"]}-12-31'}
            for annual in years
        }
        outcome, item = finish_stock(
            {'code': '000001', 'name': '测试股票', 'exchange': 'SZ'},
            years,
            dividends,
            prices,
            period,
        )
        assert outcome == 'matched'
        result[str(period)] = {
            'snapshot': {
                'as_of': '2026-11-25',
                'updated_at': datetime(
                    2026, 11, 25, 12, 0, 0, 900000, tzinfo=timezone.utc
                ).isoformat(),
                'run_id': 'synthetic-publication',
                'years': period,
                'start_year': 2026 - period,
                'end_year': 2025,
                'rule_version': RULE_VERSION,
                'source': PUBLIC_SOURCE,
                'universe_count': 1,
                'excluded_count': 0,
                'insufficient_count': 0,
                'matched_count': 1,
                'evaluated_count': 1,
            },
            'items': [item],
        }
    return result


def edited_payload(payload, microseconds, run_id):
    result = deepcopy(payload)
    for part in result.values():
        part['snapshot']['updated_at'] = datetime(
            2026, 11, 25, 12, 0, 0, microseconds, tzinfo=timezone.utc
        ).isoformat()
        part['snapshot']['run_id'] = run_id
    return result


def exercise_publication_contract(engine):
    """Run actual persistence for both unit SQLite and opt-in real MySQL."""
    payload = publication_payload()
    phases = []
    assert write_public(engine, payload, lambda: None, progress=phases.append) == payload
    assert phases == ['schema', 'lock', 'write', 'commit']
    with engine.connect() as conn:
        validate_snapshot(conn)
        before = repository.read_all(conn)
    assert len(before) == 1
    assert before[0]['payload'] == payload
    for period in PERIODS:
        with Session(engine) as session:
            assert repository.read_period(session, period) == payload[str(period)]

    phases.clear()
    assert write_public(engine, payload, lambda: None, progress=phases.append) == payload
    assert phases == ['schema', 'lock']
    older = edited_payload(payload, 100000, 'older-same-second')
    assert write_public(engine, older, lambda: None) == payload
    # The third check runs after actual UPDATE, before COMMIT.
    replacement = deepcopy(payload)
    for part in replacement.values():
        part['snapshot']['run_id'] = 'same-timestamp-replacement'
    checks = []

    def cancel_after_dml():
        checks.append(1)
        if len(checks) == 3:
            raise RuntimeError('synthetic commit cancellation')

    with pytest.raises(RuntimeError, match='commit cancellation'):
        write_public(engine, replacement, cancel_after_dml)
    assert len(checks) == 3
    with engine.connect() as conn:
        assert repository.read_all(conn) == before


def test_sqlite_repository_roundtrip_replay_stale_and_rollback():
    engine = create_engine('sqlite://')
    try:
        exercise_publication_contract(engine)
    finally:
        engine.dispose()


def test_newer_but_incomplete_canonical_snapshot_cannot_be_returned_or_overwritten():
    engine = create_engine('sqlite://')
    try:
        payload = publication_payload()
        write_public(engine, payload, lambda: None)
        broken = deepcopy(payload)
        broken['5']['snapshot']['run_id'] = 'different-publication'
        with engine.begin() as conn:
            conn.execute(CashCowSnapshot.__table__.update().values(payload=broken))
        older = edited_payload(payload, 100000, 'older')
        with pytest.raises(SnapshotDataError, match='口径不一致'):
            write_public(engine, older, lambda: None)
        with engine.connect() as conn:
            assert repository.read_all(conn)[0]['payload'] == broken
    finally:
        engine.dispose()
