"""Persisted gap recovery, close cutoffs, scheduling, cancellation and authenticated API."""
from contextlib import nullcontext
from datetime import date, datetime, timedelta

import httpx
import pytest

from app.core.config import Settings
from app.services import growth_dividend as m
from app.services.growth_dividend_source import IndexQuote

MOMENT = datetime(2026, 11, 10, 18, 5, tzinfo=m.CHINA)
DAYS = [date(2018, 1, 2), date(2019, 12, 31), date(2020, 1, 2),
        date(2026, 11, 9), date(2026, 11, 10)]


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'now', lambda: MOMENT)
    service = m.GrowthDividend(Settings(_env_file=None, log_dir=tmp_path,
                               growth_dividend_worker_enabled=False),
                               client_factory=lambda: nullcontext(object()))
    yield service
    service.stop()


def provider(monkeypatch, days=DAYS, failures=None):
    calls = []
    failures = failures if failures is not None else set()
    monkeypatch.setattr(m, 'fetch_calendar', lambda client:
                        sorted(set([date(2017, 12, 29), date(2026, 12, 31)] + days)))
    def prices(client, index, start, end, *, expected_dates, check_cancel):
        check_cancel()
        calls.append((index.code, start, end, set(expected_dates)))
        close = {'399006': 2500, '000922': 5000, '000688': 1250}[index.code]
        return {day: IndexQuote(day, close, 'fixture', 'https://example.test/index')
                for day in expected_dates if day in days and (index.code, day) not in failures}
    monkeypatch.setattr(m, 'fetch_prices', prices)
    return calls


def test_first_backfill_includes_2018_and_same_day_ratios_without_fake_star_history(monitor, monkeypatch):
    calls = provider(monkeypatch)
    assert monitor.refresh(background=False, moment=MOMENT)
    data = monitor.history(MOMENT)
    assert (data['start_date'], data['end_date']) == ('2018-01-02', '2026-11-10')
    assert len(data['points']) == 5
    assert data['latest']['chinext_dividend_ratio'] == .5
    assert data['latest']['star50_dividend_ratio'] == .25
    assert all(point['star50'] is None and point['star50_dividend_ratio'] is None
               for point in data['points'][:2])
    assert data['points'][2]['star50'] == 1250
    assert data['status']['pending_points'] == 0
    assert data['status']['last_success_at']
    assert all(start.year == end.year for _, start, end, _ in calls)
    assert all(day >= date(2020, 1, 2) for code, _, _, days in calls if code == '000688' for day in days)


def test_incremental_only_requests_today_and_never_reloads_completed_years(monitor, monkeypatch):
    calls = provider(monkeypatch)
    monitor.refresh(background=False, moment=MOMENT)
    calls.clear()
    monitor.refresh(background=False, moment=MOMENT)
    assert len(calls) == 3 and all(start == end == date(2026, 11, 10) for _, start, end, _ in calls)
    calls.clear()
    monitor.refresh(background=False, moment=MOMENT.replace(day=11, hour=10))
    assert calls == []


@pytest.mark.parametrize('original_source', ['聚宽（当前平台可见历史）', 'fixture'])
def test_public_today_comparison_never_overwrites_confirmed_history(monitor, monkeypatch, original_source):
    provider(monkeypatch)
    day = MOMENT.date()
    saved = ('399006', str(day), 2345.123456789, original_source,
             'https://example.test/original', '2026-11-10T10:00:00+00:00')
    with monitor.db() as db:
        db.execute('INSERT INTO points VALUES (?,?,?,?,?,?)', saved)
    monitor.refresh(background=False, moment=MOMENT)
    with monitor.db() as db:
        assert tuple(db.execute("SELECT * FROM points WHERE code='399006' AND trade_date=?",
                                (str(day),)).fetchone()) == saved
    status = monitor.status(MOMENT)
    assert status['comparison_conflicts'] == 1
    assert status['recent_conflicts'][0]['published_close'] == saved[2]
    assert status['recent_conflicts'][0]['candidate_close'] == 2500
    assert status['error'] and '未覆盖' in status['error']
    assert status['last_success_at'] is None
    # Repeated candidates are not duplicated, and the audit survives a new service.
    monitor.refresh(background=False, moment=MOMENT)
    replacement = m.GrowthDividend(monitor.settings, client_factory=lambda: nullcontext(object()))
    assert replacement.status(MOMENT)['comparison_conflicts'] == 1
    replacement.stop()


