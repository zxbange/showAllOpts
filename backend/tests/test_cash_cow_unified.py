"""One shared acquisition, independently correct windows, atomic publication."""
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import sqlite3
import threading

import httpx
import pytest

from app.core.config import Settings
from app.services.cash_cow import CashCowScreen, PERIODS, evaluate_year, finish_stock
from app.services.cash_cow_source import EastmoneyCashCowSource, CashCowSourceError
from test_cash_cow import Source, run, year_data
from test_cash_cow_source import event, report


class InstrumentedSource(Source):
    calls = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls.append(('init',))

    def universe(self):
        self.calls.append(('universe',))
        return [dict(code=f'60000{i}', name=f'股票{i}', exchange='SH') for i in range(1, 10)]

    @staticmethod
    def dividend(year, code):
        return {'cash_dividend': 0 if code == '600005' and year == 2025 else 50}

    @staticmethod
    def financial(year, code):
        adjusted = 80
        if (code == '600002' and year == 2022) or (code == '600003' and year == 2018):
            adjusted = -1
        if (code == '600004' and year == 2025) or (code == '600009' and year == 2022):
            adjusted = None
        return dict(adjusted_profit=adjusted, net_profit=100, operating_cashflow=60)

    @staticmethod
    def dps(code):
        if code == '600006':
            return {year: None for year in range(2016, 2026)}
        if code == '600007':
            return {year: .2 if year >= 2023 else .8 for year in range(2016, 2026)}
        if code == '600008':
            return {year: .6 if year >= 2023 else .1 if year >= 2021 else .8
                    for year in range(2016, 2026)}
        return year_data()[1]

    def dividends(self, year, codes):
        self.calls.append(('dividends', year, tuple(codes)))
        return {code: self.dividend(year, code) for code in codes}

    def financials(self, year, codes):
        self.calls.append(('financials', year, tuple(codes)))
        return {code: self.financial(year, code) for code in codes}

    def per_share_dividends_by_period(self, code, end_year, periods):
        self.calls.append(('dps', code, tuple(periods)))
        return {period: self.dps(code) for period in periods}

    def prices(self, code, start_year, end_year):
        self.calls.append(('prices', code, start_year, end_year))
        return year_data()[2]

    def current_dividend_yield(self, code):
        self.calls.append(('ttm', code))
        return super().current_dividend_yield(code)


def views(tmp_path, source=Source):
    settings = Settings(_env_file=None, log_dir=tmp_path)
    return {period: CashCowScreen(settings, years=period, source_factory=source,
                                 client_factory=lambda: nullcontext(None)) for period in PERIODS}


def independent_reference(stock, period):
    """Former one-period newest-first behavior, without shared pruning."""
    rows = []
    for year in range(2025, 2025 - period, -1):
        outcome, row = evaluate_year(year, InstrumentedSource.dividend(year, stock['code']),
                                     InstrumentedSource.financial(year, stock['code']))
        if outcome != 'eligible':
            return outcome, None
        rows.append(row)
    return finish_stock(stock, sorted(rows, key=lambda row: row['year']),
                        InstrumentedSource.dps(stock['code']), year_data()[2], period)


def test_shared_pruning_matches_independent_reference_and_calls_once(tmp_path):
    InstrumentedSource.calls = []
    screens = views(tmp_path, InstrumentedSource)
    run(screens[5])
    calls = list(InstrumentedSource.calls)
    assert sum(call[0] == 'init' for call in calls) == 1
    assert sum(call[0] == 'universe' for call in calls) == 1
    for kind in ('dividends', 'financials'):
        assert [call[1] for call in calls if call[0] == kind] == list(range(2025, 2015, -1))
        observations = [(call[1], code) for call in calls if call[0] == kind for code in call[2]]
        assert len(observations) == len(set(observations))
    assert '600005' not in next(call[2] for call in calls if call[:2] == ('financials', 2025))
    for kind in ('dps', 'prices', 'ttm'):
        codes = [call[1] for call in calls if call[0] == kind]
        assert len(codes) == len(set(codes))
        assert not {'600004', '600005'} & set(codes)
    assert '600006' not in [call[1] for call in calls if call[0] == 'prices']
    for period, screen in screens.items():
        actual = screen.latest()
        reference = [independent_reference(dict(code=f'60000{i}', name=f'股票{i}', exchange='SH'), period)
                     for i in range(1, 10)]
        expected_items = sorted((item for outcome, item in reference if outcome == 'matched'),
                                key=lambda item: (-item['average_yield'], item['code']))
        assert [item['code'] for item in actual['items']] == [item['code'] for item in expected_items]
        assert [item['years'] for item in actual['items']] == [item['years'] for item in expected_items]
        counts = Counter(outcome for outcome, _ in reference)
        for outcome in ('matched', 'excluded', 'insufficient'):
            assert actual['snapshot'][outcome + '_count'] == counts[outcome]
        assert sum(counts.values()) == actual['snapshot']['universe_count'] == 9
    # Yield averages are not monotonic: a 3-year failure may pass 5 years.
    assert '600007' not in {item['code'] for item in screens[3].latest()['items']}
    assert '600007' in {item['code'] for item in screens[5].latest()['items']}
    assert '600008' in {item['code'] for item in screens[3].latest()['items']}
    assert '600008' not in {item['code'] for item in screens[5].latest()['items']}


