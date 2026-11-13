"""Real SQL selection and atomic receipts for incremental valuation history."""
from datetime import date, datetime, timedelta
import json

import pytest
from sqlalchemy import event

from app.core.cache import SnapshotCache, CHINA
from app.core.config import Settings
from app.core import history_sync
from app.services import valuation_ruler_import as imp
from history_sync_helpers import seed_history_sources

DAY = date(2020, 1, 2)


@pytest.fixture
def context(engine, tmp_path, monkeypatch):
    seed_history_sources(engine, DAY)
    monkeypatch.setattr('app.core.cache.now', lambda: datetime(2026, 11, 13, 22, tzinfo=CHINA))
    cache = SnapshotCache(Settings(_env_file=None, log_dir=tmp_path, cache_enabled=True,
                                  cache_auto_refresh=False, cache_sync_attempts=1), engine)
    service = history_sync.service_for(cache, 'valuation-ruler')
    reads = []
    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith('SELECT') and 'FROM valuation_ruler_member' in statement:
            reads.append((statement, parameters))
    event.listen(engine, 'before_cursor_execute', observe)
    yield cache, service, reads
    event.remove(engine, 'before_cursor_execute', observe)


def sync(cache, full=False):
    cache.refresh('valuation-ruler', background=False, full=full)
    return cache.read_state()['modules']['valuation-ruler']


def test_unchanged_history_reads_no_members_new_and_older_missing_days_are_selected(context, engine):
    cache, service, reads = context
    assert not sync(cache)['error']
    assert len(reads) == 1 and 'WHERE' not in reads[0][0]
    reads.clear()
    assert not sync(cache)['error']
    assert reads == []
    assert service.read_state()['joinquant_skipped_days'] == 1
    for day in (date(2019, 12, 31), date(2020, 1, 3)):
        seed_history_sources(engine, day)
    assert not sync(cache)['error']
    assert len(reads) == 1 and 'WHERE valuation_ruler_member.trade_date IN' in reads[0][0]
    assert str(DAY) not in reads[0][1]
    assert str(date(2019, 12, 31)) in reads[0][1]
    with service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM points').fetchone()[0] == 3
        assert db.execute('SELECT COUNT(*) FROM members').fetchone()[0] == 900


def test_changed_summary_rechecks_and_retains_conflict_without_repeat_audit(context, engine):
    cache, service, reads = context
    sync(cache)
    with engine.begin() as db:
        db.execute(imp.DAILY.update().values(index_close=4001))
    reads.clear()
    assert not sync(cache)['error']
    assert len(reads) == 1
    with service.db() as db:
        assert json.loads(db.execute('SELECT payload FROM points').fetchone()[0])['index_close'] == '4000.00000000'
        assert db.execute('SELECT COUNT(*) FROM comparisons').fetchone()[0] == 1
    reads.clear()
    assert not sync(cache)['error'] and not reads
    assert service.read_state()['joinquant_conflicts'] == 1
    with service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM comparisons').fetchone()[0] == 1


def test_failed_delta_does_not_advance_receipt_and_retries_missing_day(context, engine):
    cache, service, reads = context
    sync(cache)
    next_day = date(2020, 1, 3)
    seed_history_sources(engine, next_day)
    with engine.begin() as db:
        db.execute(imp.DAILY.update().where(imp.DAILY.c.trade_date == next_day).values(positive_count=299))
    assert sync(cache)['error']
    with service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM valuation_sync_receipt_v1').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM points').fetchone()[0] == 1
    with engine.begin() as db:
        db.execute(imp.DAILY.update().where(imp.DAILY.c.trade_date == next_day).values(positive_count=300))
    reads.clear()
    assert not sync(cache)['error'] and len(reads) == 1
    assert str(next_day) in reads[0][1] and str(DAY) not in reads[0][1]


@pytest.mark.parametrize('kind', ['missing_local', 'changed_local', 'source_identity', 'missing_receipt'])
def test_invalidated_receipt_forces_revalidation(context, kind):
    cache, service, reads = context
    sync(cache)
    with service.db() as db:
        if kind == 'missing_local':
            db.execute('DELETE FROM points')
            db.execute('DELETE FROM members')
            db.execute('DELETE FROM proofs')
        elif kind == 'changed_local':
            payload = json.loads(db.execute('SELECT payload FROM points').fetchone()[0])
            payload['index_close'] = '3999.00000000'
            db.execute('UPDATE points SET payload=?', (json.dumps(payload),))
        elif kind == 'source_identity':
            db.execute("UPDATE history_cache_source_v1 SET identity='other'")
        else:
            db.execute('DROP TABLE valuation_sync_receipt_v1')
    reads.clear()
    assert not sync(cache)['error']
    assert len(reads) == 1


def test_timestamp_change_rechecks_and_full_mode_detects_unannounced_member_edits(context, engine):
    cache, service, reads = context
    sync(cache)
    with engine.begin() as db:
        db.execute(imp.DAILY.update().values(updated_at=datetime(2026, 11, 13)))
    reads.clear()
    assert not sync(cache)['error'] and len(reads) == 1
    with engine.begin() as db:
        db.execute(imp.MEMBER.update().values(source='invalid'))
    # Manual member-only changes without updating the daily summary require full reconciliation.
    reads.clear()
    assert not sync(cache)['error'] and not reads
    assert sync(cache, full=True)['error'] and len(reads) == 1


def test_cancel_before_publication_keeps_receipts_and_retry_reads_delta(context, engine, monkeypatch):
    cache, service, reads = context
    sync(cache)
    seed_history_sources(engine, date(2020, 1, 3))
    real = history_sync.shutil.copyfile
    def cancel(source, target):
        result = real(source, target)
        assert cache.cancel(cache.read_state()['run_id'], 'valuation-ruler')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(history_sync.shutil, 'copyfile', cancel)
        assert sync(cache)['progress']['status'] == 'cancelled'
    assert len(imp.read_receipts(service)) == 1
    reads.clear()
    assert not sync(cache)['error'] and len(reads) == 1
    assert len(imp.read_receipts(service)) == 2


def test_many_pending_days_use_bounded_date_batches(context, engine):
    cache, _, reads = context
    sync(cache)
    day, added = DAY, 0
    while added < 51:
        day += timedelta(days=1)
        if day.weekday() < 5:
            seed_history_sources(engine, day)
            added += 1
    reads.clear()
    assert not sync(cache)['error']
    assert len(reads) == 2
    assert len(reads[0][1]) == 52  # 50 dates + LIMIT/OFFSET on SQLite.
    assert len(reads[1][1]) == 3
