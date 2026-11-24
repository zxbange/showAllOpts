from datetime import datetime
from types import SimpleNamespace

import pytest
from app.services.earnings_schedule import EarningsScheduler, acquire_execution, daily_next_run, CHINA


class Screen:
    def __init__(self):
        self.state, self.busy, self.calls = {}, False, []
        self.fail = False
    def read_state(self): return self.state
    def running(self): return self.busy
    def refresh(self, **kw):
        if self.fail: raise ValueError('unavailable')
        self.calls.append(kw)
        self.state = dict(current_slot=kw['slot'], run_id=str(len(self.calls)), active=True)
        self.busy = True
        return True
    def finish(self, ok=True):
        self.state.update(active=False, stage='complete' if ok else 'failed',
                          scheduled_success_slot=self.state['current_slot'] if ok else None)
        self.busy = False


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr('app.services.earnings_schedule.task_config', lambda *a: {'enabled': True, 'times': ['23:00']})
    a, b = Screen(), Screen()
    return EarningsScheduler(SimpleNamespace(log_path=tmp_path), a, b), a, b


def at(day=16, hour=23): return datetime(2026, 11, day, hour, tzinfo=CHINA)


def test_daily_serial_and_no_repeat(queue):
    q, a, b = queue
    assert q.tick(at(hour=22)) is None
    assert q.tick(at()) == 'dividend_selection'
    assert q.tick(at()) == 'dividend_selection'
    assert len(a.calls) == 1 and not b.calls
    a.finish()
    assert q.tick(at()) == 'cash_cow'
    b.finish()
    assert q.tick(at()) is None
    assert len(b.calls) == 1
    assert [j['status'] for j in q.jobs()] == ['complete', 'complete']


def test_failed_source_does_not_block_cash_cow(queue):
    q, a, b = queue
    a.fail = True
    assert q.tick(at()) == 'cash_cow'
    assert q.jobs()[0]['status'] == 'failed'


def test_interrupted_run_retained_but_unstarted_past_slot_cannot_use_current_snapshot(queue):
    q, a, b = queue
    q.tick(at())
    a.busy = False  # Process died, active flag survived.
    restarted = EarningsScheduler(q.settings, a, b)
    assert restarted.tick(at(day=17, hour=1)) is None
    assert [job['status'] for job in restarted.jobs()] == ['interrupted', 'skipped']
    assert not b.calls
    assert restarted.tick(at(day=17)) == 'dividend_selection'
    assert [call['slot'] for call in a.calls] == ['2026-11-16', '2026-11-17']


def test_manual_conflict_waits_without_losing_slot(queue):
    q, a, b = queue
    a.busy = True
    assert q.tick(at()) is None
    assert q.jobs()[0]['status'] == 'pending'
    a.busy = False
    assert q.tick(at()) == 'dividend_selection'


def test_other_task_manual_execution_lease_conflict(queue):
    q, a, b = queue
    a.refresh = lambda **kw: False
    assert q.tick(at()) is None
    assert q.jobs()[0]['status'] == 'pending'
    assert not b.calls


def test_second_coordinator_cannot_duplicate(queue):
    q, a, b = queue
    other = EarningsScheduler(q.settings, a, b)
    q.tick(at())
    other.tick(at())
    assert len(a.calls) == 1


def test_next_run_is_daily_and_uses_configured_time():
    assert daily_next_run(at(hour=22)).isoformat() == '2026-11-16T23:00:00+08:00'
    assert daily_next_run(at()).isoformat() == '2026-11-17T23:00:00+08:00'
    assert daily_next_run(at(hour=22), at='22:30').hour == 22


def test_shared_execution_lease_blocks_other_modules(tmp_path):
    settings = SimpleNamespace(log_path=tmp_path)
    class Lock:
        def release(self): pass
    first = acquire_execution(settings, Lock)
    assert first is not None
    assert acquire_execution(settings, Lock) is None
    first.release()
    third = acquire_execution(settings, Lock)
    assert third is not None
    third.release()


@pytest.mark.parametrize('stamp,expected', [
    ('2024-02-28T23:00:00+08:00', None),
    ('2025-02-28T23:00:00+08:00', None),
    ('2026-12-31T23:00:00+08:00', None),
    ('2026-11-16T14:59:00+00:00', '2026-11-16T23:00:00+08:00'),
    ('2026-11-16T15:00:00+00:00', '2026-11-17T23:00:00+08:00'),
])
def test_daily_calendar_boundary(stamp, expected):
    result = daily_next_run(datetime.fromisoformat(stamp))
    assert (result.isoformat() if result else None) == expected


def test_enabled_schedule_time_change_and_disabled(queue, monkeypatch):
    q, a, b = queue
    monkeypatch.setattr('app.services.earnings_schedule.task_config', lambda *a: {'enabled': False, 'times': ['22:00']})
    assert q.tick(at()) is None
    assert q.jobs() == []
    monkeypatch.setattr('app.services.earnings_schedule.task_config', lambda *a: {'enabled': True, 'times': ['22:00']})
    assert q.tick(at(hour=22)) == 'dividend_selection'


@pytest.mark.parametrize('moment', [at(day=14), datetime(2026, 10, 1, 23, tzinfo=CHINA), datetime(2027, 1, 4, 23, tzinfo=CHINA)])
def test_closed_or_unknown_dates_do_not_launch_queue(queue, moment):
    q, a, b = queue
    assert q.tick(moment) is None
    assert not a.calls and not b.calls and not q.jobs()


def test_legacy_weekend_queue_is_retired_without_launch(queue):
    q, a, b = queue
    with q.db() as db:
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)', ('2026-11-14', 'dividend_selection', 0, 'pending', None, None))
    assert q.tick(at()) == 'dividend_selection'
    assert a.calls[0]['slot'] == '2026-11-16'
    assert q.jobs()[0]['status'] == 'skipped'


def test_next_run_skips_weekend_and_holiday():
    assert daily_next_run(at(day=13)).isoformat() == '2026-11-16T23:00:00+08:00'
    assert daily_next_run(datetime(2026, 9, 30, 23, tzinfo=CHINA)).isoformat() == '2026-10-08T23:00:00+08:00'