def test_older_missing_data_never_poison_short_window(tmp_path):
    InstrumentedSource.calls = []
    screens = views(tmp_path, InstrumentedSource)
    run(screens[10])
    assert '600009' in {item['code'] for item in screens[3].latest()['items']}
    assert '600009' not in {item['code'] for item in screens[5].latest()['items']}
    assert screens[3].latest()['snapshot']['insufficient_count'] == 2
    assert screens[5].latest()['snapshot']['insufficient_count'] == 3


@pytest.mark.parametrize('split_year,valid_periods', [(2018, {3, 5}), (2022, {3})])
def test_event_capital_and_daily_network_read_once_with_window_specific_basis(tmp_path, monkeypatch,
                                                                           split_year, valid_periods):
    requests = []
    events = [event(year, f'{year + 1}-06-01', '10派5元') for year in range(2016, 2026)]
    events.append(event(split_year, f'{split_year}-04-01', '10转2派3元'))
    capital = [dict(END_DATE='2000-01-01', TOTAL_SHARES=100),
               dict(END_DATE=f'{split_year}-04-01', TOTAL_SHARES=120, CHANGE_REASON='转增股上市')]
    bars = [f'{year}-{day},10,10,10,10,1,1,1,1,1,1'
            for year in range(2016, 2026) for day in ('01-05', '12-30')]
    bars.append('2026-11-09,10,10,10,10,1,1,1,1,1,1')

    def handler(request):
        kind = request.url.params.get('reportName') or 'daily'
        requests.append(kind)
        data = (report(events) if kind == 'RPT_F10_DIVIDEND_MAIN' else report(capital)
                if kind == 'RPT_F10_EH_EQUITY' else
                dict(rc=0, data=dict(code='600001', dktotal=len(bars), klines=bars)))
        return httpx.Response(200, json=data)

    monkeypatch.setattr(EastmoneyCashCowSource, '_rate_limit', lambda self: self.check_cancel())
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source = EastmoneyCashCowSource(client, cache_dir=tmp_path, as_of='2026-11-10')
        dps = source.per_share_dividends_by_period('600001', 2025, PERIODS)
        prices = source.prices('600001', 2016, 2025)
        for period in PERIODS:
            years = year_data()[0][-period:]
            result = finish_stock(dict(code='600001', name='测试', exchange='SH'), years, dps[period], prices, period)
            assert result[0] == ('matched' if period in valid_periods else 'insufficient')
        source.current_dividend_yield('600001')
        assert Counter(requests) == {'RPT_F10_DIVIDEND_MAIN': 1, 'RPT_F10_EH_EQUITY': 1, 'daily': 1}
        # A warm retry's validated cache produces no additional HTTP requests.
        restarted = EastmoneyCashCowSource(client, cache_dir=tmp_path, as_of='2026-11-10')
        assert restarted.per_share_dividends_by_period('600001', 2025, PERIODS) == dps
        assert restarted.prices('600001', 2016, 2025) == prices
        restarted.current_dividend_yield('600001')
        assert len(requests) == 3


