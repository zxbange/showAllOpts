"""Incremental transfers, source deletions, quarter changes and full rebuild escape hatch."""
from datetime import date, datetime

import pytest
from sqlalchemy import event, select

from app.core.cache import MODULES, SnapshotCache
from app.core.cache_sync import OMIT
from app.models.dividend import DividendSnapshot, DividendStock
from app.models.earnings import EarningsRun, EarningsStock
from app.models.momentum import IndustryMomentum
from test_cache import cached
from test_momentum_api import row as momentum_row
from test_crowding_api import row as crowding_row
from test_microcap_api import row as microcap_row
from test_etf_api import row as etf_row
from test_breadth_api import row as breadth_row
from test_earnings_api import seed as earnings_seed


HISTORY = [
    ('industry-momentum', lambda day: momentum_row(day, 'B', 3), 'momentum'),
    ('market-crowding', lambda day: crowding_row(day, .3), 'ratio'),
    ('microcap-temperature', lambda day: microcap_row(day, 50), 'score'),
    ('etf-shares', lambda day: etf_row(day, 1000), 'shares'),
    ('microcap-breadth', lambda day: breadth_row(day), 'high_5'),
]


def assert_matches(cache, session, module):
    assert not next(m for m in cache.status()['modules'] if m['id'] == module)['error']
    count = 0
    with cache.session(module) as local:
        for model in MODULES[module][1]:
            table = model.__table__
            columns = [c for c in table.columns if c.name not in OMIT]
            query = select(*columns).order_by(*table.primary_key.columns)
            expected = session.execute(query).all()
            assert local.execute(query).all() == expected
            count += len(expected)
    assert cache.read_state()['modules'][module]['rows'] == count


@pytest.mark.parametrize('module,factory,value_column', HISTORY)
def test_history_incremental_backfill_corrections_deletions_and_no_change(cached, session, engine, module, factory, value_column):
    _, cache = cached
    old = factory(date(2026, 1, 10))
    removed = factory(date(2026, 1, 11))
    session.add_all([old, removed])
    session.commit()
    cache.refresh(module, background=False)
    setattr(old, value_column, 7)
    old.updated_at = datetime(2026, 10, 18, 18)
    session.delete(removed)
    # Backfill precedes the cached maximum date: a MAX(trade_date) cursor would lose it.
    session.add_all([factory(date(2025, 12, 1)), factory(date(2026, 1, 12))])
    session.commit()
    statements = []
    def capture(conn, cursor, sql, parameters, *args): statements.append((sql, parameters))
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        cache.refresh(module, background=False)
        assert len(statements) == 2  # One lightweight manifest plus changed rows only.
        assert all(sql.lstrip().startswith('SELECT') for sql, _ in statements)
        assert '.' + value_column not in statements[0][0]
        assert 'WHERE' in statements[1][0]
        assert len(statements[1][1][:-2]) == 3 * len(old.__table__.primary_key.columns)
        statements.clear()
        cache.refresh(module, background=False)
        assert len(statements) == 1
        assert '.' + value_column not in statements[0][0]
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert_matches(cache, session, module)


def test_many_changed_keys_are_batched_without_duplicate_rows(cached, session, engine):
    _, cache = cached
    cache.refresh('industry-momentum', background=False)
    session.add_all([momentum_row(date(2026, 1, 7), str(i), i) for i in range(501)])
    session.commit()
    statements = []
    def capture(conn, cursor, sql, parameters, *args): statements.append((sql, parameters))
    event.listen(engine, 'before_cursor_execute', capture)
    try: cache.refresh('industry-momentum', background=False)
    finally: event.remove(engine, 'before_cursor_execute', capture)
    assert len(statements) == 4
    assert [len(params[:-2]) for _, params in statements[1:]] == [750, 750, 3]
    assert_matches(cache, session, 'industry-momentum')


def test_full_rebuild_repairs_manual_change_without_updated_at(cached, session):
    settings, cache = cached
    cache.refresh('industry-momentum', background=False)
    session.query(IndustryMomentum).update({'momentum': 42})
    session.commit()
    cache.refresh('industry-momentum', background=False)
    with cache.session('industry-momentum') as local:
        assert local.scalar(select(IndustryMomentum.momentum)) != 42
    cache.refresh('industry-momentum', background=False, full=True)
    assert_matches(cache, session, 'industry-momentum')


