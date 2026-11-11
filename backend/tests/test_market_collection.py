from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import subprocess
import sys
import threading

import httpx
import portalocker
import pytest

from app.core.config import Settings
from app.services.etf_collection import WriterFailure
from app.services.etf_configuration import ConfigurationFailure
from app.services.market_collection import (
    CHINA, CollectionBusy, CollectionInterrupted, MarketCollection, PacedHTTPClient,
)
from app.services.market_sina import SinaDataError, SinaRateLimitError
from app.services.market_methodology import JOINQUANT_HEAT_METHOD, LEGACY_SINA_HEAT_METHOD


N = 120
DAY = date(2026, 11, 10)
EVENING = datetime(2026, 11, 10, 18, 30, tzinfo=CHINA)
CALENDAR = [date(2026, 11, day) for day in (6, 9, 10, 11, 12, 13, 16)] + [date(2026, 12, 31)]


class Clock:
    def __init__(self, value=EVENING):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)
        return self.value


def snapshot(day, *, edge_cases=False):
    rows = []
    for i in range(N):
        prefix = 'sh' if i < 10 else 'sz' if i < 20 else 'bj'
        rows.append(dict(symbol=f'{prefix}{600000 + i:06}', name=f'股票{i}',
                         close=str(10 + i), volume='100', amount=str(1000 + i * 100),
                         ticktime='15:00:00', raw={'test_record': i}))
    if edge_cases:
        rows[-1].update(volume='0', amount='0')
    return dict(date=str(day), expected_count=len(rows), rows=rows,
                fetched_at='2026-11-10T10:00:00+00:00', raw={'pages': [rows]})


class Provider:
    def __init__(self, clock, *, edge_cases=False):
        self.clock = clock
        self.edge_cases = edge_cases
        self.universe_calls, self.quote_calls, self.session_calls = [], [], []
        self.failures = set()
        self.exceptions = {}
        self.on_quote = None
        self.snapshot_override = None
        self.quote_override = None
        # This synthetic fixture supplies independently verified qfq evidence.
        # The real Sina provider explicitly reports verified=False.
        self.heat_verified = True
        self.hq_calls, self.hq_cached = [], []
        self.hq_exception_at = None
        self.hq_exception = CollectionInterrupted()
        self.hq_override = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def latest_session_day(self):
        self.session_calls.append(self.clock().date())
        return self.clock().date()

    def fetch_universe(self, day):
        self.universe_calls.append(day)
        return deepcopy(self.snapshot_override or snapshot(day, edge_cases=self.edge_cases))

    def heat_capability(self):
        return {'methodology': JOINQUANT_HEAT_METHOD, 'verified': self.heat_verified}

    def fetch_dated_snapshot(self, universe, day, *, cached_rows=None, checkpoint=None):
        cached = {row['symbol']: row for row in (cached_rows or [])}
        self.hq_cached.append(set(cached))
        missing = [row for row in universe['rows'] if row['symbol'] not in cached]
        for offset in range(0, len(missing), 40):
            batch = deepcopy(missing[offset:offset + 40])
            self.hq_calls.append((day, [row['symbol'] for row in batch]))
            if self.hq_exception_at == len(self.hq_calls):
                raise self.hq_exception
            for row in batch:
                row.update(date=str(day), time='15:00:00', hq_state='00',
                           paused=0 if int(row['volume']) > 0 else None,
                           hq_raw={'fixture': row['symbol']}, node_amount=row['amount'])
                row['amount'] = str(Decimal(row['amount']) + Decimal('.25')) if int(row['volume']) else '0'
                if self.hq_override:
                    row.update(self.hq_override(row))
            if checkpoint:
                checkpoint(batch)
            cached.update({row['symbol']: row for row in batch})
        result = deepcopy(universe)
        result.update(rows=[cached[row['symbol']] for row in universe['rows']],
                      source_has_trading_date=True, dated_snapshot_verified=True,
                      date_verified_by='sina_hq')
        return result

    def fetch_quote(self, symbol, day, *, snapshot=None, datalen=1):
        self.quote_calls.append((symbol, day, datalen))
        if self.on_quote:
            self.on_quote(symbol)
        if symbol in self.exceptions:
            raise self.exceptions[symbol]
        result = dict(symbol=symbol, day=str(day), date=str(day), status='current',
                      close=snapshot['close'], volume=snapshot['volume'],
                      ma20=str(int(snapshot['close']) + (int(symbol[-1]) % 3 - 1)),
                      error=None, raw={'requests': [{'status_code': 200, 'text': 'test'}]})
        result.update(methodology=JOINQUANT_HEAT_METHOD, verified=True,
                      price_basis='qfq', denominator='eligible', qfq_close=snapshot['close'],
                      paused=0, history_count=20, eligible=True,
                      above=int(symbol[-1]) % 3 == 0,
                      evidence={'calendar_verified': True, 'listing_verified': True,
                                'paused_verified': True, 'prices_verified': True,
                                'universe_verified': True, 'price_basis': 'qfq',
                                'adjustment_basis': 'synthetic-test-only'})
        if self.edge_cases and symbol == 'bj600119':
            result.update(status='paused', paused=1, eligible=False, above=False)
        if self.edge_cases and symbol == 'bj600118':
            result.update(status='insufficient_history', ma20=None, history_count=5, eligible=False, above=False)
        if symbol in self.failures:
            result.update(status='transient_failure', date=None, ma20=None, error='network_error')
        if self.quote_override:
            result = self.quote_override(result)
        return result


class Writer:
    def __init__(self):
        self.calls = []
        self.error = None
        self.preserve = set()
        self.dates = {'heat': set(), 'crowding': set()}

    def __call__(self, owner, day, heat, crowding, source):
        self.calls.append((day, deepcopy(heat), deepcopy(crowding), deepcopy(source)))
        if self.error:
            raise self.error
        result = dict(heat_status='pending', crowding_status='pending', heat_hash=None, crowding_hash=None)
        for kind, value in (('heat', heat), ('crowding', crowding)):
            if value is not None:
                self.dates[kind].add(str(day))
                result[kind + '_status'] = 'existing_preserved' if kind in self.preserve else 'written'
                result[kind + '_hash'] = None if kind in self.preserve else hashlib.sha256(
                    json.dumps(value, sort_keys=True).encode()).hexdigest()
        return result


def worker(tmp_path, *, moment=EVENING, edge_cases=False, refresh=None):
    clock = Clock(moment)
    provider = Provider(clock, edge_cases=edge_cases)
    writer = Writer()
    settings = Settings(_env_file=None, log_dir=tmp_path,
                        market_collection_worker_enabled=True,
                        market_collection_writer_url='mysql+pymysql://writer:fake-password@127.0.0.1:3306/showallopts')
    collection = MarketCollection(settings, refresh, clock=clock,
                                  provider_factory=lambda client: provider, writer=writer,
                                  client_factory=lambda: nullcontext(object()),
                                  calendar_loader=lambda client: list(CALENDAR),
                                  source_dates_loader=lambda owner: writer.dates)
    collection.start = lambda: None
    return collection, provider, writer, clock


def test_missing_writer_never_uses_readonly_or_starts_network(tmp_path):
    collection = MarketCollection(Settings(_env_file=None, log_dir=tmp_path))
    collection.start()
    assert collection.worker is None
    assert collection.status()['disabled_reason'] == 'missing_writer_configuration'
    assert not collection.tick(EVENING)
    with pytest.raises(ConfigurationFailure):
        collection.request_run('request')