def test_cross_period_run_status_lock_cancel_and_restart_keep_all_snapshots(tmp_path):
    screens = views(tmp_path)
    run(screens[3])
    before = {period: screen.latest()['snapshot'] for period, screen in screens.items()}
    entered, resume = threading.Event(), threading.Event()

    class Blocking(Source):
        def universe(self):
            entered.set()
            assert resume.wait(5)
            self.check_cancel()
            return super().universe()

    screens[3].source_factory = Blocking
    assert screens[3].refresh(moment=datetime(2026, 11, 10, tzinfo=timezone.utc))
    try:
        assert entered.wait(5)
        statuses = [screen.status() for screen in screens.values()]
        assert all(state['running'] for state in statuses)
        assert all(state['periods'] == [3, 5, 10] and state['collection_years'] == 10 for state in statuses)
        assert len({state['run_id'] for state in statuses}) == 1
        assert not screens[5].refresh()
        assert screens[10].cancel()
        assert all(screen.status()['cancelling'] for screen in screens.values())
    finally:
        resume.set()
        screens[3].worker.join(5)
    for period, screen in views(tmp_path).items():
        assert screen.latest()['snapshot'] == before[period]
        assert screen.status()['stage'] == '已取消，保留上次结果'
        assert not screen.status()['running']


def test_second_snapshot_write_failure_rolls_back_every_period(tmp_path, monkeypatch):
    from contextlib import contextmanager
    screens = views(tmp_path)
    screen = screens[10]
    run(screen)
    saved = {period: view.latest()['snapshot'] for period, view in screens.items()}
    database = screen.db

    @contextmanager
    def fail_second_write():
        with database() as db:
            class Connection:
                def execute(self, sql, params=()):
                    if sql.startswith('INSERT OR REPLACE INTO snapshots') and params[0] == 5:
                        raise sqlite3.OperationalError('rollback test')
                    return db.execute(sql, params)
            yield Connection()

    monkeypatch.setattr(screen, 'db', fail_second_write)
    run(screen)
    assert {period: view.latest()['snapshot'] for period, view in screens.items()} == saved
    assert all(view.status()['stage'] == '更新失败，保留上次结果' for view in screens.values())


def test_migrate_legacy_snapshots_readonly_and_preserve_history(tmp_path):
    originals = {}
    for period in PERIODS:
        path = tmp_path / 'market-data' / 'cash-cow' / str(period) / 'screen.sqlite3'
        path.parent.mkdir(parents=True)
        payload = dict(snapshot=dict(updated_at=f'2026-0{period % 7 + 1}-01T21:00:00+08:00',
                                     previous_updated_at='2025-12-31T21:00:00+08:00', years=period), items=[])
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE snapshot (id INTEGER PRIMARY KEY, payload TEXT)')
            db.execute('INSERT INTO snapshot VALUES(1,?)', (json.dumps(payload),))
        originals[period] = (path, path.read_bytes(), payload)
    screens = views(tmp_path)
    for period, screen in screens.items():
        assert screen.latest()['snapshot'] == originals[period][2]['snapshot']
        assert screen.status()['last_success_at'] == originals[period][2]['snapshot']['updated_at']
    class Broken(Source):
        def universe(self):
            raise CashCowSourceError('source unavailable')
    screens[3].source_factory = Broken
    run(screens[3])
    for period, screen in screens.items():
        assert screen.latest()['snapshot'] == originals[period][2]['snapshot']
    run(screens[10])
    for period, screen in views(tmp_path).items():
        snapshot = screen.latest()['snapshot']
        assert snapshot['previous_updated_at'] == originals[period][2]['snapshot']['updated_at']
        assert snapshot['previous_previous_updated_at'] == '2025-12-31T21:00:00+08:00'
        assert originals[period][0].read_bytes() == originals[period][1]


def test_api_refresh_from_any_period_updates_every_snapshot(client, monkeypatch):
    screen = client.app.state.cash_cow[3]
    screen.source_factory = Source
    screen.client_factory = lambda: nullcontext(None)
    original = screen.refresh
    monkeypatch.setattr(screen, 'refresh', lambda: original(background=False,
                        moment=datetime(2026, 11, 10, tzinfo=timezone.utc)))
    assert client.post('/api/v1/cash-cow/refresh?years=3').json() == {'started': True}
    results = [client.get(f'/api/v1/cash-cow/latest?years={period}').json() for period in PERIODS]
    assert all(result['snapshot']['matched_count'] == 1 for result in results)
    assert len({result['snapshot']['run_id'] for result in results}) == 1
    assert [result['years'] for result in results] == [3, 5, 10]


