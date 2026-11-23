from datetime import date, datetime, timedelta, timezone
from contextlib import nullcontext
from types import SimpleNamespace

import portalocker
import pytest

from app.core.config import Settings
from app.services.etf_collection import ETFCollection, CHINA, scheduled_slots


DAY = date(2026, 11, 6)
EVENING = datetime(2026, 11, 6, 18, 30, tzinfo=CHINA)


class Calendar:
    def default_range(self, count):
        return DAY - timedelta(days=1), DAY

    def range(self, start, end):
        return [start, end]


class Collector:
    def __init__(self):
        self.calls = []
        self.writes = []
        self.failure = False
        self.write_failure = False
        self.calendar_targets = []

    def catalog(self):
        return {'510300.XSHG': '沪', '159919.XSHE': '深'}

    def ExchangeClient(self, http):
        return SimpleNamespace(calendar=lambda: Calendar())

    def collect(self, client, start, end, funds, include_prices, include_shares, calendar):
        self.calls.append((start, funds, include_prices, include_shares))
        if self.failure:
            raise ValueError('do not expose secret')
        rows = [dict(fund_code=code, trade_date=start) for code in funds]
        return rows if include_shares else [], rows if include_prices else [], {}

    def existing_observations(self, engine, targets):
        return set()

    def write_records(self, engine, shares, prices, calendar, *, calendar_target, insert_only=False):
        self.calendar_targets.append(calendar_target)
        if self.write_failure:
            raise RuntimeError('secret writer DSN')
        self.writes.append((shares, prices))


def worker(tmp_path, *, schedule=None, **kwargs):
    settings = Settings(_env_file=None, log_dir=tmp_path, etf_collection_enabled=True,
                        db_user='etf_writer', db_password='', etf_collection_lookback_days=2)
    c = Collector()
    disposed = []
    w = ETFCollection(settings, collector=c, client_factory=lambda: nullcontext(None),
                      engine_factory=lambda *a, **k: SimpleNamespace(dispose=lambda: disposed.append(True)),
                      clock=lambda: EVENING, **kwargs)
    # Most historical regressions exercise an explicitly configured evening run.
    # The production default morning schedule has its own coverage below.
    load = w.configuration.load
    def configured_load():
        config = load()
        return config | {'schedule': schedule or ['18:30']} if config['revision'] == 0 else config
    w.configuration.load = configured_load
    return w, c, disposed


def test_timezone_and_naive():
    assert scheduled_slots(EVENING.astimezone(timezone.utc)) == [EVENING.replace(hour=9, minute=0)]
    with pytest.raises(ValueError):
        scheduled_slots(datetime(2026, 11, 6))


def test_disabled_and_missing_configuration(tmp_path):
    w = ETFCollection(Settings(_env_file=None, log_dir=tmp_path, db_user='', etf_collection_enabled=False))
    w.start()
    assert w.status()['enabled'] is False
    assert w.status()['ready'] is False
    assert w.status()['disabled_reason'] == 'missing_writer_configuration'
    assert w.worker is not None
    w.settings.etf_collection_enabled = True
    w.start()
    assert w.status()['status'] == 'missing_writer_configuration'
    assert w.tick(EVENING) is False
    w.stop()


def test_no_startup_network_or_07_slot(tmp_path):
    w, c, _ = worker(tmp_path)
    w.start()
    w.stop()
    assert not c.calls
    assert not w.tick(EVENING)  # startup floor excludes the past slot
    w.floor = None
    assert not w.tick(EVENING.replace(hour=7))


def test_evening_idempotency_only_updates_latest_and_preserves_legacy_history(tmp_path):
    w, c, disposed = worker(tmp_path)
    old = ('prices', '159919.XSHE', '2026-11-05', 'blocked_unverified_history', 0)
    with w.connect() as db:
        db.execute('INSERT INTO observations VALUES (?,?,?,?,?)', old)
    assert w.tick(EVENING)
    assert not w.tick(EVENING + timedelta(minutes=31))
    assert len(c.writes) == len(disposed) == 1
    assert not w.status()['gaps']  # Daily status does not pretend to measure JoinQuant history.
    assert {day for day, *_ in c.calls} == {DAY}
    with w.connect() as db:
        assert tuple(db.execute("SELECT * FROM observations WHERE day='2026-11-05'").fetchone()) == old
    w2, c2, _ = worker(tmp_path)
    assert not w2.tick(EVENING)
    assert not c2.calls


def test_morning_collects_previous_completed_day_shares_and_prices(tmp_path):
    w, c, _ = worker(tmp_path, schedule=['09:00'])
    assert w.tick(EVENING + timedelta(days=3, hours=-9))
    assert all(call[0] <= DAY for call in c.calls)
    assert any(call[2] for call in c.calls) and any(call[3] for call in c.calls)


def test_failed_write_never_advances_complete_and_redacts(tmp_path):
    w, c, disposed = worker(tmp_path)
    c.write_failure = True
    assert not w.tick(EVENING)
    assert disposed
    state = w.status()
    assert state['runs'][0]['error'] == 'writer_write_failed'
    assert all(g['status'] != 'complete' for g in state['gaps'])
    assert 'secret' not in str(state)
    c.write_failure = False
    assert not w.tick(EVENING + timedelta(minutes=29))
    assert w.tick(EVENING + timedelta(minutes=30))


def test_bounded_retry_and_crash_recovery(tmp_path):
    w, c, _ = worker(tmp_path)
    with w.connect() as db:
        db.execute('INSERT INTO slots VALUES (?,?,?,?,?)',
                   (EVENING.isoformat(), 'running', 1, EVENING.isoformat(), None))
    assert w.tick(EVENING + timedelta(minutes=30))
    assert w.status()['runs'][0]['attempts'] == 2
    assert not w.tick(EVENING + timedelta(hours=1))


def test_cross_process_lock_prevents_any_fetch(tmp_path):
    w, c, _ = worker(tmp_path)
    w.root.mkdir(parents=True)
    with portalocker.Lock(str(w.root / 'collection.lock'), timeout=0):
        assert not w.tick(EVENING)
    assert not c.calls


def test_cache_pending_retries_without_network(tmp_path):
    calls = []
    w, c, _ = worker(tmp_path, refresh=lambda: calls.append(1) or False)
    assert w.tick(EVENING)
    assert calls == [1]
    assert w.status()['metadata']['cache_pending'] == '1'
    fetch_count = len(c.calls)
    w.clock = lambda: EVENING + timedelta(minutes=5)
    w.refresh = lambda: calls.append(1) or True
    assert not w.tick(EVENING + timedelta(minutes=5))
    assert len(c.calls) == fetch_count
    assert calls == [1, 1]
    assert w.status()['metadata']['cache_pending'] == '0'


def test_fresh_calendar_failure_no_writer(tmp_path):
    w, c, disposed = worker(tmp_path)
    def fail():
        raise ValueError('unpublished calendar')
    c.ExchangeClient = lambda http: SimpleNamespace(calendar=fail)
    assert not w.tick(EVENING)
    assert not c.calls and not disposed


