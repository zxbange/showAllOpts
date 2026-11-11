"""Momentum/breadth repairs use committed keys, including unchanged date bounds."""
from datetime import date, datetime, timedelta
import sqlite3

import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.core.append_sync import APPEND_PARTITIONS
from app.core.cache import MODULES
from app.core.result_sync import CHANGE, CURSOR, HEAD, read_cursor
from test_breadth_api import row as breadth_row
from test_cache import cached
from test_cache_incremental import assert_matches
from test_momentum_api import row as momentum_row
from test_result_journal_sync import journal

CASES = [
    ('industry-momentum', lambda day: momentum_row(day, 'X', 3), 'momentum'),
    ('microcap-breadth', lambda day: breadth_row(day), 'high_5'),
]


@pytest.fixture(params=CASES, ids=[case[0] for case in CASES])
def history_case(request, session):
    module, factory, value = request.param
    model = MODULES[module][1][0]
    session.query(model).delete()
    session.commit()
    return module, model, factory, value


def install_writer(engine, module, model):
    writer = sessionmaker(bind=engine)
    journal._install_result_journal(writer, (model,), module)
    return writer


def entry(cache, module):
    return cache.read_state()['modules'][module]


def path(cache, module):
    return cache.snapshot_path(module, entry(cache, module))


def test_baseline_and_same_bounds_committed_backfill_correction_and_delete(cached, history_case, engine, session):
    _, cache = cached
    module, model, factory, value = history_case
    assert module not in APPEND_PARTITIONS
    writer = install_writer(engine, module, model)
    with writer.begin() as source:
        source.add_all(factory(date(2026, 1, day)) for day in [1, 3, 5])
    cache.refresh(module, background=False)
    assert_matches(cache, session, module)
    assert read_cursor(path(cache, module), module)['revision'] == 1
    old = path(cache, module)
    with writer.begin() as source:
        # No timestamp change, no MIN/MAX change, and no net row-count change.
        setattr(source.query(model).filter_by(trade_date=date(2026, 1, 1)).one(), value, 9)
        journal._result_bulk(source.query(model).filter_by(trade_date=date(2026, 1, 3)))
        source.add(factory(date(2026, 1, 2)))
    statements = []
    def capture(conn, cursor, sql, *args):
        statements.append(sql)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        cache.refresh(module, background=False)
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert path(cache, module) != old
    assert_matches(cache, session, module)
    assert read_cursor(path(cache, module), module)['revision'] == 2
    payloads = [sql for sql in statements if 'FROM ' + model.__tablename__ in sql]
    assert len(payloads) == 1 and ' IN (' in payloads[0]
    assert not any('GROUP BY' in sql or 'member_codes' in sql for sql in statements)
    with cache.session(module) as local:
        if module == 'industry-momentum':
            names = {index['name'] for index in inspect(local.connection()).get_indexes(model.__tablename__)}
            assert {'ix_local_momentum_type_date', 'ix_local_momentum_history'} <= names
    with writer.begin() as source:
        journal._result_bulk(source.query(model))
    cache.refresh(module, background=False)
    assert_matches(cache, session, module)
    assert read_cursor(path(cache, module), module)['rows'] == 0
    with writer.begin() as source:
        source.add(factory(date(2025, 1, 1)))
    cache.refresh(module, background=False)
    assert_matches(cache, session, module)


@pytest.mark.parametrize('history_size', [1, 2501])
def test_journal_noop_is_bounded_without_manifest_or_copy(cached, history_case, engine, monkeypatch, history_size):
    _, cache = cached
    module, model, factory, _ = history_case
    writer = install_writer(engine, module, model)
    with writer.begin() as source:
        source.add_all(factory(date(2020, 1, 1) + timedelta(days=i)) for i in range(history_size))
    cache.refresh(module, background=False)
    before = entry(cache, module).copy()
    source_sql, local_sql = [], []
    def capture(conn, cursor, sql, *args):
        (source_sql if conn.engine is engine else local_sql).append(sql)
    def cannot_copy(*args):
        raise AssertionError('Journal no-op must not copy SQLite')
    monkeypatch.setattr('app.core.result_sync.shutil.copyfile', cannot_copy)
    event.listen(Engine, 'before_cursor_execute', capture)
    try:
        cache.refresh(module, background=False)
    finally:
        event.remove(Engine, 'before_cursor_execute', capture)
    selects = [sql for sql in source_sql if sql.startswith('SELECT')]
    assert len(selects) == 1 and 'FROM result_sync_head' in selects[0]
    assert not any('FROM ' + model.__tablename__ in sql or 'count(' in sql.lower() for sql in local_sql)
    after = entry(cache, module)
    assert not after['error']
    assert (after['file'], after['updated_at']) == (before['file'], before['updated_at'])