@pytest.mark.parametrize('legacy_slots,should_run', [
    (['2026-09-30'] * 3, False),
    (['2026-09-30', None, '2026-09-30'], True),
    (['2026-08-31', '2026-09-30', '2026-09-30'], True),
])
def test_completed_legacy_month_not_repeated_but_partial_month_recomputed(tmp_path, legacy_slots, should_run,
                                                                        monkeypatch):
    from app.services.cash_cow_schedule import MonthlyCashCowScheduler, CHINA
    settings = Settings(_env_file=None, log_dir=tmp_path)
    for period, slot in zip(PERIODS, legacy_slots):
        path = tmp_path / 'market-data' / 'cash-cow' / str(period) / 'screen.sqlite3'
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE snapshot (id INTEGER PRIMARY KEY, payload TEXT)')
            db.execute('INSERT INTO snapshot VALUES(1,?)', (json.dumps(dict(snapshot={}, items=[])),))
            db.execute('CREATE TABLE state (id INTEGER PRIMARY KEY, payload TEXT)')
            db.execute('INSERT INTO state VALUES(1,?)', (json.dumps(dict(scheduled_success_slot=slot)),))
    screens = views(tmp_path)
    scheduler = MonthlyCashCowScheduler(settings, screens)
    with scheduler.db() as db:
        db.execute('INSERT INTO settings VALUES (?,?)', ('enabled_at', '2026-09-01T00:00:00+08:00'))
    calls = []
    monkeypatch.setattr(screens[10], 'refresh', lambda **kwargs: calls.append(kwargs) or True)
    scheduler.tick(datetime(2026, 10, 1, tzinfo=CHINA))
    assert bool(calls) == should_run


def test_subprocess_lock_and_hard_interruption_preserve_all_snapshots(tmp_path):
    import subprocess
    import sys
    import time
    screens = views(tmp_path)
    run(screens[10])
    saved = {period: screen.latest()['snapshot'] for period, screen in screens.items()}
    ready = tmp_path / 'process-ready'
    code = '''
from contextlib import nullcontext
from pathlib import Path
import sys
import time
from app.core.config import Settings
from app.services.cash_cow import CashCowScreen
class Blocked:
    def __init__(self, *args, **kwargs): pass
    def universe(self):
        Path(sys.argv[2]).touch()
        while True: time.sleep(.05)
screen = CashCowScreen(Settings(_env_file=None, log_dir=sys.argv[1]), years=3,
                       source_factory=Blocked, client_factory=lambda: nullcontext(None))
screen.refresh(background=False)
'''
    process = subprocess.Popen([sys.executable, '-c', code, str(tmp_path), str(ready)])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists()
        assert all(screen.status()['running'] for screen in screens.values())
        assert not screens[5].refresh()
    finally:
        process.terminate()
        process.wait(timeout=10)
    for period, screen in screens.items():
        assert screen.latest()['snapshot'] == saved[period]
        assert screen.status()['stage'] == '上次更新中断，请重新更新'
    assert run(screens[5])
    assert len({screen.latest()['snapshot']['run_id'] for screen in screens.values()}) == 1
    assert all(screen.latest()['snapshot']['run_id'] != saved[period]['run_id']
               for period, screen in screens.items())


def test_latest_holds_publication_lock_across_state_and_snapshot_read(tmp_path, monkeypatch):
    screens = views(tmp_path)
    reader, writer = screens[3], screens[10]
    run(writer)
    previous = reader.latest()['snapshot']
    attempting, completed = threading.Event(), threading.Event()
    original_lock, original_read = writer.publication_lock, reader._read_result

    def publication_lock():
        attempting.set()
        return original_lock()

    def refresh():
        try:
            run(writer)
        finally:
            completed.set()

    thread = threading.Thread(target=refresh)

    def during_read(db):
        thread.start()
        assert attempting.wait(5)
        # The writer has finished calculations but cannot replace any snapshot
        # until this response has read both its state and selected snapshot.
        assert not completed.wait(.1)
        return original_read(db)

    monkeypatch.setattr(writer, 'publication_lock', publication_lock)
    monkeypatch.setattr(reader, '_read_result', during_read)
    try:
        result = reader.latest()
    finally:
        thread.join(5)
    assert not thread.is_alive()
    assert result['snapshot'] == previous
    assert result['status']['run_id'] == previous['run_id']
    assert writer.latest()['snapshot']['run_id'] != previous['run_id']
