"""Local-only collection/scheduling tests; no real database or provider requests."""
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
import threading

from fastapi.testclient import TestClient
import pytest

from app.core.config import Settings
from app.main import create_app
from app.services import margin_guarantee as m


MOMENT = datetime(2026, 10, 28, 20, tzinfo=m.CHINA)
BASE = '/api/v1/margin-guarantee'


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'now', lambda: MOMENT)
    settings = Settings(_env_file=None, log_dir=tmp_path, margin_worker_enabled=False,
                        cache_enabled=False, social_worker_enabled=False)
    service = m.MarginGuarantee(settings, client_factory=lambda: nullcontext(object()), delay=0)
    yield service
    service.stop()


def report(day):
    return m.MarginReport(day, 'https://www.csdata.cn/cmsmc/articleFileDir/test/' + str(day) + '.xlsx')


def provider(monkeypatch, days):
    calls = {'lists': [], 'points': []}
    def listing(client, start, end, check_cancel):
        check_cancel()
        calls['lists'].append((start, end))
        return [report(day) for day in days if start <= day <= end]
    def point(client, item):
        calls['points'].append(item.trade_date)
        return {'date': item.trade_date.isoformat(), 'ratio': 255.6273, 'source_url': item.url}
    monkeypatch.setattr(m, 'list_reports', listing)
    monkeypatch.setattr(m, 'fetch_point', point)
    return calls


def save_points(service, values):
    with service.db() as db:
        db.executemany('INSERT INTO points VALUES (?,?,?,?)', [
            (day.isoformat(), ratio, report(day).url, MOMENT.isoformat()) for day, ratio in values])


def test_first_backfill_then_incremental_never_redownloads_saved_days(monitor, monkeypatch):
    days = [date(2020, 10, 28), date(2026, 10, 26), date(2026, 10, 27)]
    calls = provider(monkeypatch, days)
    assert monitor.refresh(background=False, moment=MOMENT)
    assert calls['lists'] == [(date(2020, 10, 28), date(2026, 10, 28))]
    assert calls['points'] == sorted(days, reverse=True)
    assert monitor.history(MOMENT)['summary']['observed_days'] == 3
    assert monitor.status(MOMENT)['pending_reports'] == 0
    saved = monitor.history(MOMENT)['points']
    assert monitor.refresh(background=False, moment=MOMENT)
    assert calls['lists'][-1] == (date(2026, 10, 20), date(2026, 10, 28))
    assert calls['points'] == sorted(days, reverse=True)
    assert monitor.history(MOMENT)['points'] == saved
    days.append(date(2026, 10, 28))
    assert monitor.refresh(background=False, moment=MOMENT)
    assert calls['points'][-1] == date(2026, 10, 28)
    assert len(calls['points']) == 4


def test_failed_old_day_is_retried_after_restart_outside_new_listing_window(monitor, monkeypatch):
    old, latest = date(2021, 1, 4), date(2026, 10, 27)
    calls = provider(monkeypatch, [old, latest])
    original = m.fetch_point
    def fail_old(client, item):
        if item.trade_date == old:
            raise TimeoutError('synthetic request timeout')
        return original(client, item)
    monkeypatch.setattr(m, 'fetch_point', fail_old)
    monitor.refresh(background=False, moment=MOMENT)
    assert monitor.status(MOMENT)['pending_reports'] == 1
    assert monitor.status(MOMENT)['error']
    assert monitor.history(MOMENT)['latest']['date'] == str(latest)
    replacement = m.MarginGuarantee(monitor.settings, client_factory=lambda: nullcontext(object()), delay=0)
    monkeypatch.setattr(m, 'fetch_point', original)
    replacement.refresh(background=False, moment=MOMENT)
    assert calls['lists'][-1][0] == latest-timedelta(days=7)
    assert calls['points'] == [latest, old]
    assert replacement.status(MOMENT)['pending_reports'] == 0
    assert replacement.status(MOMENT)['error'] is None
    assert replacement.history(MOMENT)['summary']['observed_days'] == 2


def test_cancel_preserves_committed_day_and_resume_downloads_only_pending(monitor, monkeypatch):
    days = [date(2026, 10, 26), date(2026, 10, 27)]
    calls = provider(monkeypatch, days)
    original_write = monitor.write_state
    cancelled = []
    def checkpoint(state, db=None):
        original_write(state, db)
        if state.get('processed') == 1 and state.get('active') and not cancelled:
            cancelled.append(monitor.cancel())
    monkeypatch.setattr(monitor, 'write_state', checkpoint)
    monitor.refresh(background=False, moment=MOMENT)
    assert cancelled == [True]
    status = monitor.status(MOMENT)
    assert not status['running'] and not status['cancelling'] and status['error'] is None
    assert status['pending_reports'] == 1 and '已停止' in status['stage']
    assert calls['points'] == [days[-1]]
    monkeypatch.setattr(monitor, 'write_state', original_write)
    monitor.refresh(background=False, moment=MOMENT)
    assert calls['points'] == [days[-1], days[0]]
    assert monitor.history(MOMENT)['summary']['observed_days'] == 2
    assert not monitor.cancel()


