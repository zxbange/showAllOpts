"""Incremental transfers, source deletions, quarter changes and full rebuild escape hatch."""
from datetime import date, datetime

import pytest
from sqlalchemy import event, select

from app.core.cache import MODULES, SnapshotCache
from app.core.cache_sync import OMIT
from app.models.dividend import DividendSnapshot, DividendStock
from app.models.momentum import IndustryMomentum
from test_cache import cached
from test_momentum_api import row as momentum_row
from test_microcap_api import row as microcap_row


HISTORY = [('microcap-temperature', lambda day: microcap_row(day, 50), 'score')]


def test_dividend_legacy_schema_and_new_history_refresh(cached, session, engine):
    from sqlalchemy import text
    from test_dividend_collector import meta, row
    _, cache = cached
    session.query(DividendStock).delete()
    session.query(DividendSnapshot).delete()
    session.add(DividendSnapshot(id=1,updated_at=datetime(2026,10,16),**meta(rule_version='dividend_static_fy_v4')))
    session.add(DividendStock(**row())); session.commit()
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE dividend_selection_stock DROP COLUMN dividend_yield_history'))
        connection.execute(text('ALTER TABLE dividend_selection_stock DROP COLUMN dividend_per_share'))
        connection.execute(text('ALTER TABLE dividend_selection_stock DROP COLUMN dividend_fiscal_year'))
    cache.refresh('dividend-selection',background=False)
    with cache.session('dividend-selection') as local:
        legacy = local.query(DividendStock).one()
        assert legacy.dividend_yield_history is None
        assert legacy.dividend_per_share is None
        assert legacy.dividend_fiscal_year is None
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE dividend_selection_stock ADD COLUMN dividend_yield_history JSON'))
        connection.execute(text('ALTER TABLE dividend_selection_stock ADD COLUMN dividend_per_share NUMERIC(24,8)'))
        connection.execute(text('ALTER TABLE dividend_selection_stock ADD COLUMN dividend_fiscal_year INTEGER'))
        connection.execute(DividendStock.__table__.update().values(dividend_yield_history=row()['dividend_yield_history']))
    cache.refresh('dividend-selection',background=False)
    with cache.session('dividend-selection') as local:
        assert local.query(DividendStock).one().dividend_yield_history == row()['dividend_yield_history']


def assert_matches(cache, session, module):
    assert not cache.read_state()['modules'][module]['error']
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
    def capture(conn, cursor, sql, parameters, *args):
        if not sql.startswith('PRAGMA'): statements.append((sql, parameters))
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


def test_many_new_rows_use_complete_manifest_and_bounded_key_batches(cached, session, engine):
    _, cache = cached
    cache.refresh('industry-momentum', background=False)
    session.add_all([momentum_row(date(2026, 1, 7), str(i), i) for i in range(501)])
    session.commit()
    statements = []
    def capture(conn, cursor, sql, parameters, *args):
        if not sql.startswith('PRAGMA'): statements.append((sql, parameters))
    event.listen(engine, 'before_cursor_execute', capture)
    try: cache.refresh('industry-momentum', background=False)
    finally: event.remove(engine, 'before_cursor_execute', capture)
    assert len(statements) == 4  # Full key manifest plus 250 + 250 + 1 changed keys.
    assert 'industry_momentum.updated_at' in statements[0][0]
    assert 'GROUP BY' not in statements[0][0]
    assert all(' IN (' in sql and 'trade_date >' not in sql for sql, _ in statements[1:])
    assert sorted(len(params[:-2]) for _, params in statements[1:]) == [3, 750, 750]
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


def test_dividend_replacement_drops_removed_stocks_and_updates_same_key(cached, session):
    _, cache = cached
    cache.refresh('dividend-selection', background=False)
    session.query(DividendStock).filter_by(method='bank').delete()
    session.query(DividendStock).filter_by(method='value').update({'name': 'Changed', 'rank': 1})
    session.get(DividendSnapshot, 1).value_count = 1
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
    resumed = SnapshotCache(settings, engine)
    resumed.tick()
    assert resumed.read_state()['pending_full'] is True
    resumed.refresh('industry-momentum', background=False)
    assert_matches(cache, session, 'industry-momentum')