def test_latest_gap_exhaustion_does_not_hide_successful_daily_observations(tmp_path):
    w, c, _ = worker(tmp_path)
    def partial(client, start, end, funds, include_prices, include_shares, calendar):
        if include_shares and '510300.XSHG' in funds:
            return [], [], {}
        rows = [dict(fund_code=code, trade_date=start) for code in funds]
        return rows if include_shares else [], rows if include_prices else [], {}
    c.collect = partial
    for i in (0, 3, 4, 5, 6, 7):
        assert not w.tick(EVENING + timedelta(days=i))
    gaps = w.status()['gaps']
    assert gaps == [dict(kind='shares', code='510300.XSHG', day=DAY.isoformat(),
                         status='retry_exhausted', attempts=6)]
    with w.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM observations WHERE status='complete'").fetchone()[0] == 3


def test_month_boundary_slot_in_shanghai(tmp_path):
    # 01:30 UTC on Jan 1 is 09:30 in Shanghai, no 07 cache schedule collision.
    moment = datetime(2027, 1, 1, 1, 30, tzinfo=timezone.utc)
    assert scheduled_slots(moment)[0].isoformat() == '2027-01-01T09:00:00+08:00'
    assert all(slot.hour != 7 for slot in scheduled_slots(moment))


def test_collection_status_endpoint(client):
    response = client.get('/api/v1/etf-shares/collection-status')
    assert response.status_code == 200
    assert response.json()['disabled_reason'] is None
    assert response.json()['ready'] is True
    assert 'writer_url' not in response.text.lower()



def test_weekend_latest_close_price_can_be_recovered(tmp_path):
    w, c, _ = worker(tmp_path)
    weekend = EVENING + timedelta(days=1)
    assert not w.tick(weekend)
    w.clock = lambda: weekend
    w.start = lambda: None
    w.request_run('weekend-manual')
    assert w.tick(weekend)
    assert any(day == DAY and '159919.XSHE' in funds and prices
               for day, funds, prices, shares in c.calls)
    assert all(g['day'] != DAY.isoformat() for g in w.status()['gaps'])


def test_partial_latest_data_gets_one_delayed_retry(tmp_path):
    w, c, _ = worker(tmp_path)
    real = c.collect
    def partial(*args, **kwargs):
        shares, prices, gaps = real(*args, **kwargs)
        return shares, [], gaps
    c.collect = partial
    assert not w.tick(EVENING)
    assert w.status()['runs'][0]['status'] == 'partial'
    first_count = len(c.calls)
    assert not w.tick(EVENING + timedelta(minutes=29))
    assert len(c.calls) == first_count
    c.collect = real
    assert w.tick(EVENING + timedelta(minutes=30))
    assert w.status()['runs'][0]['attempts'] == 2


def test_restart_recovers_same_day_but_abandons_old_slot(tmp_path):
    w, c, _ = worker(tmp_path)
    w.floor = EVENING + timedelta(minutes=10)
    with w.connect() as db:
        for slot in (EVENING, EVENING - timedelta(days=1)):
            db.execute('INSERT INTO slots VALUES (?,?,?,?,?)',
                       (slot.isoformat(), 'running', 1, slot.isoformat(), None))
    assert w.tick(EVENING + timedelta(minutes=30))
    assert [r['status'] for r in w.status()['runs']] == ['complete', 'abandoned']


def test_prices_are_fetched_once_per_code_per_run(tmp_path):
    w, c, _ = worker(tmp_path)
    calls = []
    c.ExchangeClient = lambda http: SimpleNamespace(calendar=lambda: Calendar(),
        prices=lambda code: calls.append(code) or [])
    real = c.collect
    def collect(client, start, end, **kwargs):
        if kwargs['include_prices']:
            for code in kwargs['funds']:
                client.prices(code)
        return real(client, start, end, **kwargs)
    c.collect = collect
    assert w.tick(EVENING)
    assert calls.count('510300.XSHG') == 1
    assert calls.count('159919.XSHE') == 1


@pytest.mark.parametrize('writer', [
    'mysql+pymysql://writer@different-host:3306/showallopts',
    'mysql+pymysql://writer@127.0.0.1:3307/showallopts',
    'mysql+pymysql://writer@127.0.0.1:3306/other_db',
    'sqlite://', 'not a URL',
])
def test_writer_target_mismatch_never_starts_or_fetches(tmp_path, writer):
    from pydantic import SecretStr
    w,c,_=worker(tmp_path);w.settings.etf_collection_writer_url=SecretStr(writer)
    assert w.configured and w.tick(EVENING) and c.calls
    assert w.configuration.load()['host']=='127.0.0.1'
    assert writer not in str(w.status())


def test_status_reports_target_not_completed_coverage(tmp_path):
    w, c, _ = worker(tmp_path)
    assert w.tick(EVENING)
    status = w.status()
    assert status['target_sessions'] == 1
    assert status['historical_dates_per_run'] == 0
    assert status['collection_scope'] == 'latest_completed_session'
    assert 'history_complete' not in status
    assert status['coverage_is_target_not_completion'] is True


@pytest.mark.parametrize('hour,expected_day', [(14, DAY - timedelta(days=1)), (16, DAY)])
def test_daily_scope_ignores_legacy_history_target_and_respects_confirmed_close(tmp_path, hour, expected_day):
    from test_etf_page_configuration import save
    w, collector, _ = worker(tmp_path)
    save(w, {'lookback_days': 1500, 'batch_days': 20, 'enabled': False})
    calls = []
    class DailyCalendar(Calendar):
        def default_range(self, count):
            calls.append(count)
            assert count == 1
            return DAY, DAY
        def previous(self, day):
            return day - timedelta(days=1)
    collector.ExchangeClient = lambda http: SimpleNamespace(calendar=lambda: DailyCalendar())
    w.request_run(f'latest-only-request-{hour}')
    assert w.tick(EVENING.replace(hour=hour))
    assert calls == [1] and {day for day, *_ in collector.calls} == {expected_day}
    with w.connect() as db:
        assert [row[0] for row in db.execute('SELECT DISTINCT day FROM observations')] == [expected_day.isoformat()]
    assert w.status()['target_sessions'] == 1 and w.status()['manual']['status'] == 'complete'


def test_latest_prices_require_fresh_verification_even_when_prior_values_exist(tmp_path):
    w, collector, _ = worker(tmp_path)
    assert w.tick(EVENING)
    original = collector.collect
    def missing_price(*args, **kwargs):
        return ([], [], {}) if kwargs['include_prices'] else original(*args, **kwargs)
    collector.collect = missing_price
    assert not w.tick(EVENING + timedelta(days=3))
    assert w.status()['runs'][0]['status'] == 'partial'
    with w.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM observations WHERE kind='prices' AND status='complete'").fetchone()[0] == 2
    assert collector.writes[-1][1] == []  # Keep existing database prices, never synthesize a replacement.


