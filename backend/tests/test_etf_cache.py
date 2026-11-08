from datetime import date, datetime
from decimal import Decimal
import sqlite3

from sqlalchemy import event, select

from app.models.etf import ETFPrice, ETFShare, ETFTradingDay
from test_cache import cached
from test_etf_api import row
from test_etf_market_value import price


def state(cache):
    return cache.read_state()['modules']['etf-shares']


def test_same_day_corrections_backfill_late_prices_and_noop(cached, session):
    _, cache = cached
    d1, d2 = date(2026, 1, 6), date(2026, 1, 7)
    session.add(row(d2, 10))
    session.commit()
    cache.refresh('etf-shares', background=False)
    first = state(cache)['file']
    # Timestamp deliberately unchanged: repeated official release within one second.
    session.get(ETFShare, ('510300.XSHG', d2)).shares = 11
    session.add_all([row(d1, 9), price(d1, '2'), price(d2, '3')])
    session.commit()
    cache.refresh('etf-shares', background=False)
    assert not state(cache)['error'] and state(cache)['file'] != first
    with cache.session('etf-shares') as local:
        assert [r.shares for r in local.scalars(select(ETFShare).order_by(ETFShare.trade_date))] == [9, 11]
        assert [r.close_price for r in local.scalars(select(ETFPrice).order_by(ETFPrice.trade_date))] == [Decimal(2), Decimal(3)]
    second = state(cache)['file']
    cache.refresh('etf-shares', background=False)
    assert state(cache)['file'] == second
    session.get(ETFPrice, ('510300.XSHG', d2)).close_price = Decimal('3.5')
    session.delete(session.get(ETFShare, ('510300.XSHG', d1)))
    session.commit()
    cache.refresh('etf-shares', background=False)
    with cache.session('etf-shares') as local:
        assert local.get(ETFPrice, ('510300.XSHG', d2)).close_price == Decimal('3.5')
        assert local.get(ETFShare, ('510300.XSHG', d1)) is None


def test_incremental_reads_three_sessions_and_all_new_days_only(cached, session, engine):
    _, cache = cached
    days = [date(2026, 1, d) for d in (5, 6, 7, 8, 9, 12, 13, 14, 15)]
    session.add_all([row(day, 10) for day in days[:5]])
    session.add_all([price(day, '2') for day in days[:5]])
    session.add_all([ETFTradingDay(trade_date=day, previous_trade_date=days[i-1] if i else None,
                                  source='TEST', updated_at=datetime(2026, 1, 9, 18)) for i, day in enumerate(days[:5])])
    session.commit()
    cache.refresh('etf-shares', background=False)
    # Old revisions/deletions must be ignored by daily sync, even after a long gap.
    session.get(ETFShare, ('510300.XSHG', days[0])).shares = 99
    session.delete(session.get(ETFShare, ('510300.XSHG', days[1])))
    session.get(ETFShare, ('510300.XSHG', days[2])).shares = 12
    session.delete(session.get(ETFShare, ('510300.XSHG', days[3])))
    session.add_all([row(day, 20) for day in days[5:]])
    session.add_all([price(day, '3') for day in days[5:]])
    session.commit()
    reads = []
    def record(conn, cursor, sql, parameters, *args):
        if sql.lstrip().startswith('SELECT') and 'FROM etf_' in sql:
            reads.append((sql, parameters))
    event.listen(engine, 'before_cursor_execute', record)
    try:
        cache.refresh('etf-shares', background=False)
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    assert not state(cache)['error']
    assert reads and all('WHERE' in sql for sql, _ in reads)
    assert any('2026-01-07' in str(params) for _, params in reads)
    with cache.session('etf-shares') as local:
        assert local.get(ETFShare, ('510300.XSHG', days[0])).shares == 10
        assert local.get(ETFShare, ('510300.XSHG', days[1])).shares == 10
        assert local.get(ETFShare, ('510300.XSHG', days[2])).shares == 12
        assert local.get(ETFShare, ('510300.XSHG', days[3])) is None
        assert all(local.get(ETFShare, ('510300.XSHG', day)).shares == 20 for day in days[5:])
    # Explicit history repair still re-reads everything.
    cache.refresh('etf-shares', background=False, full=True)
    assert not state(cache)['error']
    with cache.session('etf-shares') as local:
        assert local.get(ETFShare, ('510300.XSHG', days[0])).shares == 99
        assert local.get(ETFShare, ('510300.XSHG', days[1])) is None


def test_prices_keep_independent_incremental_boundary(cached, session):
    _, cache = cached
    days = [date(2026, 1, d) for d in (5, 6, 7, 8, 9, 12, 13)]
    session.add_all([row(day, 10) for day in days])
    session.add_all([price(day, '2') for day in days[:3]])
    session.commit()
    cache.refresh('etf-shares', background=False)
    session.add_all([price(day, '3') for day in days[3:]])
    session.commit()
    cache.refresh('etf-shares', background=False)
    assert not state(cache)['error']
    with cache.session('etf-shares') as local:
        assert len(list(local.scalars(select(ETFPrice)))) == len(days)


