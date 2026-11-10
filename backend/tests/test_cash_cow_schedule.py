from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.cash_cow_schedule import CHINA, MonthlyCashCowScheduler, next_run


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=CHINA)


class Screen:
    def __init__(self):
        self.state, self.calls, self.busy = {}, [], False

    def read_state(self):
        return dict(self.state)

    def running(self):
        return self.busy

    def refresh(self, background, moment, slot):
        assert background
        if self.busy:
            return False
        self.calls.append((moment, slot))
        self.state.update(active=True, scheduled_slot=slot, current_scheduled_slot=slot,
                          scheduled_attempt_at=moment.isoformat(), last_attempt_at=moment.isoformat())
        self.busy = True
        return True

    def finish(self, success=True):
        self.busy = False
        self.state['active'] = False
        if success:
            self.state['scheduled_success_slot'] = self.state['scheduled_slot']


@pytest.fixture
def setup(tmp_path):
    settings = SimpleNamespace(log_path=tmp_path)
    screen = Screen()
    screens = {period: screen for period in (3, 5, 10)}
    return settings, screens, MonthlyCashCowScheduler(settings, screens)


@pytest.mark.parametrize('moment,expected', [
    ('2024-02-01T12:00', '2024-02-29T21:00'),
    ('2025-02-01T12:00', '2025-02-28T21:00'),
    ('2025-02-28T20:59', '2025-02-28T21:00'),
    ('2026-04-30T21:00', '2026-05-31T21:00'),
    ('2026-11-30T21:00', '2026-12-31T21:00'),
    ('2024-02-29T23:00', '2024-03-31T21:00'),  # Sunday, not Friday March 29
    ('2024-03-31T19:59', '2024-03-31T21:00'),
    ('2024-03-31T21:00', '2024-04-30T21:00'),  # Next month has 30 days
    ('2026-12-31T21:00', '2027-01-31T21:00'),
])
def test_next_calendar_month(moment, expected):
    assert next_run(at(moment)) == at(expected)


def test_timezone_and_naive_rejection():
    assert next_run(datetime(2024, 3, 31, 13, tzinfo=timezone.utc)) == at('2024-04-30T21:00')
    with pytest.raises(ValueError):
        next_run(datetime(2024, 3, 31))


def test_cold_initialization_no_prior_month_network(setup):
    settings, screens, scheduler = setup
    assert scheduler.tick(at('2026-11-10T12:00')) is None
    assert scheduler.tick(at('2026-11-30T20:59')) is None
    assert not any(screen.calls for screen in screens.values())
    assert scheduler.tick(at('2026-11-30T21:00')) == (3, 5, 10)


def test_one_shared_run_and_restart(setup):
    settings, screens, scheduler = setup
    scheduler.tick(at('2026-09-01T12:00'))
    moment = at('2026-09-30T21:00')
    assert scheduler.tick(moment) == (3, 5, 10)
    assert scheduler.tick(moment) is None
    screens[10].finish()
    scheduler = MonthlyCashCowScheduler(settings, screens)
    assert scheduler.tick(at('2026-10-01T12:00')) is None
    assert len(screens[10].calls) == 1


def test_failure_once_day_and_interrupted_recovery(setup):
    settings, screens, scheduler = setup
    scheduler.tick(at('2026-09-01T12:00'))
    assert scheduler.tick(at('2026-09-30T21:00')) == (3, 5, 10)
    screens[10].finish(False)
    assert scheduler.tick(at('2026-09-30T21:01')) is None
    restarted = MonthlyCashCowScheduler(settings, screens)
    assert restarted.tick(at('2026-09-30T23:00')) is None
    assert restarted.tick(at('2026-10-01T00:00')) == (3, 5, 10)
    screens[10].busy = False  # process died while persisted active=True
    assert restarted.tick(at('2026-10-01T00:01')) == (3, 5, 10)
    screens[10].finish()
    assert restarted.tick(at('2026-10-01T00:02')) is None


def test_only_latest_missed_month_after_enablement(setup):
    settings, screens, scheduler = setup
    scheduler.tick(at('2025-01-01T00:00'))
    restarted = MonthlyCashCowScheduler(settings, screens)
    assert restarted.tick(at('2026-11-10T12:00')) == (3, 5, 10)
    assert screens[3].calls[-1][1] == '2026-10-31'


def test_manual_run_blocks_all_scheduled_launches(setup):
    _, screens, scheduler = setup
    scheduler.tick(at('2026-09-01T12:00'))
    screens[10].busy = True
    assert scheduler.tick(at('2026-09-30T21:00')) is None
    assert not any(screen.calls for screen in screens.values())


def test_refused_launch_does_not_consume_attempt(setup, monkeypatch):
    _, screens, scheduler = setup
    scheduler.tick(at('2026-09-01T12:00'))
    original = screens[3].refresh
    monkeypatch.setattr(screens[3], 'refresh', lambda **kwargs: False)
    assert scheduler.tick(at('2026-09-30T21:00')) is None
    assert not screens[5].calls
    monkeypatch.setattr(screens[3], 'refresh', original)
    assert scheduler.tick(at('2026-09-30T21:01')) == (3, 5, 10)


def test_manual_interruption_does_not_bypass_same_day_retry_limit(setup):
    _, screens, scheduler = setup
    scheduler.tick(at('2026-09-01T12:00'))
    assert scheduler.tick(at('2026-09-30T21:00')) == (3, 5, 10)
    screens[10].finish(success=False)
    screens[3].state.update(active=True, current_scheduled_slot=None)
    assert scheduler.tick(at('2026-09-30T21:00')) is None
    assert len(screens[3].calls) == 1


def test_next_month_runs_again_once_per_period(setup):
    _, screens, scheduler = setup
    scheduler.tick(at('2026-09-01T12:00'))
    for month_end in ('2026-09-30T21:00', '2026-10-31T21:00'):
        assert scheduler.tick(at(month_end)) == (3, 5, 10)
        screens[10].finish()
        assert scheduler.tick(at(month_end)) is None
    assert [len(screen.calls) for screen in screens.values()] == [2, 2, 2]


def test_february_leap_trigger_and_utc_boundary(setup):
    _, screens, scheduler = setup
    scheduler.tick(at('2024-02-01T12:00'))
    assert scheduler.tick(datetime(2024, 2, 28, 13, tzinfo=timezone.utc)) is None
    assert scheduler.tick(datetime(2024, 2, 29, 12, 59, tzinfo=timezone.utc)) is None
    assert scheduler.tick(datetime(2024, 2, 29, 13, tzinfo=timezone.utc)) == (3, 5, 10)
    assert screens[3].calls[-1][1] == '2024-02-29'