def test_new_manual_click_retries_latest_exhausted_observations_without_resetting_history(tmp_path):
    from test_etf_page_configuration import save
    w, collector, _ = worker(tmp_path)
    save(w, {'enabled': False})
    with w.connect() as db:
        db.execute("INSERT INTO observations VALUES ('prices','510300.XSHG',?,'retry_exhausted',6)", (DAY.isoformat(),))
        db.execute("INSERT INTO observations VALUES ('prices','159919.XSHE','2024-01-02','retry_exhausted',6)")
    w.request_run('latest-manual-retry-1')
    assert w.tick(EVENING)
    with w.connect() as db:
        assert tuple(db.execute("SELECT status,attempts FROM observations WHERE day='2024-01-02'").fetchone()) == ('retry_exhausted',6)
        assert tuple(db.execute("SELECT status,attempts FROM observations WHERE kind='prices' AND code='510300.XSHG' AND day=?", (DAY.isoformat(),)).fetchone()) == ('complete',7)
    assert all(day == DAY for day, *_ in collector.calls)



def test_default_enabled_but_missing_writer_is_not_ready(tmp_path, caplog):
    settings = Settings(_env_file=None, log_dir=tmp_path, db_user='')
    assert settings.etf_collection_enabled is True
    w = ETFCollection(settings)
    w.start()
    assert w.worker is not None
    w.stop()
    assert w.status()['ready'] is False
    assert w.status()['status'] == 'missing_writer_configuration'
    assert 'ETF collection not ready: missing_writer_configuration' in caplog.text


@pytest.mark.parametrize(('number', 'code'), [
    (1044, 'writer_permission_denied'), (1045, 'writer_permission_denied'),
    (1142, 'writer_permission_denied'), (1143, 'writer_permission_denied'),
    (1146, 'writer_table_missing'), (1049, 'writer_database_missing'),
    (2003, 'writer_connection_failed'), (2013, 'writer_connection_failed'),
])
def test_writer_errors_are_classified_without_driver_message(tmp_path, number, code):
    from sqlalchemy.exc import OperationalError
    w, c, _ = worker(tmp_path)
    def deny(*args, **kwargs):
        raise OperationalError('secret SQL', {}, Exception(number, 'secret password and DSN'))
    c.write_records = deny
    assert not w.tick(EVENING)
    state = w.status()
    assert state['status'] == code
    assert state['ready'] is False
    assert state['runs'][0]['error'] == code
    assert 'secret' not in str(state)
    c.write_records = lambda *args, **kwargs: None
    assert w.tick(EVENING + timedelta(minutes=30))
    assert w.status()['ready'] is True
    assert w.status()['status'] == 'configured'



def test_latest_price_failure_does_not_erase_successful_shares_or_touch_history(tmp_path):
    w, c, _ = worker(tmp_path)
    old = DAY - timedelta(days=1)
    with w.connect() as db:
        db.execute('INSERT INTO observations VALUES (?,?,?,?,?)', ('shares','510300.XSHG',old.isoformat(),'complete',1))
    real = c.collect
    def late_failure(client, start, end, **kwargs):
        if kwargs['include_prices']:
            raise ValueError('later price source failed')
        return real(client, start, end, **kwargs)
    c.collect = late_failure
    for i in (0, 3, 4, 5, 6, 7):
        w.tick(EVENING + timedelta(days=i))
    with w.connect() as db:
        rows = db.execute("SELECT status,attempts FROM observations WHERE day=? AND kind='shares'",
                          (old.isoformat(),)).fetchall()
    assert rows and all(tuple(r) == ('complete', 1) for r in rows)
    assert c.writes and all(r['trade_date'] == DAY for shares, _ in c.writes for r in shares)
    old_price = next(r for r in w.status()['gaps'] if r['day'] == DAY.isoformat()
                     and r['code'] == '510300.XSHG' and r['kind'] == 'prices')
    assert old_price['status'] == 'retry_exhausted' and old_price['attempts'] == 6


def test_six_writer_failures_do_not_consume_gap_budget(tmp_path):
    w, c, _ = worker(tmp_path)
    c.write_failure = True
    for i in (0, 3, 4, 5, 6, 7):
        assert not w.tick(EVENING + timedelta(days=i))
    with w.connect() as db:
        rows = db.execute("SELECT status,attempts FROM observations WHERE kind='shares'").fetchall()
    assert rows and all(tuple(r) == ('pending', 0) for r in rows)
    c.write_failure = False
    assert w.tick(EVENING + timedelta(days=10))
    with w.connect() as db:
        rows = db.execute("SELECT status,attempts FROM observations WHERE kind='shares'").fetchall()
    assert all(tuple(r) == ('complete', 1) for r in rows)
    assert c.writes


def test_sse_share_fetch_memoized_per_date_for_multiple_funds(tmp_path):
    w, c, _ = worker(tmp_path)
    c.catalog = lambda: {'510300.XSHG': '沪1', '510310.XSHG': '沪2'}
    calls = []
    c.ExchangeClient = lambda http: SimpleNamespace(calendar=lambda: Calendar(),
        sse_shares=lambda day: calls.append(day) or [])
    real = c.collect
    def collect(client, start, end, **kwargs):
        if kwargs['include_shares']:
            client.sse_shares(start)
        return real(client, start, end, **kwargs)
    c.collect = collect
    assert w.tick(EVENING)
    assert calls == [DAY]



@pytest.mark.parametrize('failure_mode', ['exception', 'empty'])
def test_next_morning_requires_fresh_verification_not_old_complete_state(tmp_path, failure_mode):
    schedule = ['18:30']
    w, c, _ = worker(tmp_path, schedule=schedule)
    assert w.tick(EVENING)
    schedule[0] = '09:00'
    old_writes = list(c.writes)
    real = c.collect
    def unavailable(*args, **kwargs):
        if kwargs['include_shares']:
            if failure_mode == 'exception':
                raise ValueError('temporary source failure')
            return [], [], {}
        return real(*args, **kwargs)
    c.collect = unavailable
    morning = EVENING + timedelta(days=3, hours=-9)
    assert not w.tick(morning)
    assert w.status()['runs'][0]['status'] == 'partial'
    with w.connect() as db:
        rows = db.execute("SELECT status FROM observations WHERE day=? AND kind='shares'", (DAY.isoformat(),)).fetchall()
    assert rows and all(row[0] == 'complete' for row in rows)
    assert c.writes[:len(old_writes)] == old_writes
    assert c.writes[-1][0] == []  # Failed shares never replace/delete old business rows.
    assert {row['trade_date'] for row in c.writes[-1][1]} == {DAY}  # Fresh prices still commit.
    assert not w.tick(morning + timedelta(minutes=29))
    c.collect = real
    assert w.tick(morning + timedelta(minutes=30))
    assert w.status()['runs'][0]['status'] == 'complete'
    assert w.status()['runs'][0]['attempts'] == 2



def test_writer_connection_has_bounded_timeouts(tmp_path):
    w, _, _ = worker(tmp_path)
    received = []
    def factory(url, **kwargs):
        received.append(kwargs)
        return SimpleNamespace(dispose=lambda: None)
    w.engine_factory = factory
    assert w.tick(EVENING)
    assert received[0]['connect_args']=={'connect_timeout':60,'read_timeout':60,'write_timeout':60,'ssl_disabled':True}
    assert received[0]['hide_parameters'] and callable(received[0]['creator'])