def test_network_failure_preserves_history_and_stale_date(monitor, monkeypatch):
    save_points(monitor, [(date(2026, 10, 26), 255.6273)])
    def failed(*args, **kwargs):
        raise TimeoutError('synthetic offline')
    monkeypatch.setattr(m, 'list_reports', failed)
    monitor.refresh(background=False, moment=MOMENT)
    data = monitor.history(MOMENT)
    assert data['latest']['date'] == '2026-10-26'
    assert data['latest']['ratio'] == 255.6273
    assert data['summary']['change_pp'] is None
    assert data['status']['error'] and not data['status']['running']
    assert not monitor.read_state().get('catalog_ready')


def test_abandoned_process_state_resumes_committed_checkpoint(monitor, monkeypatch):
    old, latest = date(2021, 1, 4), date(2026, 10, 27)
    save_points(monitor, [(latest, 255.6273)])
    with monitor.db() as db:
        db.executemany('INSERT INTO reports VALUES (?,?)', [(str(day), report(day).url) for day in [old, latest]])
    monitor.write_state({'catalog_ready': True, 'active': True, 'processed': 1,
                         'run_id': 'a'*32, 'stage': '读取中证日报 2021-01-04'})
    assert '中断' in monitor.status(MOMENT)['stage']
    calls = provider(monkeypatch, [])
    monitor.refresh(background=False, moment=MOMENT)
    assert calls['points'] == [old]
    assert monitor.history(MOMENT)['summary']['observed_days'] == 2
    assert monitor.status(MOMENT)['error'] is None


def test_repeated_provider_failure_stops_round_without_losing_success(monitor, monkeypatch):
    days = [date(2026, 10, day) for day in range(23, 29)]
    provider(monkeypatch, days)
    attempts = []
    def point(client, item):
        attempts.append(item.trade_date)
        if item.trade_date != days[-1]:
            raise TimeoutError('synthetic provider outage')
        return {'date': str(item.trade_date), 'ratio': 255}
    monkeypatch.setattr(m, 'fetch_point', point)
    monitor.refresh(background=False, moment=MOMENT)
    assert attempts == sorted(days, reverse=True)[:4]
    assert monitor.history(MOMENT)['summary']['observed_days'] == 1
    assert monitor.status(MOMENT)['pending_reports'] == 5
    assert monitor.status(MOMENT)['error']


@pytest.mark.parametrize('bad_ratio', [None, float('nan'), float('inf'), -1, 0, 10000])
def test_invalid_daily_value_is_not_published(monitor, monkeypatch, bad_ratio):
    provider(monkeypatch, [date(2026, 10, 28)])
    monkeypatch.setattr(m, 'fetch_point', lambda client, item: {'date': str(item.trade_date), 'ratio': bad_ratio})
    monitor.refresh(background=False, moment=MOMENT)
    assert monitor.history(MOMENT)['latest'] is None
    assert monitor.status(MOMENT)['pending_reports'] == 1
    assert monitor.status(MOMENT)['error']


def test_date_mismatch_does_not_publish_official_value_under_wrong_day(monitor, monkeypatch):
    provider(monkeypatch, [date(2026, 10, 28)])
    monkeypatch.setattr(m, 'fetch_point', lambda client, item: {'date': '2026-10-27', 'ratio': 255})
    monitor.refresh(background=False, moment=MOMENT)
    assert monitor.history(MOMENT)['points'] == []
    assert monitor.status(MOMENT)['pending_reports'] == 1


