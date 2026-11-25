"""Regression tests for the valuation lifecycle adapter and its durable ledger."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from app.core.config import Settings
from app.core.history_sync import database_syncing
from app.services import valuation_ruler as m

MOMENT = datetime(2026, 11, 24, 18, 30, tzinfo=m.CHINA)
SLOT = MOMENT.isoformat()


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'now', lambda: MOMENT)
    settings = Settings(_env_file=None, log_dir=tmp_path, valuation_ruler_worker_enabled=False)
    return m.ValuationRuler(settings)


def test_history_lease_does_not_admit_collection_or_cancel(monitor):
    lease = monitor.acquire(for_history=True)
    assert lease is not None
    try:
        before = monitor.read_state()
        assert database_syncing(monitor, monitor.running())
        assert not monitor.cancel()
        assert not monitor.start_job(
            'public', lambda *args: pytest.fail('busy request executed'), moment=MOMENT
        )
        assert monitor.read_state() == before
    finally:
        lease.release()
        lease.release()
    assert not monitor.running()
    assert not database_syncing(monitor, False)
    run_id = monitor.start_job(
        'public', lambda *args: None, moment=MOMENT, background=False, return_run_id=True
    )
    assert run_id == monitor.read_state()['run_id']
    assert monitor.read_state()['phase'] == 'complete'


def test_slot_admission_transaction_failure_does_not_touch_prior_state(monitor):
    before = monitor.read_state()
    with monitor.db() as db:
        db.execute(
            """CREATE TRIGGER abort_slot BEFORE INSERT ON slots
            BEGIN SELECT RAISE(ABORT, 'synthetic slot write failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match='synthetic slot write failure'):
        monitor.start_job(
            'public',
            lambda *args: pytest.fail('uncommitted admission executed'),
            moment=MOMENT,
            slot=SLOT,
            background=False,
        )
    assert monitor.read_state() == before
    assert not monitor.running()
    with monitor.db() as db:
        assert db.execute('SELECT COUNT(*) FROM slots').fetchone()[0] == 0


def test_terminal_observer_failure_does_not_reclassify_committed_run(monitor, monkeypatch):
    terminal = []

    def fail_observer(*args, **kwargs):
        assert monitor.running()  # Keep final result capture inside the lease.
        terminal.append(kwargs)
        raise RuntimeError('synthetic observer failure')

    monkeypatch.setattr(m, 'record_task', fail_observer)
    with pytest.raises(RuntimeError, match='synthetic observer failure'):
        monitor.start_job('public', lambda *args: None, moment=MOMENT, slot=SLOT, background=False)

    state = monitor.read_state()
    assert not monitor.running()
    assert not state['active']
    assert state['phase'] == 'complete'
    assert state['last_success_at'] == MOMENT.isoformat()
    assert len(terminal) == 1
    assert terminal[0]['run_key'] == state['run_id']
    assert terminal[0]['outcome'] == 'complete'
    with monitor.db() as db:
        assert (
            db.execute('SELECT outcome FROM slots WHERE slot=?', (SLOT,)).fetchone()[0]
            == 'complete'
        )
    # A subsequent request respects the existing terminal slot.
    assert not monitor.start_job(
        'public',
        lambda *args: pytest.fail('completed slot ran again'),
        moment=MOMENT,
        slot=SLOT,
        background=False,
    )


def test_existing_run_budget_and_retry_clock_use_same_frozen_request(monitor, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(m, 'monotonic', lambda: clock[0])

    def run(monitor, state, check_cancel, moment):
        assert moment == MOMENT
        clock[0] += m.MAX_RUN_SECONDS
        check_cancel()  # Exact boundary remains admitted.
        clock[0] += 1
        check_cancel()

    assert monitor.start_job('public', run, moment=MOMENT, slot=SLOT, background=False)
    state = monitor.read_state()
    assert state['phase'] == 'failed'
    assert not state['active']
    retry = MOMENT + timedelta(seconds=m.MAX_RUN_SECONDS + 1, minutes=15)
    assert datetime.fromisoformat(state['retry_after']) == retry
    assert not monitor.start_job(
        'public',
        lambda *args: pytest.fail('retry launched before cooldown'),
        moment=retry - timedelta(seconds=1),
        slot=SLOT,
        background=False,
    )
    assert monitor.start_job(
        'public', lambda *args: None, moment=retry, slot=SLOT, background=False
    )
    assert monitor.read_state()['phase'] == 'complete'