def test_changing_daily_time_cannot_create_second_automatic_run(tmp_path):
    schedule = ['18:30']
    w, c, _ = worker(tmp_path, schedule=schedule)
    assert w.tick(EVENING)
    count = len(c.calls)
    schedule[0] = '19:00'
    assert not w.tick(EVENING.replace(hour=19, minute=0))
    assert len(c.calls) == count
    assert w.tick(EVENING.replace(hour=19, minute=0) + timedelta(days=3))


def test_collection_logs_partial_summary_and_safe_failure(tmp_path, caplog):
    import logging
    w, c, _ = worker(tmp_path)
    c.failure = True
    with caplog.at_level(logging.INFO):
        assert w.tick(EVENING) is False
    assert 'ETF collection start' in caplog.text
    assert 'ETF source finish' in caplog.text and 'failed=4' in caplog.text
    assert 'ETF collection finish' in caplog.text and 'outcome=partial' in caplog.text
    assert 'kind=shares' in caplog.text and 'frames=' in caplog.text
    assert 'do not expose secret' not in caplog.text


def test_collection_logs_unexpected_calendar_failure(tmp_path, caplog):
    import logging
    import httpx
    w, c, _ = worker(tmp_path)
    def failed_client(http):
        raise httpx.ConnectTimeout('private-url-and-token')
    c.ExchangeClient = failed_client
    with caplog.at_level(logging.INFO):
        assert w.tick(EVENING) is False
    assert 'ETF collection failed' in caplog.text and 'phase=calendar' in caplog.text
    assert 'reason=http_timeout' in caplog.text and 'outcome=failed' in caplog.text
    assert 'private-url-and-token' not in caplog.text


def test_manual_etf_notification_uses_shared_change_gate(tmp_path, monkeypatch):
    from app.services import collection_notifications as notifications
    monkeypatch.setattr(notifications, 'enabled', lambda *_: True)
    monkeypatch.setattr(notifications, 'start_notifications', lambda *_: None)
    w, collector, _ = worker(tmp_path)
    from test_etf_page_configuration import save
    save(w, {'enabled': False})
    original_write = collector.write_records
    def counting_write(*args, **kwargs):
        original_write(*args, **kwargs)
        return 4
    collector.write_records = counting_write
    w.request_run('manual-etf-notify')
    assert w.tick(EVENING)
    with notifications._database(w.settings) as db:
        assert db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 1
    w.request_run('manual-etf-notify')
    w.tick(EVENING)
    with notifications._database(w.settings) as db:
        assert db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 1


def test_run_history_keeps_automatic_failure_when_manual_collection_succeeds(tmp_path):
    w, c, _ = worker(tmp_path, refresh=lambda: True)
    w.start = lambda: None
    c.write_failure = True
    assert not w.tick(EVENING)
    assert w.status()['latest_result']['status'] == 'failed'
    c.write_failure = False
    w.request_run('manual-after-failed-schedule')
    assert w.tick(EVENING + timedelta(minutes=1))
    state = w.status()
    assert [(r['trigger'], r['status']) for r in state['recent_runs']] == [
        ('manual', 'complete'), ('scheduled', 'failed')]
    assert state['runs'][0]['status'] == 'failed'
    assert state['latest_result']['finished_at'] is not None
    w.request_run('manual-after-failed-schedule')
    assert len(w.status()['recent_runs']) == 2


def test_run_history_preserves_each_automatic_retry_attempt(tmp_path):
    w, c, _ = worker(tmp_path, refresh=lambda: True)
    c.write_failure = True
    assert not w.tick(EVENING)
    c.write_failure = False
    assert w.tick(EVENING + timedelta(minutes=30))
    state = w.status()
    assert [(r['status'], r['attempt']) for r in state['recent_runs']] == [('complete', 2), ('failed', 1)]
    assert state['runs'][0]['status'] == 'complete'
    assert state['runs'][0]['attempts'] == 2


def test_legacy_run_history_migration_is_idempotent_and_does_not_invent_finish_time(tmp_path):
    w, c, _ = worker(tmp_path, refresh=lambda: True)
    with w.connect() as db:
        db.execute("DELETE FROM metadata WHERE key='run_history_migrated'")
        db.execute('INSERT INTO slots VALUES (?,?,?,?,?)',
                   (EVENING.isoformat(), 'failed', 2, EVENING.isoformat(), 'source_unavailable'))
        db.execute('INSERT INTO manual_runs(id,status,requested_at) VALUES (?,?,?)',
                   ('old-manual', 'complete', (EVENING + timedelta(minutes=1)).isoformat()))
    state = w.status()
    assert [(r['trigger'], r['status']) for r in state['recent_runs']] == [('manual', 'complete'), ('scheduled', 'failed')]
    assert all(r['finished_at'] is None and r['time_basis'] == 'legacy_request_time' for r in state['recent_runs'])
    assert len(w.status()['recent_runs']) == 2
    assert state['runs'][0]['status'] == 'failed'


def test_latest_history_failure_is_not_hidden_by_previous_success(tmp_path):
    w, c, _ = worker(tmp_path, refresh=lambda: True)
    w.start = lambda: None
    assert w.status()['latest_result'] is None
    w.request_run('first-manual-success')
    assert w.tick(EVENING)
    c.write_failure = True
    w.request_run('second-manual-failure')
    assert not w.tick(EVENING + timedelta(minutes=1))
    state = w.status()
    assert [r['status'] for r in state['recent_runs']] == ['failed', 'complete']
    assert state['latest_result']['status'] == 'failed'


def test_history_does_not_report_success_before_cache_publication(tmp_path):
    w, c, _ = worker(tmp_path, refresh=lambda: False)
    assert w.tick(EVENING)  # Preserve collection/automatic scheduling semantics.
    state = w.status()
    assert state['runs'][0]['status'] == 'complete'
    assert state['cache_pending']
    assert state['latest_result']['status'] == 'partial'


def test_etf_terminal_history_and_module_indicator_use_same_latest_outcome(tmp_path):
    from app.services.collection_outcomes import latest_outcomes
    w, c, _ = worker(tmp_path, refresh=lambda: True)
    w.start = lambda: None
    w.request_run('indicator-success')
    assert w.tick(EVENING)
    assert latest_outcomes(w.settings)['items']['etf-shares']['outcome'] == 'success'
    c.write_failure = True
    w.request_run('indicator-failure')
    assert not w.tick(EVENING + timedelta(minutes=1))
    assert latest_outcomes(w.settings)['items']['etf-shares']['outcome'] == 'failed'
    assert w.status()['latest_result']['status'] == 'failed'


def test_pending_cache_does_not_turn_module_indicator_green(tmp_path):
    from app.services.collection_outcomes import latest_outcomes
    w, c, _ = worker(tmp_path, refresh=lambda: False)
    assert w.tick(EVENING)
    assert latest_outcomes(w.settings)['items']['etf-shares']['outcome'] == 'partial'