def test_earnings_only_downloads_changed_quarters_and_tracks_membership(cached, session, engine):
    _, cache = cached
    past, current = date(2026, 6, 30), date(2026, 9, 30)
    earnings_seed(session, period=past)
    cache.refresh('earnings-gap', background=False)
    # Even if a writer forgets to update the header, new stock keys are discovered.
    earnings_seed(session, code='600002.XSHG')
    statements = []
    def capture(conn, cursor, sql, parameters, *args): statements.append((sql, parameters))
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        cache.refresh('earnings-gap', background=False)
        details = [(sql, params) for sql, params in statements if 'earnings_gap_stock.events' in sql]
        assert len(details) == 1 and details[0][1][:-2] == (str(current),)
        statements.clear()
        cache.refresh('earnings-gap', background=False)
        assert len(statements) == 2
        assert all('earnings_gap_stock.events' not in sql for sql, _ in statements)
    finally: event.remove(engine, 'before_cursor_execute', capture)
    assert_matches(cache, session, 'earnings-gap')
    # Corrections signal through the quarter header; a removed quarter disappears locally.
    session.query(EarningsStock).filter_by(report_period=current).update({'name': 'Corrected'})
    session.get(EarningsRun, current).updated_at = datetime(2026, 10, 18, 19)
    session.query(EarningsStock).filter_by(report_period=past).delete()
    session.query(EarningsRun).filter_by(report_period=past).delete()
    session.commit()
    cache.refresh('earnings-gap', background=False)
    assert_matches(cache, session, 'earnings-gap')


def test_dividend_replacement_drops_removed_stocks_and_updates_same_key(cached, session):
    _, cache = cached
    cache.refresh('dividend-selection', background=False)
    session.query(DividendStock).filter_by(method='classic').delete()
    session.query(DividendStock).filter_by(method='value').update({'name': 'Changed', 'rank': 1})
    session.get(DividendSnapshot, 1).classic_count = 0
    session.commit()
    cache.refresh('dividend-selection', background=False)
    assert_matches(cache, session, 'dividend-selection')


def test_interrupted_incremental_transfer_does_not_publish_partial_changes(cached, session, engine):
    _, cache = cached
    cache.refresh('industry-momentum', background=False)
    before = cache.read_state()['modules']['industry-momentum'].copy()
    session.add(momentum_row(date(2026, 1, 7), 'A', 7))
    session.commit()
    def fail(conn, cursor, sql, *args):
        if 'industry_momentum.momentum,' in sql: raise RuntimeError('interrupted details query')
    event.listen(engine, 'before_cursor_execute', fail)
    try: cache.refresh('industry-momentum', background=False)
    finally: event.remove(engine, 'before_cursor_execute', fail)
    after = cache.read_state()['modules']['industry-momentum']
    assert after['file'] == before['file'] and after['updated_at'] == before['updated_at']
    assert after['error'] and len(list(cache.root.glob('industry-momentum-*.sqlite'))) == 1
    cache.refresh('industry-momentum', background=False)
    assert_matches(cache, session, 'industry-momentum')


def test_pending_full_rebuild_survives_restart(cached, session, engine):
    settings, cache = cached
    cache.refresh(background=False)
    session.query(IndustryMomentum).update({'momentum': 42})
    session.commit()
    state = cache.read_state()
    state.update(pending=['industry-momentum'], pending_full=True)
    cache.write_state(state)
    SnapshotCache(settings, engine).tick()
    assert_matches(cache, session, 'industry-momentum')


def test_high_growth_snapshot_replaces_removed_stocks_and_preserves_evidence(cached,session):
    from app.models.growth import GrowthStock, GrowthSnapshot
    _,cache=cached
    cache.refresh('high-growth',background=False)
    assert_matches(cache,session,'high-growth')
    stock=session.query(GrowthStock).one()
    stock.pe_ttm=23
    session.commit()
    cache.refresh('high-growth',background=False)
    assert_matches(cache,session,'high-growth')
    session.delete(stock)
    session.query(GrowthSnapshot).one().matched_count=0
    session.commit()
    cache.refresh('high-growth',background=False)
    assert_matches(cache,session,'high-growth')
