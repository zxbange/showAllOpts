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

    def write_records(self, engine, shares, prices, calendar):
        if self.write_failure:
            raise RuntimeError('secret writer DSN')
        self.writes.append((shares, prices))


def worker(tmp_path, **kwargs):
    settings = Settings(_env_file=None, log_dir=tmp_path, etf_collection_enabled=True,
                        etf_collection_writer_url='mysql+pymysql://etf_writer@127.0.0.1:3306/showallopts', etf_collection_lookback_days=2)
    c = Collector()
    disposed = []
    w = ETFCollection(settings, collector=c, client_factory=lambda: nullcontext(None),
                      engine_factory=lambda *a, **k: SimpleNamespace(dispose=lambda: disposed.append(True)),
                      clock=lambda: EVENING, **kwargs)
    return w, c, disposed


def test_timezone_and_naive():
    assert scheduled_slots(EVENING.astimezone(timezone.utc))[-1] == EVENING
    with pytest.raises(ValueError):
        scheduled_slots(datetime(2026, 11, 6))


def test_disabled_and_missing_configuration(tmp_path):
    w = ETFCollection(Settings(_env_file=None, log_dir=tmp_path, etf_collection_enabled=False))
    w.start()
    assert w.status()['enabled'] is False
    assert w.status()['ready'] is False
    assert w.status()['disabled_reason'] == 'missing_writer_configuration'
    assert w.worker is None
    w.settings.etf_collection_enabled = True
    w.start()
    assert w.status()['status'] == 'missing_writer_configuration'
    assert w.tick(EVENING) is False
    assert not w.root.exists()


def test_no_startup_network_or_07_slot(tmp_path):
    w, c, _ = worker(tmp_path)
    w.start()
    w.stop()
    assert not c.calls
    assert not w.tick(EVENING)  # startup floor excludes the past slot
    w.floor = None
    assert not w.tick(EVENING.replace(hour=7))


def test_evening_idempotency_and_blocked_history(tmp_path):
    w, c, disposed = worker(tmp_path)
    assert w.tick(EVENING)
    assert not w.tick(EVENING + timedelta(minutes=31))
    assert len(c.writes) == len(disposed) == 1
    gaps = w.status()['gaps']
    assert gaps == [dict(kind='prices', code='159919.XSHE', day='2026-11-05',
                         status='blocked_unverified_history', attempts=0)]
    w2, c2, _ = worker(tmp_path)
    assert not w2.tick(EVENING)
    assert not c2.calls


def test_morning_never_fetches_prices(tmp_path):
    w, c, _ = worker(tmp_path)
    assert w.tick(EVENING + timedelta(days=1, hours=-9))
    assert all(not call[2] and call[3] for call in c.calls)


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


def test_missing_gap_exhaustion_does_not_hide_other_dates(tmp_path):
    w, c, _ = worker(tmp_path)
    old = DAY - timedelta(days=1)
    def partial(client, start, end, funds, include_prices, include_shares, calendar):
        if start == old:
            return [], [], {}
        rows = [dict(fund_code=code, trade_date=start) for code in funds]
        return rows if include_shares else [], rows if include_prices else [], {}
    c.collect = partial
    for i in range(6):
        assert w.tick(EVENING + timedelta(days=i))
    old_gaps = [r for r in w.status()['gaps'] if r['day'] == old.isoformat() and r['kind'] == 'shares']
    assert all(r['status'] == 'retry_exhausted' and r['attempts'] == 6 for r in old_gaps)
    assert old_gaps


def test_month_boundary_slot_in_shanghai(tmp_path):
    # 01:30 UTC on Jan 1 is 09:30 in Shanghai, no 07 cache schedule collision.
    moment = datetime(2027, 1, 1, 1, 30, tzinfo=timezone.utc)
    assert scheduled_slots(moment)[0].isoformat() == '2027-01-01T09:30:00+08:00'
    assert all(slot.hour != 7 for slot in scheduled_slots(moment))


def test_collection_status_endpoint(client):
    response = client.get('/api/v1/etf-shares/collection-status')
    assert response.status_code == 200
    assert response.json()['disabled_reason'] == 'missing_writer_configuration'
    assert response.json()['ready'] is False
    assert 'writer_url' not in response.text.lower()