def test_recovered_manual_and_scheduled_attempts_remain_visible(tmp_path):
    w, c, _ = worker(tmp_path)
    w.start = lambda: None
    stamp = EVENING.isoformat()
    with w.connect() as db:
        db.execute('INSERT INTO manual_runs(id,status,requested_at) VALUES (?,?,?)', ('crashed-manual', 'running', stamp))
        db.execute('INSERT INTO slots VALUES (?,?,?,?,?)', (stamp, 'running', 1, stamp, None))
        # Recover while holding the same lock as the normal worker path.
        w._recover_manual_runs(db)
    rows = w.status()['recent_runs']
    assert len(rows) == 2
    assert all(row['status'] == 'interrupted' and row['time_basis'] == 'recovered_at' for row in rows)
    assert all(row['finished_at'] == stamp for row in rows)
    with w.connect() as db:
        w._recover_manual_runs(db)
    assert len(w.status()['recent_runs']) == 2


def test_crash_between_remote_completion_and_cache_publication_is_not_success(tmp_path):
    w, c, _ = worker(tmp_path, refresh=lambda: True)
    def crash(db):
        raise SystemExit('simulated process exit')
    w._refresh = crash
    w.start = lambda: None
    w.request_run('crashed-publication')
    with pytest.raises(SystemExit):
        w.tick(EVENING)
    assert w.status()['manual']['status'] == 'complete'
    assert w.status()['latest_result'] is None
    with w.connect() as db:
        assert db.execute('SELECT status FROM run_history').fetchone()[0] == 'publishing'
        w._recover_manual_runs(db)
    assert w.status()['latest_result']['status'] == 'interrupted'
    assert w.status()['latest_result']['time_basis'] == 'recovered_at'


def test_queued_cancel_is_recorded_once_without_starting_collection(tmp_path):
    w, c, _ = worker(tmp_path)
    w.start = lambda: None
    w.request_run('never-started')
    assert w.request_cancel('never-started')['status'] == 'cancelled'
    assert w.request_cancel('never-started')['status'] == 'cancelled'
    assert len(w.status()['recent_runs']) == 1
    assert w.status()['latest_result']['status'] == 'cancelled'
    assert not c.calls


def test_history_write_failure_does_not_rollback_scheduling_outcome(tmp_path):
    w, c, _ = worker(tmp_path)
    with w.connect() as db:
        db.execute('INSERT INTO slots VALUES (?,?,?,?,?)', (EVENING.isoformat(), 'complete', 1, EVENING.isoformat(), None))
        assert not w._write_history(db, 'INSERT INTO missing_history_table VALUES (?)', ('ignored',))
    assert w.status()['runs'][0]['status'] == 'complete'


def test_failure_history_survives_exit_before_cache_refresh(tmp_path):
    w, c, _ = worker(tmp_path)
    w.start = lambda: None
    c.write_failure = True
    w._refresh = lambda db: (_ for _ in ()).throw(SystemExit('simulated process exit'))
    w.request_run('failed-before-cache')
    with pytest.raises(SystemExit):
        w.tick(EVENING)
    assert w.status()['latest_result']['status'] == 'failed'
    assert w.status()['manual']['status'] == 'failed'
    from app.services.collection_outcomes import latest_outcomes
    assert latest_outcomes(w.settings)['items']['etf-shares']['outcome'] == 'unknown'
    with w.connect() as db:
        w._recover_manual_runs(db)
    assert latest_outcomes(w.settings)['items']['etf-shares']['outcome'] == 'failed'


def test_recent_results_sort_by_finish_not_start_or_manual_request_time(tmp_path):
    w, c, _ = worker(tmp_path)
    with w.connect() as db:
        # A scheduled task starts first but finishes after a queued manual cancel.
        for args in [
            ('auto', 'scheduled', 'complete', None, EVENING.isoformat(),
             (EVENING + timedelta(minutes=3)).isoformat(), EVENING.isoformat(), 1, 'completed_at'),
            ('cancel', 'manual', 'cancelled', None, (EVENING + timedelta(minutes=1)).isoformat(),
             (EVENING + timedelta(minutes=2)).isoformat(), None, None, 'completed_at'),
            ('legacy', 'manual', 'failed', None, (EVENING + timedelta(days=1)).isoformat(),
             None, None, None, 'legacy_request_time'),
        ]:
            db.execute('INSERT INTO run_history VALUES (?,?,?,?,?,?,?,?,?)', args)
    assert [row['id'] for row in w.status()['recent_runs']] == ['auto', 'cancel', 'legacy']
    assert w.status()['latest_result']['id'] == 'auto'


def test_repeated_old_cancel_does_not_get_a_new_completion_time(tmp_path):
    from app.services.collection_outcomes import latest_outcomes, database
    w, c, _ = worker(tmp_path, refresh=lambda: True)
    w.start = lambda: None
    w.request_run('old-cancel')
    w.request_cancel('old-cancel')
    original_stamp = w.status()['latest_result']['finished_at']
    # Model a ledger observation missing after a crash while history is durable.
    with database(w.settings) as db:
        db.execute('DELETE FROM results')
    w.clock = lambda: EVENING + timedelta(minutes=1)
    w.request_run('new-success')
    assert w.tick(EVENING + timedelta(minutes=1))
    w.clock = lambda: EVENING + timedelta(minutes=2)
    w.request_cancel('old-cancel')
    assert latest_outcomes(w.settings)['items']['etf-shares']['outcome'] == 'success'
    old = next(row for row in w.status()['recent_runs'] if row['id'] == 'manual:old-cancel')
    assert old['finished_at'] == original_stamp


def test_repeated_legacy_cancel_keeps_unknown_completion_time_and_latest_indicator(tmp_path):
    from app.services.collection_outcomes import latest_outcomes
    w, c, _ = worker(tmp_path, refresh=lambda: True)
    w.start = lambda: None
    with w.connect() as db:
        db.execute("DELETE FROM metadata WHERE key='run_history_migrated'")
        db.execute('INSERT INTO manual_runs(id,status,requested_at) VALUES (?,?,?)',
                   ('legacy-cancel', 'cancelled', EVENING.isoformat()))
    legacy = w.status()['latest_result']
    assert legacy['id'] == 'legacy-manual:legacy-cancel'
    assert legacy['finished_at'] is None
    assert latest_outcomes(w.settings)['items']['etf-shares']['outcome'] == 'unknown'
    w.clock = lambda: EVENING + timedelta(minutes=1)
    w.request_run('newer-success')
    assert w.tick(EVENING + timedelta(minutes=1))
    w.clock = lambda: EVENING + timedelta(minutes=2)
    for _ in range(2):
        assert w.request_cancel('legacy-cancel')['status'] == 'cancelled'
    rows = w.status()['recent_runs']
    assert len(rows) == 2
    assert rows[1] == legacy
    assert not any(row['id'] == 'manual:legacy-cancel' for row in rows)
    assert latest_outcomes(w.settings)['items']['etf-shares']['outcome'] == 'success'


