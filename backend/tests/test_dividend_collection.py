from contextlib import nullcontext
from copy import deepcopy
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from app.models.dividend import DividendSnapshot, DividendStock
from app.services.dividend_collection import (
    CHINA, DividendCollection, DividendCollectionError, validate_result, write_result,
)
from app.services.dividend_public_source import rank_records


def result(day=date(2026, 11, 16)):
    stocks = [dict(symbol='600036.SH', name='Bank'), dict(symbol='600900.SH', name='Other')]
    vals = {r['symbol']: dict(pe_ttm=7, total_mv=1000000, close=10, pb=1) for r in stocks}
    cash = {r['symbol']: {'fiscal_year': 2025, 'annual_dps': {2025: .5, 2024: .4, 2023: .3}} for r in stocks}
    industries = {'600036.SH': dict(l1_code='801780.SI', l1_name='Bank'),
                  '600900.SH': dict(l1_code='801160.SI', l1_name='Utility')}
    return rank_records(stocks, vals, cash, industries, day, 5500)


@pytest.fixture
def engine():
    engine = create_engine('sqlite://', poolclass=StaticPool)
    yield engine
    engine.dispose()


def read(engine):
    with engine.connect() as conn:
        return list(conn.execute(select(DividendSnapshot.__table__))), list(conn.execute(select(DividendStock.__table__)))


def test_two_groups_publish_together(engine):
    meta, rows = result()
    assert write_result(engine, meta, rows) == 2
    head, stocks = read(engine)
    assert len(head) == 1
    assert {r.method for r in stocks} == {'value', 'bank'}


def test_cancel_after_delete_rolls_back_both_groups(engine):
    meta, rows = result()
    write_result(engine, meta, rows)
    before = read(engine)
    calls = []
    def cancel():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError('cancelled')
    changed = deepcopy(rows)
    changed[0]['name'] = 'Replacement'
    with pytest.raises(RuntimeError):
        write_result(engine, meta, changed, cancel)
    assert read(engine) == before


def test_older_date_cannot_replace(engine):
    meta, rows = result()
    write_result(engine, meta, rows)
    before = read(engine)
    meta['trade_date'] = date(2026, 11, 13)
    for row in rows: row['trade_date'] = meta['trade_date']
    with pytest.raises(DividendCollectionError, match='newer'):
        write_result(engine, meta, rows)
    assert read(engine) == before


def test_incomplete_result_cannot_replace(engine):
    meta, rows = result()
    write_result(engine, meta, rows)
    before = read(engine)
    with pytest.raises(DividendCollectionError):
        write_result(engine, meta, [r for r in rows if r['method'] == 'bank'])
    assert read(engine) == before


def test_complete_empty_result_is_publishable(engine):
    meta, rows = result()
    write_result(engine, meta, rows)
    meta.update(value_count=0, value_candidates=0)
    write_result(engine, meta, [])
    assert read(engine)[1] == []


def test_unverified_source_is_blocked_without_request(tmp_path, monkeypatch):
    monkeypatch.setattr('app.services.dividend_collection.PUBLIC_SOURCE_READY', False)
    service = DividendCollection(SimpleNamespace(log_path=tmp_path))
    assert not service.status()['available']
    with pytest.raises(DividendCollectionError, match='同日估值'):
        service.refresh(background=False)
    assert not service.running()
    assert service.read_state() == {}


def test_injected_source_shares_one_collection_and_preserves_failure(tmp_path, monkeypatch):
    import app.services.dividend_collection as module
    engine = create_engine('sqlite:///' + str(tmp_path / 'results.sqlite'))
    settings = SimpleNamespace(log_path=tmp_path)
    monkeypatch.setattr(module, 'database_config', lambda settings: {'revision': 1, 'host': 'localhost', 'port': 3306, 'database': 'test'})
    calls = []
    class Source:
        def __init__(self, *args, **kwargs): pass
        def collect(self):
            calls.append(1)
            if len(calls) > 1: raise ValueError('incomplete source')
            return result()
    service = DividendCollection(settings, source_factory=Source,
        engine_factory=lambda *a, **k: engine, client_factory=lambda: nullcontext(None))
    monkeypatch.setattr(service, 'sync_local', lambda meta, rows: None)
    moment = datetime(2026, 11, 16, 21, tzinfo=CHINA)
    assert service.refresh(background=False, moment=moment)
    assert service.status()['stage'] == 'complete'
    before = read(engine)
    assert service.refresh(background=False, moment=moment)
    assert service.status()['stage'] == 'failed'
    assert read(engine) == before
    assert len(calls) == 2


