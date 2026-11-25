"""Opt-in, single-host runtime control for the two durable collector queues."""

import hashlib
import json
import logging
import os
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

import portalocker

LOG = logging.getLogger(__name__)
GROUPS = frozenset(('etf', 'market'))
HEARTBEAT_SECONDS = 2.0
STALE_SECONDS = 12.0
PROTOCOL_VERSION = 2
STATES = frozenset(('starting', 'ready', 'draining', 'stopped', 'degraded'))
_UNSETTLED_AUTHORITIES: list[portalocker.Lock] = []
BACKEND_SOURCE_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def release_identity(backend: Path = BACKEND_SOURCE_ROOT) -> str:
    """Fingerprint the actual backend source and locked dependency manifest.

    Capture once per process from its own imported source location, not a
    caller-supplied release label. Deployment must still guarantee immutable
    code while either role is alive and verify the full Git SHA and venv.
    """
    files = [backend / 'run.py', backend / 'requirements.lock.txt']
    files += [
        path
        for path in (backend / 'app').rglob('*')
        if path.is_file() and path.suffix in ('.py', '.json') and '__pycache__' not in path.parts
    ]
    if backend / 'app' / 'services' / 'queue_runtime.py' not in files:
        raise OSError('Incomplete queue runtime release')
    digest = hashlib.sha256()
    digest.update(f'queue-release-v1\0{sys.version_info[:3]}\0'.encode())
    for path in sorted(files):
        # Length-prefixed paths and content digests make the encoding
        # deterministic across checkout paths and resistant to ambiguity.
        name = path.relative_to(backend).as_posix().encode()
        digest.update(len(name).to_bytes(4, 'big'))
        digest.update(name)
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def process_generation(pid: int) -> str:
    """Linux process start time; distinguish a stale heartbeat from a new owner."""
    if type(pid) is not int or pid <= 0:
        raise ValueError('Invalid queue owner')
    # /proc stat field 22 is starttime; comm may contain spaces or parentheses.
    fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] in ('Z', 'X') or int(fields[19]) <= 0:
        raise ValueError('Queue owner has exited')
    return fields[19]


class QueueRoleConflict(RuntimeError):
    """A mutually exclusive queue execution role is already active."""


class QueueExecutorUnavailable(RuntimeError):
    """A split API cannot currently admit a new durable queue request."""