def test_native_history_pruning_is_opt_in_and_never_touches_recovery_ledgers(tmp_path):
    from app.services.etf_collection_history import prune_native_history
    w, _, _ = worker(tmp_path)
    with w.connect() as db:
        for index in range(8):
            stamp = (EVENING + timedelta(minutes=index)).isoformat()
            db.execute('INSERT INTO run_history VALUES (?,?,?,?,?,?,?,?,?)',
                       (str(index), 'manual', 'failed', None, stamp, stamp, None, None, 'completed_at'))
            db.execute('INSERT INTO run_history_errors VALUES (?,?)', (str(index), '{}'))
        db.execute('INSERT INTO run_history VALUES (?,?,?,?,?,?,?,?,?)',
                   ('active', 'scheduled', 'publishing', None, EVENING.isoformat(), None, None, 1, 'started_at'))
        db.execute('INSERT INTO manual_runs(id,status,requested_at) VALUES (?,?,?)', ('old', 'failed', EVENING.isoformat()))
        db.execute('INSERT INTO slots VALUES (?,?,?,?,?)', (EVENING.isoformat(), 'failed', 1, EVENING.isoformat(), 'old'))
        db.execute('INSERT INTO observations VALUES (?,?,?,?,?)', ('shares', 'x', DAY.isoformat(), 'pending', 2))
        before = {name: [tuple(r) for r in db.execute('SELECT * FROM ' + name)] for name in ('slots', 'manual_runs', 'observations', 'metadata')}
    assert prune_native_history(w.settings) == {'module': 'etf-shares', 'candidates': 3, 'deleted': 0}
    with pytest.raises(ValueError):
        prune_native_history(w.settings, apply=True)
    assert len(w.status()['recent_runs']) == 8
    assert prune_native_history(w.settings, apply=True, confirmed=True)['deleted'] == 3
    with w.connect() as db:
        assert [r[0] for r in db.execute('SELECT id FROM run_history ORDER BY id')] == ['3', '4', '5', '6', '7', 'active']
        assert db.execute('SELECT COUNT(*) FROM run_history_errors').fetchone()[0] == 5
        assert before == {name: [tuple(r) for r in db.execute('SELECT * FROM ' + name)] for name in before}
    assert prune_native_history(w.settings)['candidates'] == 0
    from app.services.etf_collection_history import restore_native_history
    assert restore_native_history(w.settings)['candidates'] == 3
    assert len(w.status()['recent_runs']) == 5
    with pytest.raises(ValueError):
        restore_native_history(w.settings, apply=True)
    assert restore_native_history(w.settings, apply=True, confirmed=True)['restored'] == 3
    assert len(w.status()['recent_runs']) == 8
    assert restore_native_history(w.settings)['candidates'] == 0
    with w.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM run_history_archive').fetchone()[0] == 3


def test_pruned_cancel_history_cannot_reappear_on_repeat_cancel(tmp_path):
    w, _, _ = worker(tmp_path)
    w.start = lambda: None
    w.request_run('old-cancel')
    w.request_cancel('old-cancel')
    with w.connect() as db:
        db.execute('DELETE FROM run_history')
    assert w.request_cancel('old-cancel')['status'] == 'cancelled'
    assert not w.status()['recent_runs']


def test_etf_history_safe_details_preserve_stage_without_driver_secrets(tmp_path):
    w, c, _ = worker(tmp_path)
    c.write_failure = True
    w.tick(EVENING)
    row = w.status()['latest_result']
    assert row['status'] == 'failed'
    assert row['error_details']['stage'] == 'database_write'
    assert 'secret' not in str(row['error_details'])
    assert 'DSN' not in str(row['error_details'])


def test_native_history_archive_mismatch_rolls_back_without_losing_active_rows(tmp_path):
    from app.services.etf_collection_history import prune_native_history
    w, _, _ = worker(tmp_path)
    with w.connect() as db:
        for index in range(6):
            stamp = (EVENING + timedelta(minutes=index)).isoformat()
            db.execute('INSERT INTO run_history VALUES (?,?,?,?,?,?,?,?,?)',
                       (str(index), 'manual', 'failed', None, stamp, stamp, None, None, 'completed_at'))
        db.execute('CREATE TABLE run_history_archive (id TEXT PRIMARY KEY, original_rowid INTEGER, payload TEXT, details TEXT)')
        db.execute('INSERT INTO run_history_archive VALUES (?,?,?,?)', ('0', 1, '{}', None))
    with pytest.raises(ValueError, match='verification failed'):
        prune_native_history(w.settings, apply=True, confirmed=True)
    assert len(w.status()['recent_runs']) == 6


def test_native_history_default_retention_does_not_archive(tmp_path):
    w, _, _ = worker(tmp_path)
    assert not getattr(w.settings, 'collection_history_retention_enabled', False)
    w._retain_history()
    assert not (w.root / 'state.sqlite').exists()


def test_native_retention_only_when_explicitly_enabled(tmp_path):
    w, _, _ = worker(tmp_path)
    with w.connect() as db:
        for index in range(6):
            stamp = (EVENING + timedelta(minutes=index)).isoformat()
            db.execute('INSERT INTO run_history VALUES (?,?,?,?,?,?,?,?,?)',
                       (str(index), 'manual', 'failed', None, stamp, stamp, None, None, 'completed_at'))
    w._retain_history()
    assert len(w.status()['recent_runs']) == 6
    w.settings = w.settings.model_copy(update={'collection_history_retention_enabled': True})
    w._retain_history()
    assert len(w.status()['recent_runs']) == 5
    with w.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM run_history_archive').fetchone()[0] == 1


def test_native_history_restore_after_multiple_archive_cycles(tmp_path):
    from app.services.etf_collection_history import prune_native_history, restore_native_history
    w, _, _ = worker(tmp_path)
    for start in (0, 6):
        with w.connect() as db:
            for index in range(start, start + 6):
                stamp = (EVENING + timedelta(minutes=index)).isoformat()
                db.execute('INSERT INTO run_history VALUES (?,?,?,?,?,?,?,?,?)',
                           (str(index), 'manual', 'failed', None, stamp, stamp, None, None, 'completed_at'))
        prune_native_history(w.settings, apply=True, confirmed=True)
    assert len(w.status()['recent_runs']) == 5
    assert restore_native_history(w.settings, apply=True, confirmed=True)['restored'] == 7
    assert [row['id'] for row in w.status()['recent_runs']] == [str(i) for i in reversed(range(12))]
    w.settings = w.settings.model_copy(update={'collection_history_retention_enabled': True})
    with pytest.raises(ValueError, match='Disable'):
        restore_native_history(w.settings, apply=True, confirmed=True)


def test_history_cli_preview_and_restore_preview_do_not_write(tmp_path, monkeypatch, capsys):
    import importlib.util
    import sys
    from pathlib import Path
    from app.services.etf_collection_history import prune_native_history
    w, _, _ = worker(tmp_path)
    with w.connect() as db:
        for index in range(6):
            stamp = (EVENING + timedelta(minutes=index)).isoformat()
            db.execute('INSERT INTO run_history VALUES (?,?,?,?,?,?,?,?,?)',
                       (str(index), 'manual', 'failed', None, stamp, stamp, None, None, 'completed_at'))
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'prune_collection_history.py'
    spec = importlib.util.spec_from_file_location('prune_cli_test', path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, 'Settings', lambda: w.settings)
    for args in ([], ['--restore']):
        before = (w.root / 'state.sqlite').read_bytes()
        monkeypatch.setattr(sys, 'argv', [str(path), *args])
        cli.main()
        assert (w.root / 'state.sqlite').read_bytes() == before
    prune_native_history(w.settings, apply=True, confirmed=True)
    before = (w.root / 'state.sqlite').read_bytes()
    monkeypatch.setattr(sys, 'argv', [str(path), '--restore'])
    cli.main()
    assert (w.root / 'state.sqlite').read_bytes() == before