def test_weekend_latest_close_price_can_be_recovered(tmp_path):
    w, c, _ = worker(tmp_path)
    assert w.tick(EVENING + timedelta(days=1))
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
    w, c, _ = worker(tmp_path)
    w.settings.etf_collection_writer_url = SecretStr(writer)
    w.start()
    assert w.worker is None
    assert not w.tick(EVENING)
    assert not c.calls
    assert w.status()['status'] in {'writer_target_mismatch', 'invalid_writer_configuration'}
    assert writer not in str(w.status())


def test_status_reports_target_not_completed_coverage(tmp_path):
    w, c, _ = worker(tmp_path)
    assert w.tick(EVENING)
    status = w.status()
    assert status['target_sessions'] == 2
    assert status['historical_dates_per_run'] == 5
    assert status['coverage_is_target_not_completion'] is True



def test_default_enabled_but_missing_writer_is_not_ready(tmp_path, caplog):
    settings = Settings(_env_file=None, log_dir=tmp_path)
    assert settings.etf_collection_enabled is True
    w = ETFCollection(settings)
    w.start()
    assert not w.worker
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



def test_later_source_failure_does_not_exhaust_earlier_success(tmp_path):
    w, c, _ = worker(tmp_path)
    old = DAY - timedelta(days=1)
    real = c.collect
    def late_failure(client, start, end, **kwargs):
        if start == old and kwargs['include_prices']:
            raise ValueError('later price source failed')
        return real(client, start, end, **kwargs)
    c.collect = late_failure
    for i in range(6):
        w.tick(EVENING + timedelta(days=i))
    with w.connect() as db:
        rows = db.execute("SELECT status,attempts FROM observations WHERE day=? AND kind='shares'",
                          (old.isoformat(),)).fetchall()
    assert rows and all(tuple(r) == ('complete', 1) for r in rows)
    assert any(r['trade_date'] == old for shares, _ in c.writes for r in shares)
    old_price = next(r for r in w.status()['gaps'] if r['day'] == old.isoformat()
                     and r['code'] == '510300.XSHG' and r['kind'] == 'prices')
    assert old_price['status'] == 'retry_exhausted' and old_price['attempts'] == 6


def test_six_writer_failures_do_not_consume_gap_budget(tmp_path):
    w, c, _ = worker(tmp_path)
    c.write_failure = True
    for i in range(6):
        assert not w.tick(EVENING + timedelta(days=i))
    with w.connect() as db:
        rows = db.execute("SELECT status,attempts FROM observations WHERE kind='shares'").fetchall()
    assert rows and all(tuple(r) == ('pending', 0) for r in rows)
    c.write_failure = False
    assert w.tick(EVENING + timedelta(days=6))
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
    assert len(calls) == 2 and len(set(calls)) == 2



@pytest.mark.parametrize('failure_mode', ['exception', 'empty'])
def test_next_morning_requires_fresh_verification_not_old_complete_state(tmp_path, failure_mode):
    w, c, _ = worker(tmp_path)
    assert w.tick(EVENING)
    old_writes = list(c.writes)
    real = c.collect
    def unavailable(*args, **kwargs):
        if kwargs['include_shares']:
            if failure_mode == 'exception':
                raise ValueError('temporary source failure')
            return [], [], {}
        return real(*args, **kwargs)
    c.collect = unavailable
    morning = EVENING + timedelta(days=1, hours=-9)
    assert not w.tick(morning)
    assert w.status()['runs'][0]['status'] == 'partial'
    with w.connect() as db:
        rows = db.execute("SELECT status FROM observations WHERE day=? AND kind='shares'", (DAY.isoformat(),)).fetchall()
    assert rows and all(row[0] == 'complete' for row in rows)
    assert c.writes[:len(old_writes)] == old_writes
    assert c.writes[-1] == ([], [])  # No replacement/deletion of old business rows.
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
    assert received == [{'pool_pre_ping': True, 'connect_args': {
        'connect_timeout': 10, 'read_timeout': 60, 'write_timeout': 60}}]
