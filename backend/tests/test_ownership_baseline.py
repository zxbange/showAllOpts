from datetime import date
import os
import time

import pytest
from sqlalchemy import create_engine, select

from app.core.cache import SnapshotCache
from app.core.ownership_baseline import CHECKPOINT, PARTIAL_NAME
from app.core.ownership_sync import read_cursor, HEAD
from app.models.ownership import OwnershipStock, OwnershipReport, OwnershipHolders
from test_ownership_sync import journal, MODULE, STAMP, entry, cursor
from test_ownership_collector import mod


def checkpoint(cache):
    engine = create_engine('sqlite:///' + (cache.root / PARTIAL_NAME).as_posix())
    try:
        with engine.connect() as connection:
            return dict(connection.execute(select(CHECKPOINT)).mappings().one())
    finally:
        engine.dispose()


def interrupt_after_first_page(journal, monkeypatch):
    from app.core import ownership_baseline as baseline
    ns, factory, cache = journal
    with factory() as session:
        for code in ('600002.XSHG', '600003.XSHG', '600004.XSHG'):
            session.add(ns.OwnershipStock(code=code, name='测试', updated_at=STAMP))
        session.commit()
    monkeypatch.setattr('app.core.cache_sync.READ_PAGE_SIZE', 2)
    original = baseline.read_batches
    def interrupted(*args, **kwargs):
        for batch in original(*args, **kwargs):
            yield batch
            raise RuntimeError('simulated disconnect after committed page')
    monkeypatch.setattr(baseline, 'read_batches', interrupted)
    cache.refresh(MODULE, background=False)
    assert entry(cache)['error']
    assert not cache.status()['modules'][-1]['ready']
    assert checkpoint(cache)['rows'] == 2
    assert checkpoint(cache)['last_code'] == '600002.XSHG'
    monkeypatch.setattr(baseline, 'read_batches', original)
    return original


def test_restart_resumes_and_repairs_changes_behind_seek_key(journal, engine, monkeypatch):
    from app.core import ownership_baseline as baseline
    ns, factory, cache = journal
    original = interrupt_after_first_page(journal, monkeypatch)
    with factory() as session:
        session.query(ns.OwnershipStock).filter_by(code='600001.XSHG').one().name = '已更正'
        session.delete(session.query(ns.OwnershipStock).filter_by(code='600002.XSHG').one())
        session.add(ns.OwnershipStock(code='000001.XSHE', name='晚到数据', updated_at=STAMP))
        session.commit()
    conditions = []
    def capture(source, columns, keys, condition=None, **kwargs):
        conditions.append(condition)
        yield from original(source, columns, keys, condition, **kwargs)
    monkeypatch.setattr(baseline, 'read_batches', capture)
    resumed = SnapshotCache(cache.settings, engine)
    resumed.refresh(MODULE, background=False)
    assert not entry(resumed)['error']
    assert conditions[0] is not None  # Does not restart the first table from its beginning.
    with resumed.session(MODULE) as local:
        actual = {r.code:r.name for r in local.query(OwnershipStock)}
        assert actual == {'000001.XSHE':'晚到数据', '600001.XSHG':'已更正', '600003.XSHG':'测试', '600004.XSHG':'测试'}
        assert local.query(OwnershipReport).count() == 1
        assert local.query(OwnershipHolders).count() == 1
    assert entry(resumed)['rows'] == 6
    assert cursor(resumed)['revision'] == 3
    assert not (resumed.root / PARTIAL_NAME).exists()


