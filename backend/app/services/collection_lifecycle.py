"""Small lifecycle primitives for cooperating collectors.

This layer deliberately owns no business database, scheduler, retry ledger,
notification, publication, or public API contract. An acquired lease is handed
to the collector, whose existing finalizer records its terminal outcome before
releasing it. In particular, do not move release outside a decorated collector.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock, Thread
from time import monotonic
from typing import Protocol


class Releasable(Protocol):
    def release(self) -> None: ...


class TryLock(Releasable, Protocol):
    def acquire(self) -> object: ...


class StopSignal(Protocol):
    def is_set(self) -> bool: ...


class RunLease:
    """Own precisely one acquired lock; repeated cleanup cannot release it twice."""

    def __init__(self, lock: Releasable) -> None:
        self._lock = lock
        self._guard = Lock()
        self._released = False

    def release(self) -> None:
        with self._guard:
            if not self._released:
                self._lock.release()
                self._released = True


def try_lease(
    lock: TryLock,
    busy_error: type[Exception],
    *,
    on_acquired: Callable[[], None] | None = None,
) -> RunLease | None:
    """Acquire without blocking; run ownership hooks under the same lock."""
    try:
        lock.acquire()
    except busy_error:
        return None
    lease = RunLease(lock)
    try:
        if on_acquired is not None:
            on_acquired()
    except BaseException:
        lease.release()
        raise
    return lease


def is_running(acquire: Callable[[], RunLease | None]) -> bool:
    lease = acquire()
    if lease is None:
        return True
    lease.release()
    return False


def cancellation_marker(root: Path, run_id: object) -> Path | None:
    if not isinstance(run_id, str) or re.fullmatch(r'[0-9a-f]{32}', run_id) is None:
        return None
    return root / ('cancel-' + run_id)


def request_cancel(root: Path, run_id: object) -> bool:
    marker = cancellation_marker(root, run_id)
    if marker is None:
        return False
    marker.touch()
    return True


@dataclass(frozen=True)
class RunRequest:
    requested_at: datetime
    slot: str | None
    trigger: str

    @property
    def automatic(self) -> bool:
        return self.slot is not None or self.trigger != 'manual'


@dataclass(frozen=True)
class RunContext:
    request: RunRequest
    marker: Path
    stop_signal: StopSignal
    automatic_allowed: Callable[[datetime], bool]
    manual_isolated: Callable[[datetime], bool]
    cancelled_error: Callable[[], Exception]
    clock: Callable[[], float] = field(default=monotonic, repr=False)
    started: float = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'started', self.clock())

    @property
    def elapsed(self) -> float:
        return self.clock() - self.started

    @property
    def effective_moment(self) -> datetime:
        # Use elapsed monotonic time, not a second wall-clock sampling, so
        # the calendar/cancellation policy shares the admitted run's basis.
        return self.request.requested_at + timedelta(seconds=self.elapsed)

    def check_cancel(self) -> None:
        if (
            (self.request.automatic and not self.automatic_allowed(self.effective_moment))
            or self.stop_signal.is_set()
            or self.marker.exists()
        ):
            raise self.cancelled_error()

    def isolate_manual_failure(self, *, include_requested: bool = True) -> bool:
        return not self.request.automatic and (
            (include_requested and self.manual_isolated(self.request.requested_at))
            or self.manual_isolated(self.effective_moment)
        )


def start_worker(
    callback: Callable[..., None],
    *,
    name: str,
    assign: Callable[[Thread], None],
    factory: Callable[..., Thread] = Thread,
    args: tuple[object, ...] | None = None,
) -> None:
    """Assign before start, preserving access to the thread even on failure.

    The admitting caller still owns cleanup if construction or start raises.
    After a successful start, the collector owns cleanup.
    """
    worker = (
        factory(target=callback, name=name, daemon=True)
        if args is None
        else factory(target=callback, args=args, name=name, daemon=True)
    )
    assign(worker)
    worker.start()