@pytest.mark.parametrize('missing_journal', ['tables', 'module-head'])
def test_legacy_manifest_repairs_old_append_snapshot_and_journal_upgrade(cached, history_case, engine, session, missing_journal):
    _, cache = cached
    module, model, factory, value = history_case
    if missing_journal == 'module-head':
        HEAD.metadata.create_all(engine)
    session.add_all(factory(date(2026, 1, day)) for day in [1, 3, 5])
    session.commit()
    cache.refresh(module, background=False)
    old = path(cache, module)
    # Simulate a pre-upgrade snapshot whose MIN/MAX certificate hides a hole.
    partition_column = 'industry_type' if module == 'industry-momentum' else 'series_id'
    partition = getattr(factory(date(2026, 1, 1)), partition_column)
    with sqlite3.connect(old) as local:
        local.execute('CREATE TABLE append_cache_cursor_v1 (partition TEXT PRIMARY KEY, first_date DATE, successful_date DATE)')
        local.execute("INSERT INTO append_cache_cursor_v1 VALUES (?, '2026-01-01', '2026-01-05')", (partition,))
        local.execute('CREATE TABLE append_cache_version (version INTEGER PRIMARY KEY, total_rows INTEGER)')
        local.execute('INSERT INTO append_cache_version VALUES (1, 3)')
        local.execute('DELETE FROM ' + model.__tablename__ + " WHERE trade_date='2026-01-03'")
    session.add(factory(date(2026, 1, 2)))
    session.query(model).filter_by(trade_date=date(2026, 1, 1)).update(
        {value: 11, 'updated_at': datetime(2026, 11, 11, 17)})
    session.commit()
    statements = []
    def capture(conn, cursor, sql, *args):
        statements.append(sql)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        cache.refresh(module, background=False)
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert_matches(cache, session, module)
    assert entry(cache, module)['sync_note']
    manifest = [sql for sql in statements if 'FROM ' + model.__tablename__ in sql][0]
    assert 'updated_at' in manifest and 'GROUP BY' not in manifest
    with cache.session(module) as local:
        names = set(inspect(local.connection()).get_table_names())
        assert not {CURSOR.name, 'append_cache_cursor_v1', 'append_cache_version'} & names
    writer = install_writer(engine, module, model)
    with writer.begin() as source:
        source.add(factory(date(2026, 1, 4)))
    cache.refresh(module, background=False)
    assert_matches(cache, session, module)
    assert not entry(cache, module)['sync_note']
    assert read_cursor(path(cache, module), module)['revision'] == 1


def test_journal_rollback_and_failed_cache_publish_preserve_revision(cached, history_case, engine, session, monkeypatch):
    _, cache = cached
    module, model, factory, _ = history_case
    writer = install_writer(engine, module, model)
    with writer.begin() as source:
        source.add_all(factory(date(2026, 1, day)) for day in [1, 5])
    cache.refresh(module, background=False)
    previous = path(cache, module)
    with writer() as source:
        source.add(factory(date(2026, 1, 2)))
        source.flush()
        source.add(factory(date(2026, 1, 3)))
        source.flush()
        source.rollback()
    with engine.connect() as source:
        assert source.scalar(select(HEAD.c.revision).where(HEAD.c.module == module)) == 1
        assert len(source.execute(select(CHANGE).where(CHANGE.c.module == module)).all()) == 1
    cache.refresh(module, background=False)
    assert path(cache, module) == previous
    with writer.begin() as source:
        source.add(factory(date(2026, 1, 2)))
    import app.core.result_sync as sync
    original = sync.transfer
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('interrupted after staging rows')
    monkeypatch.setattr(sync, 'transfer', fail)
    cache.refresh(module, background=False)
    assert path(cache, module) == previous
    assert read_cursor(previous, module)['revision'] == 1
    assert entry(cache, module)['error']
    assert list(cache.root.glob(module + '-*.sqlite')) == [previous]
    monkeypatch.setattr(sync, 'transfer', original)
    cache.refresh(module, background=False)
    assert_matches(cache, session, module)
    assert read_cursor(path(cache, module), module)['revision'] == 2


def test_missing_journal_requires_full_and_legacy_fallback_invalidates_cursor(cached, history_case, engine, session):
    _, cache = cached
    module, model, factory, value = history_case
    writer = install_writer(engine, module, model)
    with writer.begin() as source:
        source.add_all(factory(date(2026, 1, day)) for day in [1, 5])
    cache.refresh(module, background=False)
    previous = path(cache, module)
    with writer.begin() as source:
        source.add(factory(date(2026, 1, 3)))
    with engine.begin() as source:
        source.execute(CHANGE.delete().where(CHANGE.c.module == module, CHANGE.c.revision == 2))
    cache.refresh(module, background=False)
    assert path(cache, module) == previous and entry(cache, module)['error']
    cache.refresh(module, background=False, full=True)
    assert_matches(cache, session, module)
    assert read_cursor(path(cache, module), module)['revision'] == 2
    # A downgraded/no-journal collector cannot preserve a cursor it did not update.
    with engine.begin() as source:
        CHANGE.drop(source)
    session.add(factory(date(2026, 1, 2)))
    session.commit()
    cache.refresh(module, background=False)
    assert_matches(cache, session, module)
    assert read_cursor(path(cache, module), module) is None
    assert entry(cache, module)['sync_note']
    # Legacy same-key manual corrections still require a new timestamp or full.
    session.query(model).update({value: 17})
    session.commit()
    cache.refresh(module, background=False, full=True)
    assert_matches(cache, session, module)


def test_unconfirmed_live_key_cannot_publish_a_journal_cursor(cached, history_case, engine, session):
    _, cache = cached
    module, model, factory, _ = history_case
    writer = install_writer(engine, module, model)
    with writer.begin() as source:
        source.add_all(factory(date(2026, 1, day)) for day in [1, 5])
    cache.refresh(module, background=False)
    previous = path(cache, module)
    with writer.begin() as source:
        source.add(factory(date(2026, 1, 3)))
    # Simulate an inconsistent/manual source edit outside the collector journal.
    with engine.begin() as source:
        source.execute(model.__table__.delete().where(model.trade_date == date(2026, 1, 3)))
    cache.refresh(module, background=False)
    assert entry(cache, module)['error'] and path(cache, module) == previous
    assert read_cursor(previous, module)['revision'] == 1
    cache.refresh(module, background=False, full=True)
    assert_matches(cache, session, module)
    assert read_cursor(path(cache, module), module)['rows'] == 2
    assert read_cursor(path(cache, module), module)['revision'] == 2