def test_native_permanent_purge_keeps_union_latest_five_and_never_operations(tmp_path):
    import json
    from app.services.etf_collection_history import purge_native_history, prune_native_history
    w, _, _ = worker(tmp_path)
    with w.connect() as db:
        for index in range(8):
            stamp = (EVENING + timedelta(minutes=index)).isoformat()
            db.execute('INSERT INTO run_history VALUES (?,?,?,?,?,?,?,?,?)',
                       (str(index), 'manual', 'failed', None, stamp, stamp, None, None, 'completed_at'))
            db.execute('INSERT INTO run_history_errors VALUES (?,?)', (str(index), '{}'))
        db.execute('INSERT INTO slots VALUES (?,?,?,?,?)', (EVENING.isoformat(), 'failed', 1, EVENING.isoformat(), 'error'))
    prune_native_history(w.settings, apply=True, confirmed=True)
    with w.connect() as db:
        # Simulate a later valid completion archived while fewer active rows exist.
        row = dict(db.execute("SELECT * FROM run_history WHERE id='7'").fetchone())
        db.execute('INSERT INTO run_history_archive VALUES (?,?,?,?)', ('7', 8, json.dumps(row, sort_keys=True), '{}'))
        db.execute("DELETE FROM run_history WHERE id='7'")
        db.execute('INSERT INTO run_history VALUES (?,?,?,?,?,?,?,?,?)',
                   ('live', 'scheduled', 'publishing', None, EVENING.isoformat(), None, None, 1, 'started_at'))
        before = {name: [tuple(r) for r in db.execute('SELECT * FROM ' + name)] for name in ('slots', 'manual_runs', 'observations', 'metadata')}
    database_bytes = (w.root / 'state.sqlite').read_bytes()
    assert purge_native_history(w.settings) == {'module': 'etf-shares', 'candidates': 3, 'deleted': 0}
    assert (w.root / 'state.sqlite').read_bytes() == database_bytes
    with pytest.raises(ValueError, match='permanent'):
        purge_native_history(w.settings, apply=True)
    assert purge_native_history(w.settings, apply=True, confirmed=True)['deleted'] == 3
    with w.connect() as db:
        assert {r[0] for r in db.execute('SELECT id FROM run_history')} == {'3', '4', '5', '6', 'live'}
        assert {r[0] for r in db.execute('SELECT id FROM run_history_archive')} == {'7'}
        assert not db.execute("SELECT 1 FROM run_history_errors WHERE id IN ('0','1','2')").fetchone()
        assert before == {name: [tuple(r) for r in db.execute('SELECT * FROM ' + name)] for name in before}
    assert purge_native_history(w.settings)['candidates'] == 0


def test_native_permanent_purge_protects_unknown_states_and_unparseable_times(tmp_path):
    from app.services.etf_collection_history import purge_native_history
    w, _, _ = worker(tmp_path)
    with w.connect() as db:
        for index in range(6):
            stamp = (EVENING + timedelta(minutes=index)).isoformat()
            db.execute('INSERT INTO run_history VALUES (?,?,?,?,?,?,?,?,?)',
                       (str(index), 'manual', 'failed', None, stamp, stamp, None, None, 'completed_at'))
        for key, state, requested, finished in [
            ('unknown-state', 'future_status', EVENING.isoformat(), EVENING.isoformat()),
            ('bad-finish', 'failed', EVENING.isoformat(), 'invalid'),
            ('bad-legacy', 'failed', 'invalid', None),
            ('valid-legacy', 'failed', EVENING.isoformat(), None),
        ]:
            db.execute('INSERT INTO run_history VALUES (?,?,?,?,?,?,?,?,?)',
                       (key, 'manual', state, None, requested, finished, None, None, 'completed_at'))
    assert purge_native_history(w.settings, apply=True, confirmed=True)['deleted'] == 2
    with w.connect() as db:
        ids = {row[0] for row in db.execute('SELECT id FROM run_history')}
    assert {'unknown-state', 'bad-finish', 'bad-legacy'} <= ids
    assert '0' not in ids and 'valid-legacy' not in ids
    assert len(ids) == 8


@pytest.mark.parametrize(('omit_shares', 'omit_prices', 'code'), [
    (True, False, 'etf_missing_shares'),
    (False, True, 'etf_missing_prices'),
    (True, True, 'etf_missing_shares_and_prices'),
])
def test_valid_incomplete_source_reports_specific_gap(tmp_path, caplog, omit_shares, omit_prices, code):
    import logging
    w, c, _ = worker(tmp_path)
    collect = c.collect

    def incomplete(*args, **kwargs):
        shares, prices, extra = collect(*args, **kwargs)
        return ([] if omit_shares else shares, [] if omit_prices else prices, extra)

    c.collect = incomplete
    request_id = 'source-gap-check-123'
    w.request_run(request_id)
    with caplog.at_level(logging.INFO):
        assert not w.tick(EVENING)
    latest = w.status()['latest_result']
    assert latest['status'] == 'partial'
    assert latest['error_details']['code'] == code
    assert latest['error_details']['trade_date'] == DAY.isoformat()
    assert 'failed=0' in caplog.text
    assert f'missing_shares={2 if omit_shares else 0}' in caplog.text
    assert f'missing_prices={2 if omit_prices else 0}' in caplog.text
    assert len(c.writes) == 1  # Retain verified records even if the other series is missing.
    assert c.calendar_targets == [DAY]


def test_database_failure_takes_precedence_over_source_gap(tmp_path, caplog):
    import logging
    from pymysql.err import OperationalError
    w, c, _ = worker(tmp_path)
    c.collect = lambda *args, **kwargs: ([], [], {})

    def disconnected(*args, **kwargs):
        raise OperationalError(2013, 'private values')

    c.write_records = disconnected
    w.request_run('source-gap-write-failed-123')
    with caplog.at_level(logging.INFO):
        assert not w.tick(EVENING)
    latest = w.status()['latest_result']
    assert latest['status'] == 'failed'
    assert latest['error_details']['code'] == 'database_error'
    assert latest['error_details']['mysql_errno'] == 2013
    assert 'missing_shares=2 missing_prices=2' in caplog.text
    assert 'private values' not in caplog.text


def test_commit_connection_loss_requires_verification_and_does_not_retry_slot(tmp_path, caplog):
    import logging
    from pymysql.err import OperationalError
    from app.services.etf_diagnostics import database_phase

    w, c, _ = worker(tmp_path)

    def ambiguous_commit(*args, **kwargs):
        with database_phase('commit'):
            raise OperationalError(2013, 'connection lost after private write')

    c.write_records = ambiguous_commit
    with caplog.at_level(logging.INFO):
        assert not w.tick(EVENING)
    state = w.status()
    assert state['runs'][0]['error'] == 'writer_commit_unconfirmed'
    assert state['latest_result']['error_details']['code'] == 'etf_commit_unconfirmed'
    assert state['latest_result']['error_details']['mysql_errno'] == 2013
    assert not state['cache_pending']
    assert 'stage=commit' in caplog.text and 'run=-' not in caplog.text
    assert 'connection lost after private write' not in caplog.text
    attempts = len(c.calls)
    assert not w.tick(EVENING + timedelta(minutes=31))
    assert len(c.calls) == attempts