def test_schedule_is_once_per_day_and_cross_process_lock(tmp_path, monkeypatch):
    import app.services.dividend_collection as module
    service = DividendCollection(SimpleNamespace(log_path=tmp_path), source_factory=object)
    lock = service.acquire()
    try:
        assert not service.refresh(background=False, moment=datetime(2026, 11, 16, 21, tzinfo=CHINA))
    finally:
        lock.release()
    service.save_state({'scheduled_slot': '2026-11-16'})
    assert not service.refresh(background=False, moment=datetime(2026, 11, 16, 22, tzinfo=CHINA), slot='2026-11-16')


def test_intraday_requests_rejected(tmp_path):
    service = DividendCollection(SimpleNamespace(log_path=tmp_path), source_factory=object)
    with pytest.raises(DividendCollectionError, match='16:00'):
        service.refresh(moment=datetime(2026, 11, 16, 15, tzinfo=CHINA))


def test_unchanged_result_preserves_timestamp_and_returns_no_changes(engine):
    meta, rows = result()
    write_result(engine, meta, rows)
    before = read(engine)
    assert write_result(engine, meta, rows) == 0
    assert read(engine) == before


def publication_service(tmp_path, monkeypatch):
    import app.services.dividend_collection as module
    from sqlalchemy.orm import Session
    remote = create_engine('sqlite:///' + str(tmp_path / 'remote.sqlite'))
    local = create_engine('sqlite:///' + str(tmp_path / 'local.sqlite'))
    monkeypatch.setattr(module, 'database_config', lambda settings: dict(host='localhost', port=3306, database='test', revision=1))
    calls = []
    class Source:
        def __init__(self, *args, **kwargs): pass
        def collect(self):
            calls.append(1)
            return result()
    class Cache:
        fail = True
        def refresh(self, *a, **kw):
            assert kw['background'] is False
            if self.fail: raise ConnectionError('local unavailable')
            with remote.connect() as conn:
                head = dict(conn.execute(select(DividendSnapshot.__table__)).mappings().one())
                records = [dict(r) for r in conn.execute(select(DividendStock.__table__)).mappings()]
            head.pop('id'); head.pop('updated_at')
            write_result(local, head, records)
            return True
        def session(self, module): return Session(local)
    cache = Cache()
    service = DividendCollection(SimpleNamespace(log_path=tmp_path), cache=cache, source_factory=Source,
        engine_factory=lambda *a, **k: remote, client_factory=lambda: nullcontext(None))
    return service, remote, local, cache, calls


def test_local_failure_is_durable_pending_and_replay_does_not_refetch(tmp_path, monkeypatch):
    service, remote, local, cache, calls = publication_service(tmp_path, monkeypatch)
    moment = datetime(2026, 11, 16, 21, tzinfo=CHINA)
    service.refresh(background=False, moment=moment)
    assert service.status()['stage'] == 'publication_pending'
    assert service.status()['publication_pending'] is True
    assert service.status()['database_committed'] is True
    before = read(remote)
    assert len(before[1]) == 2
    cache.fail = False
    # A new instance simulates process restart; pending payload is durable.
    restarted = DividendCollection(service.settings, cache=cache,
        engine_factory=lambda *a, **k: remote)
    restarted.refresh(background=False, moment=moment)
    assert restarted.status()['stage'] == 'complete'
    assert restarted.pending() is None
    assert read(remote) == before  # Idempotent replay retains remote timestamp.
    assert {r.method for r in read(local)[1]} == {'value', 'bank'}
    assert calls == [1]


def test_database_failure_recovers_without_recollecting(tmp_path, monkeypatch):
    import app.services.dividend_collection as module
    service, remote, local, cache, calls = publication_service(tmp_path, monkeypatch)
    original = module.write_result
    def failure(*args, **kwargs): raise ConnectionError('database unavailable')
    monkeypatch.setattr(module, 'write_result', failure)
    service.refresh(background=False, moment=datetime(2026, 11, 16, 21, tzinfo=CHINA))
    assert service.status()['publication_pending']
    assert not service.status()['database_committed']
    monkeypatch.setattr(module, 'write_result', original)
    cache.fail = False
    service.refresh(background=False, moment=datetime(2026, 11, 16, 22, tzinfo=CHINA))
    assert service.status()['stage'] == 'complete'
    assert calls == [1]
    assert len(read(local)[1]) == 2


