"""Manual cancellation is durable, run-specific, and safe across API workers."""
from contextlib import nullcontext
import multiprocessing
import threading
from types import SimpleNamespace

import portalocker
import pytest

from app.services.etf_configuration import ConfigurationFailure
from test_etf_collection import Calendar, DAY, EVENING, worker
from test_etf_page_configuration import save


def run_in_thread(w):
    result = []
    thread = threading.Thread(target=lambda: result.append(w.tick(EVENING)))
    thread.start()
    return thread, result


def test_cancel_queued_is_idempotent_and_allows_a_new_manual_run(tmp_path):
    w, collector, _ = worker(tmp_path)
    save(w, {'enabled': False})
    w.request_run('manual-cancel-queued-1')
    cancelled = w.request_cancel('manual-cancel-queued-1')
    assert cancelled['status'] == 'cancelled' and cancelled['phase'] == 'cancelled'
    assert w.request_cancel(cancelled['id']) == cancelled
    assert w.request_run(cancelled['id']) == cancelled  # Network retries are still idempotent.
    assert not w.tick(EVENING) and not collector.calls
    w.request_run('manual-cancel-queued-2')
    assert w.tick(EVENING)
    assert w.status()['manual']['status'] == 'complete'
    assert w.request_cancel('manual-cancel-queued-1')['status'] == 'cancelled'
    assert w.request_cancel('manual-cancel-queued-2')['status'] == 'complete'
    assert w.status()['manual']['id'] == 'manual-cancel-queued-2'
    with pytest.raises(ConfigurationFailure, match='未找到'):
        w.request_cancel('unknown-request-0000')