def test_empty_history_and_six_year_stats_keep_percent_units_and_strict_rank(monitor, monkeypatch):
    empty = monitor.history(MOMENT)
    assert empty['latest'] is None and empty['summary']['percentile'] is None
    assert empty['summary']['observed_days'] == 0
    save_points(monitor, [(date(2020, 10, 27), 50), (date(2020, 10, 28), 222),
                          (date(2026, 10, 23), 280), (date(2026, 10, 26), 255.6273),
                          (date(2026, 10, 28), 255.6273), (date(2026, 10, 29), 500)])
    data = monitor.history(MOMENT)
    summary = data['summary']
    assert summary['observed_days'] == 4 and summary['below_count'] == 1
    assert summary['percentile'] == 25
    assert summary['change_pp'] == 0
    assert summary['distance_pp'] == pytest.approx(15.6273)
    assert (summary['low_ratio'], summary['low_date']) == (222, '2020-10-28')
    assert (summary['peak_ratio'], summary['peak_date']) == (280, '2026-10-23')
    assert data['start_date'] == '2020-10-28' and data['end_date'] == '2026-10-28'
    provider(monkeypatch, [])
    monitor.refresh(background=False, moment=MOMENT)
    with monitor.db() as db:
        assert not db.execute("SELECT 1 FROM points WHERE trade_date='2020-10-27'").fetchone()
    assert m.six_years_ago(date(2024, 2, 29)) == date(2018, 2, 28)


def test_fixed_china_20_slot_and_cross_worker_schedule_claim(monitor, monkeypatch):
    calls = provider(monkeypatch, [])
    another = m.MarginGuarantee(monitor.settings, client_factory=lambda: nullcontext(object()), delay=0)
    before = MOMENT-timedelta(seconds=1)
    assert m.latest_slot(MOMENT.astimezone(timezone.utc)) == MOMENT
    assert m.latest_slot(before) == MOMENT-timedelta(days=1)
    monitor.tick(before)
    another.tick(before)
    assert calls['lists'] == []
    monitor.tick(MOMENT)
    another.tick(MOMENT)
    monitor.tick(MOMENT+timedelta(minutes=10))
    assert len(calls['lists']) == 1
    assert monitor.read_state()['scheduled_slot'] == MOMENT.isoformat()
    another.tick(MOMENT+timedelta(days=1))
    assert len(calls['lists']) == 2


def test_start_after_20_installs_scheduler_without_startup_backfill(monitor, monkeypatch):
    monitor.settings.margin_worker_enabled = True
    called = []
    monkeypatch.setattr(monitor, 'refresh', lambda *a, **kw: called.append(kw))
    class DormantThread:
        def __init__(self, **kwargs):
            self.target = kwargs['target']
        def start(self):
            pass
        def join(self):
            pass
    monkeypatch.setattr(m.threading, 'Thread', DormantThread)
    monitor.start()
    monitor.tick(MOMENT+timedelta(minutes=30))
    assert called == []
    assert monitor.status(MOMENT)['next_run_at'] == '2026-10-29T20:00:00+08:00'
    monitor.tick(MOMENT+timedelta(days=1))
    assert len(called) == 1


def test_running_lock_rejects_other_worker_and_cancel_visible_across_instances(monitor, monkeypatch):
    calls = provider(monkeypatch, [date(2026, 10, 28)])
    entered, release = threading.Event(), threading.Event()
    original = m.list_reports
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(m, 'list_reports', blocked)
    another = m.MarginGuarantee(monitor.settings, client_factory=lambda: nullcontext(object()), delay=0)
    try:
        assert monitor.refresh(moment=MOMENT)
        assert entered.wait(5)
        assert another.status(MOMENT)['running']
        assert not another.refresh(background=False, moment=MOMENT)
        assert another.cancel()
        assert monitor.status(MOMENT)['cancelling']
    finally:
        release.set()
        if monitor.worker:
            monitor.worker.join(5)
    assert not monitor.status(MOMENT)['running']
    assert monitor.status(MOMENT)['error'] is None
    assert calls['points'] == []


def test_api_lifespan_is_local_read_only_until_explicit_refresh(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'now', lambda: MOMENT)
    settings = Settings(_env_file=None, log_dir=tmp_path, margin_worker_enabled=False,
                        social_worker_enabled=False, cache_enabled=False)
    calls = provider(monkeypatch, [])
    app = create_app(settings, engine)
    with TestClient(app) as client:
        service = app.state.margin_guarantee
        history = client.get(BASE+'/history')
        assert history.status_code == 200 and history.json()['latest'] is None
        assert history.headers['cache-control'] == 'no-store'
        assert client.get(BASE+'/status').json()['daily_time'] == '20:00'
        assert calls['lists'] == []
        assert client.post(BASE+'/cancel').json() == {'cancelled': False}
        real_refresh = service.refresh
        monkeypatch.setattr(service, 'client_factory', lambda: nullcontext(object()))
        monkeypatch.setattr(service, 'refresh', lambda: real_refresh(background=False, moment=MOMENT))
        response = client.post(BASE+'/refresh')
        assert response.status_code == 202 and response.json() == {'started': True}
        assert len(calls['lists']) == 1
    assert service.stop_event.is_set()