def test_equal_public_today_does_not_replace_joinquant_provenance(monitor, monkeypatch):
    provider(monkeypatch)
    saved = ('399006', str(MOMENT.date()), 2500.0, '聚宽（当前平台可见历史）',
             'https://example.test/original', '2026-11-10T10:00:00+00:00')
    with monitor.db() as db:
        db.execute('INSERT INTO points VALUES (?,?,?,?,?,?)', saved)
    monitor.refresh(background=False, moment=MOMENT)
    with monitor.db() as db:
        assert tuple(db.execute("SELECT * FROM points WHERE code='399006' AND trade_date=?",
                                (str(MOMENT.date()),)).fetchone()) == saved
    assert monitor.status(MOMENT)['comparison_conflicts'] == 0
    assert monitor.status(MOMENT)['error'] is None


def test_interior_calendar_omission_cannot_hide_a_known_missing_session(monitor, monkeypatch):
    missing = date(2020, 1, 2)
    provider(monkeypatch, failures={(index.code, missing) for index in m.INDEXES})
    monitor.refresh(background=False, moment=MOMENT)
    assert monitor.status(MOMENT)['pending_dates'] == 1
    # The new response has the same end date but silently omits an interior session.
    provider(monkeypatch, days=[day for day in DAYS if day != missing])
    monitor.refresh(background=False, moment=MOMENT)
    with monitor.db() as db:
        assert db.execute('SELECT 1 FROM calendar WHERE trade_date=?', (str(missing),)).fetchone()
    status = monitor.status(MOMENT)
    assert status['pending_dates'] == 1
    assert status['calendar_error'] and '已确认交易日' in status['calendar_error']
    assert status['last_success_at'] is None


def test_all_three_indices_missing_same_interior_day_are_retried_after_restart(monitor, monkeypatch):
    missing = date(2020, 1, 2)
    failures = {(index.code, missing) for index in m.INDEXES}
    calls = provider(monkeypatch, failures=failures)
    monitor.refresh(background=False, moment=MOMENT)
    status = monitor.status(MOMENT)
    assert status['pending_dates'] == 1 and status['pending_points'] == 3
    assert status['last_success_at'] is None
    assert monitor.history(MOMENT)['points'][2]['chinext_dividend_ratio'] is None
    assert monitor.history(MOMENT)['latest']['date'] == '2026-11-10'
    failures.clear()
    calls.clear()
    replacement = m.GrowthDividend(monitor.settings, client_factory=lambda: nullcontext(object()))
    replacement.refresh(background=False, moment=MOMENT.replace(day=11, hour=10))
    assert len(calls) == 3 and all(start == end == missing for _, start, end, _ in calls)
    assert replacement.status(MOMENT)['pending_points'] == 0
    assert replacement.status(MOMENT)['error'] is None
    replacement.stop()


def test_hole_deleted_from_old_complete_history_is_detected_without_watermark(monitor, monkeypatch):
    calls = provider(monkeypatch)
    monitor.refresh(background=False, moment=MOMENT)
    with monitor.db() as db:
        db.execute("DELETE FROM points WHERE code='000922' AND trade_date='2018-01-02'")
    calls.clear()
    monitor.refresh(background=False, moment=MOMENT.replace(day=11, hour=10))
    assert [(code, str(start), str(end)) for code, start, end, _ in calls] == [
        ('000922', '2018-01-02', '2018-01-02')]
    assert monitor.history(MOMENT)['points'][0]['chinext_dividend_ratio'] == .5


def test_before_18_excludes_intraday_and_future_calendar_days(monitor, monkeypatch):
    calls = provider(monkeypatch)
    moment = MOMENT.replace(hour=17, minute=59)
    monitor.refresh(background=False, moment=moment)
    assert monitor.history(moment)['latest']['date'] == '2026-11-09'
    assert all(day < date(2026, 11, 10) for _, _, _, days in calls for day in days)
    assert monitor.status(moment)['pending_points'] == 0
    # UTC server clock is converted to China time, including the date boundary.
    assert m.close_cutoff(datetime.fromisoformat('2026-11-10T09:59:00+00:00')) == date(2026, 11, 9)
    assert m.close_cutoff(datetime.fromisoformat('2026-11-10T10:00:00+00:00')) == date(2026, 11, 10)


def test_calendar_outage_never_advances_success_or_erases_saved_values(monitor, monkeypatch):
    provider(monkeypatch)
    monitor.refresh(background=False, moment=MOMENT)
    before = monitor.history(MOMENT)
    def offline(client):
        raise httpx.ReadTimeout('fixture offline')
    monkeypatch.setattr(m, 'fetch_calendar', offline)
    monitor.refresh(background=False, moment=MOMENT + timedelta(minutes=10))
    data = monitor.history(MOMENT)
    assert data['points'] == before['points']
    assert data['status']['last_success_at'] == before['status']['last_success_at']
    assert data['status']['calendar_error'] and data['status']['error']