def test_process_worker_switch_cannot_be_bypassed_by_manual_or_configuration_start(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    collection.settings.market_collection_worker_enabled = False
    del collection.start
    collection.start()
    assert collection.worker is None
    assert not collection.tick()
    state = collection.status()
    assert state['configured'] and state['worker_disabled'] and not state['ready']
    assert state['status'] == 'worker_disabled'
    with pytest.raises(ConfigurationFailure, match='当前进程已停用'):
        collection.request_run('manual-cannot-bypass')
    assert not provider.universe_calls and not writer.calls


def test_startup_after_18_collects_once_verified_qfq_comparison_and_eligible_denominator(tmp_path):
    collection, provider, writer, clock = worker(tmp_path, edge_cases=True)
    assert collection.tick()
    state = collection.status()
    assert state['status'] == 'complete'
    assert state['total'] == state['completed'] == N and state['pending'] == 0
    assert state['valid_count'] == (N - 2)
    assert state['no_trade_count'] == state['insufficient_history_count'] == 1
    heat = next(call[1] for call in writer.calls if call[1])
    assert heat['ratio'] == pytest.approx(heat['above_count'] / (N - 2), abs=1e-10)
    assert heat['eligible_count'] == (N - 2)
    assert heat['above_count'] == sum(int(int(row['symbol'][-1]) % 3 == 0) for row in snapshot(DAY)['rows'][:-2])
    assert writer.calls[0][2]['top_stock_count'] == N // 20
    assert len(provider.universe_calls) == 1 and len(provider.quote_calls) == N
    assert collection.provenance(DAY)['heat_hash']
    stamp = state['updated_at']
    assert not collection.tick(clock.advance(minutes=1))
    assert collection.status()['updated_at'] == stamp
    assert len(provider.quote_calls) == N


def test_before_18_no_new_snapshot_and_naive_times_rejected(tmp_path):
    collection, provider, writer, clock = worker(tmp_path, moment=EVENING.replace(hour=17))
    assert not collection.tick()
    assert not provider.universe_calls and not writer.calls
    with pytest.raises(ValueError):
        collection.tick(EVENING.replace(tzinfo=None))
    clock.advance(hours=1)
    assert collection.tick()


def test_empty_database_restart_bootstraps_only_latest_closed_session(tmp_path):
    first, provider, writer, clock = worker(tmp_path, moment=EVENING.replace(hour=9))
    assert not first.tick()
    assert first._meta('activation_date') == str(DAY)
    assert not provider.universe_calls and not provider.quote_calls and not writer.calls
    restarted, resumed, saved, later = worker(tmp_path, moment=EVENING + timedelta(days=1))
    assert restarted.tick()
    missed = restarted._day(DAY)
    assert missed is None
    assert resumed.universe_calls == [DAY + timedelta(days=1)]


def test_only_failed_stock_retried_and_universe_shared_with_both_metrics(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    provider.failures = {'sh600003'}
    assert not collection.tick()
    state = collection.status()
    assert state['completed'] == (N - 1) and state['pending'] == 1
    assert state['crowding_status'] == 'pending' and state['heat_status'] == 'pending'
    assert all(call[1] is None for call in writer.calls)
    assert not collection.tick(clock.advance(minutes=4))
    provider.failures.clear()
    collection.request_run('resume-failed-stock')
    assert collection.tick(clock.advance(minutes=1))
    assert len(provider.universe_calls) == 1
    assert len(provider.quote_calls) == (N + 4)
    assert provider.quote_calls[-1][0] == 'sh600003'
    assert writer.calls[0][3] == writer.calls[-1][3]


def test_crowding_waits_for_every_dated_price_volume_after_three_market_samples(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    publish_counts = []

    def record(owner, day, heat, crowding, source):
        publish_counts.append(len(provider.quote_calls))
        if crowding is not None:
            assert {call[0][:2] for call in provider.quote_calls} == {'sh', 'sz', 'bj'}
        return writer(owner, day, heat, crowding, source)

    collection.writer = record
    assert collection.tick()
    assert publish_counts == [N]
    assert len({call[0] for call in provider.quote_calls}) == N
    assert {call[0] for call in provider.quote_calls[:3]} == {'sh600009', 'sz600019', 'bj600119'}


@pytest.mark.parametrize('failure', ['mismatch', 'stale', 'transient_failure'])
def test_unverified_freshness_blocks_both_writes_until_sample_recovers(tmp_path, failure):
    collection, provider, writer, clock = worker(tmp_path)

    def bad_sample(quote):
        if quote['symbol'] == 'sh600009':
            return quote | {'status': failure, 'date': '2026-11-09' if failure == 'stale' else quote['date']}
        return quote

    provider.quote_override = bad_sample
    assert not collection.tick()
    assert not writer.calls
    assert collection.status()['crowding_status'] == 'pending'
    assert collection.status()['completed'] == (N - 1)
    provider.quote_override = None
    collection.request_run('resume-failed-sample')
    assert collection.tick(clock.advance(minutes=5))
    assert len(provider.universe_calls) == 1 and len(provider.quote_calls) == (N + 4)
    assert collection.status()['crowding_status'] == collection.status()['heat_status'] == 'written'


def test_fresh_sample_with_unknown_ma_allows_crowding_but_not_heat(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    provider.quote_override = lambda quote: quote | {'status': 'unknown_missing_ma', 'ma20': None} if quote['symbol'] == 'sh600009' else quote
    assert not collection.tick()
    assert collection.status()['crowding_status'] == 'written'
    assert collection.status()['heat_status'] == 'pending'
    assert len(provider.quote_calls) == N + 3  # Once per pass, never twice within a pass.
    assert len(writer.calls) == 1 and writer.calls[0][1] is None


def test_stale_unselected_stock_blocks_crowding_despite_three_current_samples(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    provider.quote_override = lambda quote: quote | {'status': 'stale', 'date': '2026-11-09'} if quote['symbol'] == 'sh600003' else quote
    assert not collection.tick()
    assert {call[0] for call in provider.quote_calls[:3]} == {'sh600009', 'sz600019', 'bj600119'}
    assert len(provider.quote_calls) == N + 3
    assert not writer.calls
    assert collection.status()['crowding_status'] == 'pending'
    provider.quote_override = None
    collection.request_run('resume-stale-stock')
    assert collection.tick(clock.advance(minutes=5))
    assert len(provider.quote_calls) == (N + 4)
    assert writer.calls[0][2]['price_verified_count'] == N


def test_restart_after_interruption_preserves_each_completed_stock(tmp_path):
    first, provider, writer, clock = worker(tmp_path)
    provider.exceptions['sz600019'] = CollectionInterrupted()
    assert not first.tick()
    assert first.status()['completed'] == 1  # First dated freshness sample committed.
    assert first.status()['status'] == 'paused'
    second, resumed, committed, later = worker(tmp_path, moment=EVENING + timedelta(minutes=1))
    assert second.tick()
    assert not resumed.universe_calls
    assert len(resumed.quote_calls) == (N - 1)
    assert 'sh600009' not in {call[0] for call in resumed.quote_calls}
    assert second.status()['status'] == 'complete'


def test_saved_failed_day_recovers_exact_day_after_midnight_without_new_pool(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    provider.failures = {'sh600003'}
    assert not collection.tick()
    provider.failures.clear()
    clock.value = EVENING + timedelta(days=1, hours=-8)  # Next day 10:00, only old snapshot.
    collection.request_run('morning-resume-after-exhaustion')
    assert collection.tick()
    assert provider.universe_calls == [DAY]
    assert provider.quote_calls[-1] == ('sh600003', DAY, 41)
    # Recovery this morning must not suppress the new 18:30 slot.
    clock.value = EVENING + timedelta(days=1)
    assert collection.tick()
    assert provider.universe_calls == [DAY, DAY + timedelta(days=1)]


def test_empty_database_ignores_old_activation_date(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    collection._put_meta(activation_date='2026-11-06')
    assert collection.tick()
    with collection.connect() as db:
        missed = [dict(row) for row in db.execute("SELECT * FROM days WHERE trade_date<'2026-11-10'")]
    assert missed == []
    assert provider.universe_calls == [DAY]
    assert {call[0] for call in writer.calls} == {DAY}


def test_empty_database_failed_bootstrap_remains_a_tracked_gap(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    provider.snapshot_override = snapshot(DAY)
    provider.snapshot_override['rows'].pop()
    assert not collection.tick()
    assert not writer.calls
    provider.snapshot_override = None
    clock.advance(days=1)
    assert collection.tick()
    assert collection._day(DAY)['status'] == 'blocked'
    assert collection._day(DAY)['error'] == 'historical_snapshot_missing'
    assert collection._meta('gap:heat:' + str(DAY))
    assert all(call[0] != DAY for call in writer.calls)


def test_weekend_calendar_check_is_cached_and_not_a_synthetic_trading_day(tmp_path):
    saturday = datetime(2026, 11, 14, 18, 30, tzinfo=CHINA)
    collection, provider, writer, clock = worker(tmp_path, moment=saturday)
    assert not collection.tick()
    assert not collection.tick(clock.advance(minutes=1))
    assert not provider.universe_calls and not writer.calls
    with collection.connect() as db:
        assert [row[0] for row in db.execute('SELECT trade_date FROM days')] == ['2026-11-13']


def test_new_year_calendar_not_treated_as_known_holiday(tmp_path):
    collection, provider, writer, clock = worker(tmp_path, moment=datetime(2027, 1, 4, 18, 30, tzinfo=CHINA))
    assert not collection.tick()
    assert collection.status()['error'] == 'calendar_incomplete'
    assert not provider.universe_calls
    collection.calendar_loader = lambda client: CALENDAR + [date(2027, 1, 4), date(2027, 12, 31)]
    assert collection.tick(clock.advance(minutes=30))
    assert provider.universe_calls == [date(2027, 1, 4)]


def test_incomplete_new_year_calendar_does_not_discard_saved_previous_day_repair(tmp_path):
    collection, provider, writer, clock = worker(tmp_path, moment=datetime(2026, 12, 31, 18, 30, tzinfo=CHINA))
    provider.failures = {'sh600003'}
    assert not collection.tick()
    provider.failures.clear()
    clock.value = datetime(2027, 1, 1, 10, tzinfo=CHINA)
    collection.request_run('repair-saved-day-after-exhaustion')
    assert collection.tick()
    assert provider.universe_calls == [date(2026, 12, 31)]
    assert provider.quote_calls[-1] == ('sh600003', date(2026, 12, 31), 41)
    assert collection._day(date(2026, 12, 31))['status'] == 'complete'
    assert collection._day(date(2027, 1, 1)) is None
    assert collection.status()['error'] == 'calendar_incomplete'


def test_initial_calendar_failure_does_not_expand_empty_table_backfill(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)

    def unavailable(client):
        raise RuntimeError('source unavailable')

    collection.calendar_loader = unavailable
    assert not collection.tick()
    collection.calendar_loader = lambda client: list(CALENDAR)
    clock.advance(days=1)
    assert collection.tick()
    assert collection._day(DAY) is None
    assert provider.universe_calls == [DAY + timedelta(days=1)]


def test_cross_process_lock_prevents_all_source_and_write_actions(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    collection.root.mkdir(parents=True)
    program = '''
import sys
import portalocker
with portalocker.Lock(sys.argv[1], timeout=0):
    print('locked', flush=True)
    sys.stdin.readline()
'''
    process = subprocess.Popen([sys.executable, '-c', program, str(collection.root / 'collection.lock')],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'locked'
        assert not collection.tick()
        with pytest.raises(CollectionBusy):
            collection.request_run('blocked-request')
    finally:
        try:
            process.communicate('\n', timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
    assert process.returncode == 0
    assert not provider.universe_calls and not writer.calls
    assert collection.tick()


def test_request_id_is_durable_and_terminal_run_id_remains_visible(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    queued = collection.request_run('same-request')
    assert queued['status'] == 'queued'
    assert collection.request_run('same-request') == queued
    with pytest.raises(ConfigurationFailure):
        collection.request_run('different-request')
    assert collection.tick()
    assert collection.status()['run_id'] == 'same-request'
    assert collection.request_run('same-request')['status'] == 'complete'
    assert not collection.tick(clock.advance(minutes=1))
    assert len(provider.quote_calls) == N


def test_same_request_committed_between_lookup_and_lock_failure_is_replayed(tmp_path, monkeypatch):
    first, provider, writer, _ = worker(tmp_path)
    retry, retried_provider, retried_writer, _ = worker(tmp_path)
    acquire = retry._acquire
    held = []

    def admit_before_lock_attempt():
        # The retry has already observed no row. Another process now commits it
        # and takes the execution lock before this retry attempts admission.
        first.request_run('admission-race-request')
        held.append(first._acquire())
        assert held[-1] is not None
        return acquire()

    monkeypatch.setattr(retry, '_acquire', admit_before_lock_attempt)
    try:
        result = retry.request_run('admission-race-request')
        assert result['id'] == 'admission-race-request'
        assert result['status'] == 'queued'
        with retry.connect() as db:
            assert db.execute('SELECT COUNT(*) FROM manual_runs').fetchone()[0] == 1
        assert not provider.universe_calls and not retried_provider.universe_calls
        assert not writer.calls and not retried_writer.calls
    finally:
        for lock in held:
            lock.release()


@pytest.mark.parametrize('trigger', ['manual', 'scheduled'])
def test_same_request_replays_while_another_worker_is_collecting(tmp_path, trigger):
    first, provider, writer, _ = worker(tmp_path)
    second, other_provider, other_writer, _ = worker(tmp_path)
    if trigger == 'manual':
        first.request_run('in-progress-request')
    entered, release = threading.Event(), threading.Event()
    results = []

    def block_quote(symbol):
        entered.set()
        assert release.wait(5)

    provider.on_quote = block_quote
    thread = threading.Thread(target=lambda: results.append(first.tick()))
    thread.start()
    try:
        assert entered.wait(5)
        current = first.status()['manual']
        assert current['trigger'] == trigger
        result = second.request_run(current['id'])
        assert result['id'] == current['id'] and result['status'] == 'running'
        state = second.status()
        assert state['running'] and state['status'] == state['manual']['status'] == 'running'
        assert not second.tick()
        with pytest.raises(CollectionBusy) as error:
            second.request_run('different-request')
        assert error.value.detail()['active_run']['id'] == current['id']
        assert error.value.detail()['code'] == 'collection_busy'
        with second.connect() as db:
            assert db.execute('SELECT COUNT(*) FROM manual_runs').fetchone()[0] == 1
        assert not other_provider.universe_calls and not other_writer.calls
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and results == [True]
    assert len(provider.quote_calls) == N and len(writer.calls) == 1


def test_uncommitted_same_request_conflict_never_admits_a_second_run(tmp_path, monkeypatch):
    first, _, _, _ = worker(tmp_path)
    retry, provider, writer, _ = worker(tmp_path)
    entered, release = threading.Event(), threading.Event()
    migrate = first._migrate_heat_methodology
    results = []

    def block_before_insert():
        entered.set()
        assert release.wait(5)
        migrate()

    monkeypatch.setattr(first, '_migrate_heat_methodology', block_before_insert)
    thread = threading.Thread(target=lambda: results.append(first.request_run('uncommitted-request')))
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(CollectionBusy) as error:
            retry.request_run('uncommitted-request')
        assert error.value.detail()['active_run'] is None
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and len(results) == 1
    assert retry.request_run('uncommitted-request')['id'] == results[0]['id']
    with retry.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM manual_runs').fetchone()[0] == 1
    assert not provider.universe_calls and not writer.calls


def test_queued_conflict_reports_the_existing_request(tmp_path):
    collection, _, _, _ = worker(tmp_path)
    queued = collection.request_run('queued-request')
    with pytest.raises(CollectionBusy) as error:
        collection.request_run('different-request')
    assert error.value.detail()['active_run'] == queued
    assert '排队' in str(error.value) and '暂停' not in str(error.value)


@pytest.mark.parametrize('cancelled', [False, True])
def test_stale_running_status_and_replay_are_consistently_paused(tmp_path, cancelled):
    collection, provider, writer, _ = worker(tmp_path)
    collection.request_run('interrupted-request')
    with collection.connect() as db:
        db.execute("UPDATE manual_runs SET status='running',phase='calendar',cancel_requested=?",
                   (int(cancelled),))
    state = collection.status()
    assert not state['running'] and state['status'] == 'paused'
    assert state['manual']['status'] == state['runs'][0]['status'] == 'paused'
    assert state['manual']['phase'] == 'paused' and state['progress'] is None
    assert state['error'] == state['manual']['error'] == ('cancelled' if cancelled else 'service_interrupted')
    replay = collection.request_run('interrupted-request')
    assert replay['status'] == 'paused' and replay['phase'] == 'paused'
    assert not provider.universe_calls and not writer.calls
    # A new intentional request performs durable recovery under the lock.
    assert collection.request_run('resume-request')['status'] == 'queued'
    with collection.connect() as db:
        previous = db.execute("SELECT * FROM manual_runs WHERE id='interrupted-request'").fetchone()
    assert previous['status'] == 'paused' and previous['error'] == state['error']


def test_run_finished_between_status_snapshot_and_lock_probe_is_not_interrupted(tmp_path, monkeypatch):
    collection, _, _, clock = worker(tmp_path)
    collection.request_run('finishing-request')
    with collection.connect() as db:
        db.execute("UPDATE manual_runs SET status='running',phase='writing'")
        db.execute("INSERT INTO days (trade_date,status,created_at,updated_at) VALUES (?,'running',?,?)",
                   (str(DAY), clock().isoformat(), clock().isoformat()))
    acquire = collection._acquire

    def finish_before_probe():
        collection._finish_run('finishing-request', 'complete')
        collection._update_day(DAY, status='complete', error=None)
        return acquire()

    monkeypatch.setattr(collection, '_acquire', finish_before_probe)
    state = collection.status()
    assert state['status'] == state['manual']['status'] == state['runs'][0]['status'] == 'complete'
    assert not state['running'] and state['progress'] is None and state['error'] is None


@pytest.mark.parametrize('state_exists', [False, True])
def test_lock_only_preparation_is_busy_even_without_an_active_run(tmp_path, state_exists):
    collection, _, _, _ = worker(tmp_path)
    if state_exists:
        with collection.connect():
            pass
    lock = collection._acquire()
    assert lock is not None
    try:
        state = collection.status()
        assert state['running'] and state['status'] == 'idle' and state['manual'] is None
        with pytest.raises(CollectionBusy) as error:
            collection.request_run('new-request')
        assert error.value.detail()['active_run'] is None
        assert '服务正忙' in str(error.value) and '暂停' not in str(error.value)
    finally:
        lock.release()


def test_terminal_replay_does_not_require_new_admission_configuration(tmp_path):
    collection, _, _, _ = worker(tmp_path)
    collection.request_run('existing-request')
    with collection.connect() as db:
        db.execute("UPDATE manual_runs SET status='complete',phase='complete'")
    collection.settings.market_collection_worker_enabled = False
    collection.settings.market_collection_writer_url = ''
    assert collection.request_run('existing-request')['status'] == 'complete'
    with pytest.raises(ConfigurationFailure):
        collection.request_run('new-request')


def test_manual_pause_persists_then_new_request_resumes_only_pending(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    collection.request_run('first-request')
    provider.on_quote = lambda symbol: collection.request_cancel('first-request') if symbol == 'sz600019' else None
    assert not collection.tick()
    assert collection.status()['status'] == 'paused'
    assert collection.status()['completed'] == 2
    previous = len(provider.quote_calls)
    assert not collection.tick(clock.advance(minutes=31))
    assert len(provider.quote_calls) == previous
    provider.on_quote = None
    collection.request_run('resume-request')
    assert collection.tick()
    assert len(provider.quote_calls) == N
    assert len(provider.universe_calls) == 1


@pytest.mark.parametrize('status_code,error', [(403, 'source_forbidden'), (429, 'source_rate_limited')])
def test_access_limit_stops_whole_round_without_host_switch_or_fast_retry(tmp_path, status_code, error):
    collection, provider, writer, clock = worker(tmp_path)
    provider.exceptions['sz600019'] = SinaRateLimitError(status_code)
    assert not collection.tick()
    assert len(provider.quote_calls) == 2
    assert collection.status()['status'] == 'paused'
    assert collection.status()['error'] == error
    with collection.connect() as db:
        assert db.execute("SELECT error FROM observations WHERE symbol='sz600019'").fetchone()[0] == error
    assert not collection.tick(clock.advance(hours=2))
    assert len(provider.quote_calls) == 2
    provider.exceptions.clear()
    collection.request_run('manual-before-cooldown')
    assert not collection.tick()
    assert len(provider.quote_calls) == 2
    clock.value = EVENING + timedelta(days=1)
    collection.request_run('manual-after-cooldown')
    assert collection.tick()
    assert provider.universe_calls == [DAY, DAY + timedelta(days=1)]
    assert len([call for call in provider.quote_calls if call[1] == DAY]) == (N + 1)  # Only the interrupted stock was attempted twice.


def test_automatic_retries_are_bounded_and_do_not_drop_successes(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    provider.failures = {'sh600003'}
    assert not collection.tick()
    assert not collection.tick(clock.advance(minutes=5))
    assert not collection.tick(clock.advance(minutes=30))
    count = len(provider.quote_calls)
    assert count == (N + 3)
    assert not collection.tick(clock.advance(hours=1))
    assert len(provider.quote_calls) == count
    assert collection.status()['completed'] == (N - 1)


def test_writer_error_preserves_raw_then_retries_without_recollecting(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    writer.error = WriterFailure('writer_permission_denied')
    assert not collection.tick()
    assert collection.status()['completed'] == N
    assert collection.status()['error'] == 'writer_permission_denied'
    writer.error = None
    assert collection.tick(clock.advance(minutes=5))
    assert len(provider.universe_calls) == 1 and len(provider.quote_calls) == N
    assert collection.status()['heat_status'] == collection.status()['crowding_status'] == 'written'


def test_preserved_legacy_crowding_does_not_discard_new_heat_result(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    writer.preserve = {'crowding'}
    assert collection.tick()
    assert collection.status()['crowding_status'] == 'existing_preserved'
    assert collection.status()['heat_status'] == 'written'
    assert collection.provenance(DAY)['crowding_hash'] is None
    assert collection.provenance(DAY)['heat_hash']


def test_preserved_legacy_results_still_reconcile_the_display_cache(tmp_path):
    refreshed = []
    collection, provider, writer, clock = worker(tmp_path, refresh=lambda: refreshed.append(1) or True)
    writer.preserve = {'heat', 'crowding'}
    assert collection.tick()
    assert refreshed == [1]
    assert not collection.status()['cache_pending']
    assert not collection.tick(clock.advance(minutes=5))
    assert refreshed == [1]
    assert not collection.status()['cache_pending']
    assert collection.provenance(DAY)['heat_hash'] is None
    assert collection.provenance(DAY)['crowding_hash'] is None


def test_cache_retry_needs_no_source_requests(tmp_path):
    refreshes = []
    collection, provider, writer, clock = worker(tmp_path, refresh=lambda: refreshes.append(1) or False)
    assert collection.tick()
    assert collection.status()['cache_pending']
    calls = len(provider.quote_calls)
    collection.refresh = lambda: refreshes.append(1) or True
    assert not collection.tick(clock.advance(minutes=5))
    assert not collection.status()['cache_pending']
    assert len(provider.quote_calls) == calls and refreshes == [1, 1]


def test_malformed_completed_quote_stays_pending_and_cannot_publish_heat(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    provider.quote_override = lambda quote: quote | {'ma20': None} if quote['symbol'] == 'sh600003' else quote
    assert not collection.tick()
    assert collection.status()['pending'] == 1
    assert not any(call[1] for call in writer.calls)
    provider.quote_override = None
    collection.request_run('retry-corrected-quote')
    assert collection.tick(clock.advance(minutes=5))
    assert len(provider.quote_calls) == (N + 4)


def test_source_exception_text_and_writer_secrets_are_not_exposed(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    provider.exceptions['sh600003'] = RuntimeError('private database password token')
    writer.error = RuntimeError('mysql://secret-account:supersecret@host')
    assert not collection.tick()
    public = json.dumps(collection.status())
    assert 'private' not in public and 'supersecret' not in public and 'mysql://' not in public


def test_request_pacing_uses_start_times_instead_of_adding_a_second_after_network(tmp_path, monkeypatch):
    import app.services.market_collection as module
    collection, _, _, _ = worker(tmp_path)
    elapsed, starts = [0.0], []
    monkeypatch.setattr(module.monotonic_time, 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(collection.stop_event, 'wait', lambda delay: elapsed.__setitem__(0, elapsed[0] + delay))

    class Client:
        def get(self, *args, **kwargs):
            starts.append(elapsed[0])
            elapsed[0] += 0.25 if len(starts) < 3 else 1.5
            return httpx.Response(200)

    client = PacedHTTPClient(Client(), collection, lambda: None)
    for _ in range(4):
        client.get('https://example.invalid/test')
    assert starts == pytest.approx([0.0, 1.0, 2.0, 3.5])


def test_background_start_checks_today_immediately_and_stop_releases_lock(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    del collection.start
    finished = threading.Event()
    real_tick = collection.tick

    def tick(moment):
        try:
            return real_tick(moment)
        finally:
            finished.set()

    collection.tick = tick
    collection.start()
    assert finished.wait(5)
    collection.stop()
    assert not collection.worker.is_alive()
    assert collection.status()['status'] == 'complete'
    assert provider.universe_calls == [DAY]
    lock = collection._acquire()
    assert lock is not None
    lock.release()


def test_unverified_heat_collects_dated_hq_only_and_never_runs_legacy_ma(tmp_path):
    collection, provider, writer, clock = worker(tmp_path, edge_cases=True)
    provider.heat_verified = False
    assert not collection.tick()
    state = collection.status()
    assert state['status'] == state['heat_status'] == 'blocked'
    assert state['error'] == 'heat_methodology_unverified'
    assert state['crowding_status'] == 'written'
    assert state['completed'] == N and state['heat_pending_count'] == N
    assert state['above_count'] == state['valid_count'] == 0
    assert state['rawprogress']['price_verified_count'] == N
    assert state['rawprogress']['dated_rows_saved'] == N
    assert provider.universe_calls == [DAY] and len(provider.hq_calls) == 3
    assert not provider.quote_calls
    assert len(writer.calls) == 1 and writer.calls[0][1] is None
    saved = json.loads(collection._day(DAY)['snapshot_json'])
    assert saved['source_has_trading_date'] and saved['dated_snapshot_verified']
    assert saved['rows'][0]['amount'] == '1000.25'
    assert saved['rows'][-1]['paused'] is None  # Zero volume is not a paused proof.
    assert writer.calls[0][2]['total_money'] == str(sum(Decimal(row['amount']) for row in saved['rows']).quantize(Decimal('.01')))
    stamp = state['updated_at']
    assert not collection.tick(clock.advance(minutes=10))
    assert len(provider.hq_calls) == 3 and collection.status()['updated_at'] != stamp
    assert collection._day(DAY)['attempts'] == 2  # Bounded capability recheck, no raw refetch.


def test_hq_batch_checkpoint_resumes_after_restart_without_repeating_successes(tmp_path):
    first, provider, writer, clock = worker(tmp_path)
    provider.heat_verified = False
    provider.hq_exception_at = 2
    assert not first.tick()
    assert first.status()['status'] == 'paused'
    assert first.status()['rawprogress']['dated_rows_saved'] == 40
    assert first.status()['completed'] == 40 and first.status()['pending'] == N - 40
    assert first.status()['heat_pending_count'] == N
    assert not writer.calls and not provider.quote_calls
    with first.connect() as db:
        saved = {row[0] for row in db.execute('SELECT symbol FROM hq_checkpoints')}
    second, resumed, committed, later = worker(tmp_path, moment=EVENING + timedelta(minutes=1))
    resumed.heat_verified = False
    assert not second.tick()
    assert not resumed.universe_calls and not resumed.quote_calls
    assert resumed.hq_cached == [saved]
    assert len(resumed.hq_calls) == 2
    assert not saved.intersection(symbol for _, batch in resumed.hq_calls for symbol in batch)
    assert second.status()['crowding_status'] == 'written'
    assert second.status()['heat_status'] == 'blocked'


@pytest.mark.parametrize('http_status', [403, 429])
def test_hq_access_limit_preserves_previous_batches_and_pauses_round(tmp_path, http_status):
    collection, provider, writer, clock = worker(tmp_path)
    provider.heat_verified = False
    provider.hq_exception_at = 2
    provider.hq_exception = SinaRateLimitError(http_status)
    assert not collection.tick()
    assert collection.status()['rawprogress']['dated_rows_saved'] == 40
    assert collection.status()['status'] == 'paused'
    assert not collection.tick(clock.advance(hours=2))
    assert len(provider.hq_calls) == 2 and not writer.calls
    provider.hq_exception_at = None
    collection.request_run('resume-hq')
    assert not collection.tick()
    assert len(provider.hq_calls) == 2  # Manual requests cannot bypass source cooldown.
    assert collection.status()['rawprogress']['dated_rows_saved'] == 40
    assert collection.status()['crowding_status'] == 'pending'
    assert collection.status()['runs'][0]['status'] == 'paused'
    assert not provider.quote_calls


def test_hq_cache_is_bound_to_node_snapshot_hash(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    provider.heat_verified = False
    provider.hq_exception_at = 2
    assert not collection.tick()
    old = collection._day(DAY)['snapshot_hash']
    replacement = snapshot(DAY)
    replacement['raw']['capture_revision'] = 'different-node-capture'
    collection._store_snapshot(str(DAY), replacement)
    assert collection._day(DAY)['snapshot_hash'] != old
    provider.hq_exception_at = None
    assert not collection.tick(clock.advance(minutes=1))
    assert provider.hq_cached[-1] == set()
    assert len(provider.hq_calls) == 5  # Three fresh batches, not two with old evidence.
    assert collection.status()['crowding_status'] == 'written'


def test_historical_undated_snapshot_cannot_be_upgraded_with_todays_hq(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    provider.heat_verified = False
    provider.hq_exception_at = 2
    assert not collection.tick()
    before = len(provider.hq_calls)
    clock.value = EVENING + timedelta(days=1, hours=-8)
    provider.hq_exception_at = None
    assert not collection.tick()
    saved = collection._day(DAY)
    assert saved['status'] == 'blocked' and saved['error'] == 'historical_snapshot_unverified'
    assert saved['heat_status'] == saved['crowding_status'] == 'blocked'
    assert len(provider.hq_calls) == before and provider.universe_calls == [DAY]
    assert not provider.quote_calls and not writer.calls


def test_dated_snapshot_writer_retry_after_midnight_needs_no_source_requests(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    provider.heat_verified = False
    writer.error = WriterFailure('writer_permission_denied')
    assert not collection.tick()
    assert collection.status()['error'] == 'writer_permission_denied'
    assert collection._dated_snapshot(json.loads(collection._day(DAY)['snapshot_json']), str(DAY))
    writer.error = None
    clock.value = EVENING + timedelta(days=1, hours=-8)
    collection.request_run('resume-saved-writer-input')
    assert not collection.tick()
    assert provider.universe_calls == [DAY] and len(provider.hq_calls) == 3
    assert not provider.quote_calls
    assert collection.status()['crowding_status'] == 'written'
    assert collection.status()['heat_status'] == 'blocked'


def test_last_hq_batch_checkpoint_can_be_reassembled_offline_after_midnight(tmp_path):
    first, provider, writer, clock = worker(tmp_path)
    provider.heat_verified = False
    fetch = provider.fetch_dated_snapshot

    def interrupted_after_all_checkpoints(*args, **kwargs):
        fetch(*args, **kwargs)
        raise CollectionInterrupted()

    provider.fetch_dated_snapshot = interrupted_after_all_checkpoints
    assert not first.tick()
    assert first.status()['rawprogress']['dated_rows_saved'] == N
    assert not first._dated_snapshot(json.loads(first._day(DAY)['snapshot_json']), str(DAY))
    assert not writer.calls
    second, resumed, committed, later = worker(tmp_path, moment=EVENING + timedelta(days=1, hours=-8))
    resumed.heat_verified = False
    assert not second.tick()
    assert not resumed.hq_calls and not resumed.universe_calls and not resumed.quote_calls
    assert len(resumed.hq_cached[0]) == N
    assert second._dated_snapshot(json.loads(second._day(DAY)['snapshot_json']), str(DAY))
    assert second.status()['crowding_status'] == 'written'
    assert second.status()['heat_status'] == 'blocked'


def test_dated_snapshot_rejects_one_wrong_day_without_partial_crowding_write(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    provider.heat_verified = False
    provider.hq_override = lambda row: {'date': '2026-11-09'} if row['symbol'] == 'bj600087' else {}
    assert not collection.tick()
    assert collection.status()['error'] == 'snapshot_invalid'
    assert collection.status()['rawprogress']['dated_rows_saved'] == 80
    assert not writer.calls and not provider.quote_calls


def test_methodology_migration_archives_bfq_and_preserves_row_ownership(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    assert collection.tick()
    old_hash = collection._day(DAY)['heat_hash']
    old_quotes = len(provider.quote_calls)
    with collection.connect() as db:
        old_heat = json.loads(db.execute('SELECT heat_json FROM days').fetchone()[0])
        old_heat.update(methodology=LEGACY_SINA_HEAT_METHOD, price_basis='bfq', denominator='universe')
        db.execute('UPDATE days SET heat_json=?', (json.dumps(old_heat),))
        for row in db.execute('SELECT symbol,quote_json FROM observations').fetchall():
            quote = json.loads(row['quote_json'])
            quote.update(methodology=LEGACY_SINA_HEAT_METHOD, verified=False)
            db.execute('UPDATE observations SET quote_json=? WHERE symbol=?', (json.dumps(quote), row['symbol']))
        db.execute("DELETE FROM metadata WHERE key='heat_methodology'")
        db.execute('INSERT INTO metadata VALUES (?,?)', ('write-intent:' + str(DAY) + ':heat', old_hash))
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('source_paused_until',?)", ((EVENING + timedelta(days=1)).isoformat(),))
    # Even the read-only UI must not label old observations with the new basis.
    assert collection.status()['above_count'] == collection.status()['valid_count'] == 0
    collection.request_run('migrate-bfq')
    assert collection._day(DAY)['heat_hash'] == old_hash
    assert collection._meta('write-intent:' + str(DAY) + ':heat') == old_hash
    assert collection._meta('source_paused_until')
    assert collection.provenance(DAY)['heat_method'] == LEGACY_SINA_HEAT_METHOD
    with collection.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM methodology_archive WHERE kind='quote'").fetchone()[0] == N
        assert db.execute("SELECT COUNT(*) FROM methodology_archive WHERE kind='heat'").fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM observations WHERE quote_json IS NOT NULL').fetchone()[0] == 0
    provider.heat_verified = False
    assert not collection.tick()
    assert len(provider.quote_calls) == old_quotes
    assert collection._day(DAY)['heat_hash'] == old_hash
    # Cooldown blocks source reconciliation too, without losing ownership.
    assert collection.status()['heat_status'] == 'blocked'
    clock.value = EVENING + timedelta(days=1)
    collection.tick()
    assert collection._day(DAY)['heat_status'] == 'existing_preserved'
    assert collection.status()['above_count'] == 0


def test_claimed_capability_does_not_turn_legacy_quotes_into_qfq_evidence(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    provider.quote_override = lambda quote: quote | {'methodology': LEGACY_SINA_HEAT_METHOD, 'verified': False}
    assert not collection.tick()
    assert collection.status()['valid_count'] == collection.status()['above_count'] == 0
    assert collection.status()['heat_pending_count'] == N
    assert not any(call[1] for call in writer.calls)
    assert collection.status()['crowding_status'] == 'written'  # Raw dated prices still match.
    with collection.connect() as db:
        assert {row[0] for row in db.execute('SELECT DISTINCT error FROM observations')} == {'heat_methodology_mismatch'}


def test_writer_gate_rejects_wrong_method_even_with_publishable_flag(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    assert collection.tick()
    before = len(writer.calls)
    assert not collection._publish(str(DAY), {'publishable': True, 'methodology': LEGACY_SINA_HEAT_METHOD}, None, snapshot(DAY))
    assert len(writer.calls) == before
    assert collection._day(DAY)['error'] == 'heat_methodology_mismatch'


def test_same_day_node_hq_mismatch_refreshes_pool_only_on_bounded_next_retry(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    provider.heat_verified = False
    provider.hq_exception_at = 2
    provider.hq_exception = SinaDataError('node price is stale', code='snapshot_quote_mismatch')
    original = provider.fetch_dated_snapshot
    old_hashes = []
    def refresh_after_mismatch(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except SinaDataError:
            old_hashes.append(collection._day(DAY)['snapshot_hash'])
            provider.snapshot_override = snapshot(DAY)
            provider.snapshot_override['rows'][45]['close'] = '999'
            raise
    provider.fetch_dated_snapshot = refresh_after_mismatch
    assert not collection.tick()
    old_hash = old_hashes[0]
    assert provider.universe_calls == [DAY, DAY]
    assert provider.hq_cached[-1] == set()  # Prior batches belonged to the old capture.
    saved = json.loads(collection._day(DAY)['snapshot_json'])
    assert saved['rows'][45]['close'] == '999'
    assert saved['node_snapshot_hash'] != old_hash
    assert collection.status()['completed'] == N and collection.status()['crowding_status'] == 'written'
    assert not provider.quote_calls
    with collection.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM methodology_archive WHERE kind='snapshot'").fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM hq_checkpoints WHERE node_snapshot_hash=?', (old_hash,)).fetchone()[0] == 40


def test_transport_failure_does_not_refresh_saved_node_snapshot(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    provider.heat_verified = False
    provider.hq_exception_at = 2
    provider.hq_exception = SinaDataError('network failure', code='http_error')
    assert not collection.tick()
    assert collection._meta('snapshot-refresh:' + str(DAY)) is None
    provider.hq_exception_at = None
    assert not collection.tick(clock.advance(minutes=5))
    assert provider.universe_calls == [DAY]
    assert len(provider.hq_cached[-1]) == 40
    assert collection.status()['crowding_status'] == 'written'


def historical_provider(provider):
    provider.historical_capability = lambda: {'verified': True}
    original = provider.fetch_universe
    def dated(day):
        result = original(day)
        result.update(source_has_trading_date=True, dated_snapshot_verified=True,
                      date_verified_by='verified_test_source')
        for row in result['rows']:
            row['date'] = str(day)
        return result
    provider.fetch_universe = dated


def test_exact_1830_boundary_and_shanghai_timezone(tmp_path):
    collection, provider, writer, clock = worker(tmp_path, moment=EVENING.replace(minute=29))
    assert not collection.tick()
    assert not provider.universe_calls
    clock.advance(minutes=1)
    assert collection.tick(clock().astimezone(timezone.utc))
    assert collection.status()['schedule'] == ['18:30']
    assert collection.status()['next_run_at'].endswith('18:30:00+08:00')


def test_source_dates_independent_gap_repair_and_preservation(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    historical_provider(provider)
    writer.dates = {'heat': {'2026-11-06', '2026-11-09'}, 'crowding': {'2026-11-06'}}
    assert collection.tick()
    assert set(provider.universe_calls) == {date(2026, 11, 9), DAY}
    backfill = next(call for call in writer.calls if call[0] == date(2026, 11, 9))
    assert backfill[1] is None and backfill[2] is not None
    assert collection._day('2026-11-09')['heat_status'] == 'existing_preserved'
    assert collection.status()['gaps'] == []
    previous_calls = len(writer.calls)
    collection.request_run('same-day-recheck')
    assert not collection.tick()
    assert len(writer.calls) == previous_calls


def test_manual_before_schedule_uses_latest_closed_session(tmp_path):
    collection, provider, writer, clock = worker(tmp_path, moment=EVENING.replace(hour=10))
    historical_provider(provider)
    writer.dates = {'heat': {'2026-11-06'}, 'crowding': {'2026-11-06'}}
    collection.request_run('morning-backfill')
    assert collection.tick()
    assert provider.universe_calls == [date(2026, 11, 9)]
    assert collection._day(DAY) is None


def test_unavailable_history_keeps_existing_metric_and_partial_run(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    writer.dates = {'heat': {'2026-11-06', '2026-11-09'}, 'crowding': {'2026-11-06'}}
    assert collection.tick()  # A later date is not held behind the missing history.
    missing = collection._day('2026-11-09')
    assert missing['heat_status'] == 'existing_preserved'
    assert missing['crowding_status'] == 'blocked'
    assert missing['error'] == 'historical_snapshot_missing'
    assert collection.status()['runs'][0]['status'] == 'partial'


def test_source_read_failure_is_not_treated_as_empty_database(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    def failed(owner):
        raise WriterFailure('writer_connection_failed')
    collection.source_dates_loader = failed
    assert not collection.tick()
    assert collection.status()['error'] == 'writer_connection_failed'
    assert not provider.universe_calls and not writer.calls


def test_deleted_source_row_is_repaired_from_saved_checkpoint(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    assert collection.tick()
    writer.dates['crowding'].clear()
    collection.request_run('repair-deleted')
    assert collection.tick()
    assert len(provider.universe_calls) == 1 and len(provider.quote_calls) == N
    assert writer.calls[-1][1] is None and writer.calls[-1][2] is not None


def test_completed_previous_session_manual_morning_is_success_not_time_error(tmp_path):
    collection, provider, writer, clock = worker(tmp_path, moment=EVENING.replace(hour=10))
    writer.dates = {'heat': {'2026-11-09'}, 'crowding': {'2026-11-09'}}
    collection.request_run('already-complete')
    assert not collection.tick()
    assert collection.status()['runs'][0]['status'] == 'complete'
    assert collection.status()['runs'][0]['error'] is None
    assert not writer.calls and not provider.universe_calls


def test_source_read_failure_preserves_completed_rows_and_cache_pending(tmp_path):
    collection, provider, writer, clock = worker(tmp_path, refresh=lambda: False)
    assert collection.tick()
    prior = collection._day(DAY)
    def failed(owner):
        raise WriterFailure('writer_connection_failed')
    collection.source_dates_loader = failed
    collection.request_run('database-down')
    assert not collection.tick()
    assert collection._day(DAY)['heat_hash'] == prior['heat_hash']
    assert collection.status()['cache_pending'] is True
    assert collection.status()['runs'][0]['error'] == 'writer_connection_failed'
    assert collection.status()['heat_status'] == 'written'


def test_stream_and_get_share_request_start_budget(tmp_path, monkeypatch):
    import app.services.market_collection as module
    collection, _, _, _ = worker(tmp_path)
    elapsed, starts = [0.0], []
    monkeypatch.setattr(module.monotonic_time, 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(collection.stop_event, 'wait', lambda delay: elapsed.__setitem__(0, elapsed[0] + delay))
    class Client:
        def get(self, *args, **kwargs):
            starts.append(elapsed[0])
            return httpx.Response(200)
        @contextmanager
        def stream(self, *args, **kwargs):
            starts.append(elapsed[0])
            yield httpx.Response(200, content=b'archive')
    client = PacedHTTPClient(Client(), collection, lambda: None)
    client.get('https://example.invalid/get')
    with client.stream('GET', 'https://example.invalid/archive') as response:
        assert b''.join(response.iter_bytes()) == b'archive'
    client.get('https://example.invalid/get')
    assert starts == pytest.approx([0, 1, 2])
    assert collection._meta('last_http_at')


@pytest.mark.parametrize('status', [403, 429])
def test_stream_rate_limit_closes_response_before_body(tmp_path, status):
    collection, _, _, _ = worker(tmp_path)
    closed = []
    class Client:
        @contextmanager
        def stream(self, *args, **kwargs):
            try:
                yield httpx.Response(status)
            finally:
                closed.append(True)
    client = PacedHTTPClient(Client(), collection, lambda: None)
    with pytest.raises(SinaRateLimitError) as error:
        with client.stream('GET', 'https://example.invalid/archive'):
            pytest.fail('rate-limited response was exposed')
    assert error.value.status_code == status and closed == [True]


def test_stream_cancellation_between_chunks_closes_transport(tmp_path):
    collection, _, _, _ = worker(tmp_path)
    consumed, closed = [], []
    class Response:
        status_code = 200
        def iter_bytes(self):
            for index in range(3):
                consumed.append(index)
                yield b'chunk'
    class Client:
        @contextmanager
        def stream(self, *args, **kwargs):
            try:
                yield Response()
            finally:
                closed.append(True)
    client = PacedHTTPClient(Client(), collection, lambda: collection._check(None))
    with pytest.raises(CollectionInterrupted):
        with client.stream('GET', 'https://example.invalid/archive') as response:
            blocks = response.iter_bytes()
            assert next(blocks) == b'chunk'
            collection.stop_event.set()
            next(blocks)
    assert consumed == [0] and closed == [True]


@pytest.mark.parametrize('heat_verified', [True, False])
def test_verified_dated_crowding_publishes_before_pending_heat(tmp_path, heat_verified):
    collection, provider, writer, clock = worker(tmp_path)
    historical_provider(provider)
    provider.heat_verified = heat_verified
    provider.failures = {'sh600003'}
    provider.on_quote = lambda symbol: (
        None if writer.calls and writer.calls[0][2] is not None else pytest.fail('heat started before crowding commit'))
    assert not collection.tick()
    assert collection._day(DAY)['crowding_status'] == 'written'
    assert collection._day(DAY)['heat_status'] in ('pending', 'blocked')
    assert writer.calls[0][1] is None
    assert writer.calls[0][2]['prices_verified'] is True
    assert writer.calls[0][2]['price_verified_count'] == N
    assert not any(call[1] for call in writer.calls)


def test_verified_dated_crowding_survives_heat_network_pause(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    historical_provider(provider)
    provider.exceptions['sh600000'] = SinaRateLimitError(403)
    assert not collection.tick()
    assert collection._day(DAY)['crowding_status'] == 'written'
    assert collection._day(DAY)['error'] == 'source_forbidden'
    assert writer.calls[0][2]['prices_verified'] is True


def test_existing_heat_and_repaired_dated_crowding_finish_without_factor_requests(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    historical_provider(provider)
    collection.source_dates_loader = lambda owner: {'heat': {DAY}, 'crowding': set()}
    provider.on_quote = lambda symbol: pytest.fail('preserved heat must not be recalculated')
    assert collection.tick()
    row = collection._day(DAY)
    assert row['status'] == 'complete' and row['error'] is None
    assert row['heat_status'] == 'existing_preserved' and row['crowding_status'] == 'written'
    assert provider.quote_calls == []


def test_missing_factor_error_survives_per_stock_and_day_status(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    historical_provider(provider)
    original = provider.fetch_quote
    def fetch(symbol, day, **kwargs):
        if symbol == 'sh600000':
            return {'symbol': symbol, 'day': str(day), 'status': 'transient_failure', 'error': 'adjustment_unavailable'}
        return original(symbol, day, **kwargs)
    provider.fetch_quote = fetch
    assert not collection.tick()
    assert collection._day(DAY)['error'] == 'adjustment_unavailable'
    with collection.connect() as db:
        row = db.execute('SELECT error FROM observations WHERE day=? AND symbol=?', (str(DAY), 'sh600000')).fetchone()
    assert row['error'] == 'adjustment_unavailable'


def test_diagnostic_logs_include_stage_counts_and_safe_errors_without_credentials(tmp_path, caplog):
    import logging
    collection, provider, writer, clock = worker(tmp_path)
    historical_provider(provider)
    with caplog.at_level(logging.INFO, logger='app.services.market_collection'):
        assert collection.tick()
    assert 'Market gap plan target=2026-11-10' in caplog.text
    assert 'source=sina securities=120 dated=True' in caplog.text
    assert 'Market heat validation date=2026-11-10' in caplog.text
    assert 'Market publication date=2026-11-10' in caplog.text
    assert 'phase=ma20' in caplog.text
    assert 'password' not in caplog.text.lower()
    assert 'mysql+' not in caplog.text


def test_monday_source_friday_target_collects_tuesday_through_friday(tmp_path):
    collection, provider, writer, clock = worker(tmp_path, moment=EVENING.replace(day=13))
    historical_provider(provider)
    writer.dates = {'heat': {'2026-11-09'}, 'crowding': {'2026-11-09'}}
    assert collection.tick()
    expected = [date(2026, 11, day) for day in (10, 11, 12, 13)]
    assert provider.universe_calls == expected
    assert {call[0] for call in writer.calls if call[1]} == set(expected)
    assert {call[0] for call in writer.calls if call[2]} == set(expected)
    assert collection._day('2026-11-06') is None
    assert collection.status()['gaps'] == []


def test_each_metric_boundary_excludes_earlier_holes_and_heat_requests(tmp_path):
    collection, provider, writer, clock = worker(tmp_path, moment=EVENING.replace(day=13))
    historical_provider(provider)
    writer.dates = {'heat': {'2026-11-11'}, 'crowding': {'2026-11-09'}}
    assert collection.tick()
    assert {call[0] for call in writer.calls if call[1]} == {date(2026, 11, 12), date(2026, 11, 13)}
    assert {call[0] for call in writer.calls if call[2]} == {date(2026, 11, day) for day in (10, 11, 12, 13)}
    assert not any(day < date(2026, 11, 12) for _, day, _ in provider.quote_calls)
    assert collection._day('2026-11-10')['heat_status'] == 'outside_window'
    assert collection._day('2026-11-10')['crowding_status'] == 'written'
    assert '2026-11-10' not in writer.dates['heat']


@pytest.mark.parametrize('stale_status', ['pending', 'partial', 'blocked', 'paused'])
def test_older_tracked_failures_do_not_expand_source_boundaries(tmp_path, stale_status):
    collection, provider, writer, clock = worker(tmp_path, moment=EVENING.replace(day=13))
    historical_provider(provider)
    collection._migrate_heat_methodology()
    with collection.connect() as db:
        for day in ('2026-11-06', '2026-11-10'):
            db.execute("INSERT INTO days(trade_date,status,error,created_at,updated_at) VALUES(?,?,?,?,?)",
                       (day, stale_status, 'source_unavailable', clock().isoformat(), clock().isoformat()))
    writer.dates = {'heat': {'2026-11-11'}, 'crowding': {'2026-11-11'}}
    assert collection.tick()
    assert provider.universe_calls == [date(2026, 11, 12), date(2026, 11, 13)]
    assert collection._day('2026-11-10')['status'] == 'superseded'
    assert collection._day('2026-11-06')['heat_status'] == 'outside_window'
    assert collection.status()['gaps'] == []


@pytest.mark.parametrize(('moment', 'expected_days'), [
    (EVENING.replace(day=13, minute=29), (10, 11, 12)),
    (EVENING.replace(day=14), (10, 11, 12, 13)),
    (EVENING.replace(day=15, hour=10), (10, 11, 12, 13)),
])
def test_manual_closed_session_boundary_with_weekends(tmp_path, moment, expected_days):
    collection, provider, writer, clock = worker(tmp_path, moment=moment)
    historical_provider(provider)
    writer.dates = {'heat': {'2026-11-09'}, 'crowding': {'2026-11-09'}}
    collection.request_run('closed-only')
    assert collection.tick()
    assert provider.universe_calls == [date(2026, 11, day) for day in expected_days]


def test_one_empty_metric_only_bootstraps_target_while_other_backfills(tmp_path):
    collection, provider, writer, clock = worker(tmp_path, moment=EVENING.replace(day=13))
    historical_provider(provider)
    writer.dates = {'heat': set(), 'crowding': {'2026-11-09'}}
    assert collection.tick()
    assert {call[0] for call in writer.calls if call[1]} == {date(2026, 11, 13)}
    assert {call[0] for call in writer.calls if call[2]} == {date(2026, 11, day) for day in (10, 11, 12, 13)}
    assert collection._day('2026-11-10')['heat_status'] == 'outside_window'


def test_failed_earlier_heat_gap_survives_later_successes(tmp_path):
    collection, provider, writer, clock = worker(tmp_path, moment=EVENING.replace(day=13))
    historical_provider(provider)
    writer.dates = {'heat': {'2026-11-09'}, 'crowding': {'2026-11-09'}}
    original = provider.fetch_quote
    def quote(symbol, day, **kwargs):
        result = original(symbol, day, **kwargs)
        if day == date(2026, 11, 10) and symbol == 'sh600003':
            result.update(status='transient_failure', error='adjustment_unavailable')
        return result
    provider.fetch_quote = quote
    assert collection.tick()
    assert writer.dates['heat'] == {'2026-11-09', '2026-11-11', '2026-11-12', '2026-11-13'}
    assert writer.dates['crowding'] == {'2026-11-09', '2026-11-10', '2026-11-11', '2026-11-12', '2026-11-13'}
    assert sum(day == date(2026, 11, 10) and symbol == 'sh600003' for symbol, day, _ in provider.quote_calls) == 4
    assert collection._meta('gap:heat:2026-11-10')
    provider.fetch_quote = original
    collection.request_run('resume-earliest-heat')
    assert collection.tick()
    assert writer.dates['heat'] == writer.dates['crowding']
    assert collection.status()['gaps'] == []


@pytest.mark.parametrize('unhashable', [False, True])
def test_malicious_source_error_code_never_enters_state_or_logs(tmp_path, caplog, unhashable):
    collection, provider, writer, clock = worker(tmp_path)
    secret = 'https://private.invalid/?token=supersecret\nFORGED LOG'
    class SourceError(Exception):
        code = {'secret': secret} if unhashable else secret
    def failed(day):
        raise SourceError(secret)
    provider.fetch_universe = failed
    assert not collection.tick()
    assert collection._day(DAY)['error'] == 'quote_unavailable'
    assert 'supersecret' not in json.dumps(collection.status()) + caplog.text
    assert 'FORGED LOG' not in caplog.text


@pytest.mark.parametrize('unsafe', ['privatewordsecret', {'token': 'privatewordsecret'}])
def test_unresolved_quote_log_only_uses_known_error_codes(tmp_path, caplog, unsafe):
    collection, provider, writer, clock = worker(tmp_path)
    provider.quote_override = lambda quote: quote | {'error': unsafe, 'status': 'transient_failure'}
    assert not collection.tick()
    assert 'privatewordsecret' not in caplog.text
    assert 'quote_unavailable' in caplog.text
    assert collection._day(DAY)['error'] == 'source_partial'


def test_automatic_upgrade_replaces_blocked_legacy_snapshot_with_current_method_marker(tmp_path):
    collection, legacy, writer, clock = worker(tmp_path)
    legacy.heat_verified = False
    writer.dates['heat'] = {'2026-11-09'}
    assert not collection.tick()
    assert collection._meta('heat_methodology') == JOINQUANT_HEAT_METHOD
    assert collection._day(DAY)['error'] == 'heat_methodology_unverified'
    old_snapshot = collection._day(DAY)['snapshot_json']
    old_crowding_hash = collection._day(DAY)['crowding_hash']
    writer.dates['crowding'].add('2026-11-13')
    writer.calls.clear()
    clock.value = EVENING.replace(day=13)
    provider = Provider(clock)
    provider.source_id = 'public_market'
    historical_provider(provider)
    original = provider.fetch_universe
    def public_snapshot(day):
        return original(day) | {'source': 'public_market'}
    provider.fetch_universe = public_snapshot
    collection.provider_factory = lambda client: provider
    assert collection.tick()  # No manual request or methodology-marker reset.
    assert provider.universe_calls == [date(2026, 11, day) for day in (10, 11, 12, 13)]
    assert collection._day(DAY)['crowding_hash'] == old_crowding_hash
    assert all(call[2] is None for call in writer.calls)
    assert collection._day(DAY)['status'] == 'complete'
    with collection.connect() as db:
        archived = [row[0] for row in db.execute("SELECT payload FROM methodology_archive WHERE kind='snapshot'")]
    assert old_snapshot in archived
    assert collection.status()['gaps'] == []


def test_blocked_automatic_retries_obey_backoff_and_daily_budget(tmp_path):
    collection, provider, writer, clock = worker(tmp_path)
    provider.heat_verified = False
    assert not collection.tick()
    assert collection._day(DAY)['attempts'] == 1
    assert not collection.tick(clock.advance(minutes=4))
    assert collection._day(DAY)['attempts'] == 1
    assert not collection.tick(clock.advance(minutes=1))
    assert collection._day(DAY)['attempts'] == 2
    assert not collection.tick(clock.advance(minutes=29))
    assert collection._day(DAY)['attempts'] == 2
    assert not collection.tick(clock.advance(minutes=1))
    assert collection._day(DAY)['attempts'] == 3
    assert not collection.tick(clock.advance(hours=3))
    assert collection._day(DAY)['attempts'] == 3


def test_crowding_table_failure_does_not_block_heat_frontier_or_repeat_same_day_write(tmp_path):
    collection, provider, writer, clock = worker(tmp_path, moment=EVENING.replace(day=13))
    historical_provider(provider)
    writer.dates = {'heat': {'2026-11-09'}, 'crowding': {'2026-11-09'}}
    attempts = []
    def separate_tables(owner, day, heat, crowding, source):
        attempts.append((day, heat is not None, crowding is not None))
        if crowding is not None:
            raise WriterFailure('writer_permission_denied')
        return writer(owner, day, heat, crowding, source)
    collection.writer = separate_tables
    assert not collection.tick()
    assert writer.dates['heat'] == {'2026-11-09', '2026-11-10', '2026-11-11', '2026-11-12', '2026-11-13'}
    assert writer.dates['crowding'] == {'2026-11-09'}
    assert [day for day, heat, crowding in attempts if crowding] == [date(2026, 11, 10)]
    assert all(not (heat and crowding) for day, heat, crowding in attempts)
    assert collection._day('2026-11-10')['error'] == 'writer_permission_denied'
    assert collection._day('2026-11-10')['heat_status'] == 'written'
    assert collection._day('2026-11-10')['crowding_status'] == 'pending'
    assert collection.status()['runs'][0]['status'] == 'partial'