def test_cancel_running_drops_uncommitted_batch_without_charging_attempts(tmp_path):
    w, collector, _ = worker(tmp_path)
    other, _, _ = worker(tmp_path)
    save(w, {'enabled': False})
    started, release = threading.Event(), threading.Event()
    original = collector.collect
    def blocked(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    collector.collect = blocked
    w.request_run('manual-cancel-fetch-1')
    thread, result = run_in_thread(w)
    try:
        assert started.wait(3)
        assert other.request_cancel('manual-cancel-fetch-1')['status'] == 'cancelling'
        assert other.request_cancel('manual-cancel-fetch-1')['status'] == 'cancelling'
        assert other.status()['manual']['status'] == 'cancelling'
        with pytest.raises(ConfigurationFailure, match='正在运行'):
            other.request_run('manual-cancel-fetch-2')
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and result == [False]
    assert w.status()['manual']['status'] == 'cancelled'
    assert w.status()['progress'] is None and not collector.writes
    with w.connect() as db:
        assert db.execute('SELECT SUM(attempts) FROM observations').fetchone()[0] == 0
    collector.collect = original
    w.request_run('manual-cancel-fetch-2')
    assert w.tick(EVENING) and collector.writes


def test_cancellation_waits_for_inflight_http_then_stops_before_next_request(tmp_path):
    w, collector, _ = worker(tmp_path)
    other, _, _ = worker(tmp_path)
    save(w, {'enabled': False})
    started, release, calls = threading.Event(), threading.Event(), []
    def get(url):
        calls.append(url)
        started.set()
        assert release.wait(5)
        return object()
    w.client_factory = lambda: nullcontext(SimpleNamespace(get=get))
    def client(http):
        def calendar():
            http.get('first')
            http.get('second')
            return Calendar()
        return SimpleNamespace(calendar=calendar)
    collector.ExchangeClient = client
    w.request_run('manual-cancel-http-01')
    thread, result = run_in_thread(w)
    try:
        assert started.wait(3)
        assert other.request_cancel('manual-cancel-http-01')['status'] == 'cancelling'
        assert thread.is_alive()
    finally:
        release.set()
        thread.join(5)
    assert result == [False] and calls == ['first']
    assert w.status()['manual']['status'] == 'cancelled' and not collector.writes


def test_cancel_during_write_preserves_commit_and_ledger_without_reporting_success(tmp_path):
    w, collector, _ = worker(tmp_path)
    other, _, _ = worker(tmp_path)
    save(w, {'enabled': False})
    started, release = threading.Event(), threading.Event()
    original = collector.write_records
    def blocked(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    collector.write_records = blocked
    w.request_run('manual-cancel-write-1')
    thread, result = run_in_thread(w)
    try:
        assert started.wait(3)
        assert other.request_cancel('manual-cancel-write-1')['status'] == 'cancelling'
        assert not collector.writes  # No forced close/rollback of the writer.
    finally:
        release.set()
        thread.join(5)
    assert result == [False] and len(collector.writes) == 1
    state = w.status()
    assert state['manual']['status'] == 'cancelled' and state['manual']['error'] is None
    assert state['cache_pending'] and not state['progress']
    with w.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM observations WHERE day=? AND status='complete'",
                          (DAY.isoformat(),)).fetchone()[0] == 4


def test_late_cancel_wins_atomic_terminal_update(tmp_path):
    w, _, _ = worker(tmp_path)
    other, _, _ = worker(tmp_path)
    save(w, {'enabled': False})
    w.request_run('manual-cancel-late-01')
    with w.connect() as db:
        db.execute("UPDATE manual_runs SET status='running' WHERE id='manual-cancel-late-01'")
    w._collect = lambda *args, **kwargs: True
    with portalocker.Lock(str(w.root / 'collection.lock'), timeout=0), w.connect() as db:
        class LateCancelConnection:
            def __getattr__(self, name):
                return getattr(db, name)
            def execute(self, statement, parameters=()):
                if "status=CASE WHEN status='cancelling'" in statement:
                    assert other.request_cancel('manual-cancel-late-01')['status'] == 'cancelling'
                return db.execute(statement, parameters)
        assert not w._run(LateCancelConnection(), EVENING, w.configuration.load(), manual_id='manual-cancel-late-01')
    assert w.status()['manual']['status'] == 'cancelled'


def test_orphaned_cancellation_recovers_without_network_and_does_not_restart(tmp_path):
    w, collector, _ = worker(tmp_path)
    save(w, {'enabled': False})
    w.request_run('manual-cancel-orphan1')
    with w.connect() as db:
        db.execute("UPDATE manual_runs SET status='running' WHERE id='manual-cancel-orphan1'")
        db.execute("INSERT INTO metadata VALUES('progress','{}')")
    # No collection.lock owner means the recorded running task was interrupted.
    assert w.request_cancel('manual-cancel-orphan1')['status'] == 'cancelled'
    assert w.status()['progress'] is None and not collector.calls
    with w.connect() as db:
        db.execute("UPDATE manual_runs SET status='cancelling' WHERE id='manual-cancel-orphan1'")
    restarted, second_collector, _ = worker(tmp_path)
    restarted.start()
    try:
        assert restarted.status()['manual']['status'] == 'cancelled'
        assert not second_collector.calls
    finally:
        restarted.stop()


def collect_in_other_process(folder, started, release, results):
    w, collector, _ = worker(folder)
    original = collector.collect
    def blocked(*args, **kwargs):
        started.set()
        if not release.wait(10):
            raise RuntimeError('Test release timed out')
        return original(*args, **kwargs)
    collector.collect = blocked
    results.put((w.tick(EVENING), bool(collector.writes)))


def test_cancellation_crosses_processes_and_retains_exclusive_run_lock(tmp_path):
    w, _, _ = worker(tmp_path)
    save(w, {'enabled': False})
    w.request_run('manual-cancel-process')
    context = multiprocessing.get_context('spawn')
    started, release, results = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=collect_in_other_process, args=(tmp_path, started, release, results))
    process.start()
    try:
        assert started.wait(8)
        assert w.request_cancel('manual-cancel-process')['status'] == 'cancelling'
        with pytest.raises(ConfigurationFailure, match='正在运行'):
            w.request_run('manual-new-process-01')
        release.set()
        process.join(10)
        assert process.exitcode == 0 and results.get(timeout=2) == (False, False)
        assert w.status()['manual']['status'] == 'cancelled'
        assert w.request_run('manual-new-process-01')['status'] == 'queued'
    finally:
        release.set()
        if process.is_alive():
            process.terminate()
            process.join(5)
        results.close()