def test_page_and_checkpoint_rollback_together(journal, monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    _, _, cache = journal
    def fail(conn, cur, sql, *args):
        if 'UPDATE ownership_baseline_checkpoint' in sql and 'last_code=' in sql:
            raise RuntimeError('checkpoint update interrupted')
    event.listen(Engine, 'before_cursor_execute', fail)
    try:
        cache.refresh(MODULE, background=False)
    finally:
        event.remove(Engine, 'before_cursor_execute', fail)
    assert entry(cache)['error'] and checkpoint(cache)['rows'] == 0
    cache.refresh(MODULE, background=False)
    assert not entry(cache)['error'] and entry(cache)['rows'] == 3


def test_generation_change_discards_obsolete_partial(journal, engine, monkeypatch):
    ns, factory, cache = journal
    interrupt_after_first_page(journal, monkeypatch)
    with engine.begin() as connection:
        connection.execute(HEAD.update().values(generation='replacement', revision=0))
        connection.execute(OwnershipStock.__table__.update().values(name='替换来源'))
    cache.refresh(MODULE, background=False)
    assert not entry(cache)['error'] and cursor(cache)['generation'] == 'replacement'
    with cache.session(MODULE) as local:
        assert all(r.name == '替换来源' for r in local.query(OwnershipStock))


def test_failed_publication_copy_reuses_completed_baseline(journal, monkeypatch):
    from app.core import ownership_baseline as baseline
    _, _, cache = journal
    original = baseline.shutil.copyfile
    monkeypatch.setattr(baseline.shutil, 'copyfile', lambda *args: (_ for _ in ()).throw(OSError('disk interrupted')))
    cache.refresh(MODULE, background=False)
    assert entry(cache)['error'] and checkpoint(cache)['table_index'] == 3
    monkeypatch.setattr(baseline.shutil, 'copyfile', original)
    monkeypatch.setattr(baseline, 'read_batches', lambda *args, **kwargs: pytest.fail('download repeated'))
    cache.refresh(MODULE, background=False)
    assert not entry(cache)['error'] and entry(cache)['rows'] == 3


def test_partial_survives_cache_cleanup(journal, monkeypatch):
    _, _, cache = journal
    interrupt_after_first_page(journal, monkeypatch)
    path = cache.root / PARTIAL_NAME
    os.utime(path, (time.time()-172800, time.time()-172800))
    cache.cleanup(cache.read_state())
    assert path.is_file()


def test_interrupted_forced_baseline_keeps_published_snapshot(journal, monkeypatch):
    from app.core import ownership_baseline as baseline
    _, _, cache = journal
    cache.refresh(MODULE, background=False)
    before = entry(cache)['file']
    original = baseline.read_batches
    def fail(*args, **kwargs):
        for batch in original(*args, **kwargs):
            yield batch
            raise RuntimeError('disconnect')
    monkeypatch.setattr(baseline, 'read_batches', fail)
    cache.refresh(MODULE, background=False, full=True)
    assert entry(cache)['file'] == before and entry(cache)['error']
    with cache.session(MODULE) as local:
        assert local.query(OwnershipStock).count() == 1
    monkeypatch.setattr(baseline, 'read_batches', original)
    cache.refresh(MODULE, background=False, full=True)
    assert entry(cache)['file'] != before and not entry(cache)['error']


def test_network_retry_resumes_committed_page_without_user_restart(journal, monkeypatch):
    from app.core import ownership_baseline as baseline
    from test_cache_network import lost_connection
    _, _, cache = journal
    original = baseline.read_batches
    failed = [False]
    conditions = []
    def fail_once(source, columns, keys, condition=None, **kwargs):
        conditions.append(condition)
        for batch in original(source, columns, keys, condition, **kwargs):
            yield batch
            if not failed[0]:
                failed[0] = True
                raise lost_connection()
    monkeypatch.setattr(baseline, 'read_batches', fail_once)
    monkeypatch.setattr(cache.stop_event, 'wait', lambda delay: False)
    cache.refresh(MODULE, background=False)
    assert failed[0] and conditions[0] is None and conditions[1] is not None
    assert not entry(cache)['error'] and entry(cache)['rows'] == 3


def test_stop_retains_checkpoint_and_pending_work(journal, monkeypatch):
    from app.core import ownership_baseline as baseline
    _, _, cache = journal
    original = baseline.read_batches
    def stopped(*args, **kwargs):
        for batch in original(*args, **kwargs):
            yield batch
            cache.stop_event.set()
    monkeypatch.setattr(baseline, 'read_batches', stopped)
    cache.refresh(MODULE, background=False)
    assert cache.read_state()['pending'] == [MODULE]
    assert checkpoint(cache)['rows'] > 0
    assert not cache.status()['modules'][-1]['ready']

def test_failed_catchup_keeps_baseline_for_retry(journal, monkeypatch):
    from app.core import ownership_baseline as baseline
    ns, factory, cache = journal
    interrupt_after_first_page(journal, monkeypatch)
    with factory() as session:
        session.query(ns.OwnershipStock).filter_by(code='600001.XSHG').one().name = '补差更正'
        session.commit()
    original = baseline.reconcile_changes
    def fail(source, target, saved, head):
        original(source, target, saved, head)
        raise RuntimeError('catchup interrupted before commit')
    monkeypatch.setattr(baseline, 'reconcile_changes', fail)
    cache.refresh(MODULE, background=False)
    assert entry(cache)['error'] and checkpoint(cache)['table_index'] == 3
    assert checkpoint(cache)['revision'] == 2
    monkeypatch.setattr(baseline, 'reconcile_changes', original)
    monkeypatch.setattr(baseline, 'read_batches', lambda *args, **kwargs: pytest.fail('baseline downloaded again'))
    cache.refresh(MODULE, background=False)
    assert not entry(cache)['error'] and cursor(cache)['revision'] == 3
    with cache.session(MODULE) as local:
        assert local.get(OwnershipStock, '600001.XSHG').name == '补差更正'

def test_baseline_reuses_write_connection_and_sizes_compact_pages(journal, monkeypatch):
    from sqlalchemy import event
    from app.core import ownership_baseline as baseline
    _, _, cache = journal
    create = baseline.create_engine
    reads = baseline.read_batches
    opened = []
    sizes = {}
    def engine(*args, **kwargs):
        result = create(*args, **kwargs)
        if not opened and str(args[0]).endswith(PARTIAL_NAME):
            count = []
            event.listen(result, 'connect', lambda *args: count.append(1))
            opened.append(count)
        return result
    def batches(source, columns, keys, *args, **kwargs):
        sizes[keys[0].table.name] = kwargs['page_size']
        yield from reads(source, columns, keys, *args, **kwargs)
    monkeypatch.setattr(baseline, 'create_engine', engine)
    monkeypatch.setattr(baseline, 'read_batches', batches)
    cache.refresh(MODULE, background=False)
    assert not entry(cache)['error']
    assert len(opened[0]) == 1
    assert sizes == {'ownership_stock':2000, 'ownership_report':500, 'ownership_holders':2000}


def test_manual_cancel_preserves_committed_page_and_resumes(journal, engine, monkeypatch):
    from app.core import ownership_baseline as baseline
    from app.core.sync_progress import report
    _, _, cache = journal
    original = baseline.read_batches
    other = SnapshotCache(cache.settings, engine)
    def cancelled(*args, **kwargs):
        for batch in original(*args, **kwargs):
            yield batch
            assert other.cancel(other.status()['run_id'])
            report('next page')
    monkeypatch.setattr(baseline, 'read_batches', cancelled)
    cache.refresh(MODULE, background=False)
    assert checkpoint(cache)['rows'] > 0
    assert entry(cache)['progress']['status'] == 'cancelled'
    assert not entry(cache).get('error')
    assert not cache.status()['modules'][-1]['ready']
    monkeypatch.setattr(baseline, 'read_batches', original)
    other.refresh(MODULE, background=False)
    assert entry(other)['progress']['status'] == 'complete'
    assert entry(other)['rows'] == 3
