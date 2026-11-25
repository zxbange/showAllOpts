"""Exercise the optional pilot through real locks, admissions, routes and worker deadlines."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_etf_collection import EVENING as ETF_EVENING
from test_etf_collection import worker as etf_worker
from test_market_collection import worker as market_worker

from app.core.config import Settings
from app.main import create_app
from app.queue_worker import drain_workers
from app.services.etf_configuration import ConfigurationFailure
from app.services.queue_runtime import (
    GROUPS,
    PROTOCOL_VERSION,
    QueueExecutorUnavailable,
    QueueRoleConflict,
    QueueRuntime,
    TickGate,
    process_generation,
    release_identity,
)


def test_shared_combined_role_excludes_dedicated_executor_across_processes(tmp_path):
    runtime = QueueRuntime(tmp_path)
    first = runtime.acquire_authority(exclusive=False)
    second = runtime.acquire_authority(exclusive=False)
    command = [
        sys.executable,
        '-c',
        'from pathlib import Path; import sys; from app.services.queue_runtime import QueueRuntime, QueueRoleConflict\n'
        'try: QueueRuntime(Path(sys.argv[1])).acquire_authority(exclusive=True)\n'
        'except QueueRoleConflict: raise SystemExit(23)\n',
        str(tmp_path),
    ]
    try:
        result = subprocess.run(
            command, cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=10
        )
        assert result.returncode == 23
    finally:
        second.release()
        first.release()
    owner = runtime.acquire_authority(exclusive=True)
    try:
        with pytest.raises(QueueRoleConflict):
            runtime.acquire_authority(exclusive=False)
        with pytest.raises(QueueRoleConflict):
            runtime.acquire_authority(exclusive=True)
    finally:
        owner.release()


def test_healthy_heartbeat_needs_actual_owner_and_both_threads(tmp_path):
    runtime = QueueRuntime(tmp_path)
    assert runtime.status() == {'state': 'unavailable', 'workers': [], 'ready': False}
    owner = runtime.acquire_authority(exclusive=True)
    try:
        runtime.publish('ready', {'etf'})
        assert not runtime.status()['ready']
        runtime.publish('ready', set(GROUPS))
        assert runtime.status()['ready']
        assert runtime.worker_running('etf')
        old = {
            'protocol': PROTOCOL_VERSION,
            'release': release_identity(),
            'owner_pid': os.getpid(),
            'owner_start': process_generation(os.getpid()),
            'state': 'ready',
            'at': time.time() - 50,
            'groups': list(GROUPS),
        }
        runtime.heartbeat.write_text(json.dumps(old))
        assert runtime.status()['state'] == 'stale'
        runtime.heartbeat.write_text(
            json.dumps(old | {'protocol': PROTOCOL_VERSION + 1, 'at': time.time()})
        )
        assert runtime.status()['state'] == 'incompatible'
        runtime.heartbeat.write_text(json.dumps(old | {'at': time.time(), 'owner_start': '0'}))
        assert not runtime.status()['ready']
        runtime.heartbeat.write_text('malformed')
        assert not runtime.status()['ready']
        runtime.publish('ready', set(GROUPS))
    finally:
        owner.release()
    assert not runtime.status()['ready']  # A fresh old file is insufficient.


@pytest.mark.parametrize('kind', ['etf', 'market'])
def test_split_manual_queue_is_not_executed_locally_and_survives_drain(tmp_path, kind):
    if kind == 'etf':
        producer, _, _ = etf_worker(tmp_path)
        executor, collector, _ = etf_worker(tmp_path)
        moment = ETF_EVENING
        observations = collector.writes
    else:
        producer, _, _, clock = market_worker(tmp_path)
        executor, _, writer, _ = market_worker(tmp_path)
        del producer.start  # Existing fixtures suppress local scheduler launch.
        moment = clock()
        observations = writer.calls
    runtime = QueueRuntime(producer.settings.log_path)
    producer.remote_worker = runtime
    executor.tick_gate = gate = TickGate()
    owner = runtime.acquire_authority(exclusive=True)
    request_id = 'split-manual-request-001'
    try:
        runtime.publish('ready', set(GROUPS))
        accepted = producer.request_run(request_id)
        assert accepted['status'] == 'queued'
        assert producer.worker is None and not observations
        assert runtime.wake_revision() is not None
        # Closing the executor's tick gate does not erase an already accepted ID.
        gate.close()
        runtime.publish('draining', set(GROUPS))
        assert not executor.tick(moment)
        with producer.connect() as db:
            assert (
                db.execute('SELECT status FROM manual_runs WHERE id=?', (request_id,)).fetchone()[0]
                == 'queued'
            )
        assert producer.request_run(request_id)['status'] == 'queued'
        with pytest.raises(ConfigurationFailure, match='已经|已有'):
            producer.request_run('another-during-drain-002')
        # Same durable queue is claimed by the next healthy dedicated executor.
        executor.tick_gate = TickGate()
        runtime.publish('ready', set(GROUPS))
        assert executor.tick(moment)
        assert observations
        assert producer.worker is None
        with producer.connect() as db:
            assert (
                db.execute('SELECT status FROM manual_runs WHERE id=?', (request_id,)).fetchone()[0]
                == 'complete'
            )
            assert (
                db.execute('SELECT count(*) FROM manual_runs WHERE id=?', (request_id,)).fetchone()[
                    0
                ]
                == 1
            )
    finally:
        owner.release()


@pytest.mark.parametrize('kind', ['etf', 'market'])
def test_split_executor_unavailable_rejects_new_request_without_writing_queue(tmp_path, kind):
    if kind == 'etf':
        producer, _, _ = etf_worker(tmp_path)
    else:
        producer, _, _, _ = market_worker(tmp_path)
        del producer.start
    producer.remote_worker = QueueRuntime(producer.settings.log_path)
    with pytest.raises(QueueExecutorUnavailable, match='未接受新任务'):
        producer.request_run('unhealthy-request-001')
    assert producer.worker is None
    with producer.connect() as db:
        assert db.execute('SELECT count(*) FROM manual_runs').fetchone()[0] == 0


def test_gate_drains_admitted_work_before_requesting_interrupt_and_blocks_new_ticks(tmp_path):
    runtime = QueueRuntime(tmp_path)
    owner = runtime.acquire_authority(exclusive=True)
    gate = TickGate()
    entered, finished = threading.Event(), threading.Event()
    stop, wake = threading.Event(), threading.Event()

    def active_tick():
        with gate.admit() as allowed:
            assert allowed
            entered.set()
            finished.wait(1)

    thread = threading.Thread(target=active_tick)
    thread.start()
    assert entered.wait(1)
    try:
        # A simulated tick is allowed to finish without seeing stop while
        # draining; later tick admission is denied even if queue wakes.
        def complete_during_drain():
            deadline = time.monotonic() + 1
            while not gate.closed and time.monotonic() < deadline:
                time.sleep(0.001)
            assert gate.closed and not stop.is_set()
            with gate.admit() as allowed:
                assert not allowed
            finished.set()

        finisher = threading.Thread(target=complete_during_drain)
        finisher.start()
        service = SimpleNamespace(worker=thread, stop_event=stop, wake_event=wake)
        assert drain_workers(runtime, gate, {'etf': service}, drain_seconds=1, stop_seconds=1)
        finisher.join(1)
        assert stop.is_set() and wake.is_set()
        assert runtime.status()['state'] == 'stopped'
    finally:
        finished.set()
        thread.join(1)
        owner.release()


def test_unresponsive_tick_drain_is_bounded_and_authority_not_released_early(tmp_path):
    runtime = QueueRuntime(tmp_path)
    owner = runtime.acquire_authority(exclusive=True)
    gate = TickGate()
    entered, finish = threading.Event(), threading.Event()

    def stuck():
        with gate.admit() as allowed:
            assert allowed
            entered.set()
            finish.wait(2)

    thread = threading.Thread(target=stuck)
    thread.start()
    assert entered.wait(1)
    service = SimpleNamespace(
        worker=thread, stop_event=threading.Event(), wake_event=threading.Event()
    )
    try:
        begin = time.monotonic()
        assert not drain_workers(
            runtime, gate, {'etf': service}, drain_seconds=0.03, stop_seconds=0.03
        )
        assert time.monotonic() - begin < 0.5
        assert service.stop_event.is_set()
        assert runtime.status()['state'] == 'degraded'
        runtime.release_after_workers(owner, [thread])
        with pytest.raises(QueueRoleConflict):
            runtime.acquire_authority(exclusive=False)
    finally:
        finish.set()
        thread.join(1)
    deadline = time.monotonic() + 1
    while True:
        try:
            combined = runtime.acquire_authority(exclusive=False)
            combined.release()
            break
        except QueueRoleConflict:
            assert time.monotonic() < deadline
            time.sleep(0.01)


def test_split_api_readiness_is_local_and_existing_health_remains_liveness(
    tmp_path, engine, monkeypatch
):
    settings = Settings(
        _env_file=None,
        log_dir=tmp_path,
        collection_runtime_role='split-api',
        cache_enabled=False,
        etf_collection_enabled=False,
        market_collection_worker_enabled=False,
        social_worker_enabled=False,
        margin_worker_enabled=False,
        growth_dividend_worker_enabled=False,
        valuation_ruler_worker_enabled=False,
        cash_cow_worker_enabled=False,
    )
    runtime = QueueRuntime(settings.log_path)
    owner = runtime.acquire_authority(exclusive=True)
    # Disable other workers in the integration harness so readiness itself
    # is the only new operation; their default app wiring is unchanged.
    from app.services.etf_collection import ETFCollection
    from app.services.market_collection import MarketCollection

    def forbidden(*_args, **_kwargs):
        pytest.fail('split API attempted local executor startup')

    monkeypatch.setattr(ETFCollection, 'start', forbidden)
    monkeypatch.setattr(MarketCollection, 'start', forbidden)

    try:
        app = create_app(settings, engine=engine)
        # Existing unrelated startup sources are already isolated by conftest.
        with TestClient(app) as client:
            assert client.get('/api/v1/health').json() == {
                'status': 'ok',
                'service': 'showallopts-api',
            }
            assert client.get('/api/v1/readiness').status_code == 503
            runtime.publish('ready', set(GROUPS))
            response = client.get('/api/v1/readiness')
            assert response.status_code == 200 and response.json()['status'] == 'ready'
            assert response.json()['queue_executor'] == 'ready'
            runtime.publish('draining', set(GROUPS))
            assert client.get('/api/v1/readiness').status_code == 503
            assert client.get('/api/v1/health').status_code == 200

            # Exercise the public HTTP boundary with authenticated real
            # routing; the underlying durable-admission tests above prove
            # the exception is raised before any new queue insertion.
            def unavailable(*_):
                runtime.require_ready('etf')

            monkeypatch.setattr(app.state.etf_collection, 'request_run', unavailable)
            monkeypatch.setattr(app.state.market_collection, 'request_run', unavailable)
            for path in ('/api/v1/etf-shares/collection-run', '/api/v1/market-collection/run'):
                response = client.post(path, json={'request_id': 'runtime-pilot-http-test'})
                assert response.status_code == 503
                assert '未接受新任务' in response.json()['detail']
    finally:
        owner.release()


def test_dedicated_supervisor_starts_only_the_two_queues_and_forwards_remote_wake(
    tmp_path, monkeypatch
):
    from app import queue_worker

    runtime = QueueRuntime(tmp_path)
    settings = Settings(_env_file=None, log_dir=tmp_path, collection_runtime_role='queue-worker')
    calls = []
    services = {}

    class FakeCollector:
        def __init__(self, group, gate):
            self.group = group
            self.configuration = object()
            self.tick_gate = gate
            self.worker = None
            self.stop_event = threading.Event()
            self.wake_event = threading.Event()
            self.awakened = threading.Event()
            services[group] = self

        def start(self):
            def loop():
                while not self.stop_event.is_set():
                    if self.wake_event.wait(0.05):
                        self.wake_event.clear()
                        self.awakened.set()

            calls.append(self.group)
            self.worker = threading.Thread(target=loop)
            self.worker.start()

    database = SimpleNamespace(dispose=lambda: calls.append('disposed'))
    cache = SimpleNamespace(
        refresh_etf_collection=lambda: None, refresh_market_collection=lambda: None
    )
    monkeypatch.setattr(queue_worker, 'VersionedDatabase', lambda *_a, **_kw: database)
    monkeypatch.setattr(queue_worker, 'SnapshotCache', lambda *_a, **_kw: cache)
    monkeypatch.setattr(
        queue_worker, 'ETFCollection', lambda *_a, tick_gate, **_kw: FakeCollector('etf', tick_gate)
    )
    monkeypatch.setattr(
        queue_worker,
        'MarketCollection',
        lambda *_a, tick_gate, **_kw: FakeCollector('market', tick_gate),
    )
    monkeypatch.setattr(queue_worker, 'start_runtime', lambda *_: None)
    monkeypatch.setattr(queue_worker, 'stop_runtime', lambda *_: None)
    monkeypatch.setattr(queue_worker, 'stop_notifications', lambda *_: None)
    monkeypatch.setattr(queue_worker, 'HEARTBEAT_SECONDS', 0.02)
    stop = threading.Event()
    result = []
    supervisor = threading.Thread(target=lambda: result.append(queue_worker.serve(settings, stop)))
    supervisor.start()
    try:
        deadline = time.monotonic() + 2
        while not runtime.status()['ready']:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert set(services) == GROUPS
        assert services['market'].shared_configuration is services['etf'].configuration
        assert services['market'].tick_gate is services['etf'].tick_gate
        assert calls == ['etf', 'market']
        runtime.signal_queue()
        assert all(service.awakened.wait(1) for service in services.values())
    finally:
        stop.set()
        supervisor.join(3)
    assert not supervisor.is_alive()
    assert result == [0]
    assert calls[-1] == 'disposed'
    assert not runtime.status()['ready']


def test_queue_worker_role_cannot_be_used_to_start_an_http_service(tmp_path, engine):
    settings = Settings(_env_file=None, log_dir=tmp_path, collection_runtime_role='queue-worker')
    with pytest.raises(RuntimeError, match='dedicated process entry'):
        create_app(settings, engine=engine)
