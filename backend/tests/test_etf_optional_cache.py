"""Missing optional ETF tables must degrade the whole published series atomically."""
from datetime import date, datetime, timedelta
from decimal import Decimal
import sqlite3

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import OperationalError

from app.core.cache import SnapshotCache
from app.models.etf import ETFPrice, ETFShare, ETFTradingDay
from app.repositories.etf import ETFRepository
from app.services.etf import ETFService
from test_cache import cached
from test_etf_api import row
from test_etf_market_value import price


DAYS = [date(2026, 1, day) for day in (5, 6, 7, 8, 9, 12)]
CODE = '159915.XSHE'
MODELS = (ETFShare, ETFPrice, ETFTradingDay)


def records(model):
    if model is ETFShare:
        return [row(day, 10, code=CODE, previous=DAYS[index - 1] if index else day - timedelta(days=1))
                for index, day in enumerate(DAYS)]
    if model is ETFPrice:
        return [price(day, '2', code=CODE) for day in DAYS]
    return [ETFTradingDay(trade_date=day, previous_trade_date=DAYS[index - 1] if index else None,
                          source='TEST', updated_at=datetime(2026, 1, 12, 18))
            for index, day in enumerate(DAYS)]


def seed(session):
    for model in MODELS:
        session.add_all(records(model))
    session.commit()


def entry(cache):
    return cache.read_state()['modules']['etf-shares']


def history(session):
    return ETFService(ETFRepository(session)).history('cyb', 0, DAYS[-1]).model_dump(mode='json')


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize('missing', [ETFPrice, ETFTradingDay])
def test_missing_optional_table_clears_all_history_and_restores(cached, session, engine, missing):
    settings, cache = cached
    seed(session)
    cache.refresh('etf-shares', background=False)
    original = cache.root / entry(cache)['file']
    with cache.session('etf-shares') as local:
        assert all(point['market_value'] is not None for point in history(local)['market_values'])

    missing.__table__.drop(engine)
    cache.refresh('etf-shares', background=False)
    degraded = entry(cache)
    assert degraded['error'] is None and degraded['file'] != original.name
    assert bool(degraded['sync_note']) is (missing is ETFPrice)
    assert degraded['rows'] == 2 * len(DAYS)
    assert not inspect(engine).has_table(missing.__tablename__)
    assert count(session, ETFShare) == len(DAYS)  # No source history is changed by refresh.
    with cache.session('etf-shares') as local:
        assert count(local, missing) == 0  # Includes dates outside the three-session window.
        assert history(local) == history(session)
        if missing is ETFPrice:
            assert all(point['market_value'] is None for point in history(local)['market_values'])
    with sqlite3.connect(original.as_uri() + '?mode=ro', uri=True) as old:
        assert old.execute('SELECT COUNT(*) FROM ' + missing.__tablename__).fetchone()[0] == len(DAYS)

    # A new worker reads the same durable decision, and a no-op does not copy again.
    other = SnapshotCache(settings, engine)
    with other.session('etf-shares') as local:
        assert count(local, missing) == 0
    other.refresh('etf-shares', background=False)
    assert entry(other)['file'] == degraded['file']
    assert entry(other)['rows'] == 2 * len(DAYS)

    # Recovery must backfill every date, including those older than the share cursor.
    missing.__table__.create(engine)
    session.add_all(records(missing))
    session.commit()
    other.refresh('etf-shares', background=False)
    assert entry(other)['error'] is None and entry(other)['sync_note'] is None
    assert entry(other)['rows'] == 3 * len(DAYS)
    with other.session('etf-shares') as local:
        assert count(local, missing) == len(DAYS)
        assert history(local) == history(session)
    restored = entry(other)['file']
    other.refresh('etf-shares', background=False)
    assert entry(other)['file'] == restored