@pytest.mark.parametrize('rolled_back_through', [date(2025, 12, 31), date(2026, 11, 30)])
def test_rolled_back_calendar_preserves_cached_sessions_and_still_recovers_known_gaps(
        monitor, monkeypatch, rolled_back_through):
    calls = provider(monkeypatch)
    monitor.refresh(background=False, moment=MOMENT)
    original_state = monitor.read_state()
    with monitor.db() as db:
        original_calendar = [row[0] for row in db.execute('SELECT trade_date FROM calendar ORDER BY trade_date')]
        db.execute("DELETE FROM points WHERE code='000922' AND trade_date='2026-11-09'")
    assert monitor.status(MOMENT)['pending_points'] == 1
    calls.clear()
    monkeypatch.setattr(m, 'fetch_calendar', lambda client: sorted(set(
        [date(2017, 12, 29), rolled_back_through] + [day for day in DAYS if day <= rolled_back_through])))
    later = MOMENT + timedelta(hours=1)
    monkeypatch.setattr(m, 'now', lambda: later)
    monitor.refresh(background=False, moment=later)
    with monitor.db() as db:
        assert [row[0] for row in db.execute('SELECT trade_date FROM calendar ORDER BY trade_date')] == original_calendar
    state = monitor.read_state()
    assert state['calendar_through'] == original_state['calendar_through'] == '2026-12-31'
    assert state['calendar_checked_at'] == original_state['calendar_checked_at']
    assert state['last_success_at'] == original_state['last_success_at']
    status = monitor.status(later)
    assert status['calendar_ready'] and status['calendar_error'] and status['error']
    assert status['pending_points'] == 0
    assert any(code == '000922' and date(2026, 11, 9) in days for code, _, _, days in calls)
    assert monitor.history(later)['latest']['date'] == '2026-11-10'


def test_partial_provider_response_keeps_other_indices_and_marks_pending(monitor, monkeypatch):
    provider(monkeypatch, failures={('000922', date(2026, 11, 10))})
    monitor.refresh(background=False, moment=MOMENT)
    latest = monitor.history(MOMENT)['latest']
    assert latest['chinext'] == 2500 and latest['star50'] == 1250
    assert latest['dividend'] is None
    assert latest['chinext_dividend_ratio'] is None and latest['star50_dividend_ratio'] is None
    assert monitor.status(MOMENT)['pending_points'] == 1


def test_empty_provider_does_not_mark_calendar_backfill_successful(monitor, monkeypatch):
    provider(monkeypatch)
    monkeypatch.setattr(m, 'fetch_prices', lambda *args, **kwargs: {})
    monitor.refresh(background=False, moment=MOMENT)
    data = monitor.history(MOMENT)
    assert data['points'] == [] and data['latest'] is None
    assert data['status']['pending_points'] == 13
    assert data['status']['last_success_at'] is None and data['status']['error']


def test_cancel_keeps_committed_window_and_resume_uses_remaining_gaps(monitor, monkeypatch):
    provider(monkeypatch)
    write = monitor.write_state
    cancelled = []
    def checkpoint(state, db=None):
        write(state, db)
        if db is None and state.get('processed', 0) and state.get('active') and not cancelled:
            cancelled.append(monitor.cancel())
    monkeypatch.setattr(monitor, 'write_state', checkpoint)
    monitor.refresh(background=False, moment=MOMENT)
    assert cancelled == [True]
    status = monitor.status(MOMENT)
    assert '已停止' in status['stage'] and 0 < status['pending_points'] < 13
    with monitor.db() as db:
        saved = db.execute('SELECT count(*) FROM points').fetchone()[0]
    assert saved > 0
    monkeypatch.setattr(monitor, 'write_state', write)
    monitor.refresh(background=False, moment=MOMENT)
    assert monitor.status(MOMENT)['pending_points'] == 0
    assert not monitor.cancel()


def test_cross_process_lock_prevents_overlapping_collection(monitor, monkeypatch):
    calls = provider(monkeypatch)
    replacement = m.GrowthDividend(monitor.settings, client_factory=lambda: nullcontext(object()))
    lock = monitor.acquire()
    try:
        assert replacement.running()
        assert not replacement.refresh(background=False, moment=MOMENT)
        assert calls == []
    finally:
        lock.release()
        replacement.stop()


