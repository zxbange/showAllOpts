"""Preserve earnings shared execution and durable publication semantics."""

from contextlib import nullcontext
from datetime import datetime

import pytest
from test_cash_cow import Source

from app.core.config import Settings
from app.services import cash_cow, dividend_collection
from app.services.earnings_schedule import acquire_execution

MOMENT = datetime(2026, 11, 16, 23, tzinfo=dividend_collection.CHINA)


def test_group_lease_cleanup_is_idempotent_and_cannot_unlock_new_run(tmp_path):
    settings = Settings(_env_file=None, log_dir=tmp_path)
    a = dividend_collection.DividendCollection(settings)
    b = cash_cow.CashCowScreen(settings)

    first = acquire_execution(settings, a.acquire)
    assert first is not None
    assert a.running()
    assert acquire_execution(settings, b.acquire) is None
    first.release()
    assert not a.running()

    second = acquire_execution(settings, b.acquire)
    assert second is not None
    first.release()
    assert b.running()
    assert acquire_execution(settings, a.acquire) is None
    second.release()
    second.release()
    third = acquire_execution(settings, a.acquire)
    assert third is not None
    third.release()


def test_group_lease_released_if_task_acquisition_raises(tmp_path):
    settings = Settings(_env_file=None, log_dir=tmp_path)
    service = dividend_collection.DividendCollection(settings)

    def broken_acquire():
        raise OSError('synthetic task acquisition failure')

    with pytest.raises(OSError, match='task acquisition failure'):
        acquire_execution(settings, broken_acquire)
    lease = acquire_execution(settings, service.acquire)
    assert lease is not None
    lease.release()


def test_cash_cow_terminal_observer_failure_preserves_published_three_windows(
    tmp_path, monkeypatch
):
    settings = Settings(_env_file=None, log_dir=tmp_path)
    screen = cash_cow.CashCowScreen(
        settings, source_factory=Source, client_factory=lambda: nullcontext(None)
    )
    outcomes = []

    def broken_record(*args, **kwargs):
        assert screen.running()
        outcomes.append(kwargs)
        raise RuntimeError('synthetic observer failure')

    monkeypatch.setattr(cash_cow, 'record_task', broken_record)
    with pytest.raises(RuntimeError, match='observer failure'):
        screen.refresh(background=False, moment=MOMENT)
    state = screen.read_state()
    assert len(outcomes) == 1
    assert outcomes[0]['outcome'] == 'success'
    assert not state['active']
    assert state['error'] is None
    assert state['stage'] == '3/5/10年筛选完成'
    assert not screen.running()
    for years in (3, 5, 10):
        reader = cash_cow.CashCowScreen(settings, years=years)
        assert reader.latest()['snapshot']['run_id'] == state['run_id']
    # Both per-task and cross-earnings group lock are now free.
    other = dividend_collection.DividendCollection(settings)
    lease = acquire_execution(settings, other.acquire)
    assert lease is not None
    lease.release()


def test_dividend_invalid_cancellation_marker_never_creates_file(tmp_path):
    settings = Settings(_env_file=None, log_dir=tmp_path)
    service = dividend_collection.DividendCollection(settings)
    lease = service.acquire()
    assert lease is not None
    try:
        service.save_state({'active': True, 'run_id': '../not-a-run'})
        assert not service.cancel()
        assert not list(service.root.glob('cancel-*'))
        service.save_state({'active': True, 'run_id': 'f' * 32})
        assert service.cancel()
        assert (service.root / ('cancel-' + 'f' * 32)).exists()
    finally:
        lease.release()
