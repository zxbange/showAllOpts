"""Append boundaries remain crowding-only; history modules reconcile all keys."""
from datetime import date, datetime, timedelta
import sqlite3
import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from app.core.append_sync import APPEND_PARTITIONS, read_cursor
from app.core.cache import MODULES
from test_cache import cached
from test_cache_incremental import assert_matches
from test_momentum_api import row as momentum_row
from test_crowding_api import row as crowding_row
from test_breadth_api import row as breadth_row

CASES = [
    ('industry-momentum', lambda d, p: momentum_row(d, 'X', 3, kind=p), 'industry_type', ['sw_l1', 'sw_l2', 'jq_l1'], 'momentum'),
    ('market-crowding', lambda d, p: crowding_row(d, .3, top_pct=p), 'top_pct', ['0.05', '0.10', '0.20'], 'ratio'),
    ('microcap-breadth', lambda d, p: breadth_row(d, series=p), 'series_id', ['A', 'B', 'C'], 'high_5'),
]

def entry(cache, module):
    return cache.read_state()['modules'][module]

@pytest.mark.parametrize('module,factory,partition,parts,value', CASES)
def test_partition_lag_new_type_retention_and_empty_reappearance(cached, session, module, factory, partition, parts, value):
    _, cache = cached
    model = MODULES[module][1][0]
    session.query(model).delete()
    session.add_all([factory(date(2026, 1, 10), parts[0]), factory(date(2026, 1, 2), parts[1])])
    session.commit()
    cache.refresh(module, background=False)
    session.add_all([factory(date(2026, 1, 3), parts[1]), factory(date(2025, 12, 1), parts[2])])
    session.commit()
    cache.refresh(module, background=False)
    assert_matches(cache, session, module)
    session.query(model).filter(getattr(model, partition) == parts[0]).delete()
    session.query(model).filter(getattr(model, partition) == parts[1], model.trade_date < date(2026, 1, 3)).delete()
    session.commit()
    cache.refresh(module, background=False)
    assert_matches(cache, session, module)
    session.query(model).delete()
    session.commit()
    cache.refresh(module, background=False)
    assert_matches(cache, session, module)
    empty_file = entry(cache, module)['file']
    cache.refresh(module, background=False)
    if module in APPEND_PARTITIONS:
        assert entry(cache, module)['file'] == empty_file
    else:
        assert entry(cache, module)['sync_note']
        assert_matches(cache, session, module)
    session.add(factory(date(2025, 1, 1), parts[0]))
    session.commit()
    cache.refresh(module, background=False)
    assert_matches(cache, session, module)

@pytest.mark.parametrize('module,factory,partition,parts,value', CASES)
def test_interior_repair_reconciles_legacy_history_but_crowding_requires_full(cached, session, module, factory, partition, parts, value):
    _, cache = cached
    model = MODULES[module][1][0]
    session.query(model).delete()
    session.add_all([factory(date(2026, 1, d), parts[0]) for d in [1, 3, 5]])
    session.commit()
    cache.refresh(module, background=False)
    before = entry(cache, module)['file']
    session.query(model).filter(model.trade_date == date(2026, 1, 3)).delete()
    session.query(model).filter(model.trade_date == date(2026, 1, 1)).update(
        {value: 7, 'updated_at': datetime(2026, 11, 11, 17)})
    session.add(factory(date(2026, 1, 2), parts[0]))
    session.commit()
    cache.refresh(module, background=False)
    if module in APPEND_PARTITIONS:
        assert entry(cache, module)['file'] == before
    else:
        assert entry(cache, module)['file'] != before
        assert_matches(cache, session, module)
    cache.refresh(module, background=False, full=True)
    assert_matches(cache, session, module)

