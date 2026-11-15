"""Committed source changes and local cursor move together, including history repairs."""
from datetime import date
import importlib.util
import warnings
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker
from app.core.result_sync import HEAD, CHANGE, CURSOR
from app.models.microcap import MicrocapTemperature
from test_cache import cached
from test_cache_incremental import assert_matches
from test_microcap_api import row

spec = importlib.util.spec_from_file_location('journal', Path(__file__).resolve().parents[2] / 'collectors/rules/result_journal.py')
journal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(journal)
MODULE = 'microcap-temperature'


def test_creation_silences_only_expected_existing_table_warnings(engine):
    class DriverWarning(Warning):
        pass

    def database_warning(conn, cursor, statement, *args):
        for table in ('result_sync_head', 'result_sync_change'):
            if 'CREATE TABLE IF NOT EXISTS '+table in statement:
                warnings.warn(DriverWarning(1050, "Table '{}' already exists".format(table)))
        if 'CREATE TABLE IF NOT EXISTS result_sync_change' in statement:
            warnings.warn(DriverWarning(1265, 'Data truncated'))
            warnings.warn(DriverWarning(1050, "Table 'other_table' already exists"))

    event.listen(engine, 'before_cursor_execute', database_warning)
    try:
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter('always')
            journal._install_result_journal(sessionmaker(bind=engine), (MicrocapTemperature,), MODULE)
            warnings.warn(DriverWarning(1050, "Table 'result_sync_head' already exists"))
        assert [w.message.args for w in seen if isinstance(w.message, DriverWarning)] == [
            (1265, 'Data truncated'), (1050, "Table 'other_table' already exists"),
            (1050, "Table 'result_sync_head' already exists")]
    finally:
        event.remove(engine, 'before_cursor_execute', database_warning)


def test_creation_failure_still_raises_and_restores_warning_filters(engine):
    def fail(conn, cursor, statement, *args):
        if 'CREATE TABLE IF NOT EXISTS result_sync_change' in statement:
            raise RuntimeError('creation failed')
    event.listen(engine, 'before_cursor_execute', fail)
    previous = list(warnings.filters)
    try:
        with pytest.raises(RuntimeError, match='creation failed'):
            journal._install_result_journal(sessionmaker(bind=engine), (MicrocapTemperature,), MODULE)
        assert warnings.filters == previous
    finally:
        event.remove(engine, 'before_cursor_execute', fail)


@pytest.fixture
def writer(engine):
    factory = sessionmaker(bind=engine)
    journal._install_result_journal(factory, (MicrocapTemperature,), MODULE)
    return factory


def test_transactions_rollback_multiple_flushes_and_bulk(writer, engine):
    with writer() as source:
        source.add(row(date(2026, 1, 1), 1))
        source.flush()
        source.add(row(date(2026, 1, 2), 2))
        source.flush()
        source.rollback()
    with engine.connect() as source:
        assert source.scalar(select(HEAD.c.revision)) == 0
        assert source.execute(select(CHANGE)).all() == []
    with writer.begin() as source:
        source.add(row(date(2026, 1, 1), 1))
        source.flush()
        source.add(row(date(2026, 1, 2), 2))
        source.flush()
    with engine.connect() as source:
        assert source.scalar(select(HEAD.c.revision)) == 1
        assert len(source.execute(select(CHANGE)).all()) == 1
    with writer.begin() as source:
        journal._result_bulk(source.query(MicrocapTemperature).filter_by(trade_date=date(2026, 1, 1)))
    with engine.connect() as source:
        assert source.scalar(select(HEAD.c.revision)) == 2


def test_same_second_history_backfill_delete_and_unchanged_skip(cached, writer, session, engine):
    _, cache = cached
    with writer.begin() as source:
        source.add_all([row(date(2026, 1, 1), 1), row(date(2026, 1, 2), 2)])
    cache.refresh(MODULE, background=False)
    with writer.begin() as source:
        source.query(MicrocapTemperature).filter_by(trade_date=date(2026, 1, 1)).one().score = 9
        journal._result_bulk(source.query(MicrocapTemperature).filter_by(trade_date=date(2026, 1, 2)))
        source.add(row(date(2025, 1, 1), 3))
    cache.refresh(MODULE, background=False)
    assert_matches(cache, session, MODULE)
    previous = cache.snapshot_path(MODULE, cache.read_state()['modules'][MODULE])
    statements = []
    def capture(conn, cursor, sql, *args):
        statements.append(sql)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        cache.refresh(MODULE, background=False)
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert cache.snapshot_path(MODULE, cache.read_state()['modules'][MODULE]) == previous
    assert not any('FROM microcap_temperature' in sql or 'FROM result_sync_change' in sql for sql in statements)