class TickGate:
    """Admit ticks atomically, then run outside the short admission lock.

    A tick admitted before close may finish all its existing business work.
    Closing never waits on a source/DB call or clears a persisted queue.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._closed = False
        self._active = 0

    @contextmanager
    def admit(self) -> Iterator[bool]:
        with self._condition:
            allowed = not self._closed
            if allowed:
                self._active += 1
        try:
            yield allowed
        finally:
            if allowed:
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def wait_drained(self, timeout: float) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: self._active == 0, timeout=max(0.0, timeout))


class QueueRuntime:
    """Credential-free, atomic heartbeat and advisory ownership lock.

    The combined API takes shared authority; the dedicated queue process takes
    exclusive authority. Split API processes do not execute either queue.
    """

    def __init__(self, log_path: Path, *, verify_release: bool = True) -> None:
        self.release = release_identity() if verify_release else None
        self.root = log_path / 'queue-runtime'
        self.heartbeat = self.root / 'heartbeat.json'
        self.wake_marker = self.root / 'wake'

    def _authority_lock(self, *, exclusive: bool) -> portalocker.Lock:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        mode = portalocker.LOCK_EX if exclusive else portalocker.LOCK_SH
        return portalocker.Lock(
            str(self.root / 'authority.lock'), mode='a', timeout=0, flags=mode | portalocker.LOCK_NB
        )

    def acquire_authority(self, *, exclusive: bool) -> portalocker.Lock:
        lock = self._authority_lock(exclusive=exclusive)
        try:
            lock.acquire()
        except portalocker.exceptions.LockException:
            raise QueueRoleConflict(
                'Incompatible queue executor is already active; stop it before changing roles.'
            ) from None
        return lock

    @staticmethod
    def release_after_workers(lock: portalocker.Lock, workers: list[Any]) -> None:
        """Do not let a still-running daemon overlap a succeeding role."""
        alive = [worker for worker in workers if worker is not None and worker.is_alive()]
        if not alive:
            lock.release()
            return

        def reap() -> None:
            for worker in alive:
                worker.join()
            lock.release()

        # A process exiting still releases its OS descriptor. While it lives,
        # even after a bounded stop timeout, the incompatible role stays out.
        try:
            threading.Thread(target=reap, daemon=True, name='queue-role-release').start()
        except RuntimeError:
            # If thread creation itself fails, keep the descriptor until
            # process exit rather than opening an unsafe overlap window.
            _UNSETTLED_AUTHORITIES.append(lock)
            LOG.error('Queue authority retained until process exit')

    def _exclusive_owner_present(self) -> bool:
        try:
            lock = self.acquire_authority(exclusive=False)
        except QueueRoleConflict:
            return True
        except OSError:
            return False
        lock.release()
        return False

    def publish(self, state: str, groups: set[str]) -> None:
        if state not in STATES or not groups <= GROUPS:
            raise ValueError('Invalid queue heartbeat state')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix='.heartbeat-', dir=self.root)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(
                    {
                        'protocol': PROTOCOL_VERSION,
                        'release': self.release,
                        'owner_pid': os.getpid(),
                        'owner_start': process_generation(os.getpid()),
                        'state': state,
                        'at': time.time(),
                        'groups': sorted(groups),
                    },
                    stream,
                )
            os.replace(name, self.heartbeat)
        finally:
            Path(name).unlink(missing_ok=True)

    def status(self) -> dict[str, Any]:
        """Read local liveness only, never access upstream or business tables."""
        unavailable = {'state': 'unavailable', 'workers': [], 'ready': False}
        try:
            with self.heartbeat.open('rb') as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                return unavailable
            value = json.loads(raw)
            if type(value.get('protocol')) is not int or value['protocol'] != PROTOCOL_VERSION:
                return unavailable | {'state': 'incompatible'}
            groups = value['groups']
            stamp = value['at']
            state = value['state']
            if (
                state not in STATES
                or type(stamp) not in (int, float)
                or not isinstance(groups, list)
                or not all(isinstance(name, str) for name in groups)
                or not set(groups) <= GROUPS
            ):
                return unavailable
            age = time.time() - stamp
            if not 0 <= age <= STALE_SECONDS:
                return unavailable | {'state': 'stale'}
            if process_generation(value['owner_pid']) != value['owner_start']:
                return unavailable
            # A fresh file from a process that has already exited is not proof
            # of ownership. Combined shared locks cannot satisfy this check.
            if not self._exclusive_owner_present():
                return unavailable
            if self.release is None or value.get('release') != self.release:
                return unavailable | {'state': 'version_mismatch'}
            return {
                'state': state,
                'workers': groups,
                'ready': state == 'ready' and set(groups) == GROUPS,
            }
        except (OSError, UnicodeError, TypeError, ValueError, KeyError, AttributeError, IndexError):
            return unavailable

    def require_ready(self, group: str) -> None:
        status = self.status()
        if not status['ready'] or group not in status['workers']:
            raise QueueExecutorUnavailable(
                '独立采集执行器暂不可用或正在停止；未接受新任务，已有任务与数据保留。'
            )

    def worker_running(self, group: str) -> bool:
        status = self.status()
        return status['ready'] and group in status['workers']

    def signal_queue(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.wake_marker.touch()
        except OSError as error:
            # The request is already durably queued. Regular scheduler polling
            # still consumes it; a notification failure must not fake rejection.
            LOG.warning('Queue wake signal unavailable error_type=%s', type(error).__name__)

    def wake_revision(self) -> int | None:
        try:
            return self.wake_marker.stat().st_mtime_ns
        except OSError:
            return None
