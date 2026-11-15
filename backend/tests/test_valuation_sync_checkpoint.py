"""Durable day checkpoints do not publish partial history or hide source changes."""
from datetime import date
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from pymysql.err import OperationalError as MysqlError
from sqlalchemy.exc import OperationalError

from app.core.cache import SnapshotCache
from app.core import history_sync
from app.services import valuation_ruler_import as imp
from history_sync_helpers import seed_history_sources
from test_valuation_incremental_sync import context, sync, DAY

NEXT = date(2020, 1, 3)


def interrupt_after_one_day(monkeypatch):
    original = imp._member_rows
    def interrupted(connection, check, days=None):
        for row in original(connection, check, days):
            yield row
            if row['trade_date'] == NEXT:
                # The boundary has already validated/committed DAY, but NEXT is incomplete.
                raise OperationalError('SELECT private_sql', {}, MysqlError(2013, 'private_detail'))
    monkeypatch.setattr(imp, '_member_rows', interrupted)


def pending(service):
    return service.root / imp.CHECKPOINT_FILE


def member_reads(reads):
    return [(sql, params) for sql, params in reads if 'valuation_ruler_member.pe_ttm' in sql]


def make_partial(context, engine, monkeypatch):
    cache, service, reads = context
    seed_history_sources(engine, NEXT)
    with monkeypatch.context() as patch:
        interrupt_after_one_day(patch)
        assert 'MYSQL_2013' in sync(cache, full=True)['error']
    with service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM points').fetchone()[0] == 0
    with closing(sqlite3.connect(pending(service))) as db:
        assert db.execute('SELECT day FROM validated').fetchall() == [(str(DAY),)]
    reads.clear()
    return cache, service, reads


def test_restart_resumes_completed_days_and_success_cleans_checkpoint(context, engine, monkeypatch):
    cache, service, reads = make_partial(context, engine, monkeypatch)
    restarted = SnapshotCache(cache.settings, engine)
    assert not sync(restarted, full=True)['error']
    queries = member_reads(reads)
    assert len(queries) == 1 and str(NEXT) in queries[0][1] and str(DAY) not in queries[0][1]
    with service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM points').fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM members').fetchone()[0] == 600
    assert not pending(service).exists()
    reads.clear()
    assert not sync(restarted)['error'] and not reads


def test_automatic_retry_reuses_checkpoint(context, engine, monkeypatch, caplog):
    cache, service, reads = context
    seed_history_sources(engine, NEXT)
    monkeypatch.setattr(cache.settings, 'cache_sync_attempts', 2)
    monkeypatch.setattr(cache.stop_event, 'wait', lambda _: False)
    original, failed = imp._member_rows, []
    def once(connection, check, days=None):
        for row in original(connection, check, days):
            yield row
            if row['trade_date'] == NEXT and not failed:
                failed.append(True)
                raise OperationalError('SELECT private_sql', {}, MysqlError(2013, 'private_detail'))
    monkeypatch.setattr(imp, '_member_rows', once)
    with caplog.at_level('INFO'):
        assert not sync(cache, full=True)['error']
    queries = member_reads(reads)
    assert len(queries) == 2 and str(DAY) not in queries[1][1] and str(NEXT) in queries[1][1]
    assert 'resumed_days=1 needed_days=1' in caplog.text
    assert 'private_sql' not in caplog.text and 'private_detail' not in caplog.text
    assert not pending(service).exists()


@pytest.mark.parametrize('change', ['summary', 'source'])
def test_changed_source_or_summary_invalidates_saved_day(context, engine, monkeypatch, change):
    cache, service, reads = make_partial(context, engine, monkeypatch)
    if change == 'summary':
        with engine.begin() as db:
            db.execute(imp.DAILY.update().where(imp.DAILY.c.trade_date == DAY).values(index_close=4001))
    else:
        monkeypatch.setattr(history_sync, 'source_identity', lambda _: 'different-source')
    assert not sync(cache, full=True)['error']
    assert len(member_reads(reads)) == 1 and 'WHERE' not in member_reads(reads)[0][0]
    with service.db() as db:
        point = json.loads(db.execute('SELECT payload FROM points WHERE trade_date=?', (str(DAY),)).fetchone()[0])
        assert point['index_close'] == ('4001.00000000' if change == 'summary' else '4000.00000000')


def test_resume_still_rejects_orphan_source_dates(context, engine, monkeypatch):
    cache, service, _ = make_partial(context, engine, monkeypatch)
    orphan = date(2020, 1, 6)
    seed_history_sources(engine, orphan)
    with engine.begin() as db:
        db.execute(imp.DAILY.delete().where(imp.DAILY.c.trade_date == orphan))
    assert sync(cache, full=True)['error']
    with service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM points').fetchone()[0] == 0


def test_resume_rejects_disappeared_members_of_saved_day(context, engine, monkeypatch):
    cache, service, _ = make_partial(context, engine, monkeypatch)
    with engine.begin() as db:
        db.execute(imp.MEMBER.delete().where(imp.MEMBER.c.trade_date == DAY))
    assert sync(cache, full=True)['error']
    with service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM points').fetchone()[0] == 0


def test_corrupted_saved_members_are_revalidated_before_publication(context, engine, monkeypatch):
    cache, service, _ = make_partial(context, engine, monkeypatch)
    with closing(sqlite3.connect(pending(service))) as db:
        db.execute("UPDATE validated SET members='{}'")
        db.commit()
    assert sync(cache, full=True)['error']
    with service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM points').fetchone()[0] == 0


def test_failed_checkpoint_cleanup_cannot_skip_next_explicit_full_sync(context, engine, monkeypatch):
    cache, service, reads = context
    unlink = Path.unlink
    def fail_cleanup(path, *args, **kwargs):
        if path.name == imp.CHECKPOINT_FILE:
            raise PermissionError('held checkpoint')
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail_cleanup)
        assert not sync(cache, full=True)['error']
    assert pending(service).exists()
    with engine.begin() as db:
        db.execute(imp.MEMBER.update().values(source='invalid'))
    reads.clear()
    assert sync(cache, full=True)['error'] and member_reads(reads)
    with service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM points').fetchone()[0] == 1


def test_valuation_full_sync_can_run_past_old_thirty_minute_budget(context, monkeypatch):
    cache, _, _ = context
    elapsed = [0]
    timer = SimpleNamespace(monotonic=lambda: elapsed[0])
    monkeypatch.setattr(history_sync, 'time', timer)
    monkeypatch.setattr(imp, 'time', timer)
    schema = imp.check_schema
    def slow_schema(connection):
        schema(connection)
        elapsed[0] = 1900
    monkeypatch.setattr(imp, 'check_schema', slow_schema)
    assert not sync(cache, full=True)['error']
