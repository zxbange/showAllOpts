"""Release identity gates and a separately supervised mock-data queue process."""

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.services.queue_runtime import QueueExecutorUnavailable, QueueRuntime, release_identity

BACKEND = Path(__file__).resolve().parents[1]
MOCK_WORKER = """
import signal
import threading
from pathlib import Path
from types import SimpleNamespace
import sys

from app import queue_worker
from app.core.config import Settings

class MockCollector:
    def __init__(self, *args, tick_gate, **kwargs):
        self.configuration = object()
        self.tick_gate = tick_gate
        self.stop_event, self.wake_event = threading.Event(), threading.Event()
        self.worker = None

    def start(self):
        self.worker = threading.Thread(target=self.loop, daemon=True)
        self.worker.start()

    def loop(self):
        while not self.stop_event.is_set():
            self.wake_event.wait(.05)
            self.wake_event.clear()

queue_worker.VersionedDatabase = lambda *a, **kw: SimpleNamespace(dispose=lambda: None)
queue_worker.SnapshotCache = lambda *a, **kw: SimpleNamespace(
    refresh_etf_collection=lambda: None, publish_etf_collection=lambda **_: None,
    verify_etf_publication=lambda **_: None,
    refresh_market_collection=lambda: None
)
queue_worker.ETFCollection = MockCollector
queue_worker.MarketCollection = MockCollector
queue_worker.start_runtime = lambda *a: None
queue_worker.stop_runtime = lambda *a: None
queue_worker.stop_notifications = lambda *a: None
queue_worker.HEARTBEAT_SECONDS = .05

stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stop.set())
raise SystemExit(queue_worker.serve(Settings(
    _env_file=None, log_dir=Path(sys.argv[1]), collection_runtime_role='queue-worker'
), stop))
"""


def clone_source(destination):
    shutil.copytree(
        BACKEND / 'app',
        destination / 'app',
        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'),
    )
    for name in ('run.py', 'requirements.lock.txt'):
        shutil.copy2(BACKEND / name, destination / name)
    return destination


def test_release_identity_tracks_actual_source_and_dependency_manifest(tmp_path):
    original = release_identity(BACKEND)
    clone = clone_source(tmp_path / 'backend')
    assert release_identity(clone) == original
    # Each process captures startup code once; clear cache to simulate restart.
    (clone / 'app' / 'services' / 'queue_runtime.py').open('a').write('\n# next release\n')
    release_identity.cache_clear()
    changed = release_identity(clone)
    assert changed != original
    (clone / 'requirements.lock.txt').open('a').write('\n# new manifest\n')
    release_identity.cache_clear()
    assert release_identity(clone) != changed
    (clone / 'run.py').unlink()
    release_identity.cache_clear()
    with pytest.raises(OSError):
        release_identity(clone)
    release_identity.cache_clear()


def test_supervised_two_roles_reject_mixed_release_and_recover(tmp_path, engine):
    settings = Settings(
        _env_file=None,
        log_dir=tmp_path / 'shared-log',
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
    alternate = clone_source(tmp_path / 'next-release')
    # Real separate source tree; no environment-supplied identity override.
    (alternate / 'app' / 'services' / 'queue_runtime.py').open('a').write('\n# alternate version\n')

    def start(source):
        return subprocess.Popen(
            [sys.executable, '-c', MOCK_WORKER, str(settings.log_path)],
            cwd=source,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(child):
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
            pytest.fail('Mock dedicated worker did not stop within test budget')
        assert child.returncode == 0

    with TestClient(create_app(settings, engine=engine)) as client:
        runtime = QueueRuntime(settings.log_path)
        assert client.get('/api/v1/readiness').status_code == 503

        def await_state(state, child):
            deadline = time.monotonic() + 8
            while runtime.status()['state'] != state:
                assert child.poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.02)

        child = start(alternate)
        try:
            await_state('version_mismatch', child)
            response = client.get('/api/v1/readiness')
            assert response.status_code == 503
            assert response.json()['queue_executor'] == 'version_mismatch'
            assert 'release' not in response.json()
            assert 'owner_pid' not in response.json()
            with pytest.raises(QueueExecutorUnavailable):
                runtime.require_ready('etf')
            assert client.get('/api/v1/health').status_code == 200
        finally:
            stop(child)

        child = start(BACKEND)
        try:
            await_state('ready', child)
            assert client.get('/api/v1/readiness').status_code == 200
        finally:
            stop(child)
        assert client.get('/api/v1/readiness').status_code == 503