def test_legacy_source_and_snapshot_upgrade_without_resetting_other_modules(cached, session, engine):
    settings, cache = cached
    root = cache.root
    day = date(2026, 1, 6)
    session.add(row(day, 10))
    session.commit()
    ETFPrice.__table__.drop(engine)
    cache.refresh('etf-shares', background=False)
    assert not state(cache)['error'] and state(cache)['sync_note']
    snapshot = cache.root / state(cache)['file']
    with sqlite3.connect(snapshot) as db:
        db.execute('DROP TABLE etf_price_daily')
    ETFPrice.__table__.create(engine)
    session.add(price(day, '2'))
    session.commit()
    cache.refresh('etf-shares', background=False)
    assert state(cache)['file'] != snapshot.name and state(cache)['sync_note'] is None
    assert cache.root == root
    with cache.session('etf-shares') as local:
        assert local.get(ETFPrice, ('510300.XSHG', day)).close_price == Decimal(2)
        assert local.get(ETFShare, ('510300.XSHG', day)).shares == 10


def test_price_sync_failure_keeps_entire_old_snapshot(cached, session, engine):
    _, cache = cached
    day = date(2026, 1, 6)
    session.add_all([row(day, 10), price(day, '2')])
    session.commit()
    cache.refresh('etf-shares', background=False)
    original = state(cache)['file']
    session.get(ETFShare, ('510300.XSHG', day)).shares = 99
    session.get(ETFPrice, ('510300.XSHG', day)).close_price = Decimal(3)
    session.commit()
    def fail(conn, cursor, sql, *args):
        if 'FROM etf_price_daily' in sql and 'WHERE' in sql:
            raise RuntimeError('price payload failed')
    event.listen(engine, 'before_cursor_execute', fail)
    try:
        cache.refresh('etf-shares', background=False)
    finally:
        event.remove(engine, 'before_cursor_execute', fail)
    assert state(cache)['file'] == original and state(cache)['error']
    with cache.session('etf-shares') as local:
        assert local.get(ETFShare, ('510300.XSHG', day)).shares == 10
        assert local.get(ETFPrice, ('510300.XSHG', day)).close_price == Decimal(2)
    cache.refresh('etf-shares', background=False)
    assert not state(cache)['error']


def test_collection_refresh_only_etf_preserves_interrupted_pending(cached, session, monkeypatch):
    _, cache = cached
    cache.root.mkdir(parents=True, exist_ok=True)
    cache.write_state({'modules': {'industry-momentum': {'progress': {'status': 'running'}}},
                       'pending': ['industry-momentum'], 'pending_full': True,
                       'scheduled_slot': '2026-11-07T07:00:00+08:00'})
    session.add(row(date(2026, 1, 6), 10))
    session.commit()
    calls = []
    original = cache.copy_module
    def copy(module, full=False):
        calls.append((module, full))
        return original(module, full)
    monkeypatch.setattr(cache, 'copy_module', copy)
    assert cache.refresh_etf_collection()
    state = cache.read_state()
    assert calls == [('etf-shares', False)]
    assert state['pending'] == ['industry-momentum'] and state['pending_full']
    assert state['scheduled_slot'] == '2026-11-07T07:00:00+08:00'
    lock = cache.acquire()
    try:
        assert cache.refresh_etf_collection() is False
    finally:
        lock.release()


def test_calendar_table_synced_without_changing_cache_namespace(cached, session):
    from datetime import datetime
    from app.models.etf import ETFTradingDay
    _, cache = cached
    root = cache.root
    session.add(ETFTradingDay(trade_date=date(2025, 12, 31), previous_trade_date=date(2025, 12, 30),
        source='SSE.INDEX_TRADING_DAYS', updated_at=datetime(2026, 1, 8)))
    session.commit()
    assert cache.refresh_etf_collection()
    with cache.session('etf-shares') as local:
        assert local.get(ETFTradingDay, date(2025, 12, 31)).previous_trade_date == date(2025, 12, 30)
    assert cache.root == root


def test_other_pending_is_durable_even_during_etf_copy(cached, monkeypatch):
    _, cache = cached
    cache.root.mkdir(parents=True, exist_ok=True)
    cache.write_state({'modules': {}, 'pending': ['industry-momentum'], 'pending_full': True})
    def interrupted_copy(module, full=False):
        # A kill -9 here must not lose the already durable non-ETF queue.
        persisted = cache.read_state()
        assert persisted['pending'] == ['etf-shares', 'industry-momentum']
        assert persisted['pending_full'] is True
        raise RuntimeError('simulated interruption after first persisted state')
    monkeypatch.setattr(cache, 'copy_module', interrupted_copy)
    assert cache.refresh_etf_collection() is False
    assert cache.read_state()['pending'] == ['industry-momentum']
    assert cache.read_state()['pending_full'] is True