def test_gap_preserves_snapshot_and_full_recovers(cached, writer, engine):
    _, cache = cached
    with writer.begin() as source:
        source.add(row(date(2026, 1, 1), 1))
    cache.refresh(MODULE, background=False)
    previous = cache.snapshot_path(MODULE, cache.read_state()['modules'][MODULE])
    with writer.begin() as source:
        source.add(row(date(2026, 1, 2), 2))
    with engine.begin() as source:
        source.execute(CHANGE.delete().where(CHANGE.c.revision == 2))
    cache.refresh(MODULE, background=False)
    assert cache.snapshot_path(MODULE, cache.read_state()['modules'][MODULE]) == previous
    assert cache.read_state()['modules'][MODULE]['error']
    cache.refresh(MODULE, background=False, full=True)
    assert not cache.read_state()['modules'][MODULE]['error']
    with cache.session(MODULE) as local:
        assert local.scalar(select(CURSOR.c.revision)) == 2


def test_generation_reset_rebuilds_and_legacy_drops_cursor(cached, writer, engine):
    _, cache = cached
    with writer.begin() as source:
        source.add(row(date(2026, 1, 1), 1))
    cache.refresh(MODULE, background=False)
    with engine.begin() as source:
        source.execute(HEAD.update().values(generation='replacement', revision=0))
        source.execute(MicrocapTemperature.__table__.delete())
    cache.refresh(MODULE, background=False)
    with cache.session(MODULE) as local:
        assert not local.execute(select(MicrocapTemperature)).all()
        assert local.scalar(select(CURSOR.c.generation)) == 'replacement'
    with engine.begin() as source:
        CHANGE.drop(source)
    cache.refresh(MODULE, background=False)
    assert cache.read_state()['modules'][MODULE]['sync_note']
    from sqlalchemy import inspect
    with cache.session(MODULE) as local:
        assert not inspect(local.connection()).has_table(CURSOR.name)


def test_failed_copy_does_not_advance_cursor_and_retry_applies_changes(cached, writer, monkeypatch):
    _, cache = cached
    with writer.begin() as source:
        source.add(row(date(2026, 1, 1), 1))
    cache.refresh(MODULE, background=False)
    previous = cache.snapshot_path(MODULE, cache.read_state()['modules'][MODULE])
    with writer.begin() as source:
        source.add(row(date(2026, 1, 2), 2))
    import app.core.result_sync as sync
    original = sync.transfer
    def fail(*args, **kwargs):
        raise OSError('simulated disk failure')
    monkeypatch.setattr(sync, 'transfer', fail)
    cache.refresh(MODULE, background=False)
    assert cache.snapshot_path(MODULE, cache.read_state()['modules'][MODULE]) == previous
    with cache.session(MODULE) as local:
        assert local.scalar(select(CURSOR.c.revision)) == 1
    monkeypatch.setattr(sync, 'transfer', original)
    cache.refresh(MODULE, background=False)
    with cache.session(MODULE) as local:
        assert local.scalar(select(CURSOR.c.revision)) == 2
        assert len(local.execute(select(MicrocapTemperature)).all()) == 2


@pytest.mark.parametrize('payload', ['not json', '[]', '[{"table":"unknown","key":[],"deleted":false}]'])
def test_malformed_journal_preserves_published_snapshot(cached, writer, engine, payload):
    _, cache = cached
    with writer.begin() as source:
        source.add(row(date(2026, 1, 1), 1))
    cache.refresh(MODULE, background=False)
    previous = cache.snapshot_path(MODULE, cache.read_state()['modules'][MODULE])
    with engine.begin() as source:
        source.execute(HEAD.update().values(revision=2))
        source.execute(CHANGE.insert().values(module=MODULE, revision=2, payload=payload))
    cache.refresh(MODULE, background=False)
    assert cache.snapshot_path(MODULE, cache.read_state()['modules'][MODULE]) == previous
    assert cache.read_state()['modules'][MODULE]['error']


def test_shared_embedded_protocols_remain_identical():
    root = Path(__file__).resolve().parents[2]
    for name in ('result_journal',):
        spec = importlib.util.spec_from_file_location(name, root / ('collectors/tools/sync_' + name + '.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.sync(check=True)


def test_independent_writers_share_revision_and_bulk_uses_current_locked_keys(writer, engine):
    from sqlalchemy.dialects import mysql
    other = sessionmaker(bind=engine)
    journal._install_result_journal(other, (MicrocapTemperature,), MODULE)
    with writer.begin() as source:
        source.add(row(date(2026, 1, 1), 1))
    with other.begin() as source:
        source.add(row(date(2026, 1, 2), 2))
    statements = []
    def capture(conn, clause, *args):
        statements.append(str(clause.compile(dialect=mysql.dialect())))
    event.listen(engine, 'before_execute', capture)
    try:
        with writer.begin() as source:
            journal._result_bulk(source.query(MicrocapTemperature))
    finally:
        event.remove(engine, 'before_execute', capture)
    locks = [sql for sql in statements if 'FOR UPDATE' in sql]
    assert len(locks) == 2
    assert 'result_sync_head' in locks[0]
    assert MicrocapTemperature.__tablename__ in locks[1]
    with engine.connect() as source:
        assert source.scalar(select(HEAD.c.revision)) == 3
        assert [r.revision for r in source.execute(select(CHANGE).order_by(CHANGE.c.revision))] == [1, 2, 3]
