"""Real exchange-calendar boundaries around otherwise synthetic collector sources."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import pytest

CHINA = ZoneInfo('Asia/Shanghai')


@pytest.mark.parametrize(('day', 'verified_target'), [
    ('2026-11-07', '2026-11-06'), ('2026-10-01', '2026-09-30'),
    ('2027-01-04', '2027-01-04'),  # Official dynamic calendar can cover outside the static scheduler year.
])
def test_etf_auto_closed_but_manual_failure_not_enrolled(tmp_path, day, verified_target):
    from test_etf_collection import worker
    w, source, _ = worker(tmp_path)
    moment = datetime.fromisoformat(day + 'T19:00:00').replace(tzinfo=CHINA)
    w.clock = lambda: moment
    w.start = lambda: None
    assert not w.tick(moment)
    assert not source.calls
    target = date.fromisoformat(verified_target)
    calendar = SimpleNamespace(default_range=lambda n: (target, target),
                               official_days={moment.date(): target == moment.date(), target: True})
    source.ExchangeClient = lambda client: SimpleNamespace(calendar=lambda: calendar)
    source.collect = lambda *a, **k: ([], [], {})
    w.request_run('manual-closed')
    assert not w.tick(moment)
    with w.connect() as db:
        assert not db.execute('SELECT 1 FROM slots').fetchone()
        rows = db.execute('SELECT day, status, attempts FROM observations').fetchall()
    assert rows and all(tuple(row) == (verified_target, 'manual_only', 0) for row in rows)


@pytest.mark.parametrize('day', ['2026-11-14', '2026-10-01', '2027-01-04'])
def test_market_auto_gate_manual_dependency_failure_does_not_schedule_retry(tmp_path, day):
    from test_market_collection import worker
    moment = datetime.fromisoformat(day + 'T23:58:00').replace(tzinfo=CHINA)
    w, provider, writer, clock = worker(tmp_path, moment=moment)
    assert not w.tick()
    assert not provider.universe_calls
    w.calendar_loader = lambda client: (_ for _ in ()).throw(ValueError('source down'))
    w.request_run('manual-closed')
    assert not w.tick()
    assert not w._meta('scheduler_next_attempt_at')
    with w.connect() as db:
        keys = [row[0] for row in db.execute("SELECT key FROM metadata WHERE key LIKE 'dependency-budget:%'")]
    assert keys and all(key.startswith('dependency-budget:manual:') for key in keys)


def test_market_closed_manual_gap_does_not_trigger_early_monday_but_normal_plan_runs(tmp_path):
    from datetime import time
    from test_market_collection import worker, historical_provider
    saturday = datetime(2026, 11, 14, 23, 58, tzinfo=CHINA)
    w, provider, writer, clock = worker(tmp_path, moment=saturday)
    w._auto_time = lambda: time(23, 58)
    historical_provider(provider)
    provider.failures = {'sh600003'}
    w.request_run('weekend-failure')
    assert not w.tick()
    assert w._meta('manual-only:2026-11-13')
    calls = len(provider.quote_calls)
    clock.value = datetime(2026, 11, 16, 9, tzinfo=CHINA)
    assert not w.tick()
    assert len(provider.quote_calls) == calls
    provider.failures.clear()
    clock.value = clock.value.replace(hour=23, minute=58)
    assert w.tick()
    assert w._day('2026-11-16')['status'] == 'complete'


def test_market_evening_schedule_uses_same_day_close(tmp_path):
    from test_market_collection import worker
    w, _, _, _ = worker(tmp_path)
    del w._auto_time
    moment = datetime(2026, 11, 17, 23, 58, tzinfo=CHINA)
    assert w._auto_time().isoformat() == '23:58:00'
    assert w._data_cutoff(moment, False) == moment.date()


def test_etf_manual_crosses_midnight_without_charging_auto_budget(tmp_path):
    from test_etf_collection import worker
    w, source, _ = worker(tmp_path)
    clock = [datetime(2026, 11, 6, 23, 59, tzinfo=CHINA)]
    w.clock = lambda: clock[0]
    w.start = lambda: None
    def fail(*args, **kwargs):
        clock[0] = datetime(2026, 11, 7, 0, 1, tzinfo=CHINA)
        return [], [], {}
    source.collect = fail
    w.request_run('cross-midnight')
    assert not w.tick(clock[0])
    with w.connect() as db:
        assert all(tuple(row) == ('manual_only', 0) for row in db.execute('SELECT status,attempts FROM observations'))


def test_market_cross_midnight_preserves_existing_auto_retry_bookkeeping(tmp_path):
    from test_market_collection import worker
    w, provider, writer, clock = worker(tmp_path, moment=datetime(2026, 11, 13, 23, 59, tzinfo=CHINA))
    provider.failures = {'sh600003'}
    assert not w.tick()
    with w.connect() as db:
        old = dict(db.execute('SELECT * FROM days').fetchone())
        budgets = dict(db.execute("SELECT key,value FROM metadata WHERE key LIKE '%budget:scheduled:%'"))
    def cross(symbol):
        clock.value = datetime(2026, 11, 14, 0, 1, tzinfo=CHINA)
    provider.on_quote = cross
    w.request_run('cross-midnight')
    assert not w.tick()
    with w.connect() as db:
        row = dict(db.execute('SELECT * FROM days').fetchone())
        assert dict(db.execute("SELECT key,value FROM metadata WHERE key LIKE '%budget:scheduled:%'")) == budgets
    for key in ('attempts_today', 'attempt_date', 'next_attempt_at', 'updated_at'):
        assert row[key] == old[key]


@pytest.mark.parametrize('kind', ['etf', 'market'])
def test_auto_stops_between_requests_when_crossing_into_weekend(tmp_path, kind):
    start = datetime(2026, 11, 13, 23, 59, tzinfo=CHINA)
    if kind == 'etf':
        from test_etf_collection import worker
        w, source, _ = worker(tmp_path)
        clock = [start]
        w.clock = lambda: clock[0]
        calls = []
        def cross(*args, **kwargs):
            calls.append(1)
            clock[0] = datetime(2026, 11, 14, 0, 1, tzinfo=CHINA)
            return [], [], {}
        source.collect = cross
        assert not w.tick(start)
        assert len(calls) == 1
        assert w.status()['runs'][0]['status'] == 'interrupted'
    else:
        from test_market_collection import worker
        w, source, writer, clock = worker(tmp_path, moment=start)
        def cross(symbol):
            clock.value = datetime(2026, 11, 14, 0, 1, tzinfo=CHINA)
        source.on_quote = cross
        assert not w.tick()
        assert len(source.quote_calls) <= 1
        assert not writer.calls