def manual_repair_fixture(tmp_path, **kwargs):
    from app.services.etf_exchange import ExchangeCalendar

    w, c, _ = worker(tmp_path, **kwargs)
    dates = [date(2026, 11, value) for value in (16, 17, 18, 19, 20, 23, 24)]
    moment = datetime(2026, 11, 23, 18, 30, tzinfo=CHINA)
    w.clock = lambda: moment
    w.start = lambda: None
    c.ExchangeClient = lambda _: SimpleNamespace(calendar=lambda: ExchangeCalendar(
        [day for day in dates if day <= w.clock().date()], w.clock().date(), 160000,
        now=w.clock(), official_days={day: True for day in dates}))
    return w, c, dates, moment


def seed_covered(db, collector, day):
    db.executemany("INSERT INTO observations VALUES (?,?,?,'complete',1)",
                   ((kind, code, day.isoformat()) for kind in ('shares', 'prices')
                    for code in collector.catalog()))


def test_manual_repairs_all_official_sessions_since_covered_day_even_if_latest_partial(tmp_path):
    w, c, dates, moment = manual_repair_fixture(tmp_path)
    _, baseline, *_, target = dates[:-1]
    with w.connect() as db:
        seed_covered(db, c, baseline)
    original = c.collect

    def missing_latest_shares(client, start, end, **kwargs):
        rows = original(client, start, end, **kwargs)
        return ([], rows[1], {}) if start == target else rows

    c.collect = missing_latest_shares
    w.request_run('manual-all-trading-gaps')
    assert not w.tick(moment)
    assert sorted({day for day, *_ in c.calls}) == dates[2:-1]
    assert c.calendar_targets == dates[2:-1]
    assert all(rows for rows, _ in c.writes[:-1]) and not c.writes[-1][0]
    with w.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM observations WHERE status=? AND day=?',
                          ('complete', '2026-11-20')).fetchone()[0] == 4
        assert db.execute('SELECT SUM(attempts) FROM observations WHERE day=?',
                          ('2026-11-20',)).fetchone()[0] == 0
    assert w.status()['latest_result']['status'] == 'partial'


def test_manual_repair_respects_source_rows_persists_gap_after_newer_success(tmp_path):
    calls = []
    w, c, dates, moment = manual_repair_fixture(tmp_path, refresh=lambda **options: calls.append(options) or False)
    baseline, gap, newest = dates[3:6]
    shenzhen = '159919.XSHE'
    with w.connect() as db:
        seed_covered(db, c, baseline)
        # A previously observed gap must survive even when a newer daily slot completed.
        seed_covered(db, c, newest)
        db.execute("INSERT INTO observations VALUES ('prices',?,?, 'pending',0)",
                   (shenzhen, gap.isoformat()))
    original = c.collect
    needed = ('prices', shenzhen, gap)
    def verify_existing(engine, targets):
        return targets - {needed}
    def unavailable(client, start, end, **kwargs):
        rows = original(client, start, end, **kwargs)
        return (rows[0], [], {}) if start == gap else rows
    c.existing_observations = verify_existing
    c.collect = unavailable
    w.request_run('manual-sticky-gap-1')
    assert not w.tick(moment)
    assert w.status()['manual_gap_start'] == gap.isoformat()
    assert w.status()['latest_result']['error_details']['trade_date'] == gap.isoformat()
    assert [kind for day, _, kind, _ in c.calls if day == gap] == [True]
    assert calls == [{'repair_start': baseline}]
    assert w.status()['metadata']['cache_repair_start'] == baseline.isoformat()

    # The next normal scheduled job moves forward but does not retry the old gap.
    c.calls.clear()
    w.clock = lambda: moment + timedelta(days=1)
    assert w.tick(w.clock())
    assert {day for day, *_ in c.calls} == {dates[-1]}
    assert w.status()['manual_gap_start'] == gap.isoformat()

    c.collect = original
    w.request_run('manual-sticky-gap-2')
    assert w.tick(w.clock())
    assert w.status()['manual_gap_start'] is None
    assert [day for day, *_ in c.calls].count(gap) == 1


def test_bootstrap_includes_unobserved_recent_gap_not_unrelated_legacy_history(tmp_path):
    w, c, dates, moment = manual_repair_fixture(tmp_path)
    old, _, _, thursday, friday, monday, _ = dates
    with w.connect() as db:
        db.execute("INSERT INTO observations VALUES ('prices','159919.XSHE',?, 'pending',0)",
                   (old.isoformat(),))
    # Friday never ran; Thursday already has all data in MySQL but no ledger.
    c.existing_observations = lambda engine, keys: {key for key in keys if key[2] == thursday}
    w.request_run('manual-recent-bootstrap')
    assert w.tick(moment)
    assert {day for day, *_ in c.calls} == {friday, monday}


def test_historical_cache_intent_survives_lock_and_clears_only_on_publication(tmp_path):
    attempts = []
    w, _, dates, moment = manual_repair_fixture(tmp_path, refresh=lambda **kw: attempts.append(kw) or False)
    with w.connect() as db:
        db.execute("INSERT INTO metadata VALUES ('cache_pending','1')")
        db.execute("INSERT INTO metadata VALUES ('cache_repair_start',?)", (dates[1].isoformat(),))
        w._refresh(db)
    assert attempts == [{'repair_start': dates[1]}]
    assert w.status()['metadata']['cache_repair_start'] == dates[1].isoformat()
    w.clock = lambda: moment + timedelta(minutes=6)
    w.refresh = lambda **kw: attempts.append(kw) or True
    with w.connect() as db:
        w._refresh(db)
    assert len(attempts) == 2
    assert 'cache_repair_start' not in w.status()['metadata']
    assert not w.status()['cache_pending']


def test_manual_checks_mysql_for_previously_complete_inside_known_gap_scope(tmp_path):
    w, c, dates, moment = manual_repair_fixture(tmp_path)
    anchor, registered, formerly_complete, latest = dates[2:6]
    with w.connect() as db:
        seed_covered(db, c, anchor)
        seed_covered(db, c, formerly_complete)
        db.execute("INSERT INTO observations VALUES ('shares','510300.XSHG',?,'pending',0)",
                   (registered.isoformat(),))
    key = ('prices', '510300.XSHG', formerly_complete)
    def data_present(engine, required):
        assert anchor not in {day for _, _, day in required}
        assert key in required
        return required - {key}
    c.existing_observations = data_present
    w.request_run('manual-check-authoritative-rows')
    assert w.tick(moment)
    assert {day for day, *_ in c.calls} == {formerly_complete, latest}
    assert [funds for day, funds, *_ in c.calls if day == formerly_complete] == [{'510300.XSHG': '沪'}]