def test_startup_backfills_once_and_scheduler_runs_daily_without_fabricated_sessions(monitor, monkeypatch):
    monitor.settings.growth_dividend_worker_enabled = True
    calls = provider(monkeypatch)
    monitor.tick(MOMENT.replace(hour=17))
    assert calls and monitor.history(MOMENT.replace(hour=17))['latest']['date'] == '2026-11-09'
    calls.clear()
    monitor.tick(MOMENT.replace(hour=17, minute=59))
    assert calls == []
    monitor.tick(MOMENT.replace(hour=18, minute=0))
    assert len(calls) == 3 and monitor.history(MOMENT)['latest']['date'] == '2026-11-10'
    calls.clear()
    monitor.tick(MOMENT.replace(hour=19))
    # Daily scheduling includes holidays/weekends but creates no invented prices.
    monitor.tick(MOMENT.replace(day=11, hour=18))
    monitor.tick(MOMENT.replace(day=14, hour=18))
    assert calls == []
    assert monitor.read_state()['scheduled_slot'] == m.slot_for(MOMENT.replace(day=14).date())


def test_failed_daily_slot_waits_for_next_trading_day_then_recovers(monitor, monkeypatch):
    days = DAYS + [date(2026, 11, 11)]
    failures = {('000922', date(2026, 11, 10))}
    calls = provider(monkeypatch, days=days, failures=failures)
    monitor.refresh(background=False, moment=MOMENT, slot=m.slot_for(MOMENT.date()), trigger='scheduled')
    assert monitor.status(MOMENT)['pending_points'] == 1
    calls.clear()
    assert not monitor.refresh(background=False, moment=MOMENT, slot=m.slot_for(MOMENT.date()))
    assert calls == []
    failures.clear()
    next_day = MOMENT + timedelta(days=1)
    assert monitor.refresh(background=False, moment=next_day, slot=m.slot_for(next_day.date()), trigger='scheduled')
    assert monitor.status(next_day)['pending_points'] == 0
    assert any(code == '000922' and date(2026, 11, 10) in days for code, _, _, days in calls)


def test_abandoned_run_is_visible_and_restart_resumes_it(monitor, monkeypatch):
    provider(monkeypatch)
    monitor.write_state({'active': True, 'run_id': 'a' * 32, 'stage': '更新中'})
    assert '中断' in monitor.status(MOMENT)['stage']
    monitor.tick(MOMENT)
    assert not monitor.status(MOMENT)['running']
    assert monitor.status(MOMENT)['pending_points'] == 0


@pytest.mark.parametrize('cached_through', [None, '2025-12-31'])
def test_expired_new_or_extended_calendar_backfills_known_history_without_claiming_success(
        monitor, monkeypatch, cached_through):
    known_days = DAYS + [date(2025, 12, 31), date(2026, 12, 31)]
    calls = provider(monkeypatch, days=known_days)
    previous_success = None
    if cached_through:
        previous_success = '2025-12-31T18:05:00+08:00'
        with monitor.db() as db:
            db.executemany('INSERT INTO calendar VALUES (?)',
                           [(str(day),) for day in known_days if str(day) <= cached_through])
        monitor.write_state({'calendar_through': cached_through, 'last_success_at': previous_success})
    moment = datetime(2027, 1, 4, 18, tzinfo=m.CHINA)
    monitor.refresh(background=False, moment=moment)
    status = monitor.status(moment)
    assert not status['calendar_ready'] and status['calendar_error']
    assert status['last_success_at'] == previous_success
    assert calls and all(end.year <= 2026 for _, _, end, _ in calls)
    assert monitor.read_state()['calendar_through'] == '2026-12-31'
    data = monitor.history(moment)
    assert (data['start_date'], data['end_date']) == ('2018-01-02', '2026-12-31')
    assert len(data['points']) == len(known_days)
    assert data['latest']['chinext_dividend_ratio'] == .5
    assert status['pending_points'] == 0 and status['error']


def test_authenticated_api_reads_offline_and_uses_existing_csrf_protection(client, monkeypatch):
    service = client.app.state.growth_dividend
    monkeypatch.setattr(service, 'refresh', lambda: True)
    monkeypatch.setattr(service, 'cancel', lambda: False)
    response = client.get('/api/v1/growth-dividend/history')
    assert response.status_code == 200 and response.json()['points'] == []
    assert response.headers['Cache-Control'] == 'no-store'
    assert client.post('/api/v1/growth-dividend/refresh').json() == {'started': True}
    assert client.post('/api/v1/growth-dividend/cancel').json() == {'cancelled': False}
    client.headers.pop('X-CSRF-Token')
    assert client.post('/api/v1/growth-dividend/refresh').status_code == 403