@pytest.mark.parametrize('error_code', [1142, 2013])
def test_optional_table_inspection_error_keeps_previous_snapshot(cached, session, engine, monkeypatch, error_code):
    import app.core.etf_sync as sync

    _, cache = cached
    seed(session)
    cache.refresh('etf-shares', background=False)
    before = entry(cache).copy()
    monkeypatch.setattr(cache.settings, 'cache_sync_attempts', 1)
    original_inspect = sync.inspect

    def failing_inspect(connection):
        inspector = original_inspect(connection)
        if connection.engine is engine:
            has_table = inspector.has_table

            def fail(name, *args, **kwargs):
                if name == ETFPrice.__tablename__:
                    raise OperationalError('test inspection', None, Exception(error_code, 'test failure'))
                return has_table(name, *args, **kwargs)

            inspector.has_table = fail
        return inspector

    monkeypatch.setattr(sync, 'inspect', failing_inspect)
    cache.refresh('etf-shares', background=False)
    assert entry(cache)['file'] == before['file']
    assert entry(cache)['rows'] == before['rows']
    assert entry(cache)['sync_note'] == before['sync_note']
    assert 'MYSQL_' + str(error_code) in entry(cache)['error']
    with cache.session('etf-shares') as local:
        assert count(local, ETFPrice) == len(DAYS)
    monkeypatch.setattr(sync, 'inspect', original_inspect)
    cache.refresh('etf-shares', background=False)
    assert entry(cache)['file'] == before['file'] and entry(cache)['error'] is None


def test_failed_degradation_does_not_publish_or_modify_old_snapshot(cached, session, engine, monkeypatch):
    import app.core.etf_sync as sync

    _, cache = cached
    seed(session)
    cache.refresh('etf-shares', background=False)
    before = entry(cache).copy()
    ETFPrice.__table__.drop(engine)
    report = sync.report

    def fail_validation(stage, **kwargs):
        if stage == '校验 ETF 份额和收盘价快照':
            raise RuntimeError('test failure after unpublished copy committed')
        return report(stage, **kwargs)

    monkeypatch.setattr(sync, 'report', fail_validation)
    cache.refresh('etf-shares', background=False)
    assert entry(cache)['file'] == before['file'] and entry(cache)['error']
    assert entry(cache)['rows'] == before['rows']
    assert entry(cache)['sync_note'] == before['sync_note']
    assert {path.name for path in cache.root.glob('etf-shares-*.sqlite')} == {before['file']}
    with cache.session('etf-shares') as local:
        assert count(local, ETFPrice) == len(DAYS)
    monkeypatch.setattr(sync, 'report', report)
    cache.refresh('etf-shares', background=False)
    assert entry(cache)['error'] is None and entry(cache)['sync_note']
    with cache.session('etf-shares') as local:
        assert count(local, ETFPrice) == 0


def test_degradation_is_published_only_after_validation_and_excludes_other_writers(cached, session, engine, monkeypatch):
    import app.core.etf_sync as sync

    settings, cache = cached
    seed(session)
    cache.refresh('etf-shares', background=False)
    before = entry(cache)['file']
    other = SnapshotCache(settings, engine)
    ETFPrice.__table__.drop(engine)
    report = sync.report
    observed = []

    def observe_candidate(stage, **kwargs):
        if stage == '校验 ETF 份额和收盘价快照':
            observed.append(stage)
            assert entry(other)['file'] == before
            assert other.refresh('etf-shares', background=False) is False
            with other.session('etf-shares') as local:
                assert count(local, ETFPrice) == len(DAYS)
                assert local.get(ETFPrice, (CODE, DAYS[0])).close_price == Decimal(2)
        return report(stage, **kwargs)

    monkeypatch.setattr(sync, 'report', observe_candidate)
    cache.refresh('etf-shares', background=False)
    assert observed and entry(cache)['error'] is None and entry(other)['file'] != before
    with other.session('etf-shares') as local:
        assert count(local, ETFPrice) == 0
        assert count(local, ETFShare) == len(DAYS)