def test_legacy_snapshot_is_rebuilt_once_not_trusted_as_cursor(cached, session):
    _, cache = cached
    module = 'market-crowding'
    session.add_all([crowding_row(date(2026, 1, day), .3) for day in [5, 6]])
    session.commit()
    cache.refresh(module, background=False)
    old = cache.root / entry(cache, module)['file']
    with sqlite3.connect(old) as local:
        local.execute('DROP TABLE append_cache_cursor_v1')
        local.execute('DROP TABLE append_cache_version')
        local.execute("DELETE FROM market_crowding WHERE trade_date='2026-01-05'")
    cache.refresh(module, background=False)
    assert entry(cache, module)['file'] != old.name
    assert_matches(cache, session, module)
    migrated = entry(cache, module)['file']
    cache.refresh(module, background=False)
    assert entry(cache, module)['file'] == migrated

@pytest.mark.parametrize('history_size', [1, 2501])
def test_noop_remote_metadata_is_bounded_and_never_copies_sqlite(cached, session, engine, monkeypatch, history_size):
    _, cache = cached
    session.add_all([crowding_row(date(2020, 1, 1)+timedelta(days=i), .3) for i in range(history_size)])
    session.commit()
    cache.refresh('market-crowding', background=False)
    before = entry(cache, 'market-crowding').copy()
    statements = []
    def capture(conn, cursor, sql, params, *args): statements.append(sql)
    def cannot_copy(*args): raise AssertionError('No-op must not copy SQLite')
    monkeypatch.setattr('app.core.append_sync.shutil.copyfile', cannot_copy)
    local_statements = []
    def capture_local(conn, cursor, sql, params, *args):
        if conn.engine is not engine: local_statements.append(sql)
    event.listen(engine, 'before_cursor_execute', capture)
    event.listen(Engine, 'before_cursor_execute', capture_local)
    try: cache.refresh('market-crowding', background=False)
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
        event.remove(Engine, 'before_cursor_execute', capture_local)
    assert not any('count(' in sql.lower() or 'FROM market_crowding' in sql for sql in local_statements)
    assert len(statements) == 1 and 'GROUP BY' in statements[0]
    assert 'updated_at' not in statements[0] and 'ratio' not in statements[0]
    after = entry(cache, 'market-crowding')
    assert not after['error'] and after['file'] == before['file'] and after['updated_at'] == before['updated_at']

def test_failed_second_partition_does_not_advance_any_published_cursor(cached, session, engine):
    _, cache = cached
    module = 'market-crowding'
    session.add_all([crowding_row(date(2026, 1, 2), .2, top_pct='0.05'),
                     crowding_row(date(2026, 1, 6), .2, top_pct='0.10')])
    session.commit()
    cache.refresh(module, background=False)
    before = entry(cache, module).copy()
    with cache.session(module) as local: cursor_before = read_cursor(local.connection())
    session.add_all([crowding_row(date(2026, 1, 3), .3, top_pct='0.05'),
                     crowding_row(date(2026, 1, 7), .7, top_pct='0.10')])
    session.commit()
    payloads = []
    def fail(conn, cursor, sql, *args):
        if 'market_crowding.ratio,' in sql:
            payloads.append(sql)
            if len(payloads) == 2: raise RuntimeError('second partition failed')
    event.listen(engine, 'before_cursor_execute', fail)
    try: cache.refresh(module, background=False)
    finally: event.remove(engine, 'before_cursor_execute', fail)
    assert len(payloads) == 2 and entry(cache, module)['file'] == before['file']
    with cache.session(module) as local: assert read_cursor(local.connection()) == cursor_before
    cache.refresh(module, background=False)
    assert_matches(cache, session, module)

@pytest.mark.parametrize('module,factory,partition,parts,value', CASES)
def test_regressed_source_boundary_reconciles_partition(cached, session, module, factory, partition, parts, value):
    _, cache = cached
    model = MODULES[module][1][0]
    session.query(model).delete()
    session.add_all([factory(date(2026, 1, day), parts[0]) for day in [5, 6]])
    session.commit()
    cache.refresh(module, background=False)
    session.query(model).filter(model.trade_date == date(2026, 1, 6)).delete()
    session.query(model).update({value: 19, 'updated_at': datetime(2026, 11, 11, 17)})
    session.commit()
    cache.refresh(module, background=False)
    assert_matches(cache, session, module)
