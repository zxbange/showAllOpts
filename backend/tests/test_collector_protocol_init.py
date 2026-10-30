"""First-use protocol initialization remains idempotent across collector processes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import importlib.util
from pathlib import Path
import threading

import pytest
from sqlalchemy import Column, Date, MetaData, Table, create_engine, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'collector_rules' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('kind', ['result', 'earnings'])
def test_concurrent_first_initialization_and_version_adoption(tmp_path, kind):
    engine = create_engine('sqlite:///' + str(tmp_path / 'init.sqlite'),
                           connect_args={'check_same_thread': False, 'timeout': 10})
    module = load('result_journal' if kind == 'result' else 'earnings_version')
    if kind == 'result':
        _, head, changes = module._result_journal_tables()
        initialize = lambda factory: module._install_result_journal(factory, (), 'market-sentiment')
    else:
        head = module._earnings_version_table()
        runs = Table('earnings_gap_run', MetaData(), Column('report_period', Date, primary_key=True))
        runs.create(engine)
        with engine.begin() as source:
            source.execute(runs.insert().values(report_period=date(2026, 9, 30)))
        initialize = module._prepare_earnings_version
    ddl_barrier, insert_barrier = threading.Barrier(2), threading.Barrier(2)
    seen = threading.local()
    def before(conn, cursor, sql, *args):
        if sql.lstrip().startswith('CREATE TABLE'):
            assert 'IF NOT EXISTS' in sql
            ddl_barrier.wait(timeout=10)
    def after(conn, cursor, sql, *args):
        # Both initializers see no head, then race to insert it.
        if sql.lstrip().startswith('SELECT') and 'FROM ' + head.name in sql and not getattr(seen, 'read', False):
            seen.read = True
            insert_barrier.wait(timeout=10)
    event.listen(engine, 'before_cursor_execute', before)
    event.listen(engine, 'after_cursor_execute', after)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(initialize, sessionmaker(bind=engine)) for _ in range(2)]
            for future in futures:
                future.result(timeout=20)
    finally:
        event.remove(engine, 'before_cursor_execute', before)
        event.remove(engine, 'after_cursor_execute', after)
    with engine.begin() as source:
        original = dict(source.execute(select(head)).mappings().one())
        assert original['revision'] == 0
        source.execute(head.update().values(revision=7))
    # Starting another copy must preserve the winner and all committed progress.
    initialize(sessionmaker(bind=engine))
    with engine.connect() as source:
        current = dict(source.execute(select(head)).mappings().one())
        assert current == dict(original, revision=7)
    engine.dispose()


@pytest.mark.parametrize('kind', ['result', 'earnings'])
def test_initialization_does_not_swallow_ddl_failures(tmp_path, kind):
    engine = create_engine('sqlite:///' + str(tmp_path / 'denied.sqlite'))
    module = load('result_journal' if kind == 'result' else 'earnings_version')
    factory = sessionmaker(bind=engine)
    def deny(conn, cursor, sql, params, *args):
        if sql.lstrip().startswith('CREATE TABLE'):
            raise OperationalError(sql, params, RuntimeError('simulated CREATE permission denied'))
    event.listen(engine, 'before_cursor_execute', deny)
    try:
        with pytest.raises(OperationalError, match='simulated CREATE permission denied'):
            if kind == 'result':
                module._install_result_journal(factory, (), 'market-sentiment')
            else:
                module._prepare_earnings_version(factory)
    finally:
        event.remove(engine, 'before_cursor_execute', deny)
        engine.dispose()
    assert not getattr(factory, '_result_journal_module', None)
    assert not getattr(factory, '_earnings_version_ready', False)


@pytest.mark.parametrize('kind', ['result', 'earnings'])
def test_existing_incompatible_schema_is_not_treated_as_success(tmp_path, kind):
    engine = create_engine('sqlite:///' + str(tmp_path / 'bad-schema.sqlite'))
    module = load('result_journal' if kind == 'result' else 'earnings_version')
    if kind == 'result':
        name, key = 'result_sync_head', 'module VARCHAR(48) PRIMARY KEY'
    else:
        name, key = 'earnings_sync_head', 'report_period DATE PRIMARY KEY'
        runs = Table('earnings_gap_run', MetaData(), Column('report_period', Date, primary_key=True))
        runs.create(engine)
        with engine.begin() as source:
            source.execute(runs.insert().values(report_period=date(2026, 9, 30)))
    with engine.begin() as source:
        source.exec_driver_sql('CREATE TABLE ' + name + ' (' + key + ')')
    factory = sessionmaker(bind=engine)
    try:
        with pytest.raises(OperationalError):
            if kind == 'result':
                module._install_result_journal(factory, (), 'market-sentiment')
            else:
                module._prepare_earnings_version(factory)
    finally:
        engine.dispose()
