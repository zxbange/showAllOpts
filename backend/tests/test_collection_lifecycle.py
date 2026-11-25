"""Fault-injection and compatibility regressions for the first lifecycle adapters."""

from datetime import datetime, timedelta
from threading import Event
from zoneinfo import ZoneInfo

import pytest

from app.core.config import Settings
from app.services import growth_dividend, margin_guarantee
from app.services.collection_lifecycle import (
    RunContext,
    RunRequest,
    cancellation_marker,
    is_running,
    request_cancel,
    start_worker,
    try_lease,
)

CHINA = ZoneInfo('Asia/Shanghai')
MOMENT = datetime(2026, 11, 20, 23, 59, tzinfo=CHINA)
RUN_ID = 'a' * 32


class Busy(Exception):
    pass


class Cancelled(Exception):
    pass


class TestLock:
    __test__ = False

    def __init__(self, *, busy=False):
        self.busy = busy
        self.releases = 0

    def acquire(self):
        if self.busy:
            raise Busy()
        self.busy = True

    def release(self):
        assert self.busy, 'Underlying lock released more than once'
        self.busy = False
        self.releases += 1


def test_lease_deduplicates_cleanup_and_busy_probe():
    raw = TestLock()
    lease = try_lease(raw, Busy)
    assert lease is not None
    assert is_running(lambda: try_lease(raw, Busy))
    assert raw.releases == 0
    lease.release()
    lease.release()
    assert raw.releases == 1
    assert not is_running(lambda: try_lease(raw, Busy))
    assert raw.releases == 2


def test_lease_releases_if_history_owner_hook_fails():
    raw = TestLock()

    def bad_hook():
        assert raw.busy
        raise OSError('owner marker failure')

    with pytest.raises(OSError, match='owner marker failure'):
        try_lease(raw, Busy, on_acquired=bad_hook)
    assert not raw.busy
    assert raw.releases == 1


@pytest.mark.parametrize(
    'trigger, slot, automatic',
    [
        ('manual', None, False),
        ('startup', None, True),
        ('manual', MOMENT.isoformat(), True),
        ('scheduled', MOMENT.isoformat(), True),
    ],
)
def test_context_uses_frozen_time_and_actual_elapsed_for_closed_day(
    tmp_path, trigger, slot, automatic
):
    clock = [100.0]
    stop = Event()
    marker = cancellation_marker(tmp_path, RUN_ID)
    assert marker is not None

    def open_day(moment):
        return moment.weekday() < 5

    context = RunContext(
        RunRequest(MOMENT, slot, trigger),
        marker,
        stop,
        open_day,
        lambda moment: not open_day(moment),
        Cancelled,
        lambda: clock[0],
    )
    context.check_cancel()
    assert context.request.automatic is automatic
    clock[0] += 120
    assert context.effective_moment == MOMENT + timedelta(minutes=2)
    assert context.request.requested_at == MOMENT
    if automatic:
        with pytest.raises(Cancelled):
            context.check_cancel()
        assert not context.isolate_manual_failure()
    else:
        context.check_cancel()
        assert context.isolate_manual_failure()
    stop.set()
    with pytest.raises(Cancelled):
        context.check_cancel()


def test_cancel_marker_is_exact_and_does_not_cancel_other_run(tmp_path):
    other_id = 'b' * 32
    for invalid in (None, '', 1234, 'A' * 32, '../evil', 'a' * 31, 'a' * 33):
        assert not request_cancel(tmp_path, invalid)
    assert not list(tmp_path.iterdir())
    assert request_cancel(tmp_path, other_id)
    marker = cancellation_marker(tmp_path, RUN_ID)
    assert marker is not None
    context = RunContext(
        RunRequest(MOMENT, None, 'manual'),
        marker,
        Event(),
        lambda moment: True,
        lambda moment: False,
        Cancelled,
    )
    context.check_cancel()
    assert request_cancel(tmp_path, RUN_ID)
    with pytest.raises(Cancelled):
        context.check_cancel()
    assert marker.exists()


def test_start_worker_assigns_before_thread_start_and_propagates_failure():
    assigned = []

    class BrokenThread:
        def __init__(self, **kwargs):
            self.target = kwargs['target']

        def start(self):
            assert assigned == [self]
            raise RuntimeError('start failure')

    with pytest.raises(RuntimeError, match='start failure'):
        start_worker(lambda: None, name='collector', assign=assigned.append, factory=BrokenThread)


@pytest.fixture(params=[growth_dividend, margin_guarantee])
def pilot(request, tmp_path):
    module = request.param
    settings = Settings(
        _env_file=None,
        log_dir=tmp_path,
        margin_worker_enabled=False,
        growth_dividend_worker_enabled=False,
    )

    def no_provider():
        raise OSError('synthetic unavailable provider')

    if module is growth_dividend:
        monitor = module.GrowthDividend(settings, client_factory=no_provider)
    else:
        monitor = module.MarginGuarantee(settings, client_factory=no_provider, delay=0)
    return module, monitor


def test_pilot_launch_and_failure_state_persist_error_never_leak_lock(pilot, monkeypatch):
    module, monitor = pilot
    original = monitor.write_state
    writes = []

    def fail_second_write(state, *args, **kwargs):
        # The existing store delegates to itself with a live SQLite connection.
        if args or kwargs.get('db') is not None:
            return original(state, *args, **kwargs)
        writes.append(state.get('active'))
        if len(writes) > 1:
            raise OSError('synthetic state persistence failure')
        return original(state, *args, **kwargs)

    class BrokenThread:
        def __init__(self, **kwargs):
            self.target = kwargs['target']

        def start(self):
            raise RuntimeError('synthetic thread start failure')

    monkeypatch.setattr(module.threading, 'Thread', BrokenThread)
    monkeypatch.setattr(monitor, 'write_state', fail_second_write)
    with pytest.raises(OSError, match='state persistence failure'):
        monitor.refresh(background=True, moment=MOMENT)
    assert writes == [True, False]
    assert not monitor.running()
    # Old active marker is interrupted, but does not own the lease.
    assert monitor.read_state()['active'] is True
    lease = monitor.acquire()
    assert lease is not None
    lease.release()


def test_pilot_outcome_observer_failure_releases_execution_lock(pilot, monkeypatch):
    module, monitor = pilot
    outcomes = []

    def broken_record(*args, **kwargs):
        outcomes.append(kwargs)
        assert monitor.running()  # Still captured under original business lock.
        raise RuntimeError('synthetic outcome observer failure')

    monkeypatch.setattr(module, 'record_task', broken_record)
    with pytest.raises(RuntimeError, match='outcome observer failure'):
        monitor.refresh(background=False, moment=MOMENT)
    assert len(outcomes) == 1
    assert outcomes[0]['outcome'] == 'failed'
    assert monitor.read_state()['active'] is False
    assert not monitor.running()