def test_false_local_refresh_never_reports_complete(tmp_path, monkeypatch):
    service, remote, local, cache, calls = publication_service(tmp_path, monkeypatch)
    cache.refresh = lambda *a, **kw: False
    service.refresh(background=False, moment=datetime(2026, 11, 16, 21, tzinfo=CHINA))
    assert service.status()['stage'] == 'publication_pending'
    assert service.pending() is not None


def test_cleanup_exception_always_releases_collection_and_shared_locks(tmp_path, monkeypatch):
    from app.services.earnings_schedule import acquire_execution
    service, remote, local, cache, calls = publication_service(tmp_path, monkeypatch)
    def broken_dispose(): raise RuntimeError('dispose failed')
    monkeypatch.setattr(remote, 'dispose', broken_dispose)
    with pytest.raises(RuntimeError, match='dispose failed'):
        service.refresh(background=False, moment=datetime(2026, 11, 16, 21, tzinfo=CHINA))
    lock = acquire_execution(service.settings, service.acquire)
    assert lock is not None
    lock.release()


def test_pending_target_change_does_not_write_another_database(tmp_path, monkeypatch):
    import app.services.dividend_collection as module
    service, remote, local, cache, calls = publication_service(tmp_path, monkeypatch)
    service.refresh(background=False, moment=datetime(2026, 11, 16, 21, tzinfo=CHINA))
    before = read(remote)
    monkeypatch.setattr(module, 'database_config', lambda s: dict(host='other', port=3306, database='test', revision=2))
    cache.fail = False
    service.refresh(background=False, moment=datetime(2026, 11, 16, 22, tzinfo=CHINA))
    assert service.status()['publication_pending']
    assert read(remote) == before
    assert calls == [1]


def test_previous_pending_recovery_does_not_consume_today_queue(tmp_path, monkeypatch):
    from app.services.earnings_schedule import EarningsScheduler
    service, remote, local, cache, calls = publication_service(tmp_path, monkeypatch)
    yesterday = datetime(2026, 11, 16, 23, tzinfo=CHINA)
    today = datetime(2026, 11, 17, 23, tzinfo=CHINA)
    service.refresh(background=False, moment=yesterday)
    assert service.pending() is not None
    cache.fail = False
    class Source:
        def __init__(self, *a, **kw): self.day = kw['as_of']
        def collect(self):
            calls.append(self.day)
            return result(self.day)
    service.source_factory = Source
    monkeypatch.setattr('app.services.earnings_schedule.task_config', lambda *a: {'enabled': True, 'times': ['23:00']})
    class Cash:
        def __init__(self): self.calls = []
        def read_state(self): return {}
        def running(self): return False
        def refresh(self, **kw): self.calls.append(kw); return True
    cash = Cash()
    queue = EarningsScheduler(service.settings, service, cash)
    assert queue.tick(today) == 'dividend_selection'
    service.collection_thread.join(timeout=5)
    assert service.status()['result_date'] == '2026-11-16'
    assert calls == [1]  # Recovery did not refetch yesterday.
    assert queue.tick(today) is None  # Requeue, do not finish today's item.
    assert queue.jobs()[0]['status'] == 'pending'
    assert not cash.calls
    assert queue.tick(today) == 'dividend_selection'
    service.collection_thread.join(timeout=5)
    assert service.status()['result_date'] == '2026-11-17'
    assert calls == [1, date(2026, 11, 17)]
    assert queue.tick(today) == 'cash_cow'
    assert queue.jobs()[0]['status'] == 'complete'
    assert len(cash.calls) == 1
    # The successful current-date slot is still deduplicated.
    assert not service.refresh(background=False, moment=today, slot='2026-11-17')


def test_verified_source_enabled_by_default_without_starting_network(tmp_path):
    service = DividendCollection(SimpleNamespace(log_path=tmp_path))
    assert service.status()['available'] is True
    assert service.status()['unavailable_reason'] is None
    assert service.read_state() == {}
