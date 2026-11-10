from contextlib import nullcontext
from datetime import datetime, timezone

import pytest

from app.core.config import Settings
from app.services.cash_cow import CashCowScreen, evaluate_year, finish_stock


STOCK = dict(code='600000', name='测试股票', exchange='SH')


def annual(year=2025, *, cash=50, adjusted=80, profit=100, flow=60):
    return evaluate_year(year, {'cash_dividend': cash},
                         dict(adjusted_profit=adjusted, net_profit=profit, operating_cashflow=flow))


@pytest.mark.parametrize('kwargs,outcome', [
    ({}, 'eligible'), ({'cash': 30}, 'eligible'), ({'cash': 70, 'flow': 70}, 'eligible'),
    ({'cash': 29.999}, 'excluded'), ({'cash': 70.001, 'flow': 100}, 'excluded'),
    ({'flow': 50}, 'eligible'), ({'flow': 49.99}, 'excluded'),
    ({'cash': 0}, 'excluded'), ({'adjusted': 0}, 'excluded'),
    ({'profit': -1}, 'excluded'), ({'cash': None}, 'insufficient'),
    ({'adjusted': None}, 'insufficient'), ({'flow': None}, 'insufficient'),
    ({'profit': float('nan')}, 'insufficient'), ({'flow': True}, 'insufficient'),
])
def test_strict_annual_rules(kwargs, outcome):
    assert annual(**kwargs)[0] == outcome


def year_data():
    years = [annual(year)[1] for year in range(2016, 2026)]
    dividends = {year: .5 for year in range(2016, 2026)}
    prices = {year: dict(year_average_close=10, price_date=f'{year}-12-31') for year in range(2016, 2026)}
    return years, dividends, prices


def test_yield_uses_each_year_own_average_unadjusted_close_and_strict_average():
    years, dividends, prices = year_data()
    outcome, item = finish_stock(STOCK, years, dividends, prices)
    assert outcome == 'matched'
    assert item['average_yield'] == 5
    for year in dividends:
        dividends[year] = .4
    assert finish_stock(STOCK, years, dividends, prices)[0] == 'excluded'
    dividends[2025] = .401
    assert finish_stock(STOCK, years, dividends, prices)[0] == 'matched'
    prices[2025]['year_average_close'] = 100
    assert finish_stock(STOCK, years, dividends, prices)[0] == 'excluded'


def test_missing_or_stale_or_incomplete_years_never_qualify():
    years, dividends, prices = year_data()
    assert finish_stock(STOCK, years[:-1], dividends, prices)[0] == 'insufficient'
    prices[2016]['price_date'] = '2016-08-31'
    assert finish_stock(STOCK, years, dividends, prices)[0] == 'insufficient'
    prices[2016]['price_date'] = '2016-12-30'
    dividends[2016] = None
    assert finish_stock(STOCK, years, dividends, prices)[0] == 'insufficient'


class Source:
    def __init__(self, client, check_cancel, as_of=None, progress=None, cache_dir=None):
        self.check_cancel = check_cancel

    def universe(self):
        return [STOCK, dict(code='000002', name='不合格', exchange='SZ'),
                dict(code='920001', name='待核验', exchange='BJ')]

    def dividends(self, year, codes):
        return {code: {'cash_dividend': 50} for code in codes}

    def financials(self, year, codes):
        return {code: dict(adjusted_profit=None if code == '920001' else 80,
                           net_profit=100, operating_cashflow=40 if code == '000002' else 60)
                for code in codes}

    def per_share_dividends(self, code, start_year, end_year):
        return year_data()[1]

    def current_dividend_yield(self, code):
        return dict(current_yield=4.5, latest_price=12, latest_price_date='2026-11-09', dividend_ttm_per_share=.54)

    def prices(self, code, start_year, end_year):
        return year_data()[2]


@pytest.fixture
def screen(tmp_path):
    return CashCowScreen(Settings(_env_file=None, log_dir=tmp_path),
                         source_factory=Source, client_factory=lambda: nullcontext(None))


def run(screen):
    return screen.refresh(background=False, moment=datetime(2026, 11, 10, tzinfo=timezone.utc))


def test_snapshot_counts_separate_verified_failures_from_insufficient(screen):
    assert screen.latest()['snapshot'] is None
    assert run(screen)
    data = screen.latest()
    assert data['snapshot']['universe_count'] == 3
    assert data['snapshot']['matched_count'] == 1
    assert data['snapshot']['excluded_count'] == 1
    assert data['snapshot']['insufficient_count'] == 1
    assert data['snapshot']['evaluated_count'] == 2
    assert data['snapshot']['start_year'] == 2016
    assert data['snapshot']['end_year'] == 2025
    assert data['items'][0]['code'] == '600000'
    assert len(data['items'][0]['years']) == 10
    assert data['status']['running'] is False


def test_failure_and_cancellation_preserve_prior_snapshot(screen):
    from app.services.cash_cow_source import CashCowSourceError
    run(screen)
    saved = screen.latest()['snapshot']

    class Broken(Source):
        def universe(self):
            raise CashCowSourceError('公开接口暂不可用')

    screen.source_factory = Broken
    run(screen)
    assert screen.latest()['snapshot'] == saved
    assert screen.status()['error'] == '公开接口暂不可用'
    assert screen.status()['stage'] == '更新失败，保留上次结果'

    class Cancelled(Source):
        def universe(self):
            assert screen.cancel()
            self.check_cancel()

    screen.source_factory = Cancelled
    run(screen)
    assert screen.latest()['snapshot'] == saved
    assert screen.status()['stage'] == '已取消，保留上次结果'
    assert not screen.status()['error']
    assert not list(screen.root.glob('cancel-*'))


def test_cross_instance_lock_and_interrupted_status(screen):
    lock = screen.acquire()
    try:
        assert not screen.refresh()
        assert screen.status()['running']
    finally:
        lock.release()
    state = screen.read_state()
    state['active'] = True
    screen.write_state(state)
    assert screen.status()['stage'] == '上次更新中断，请重新更新'
    assert not screen.cancel()


def test_cancel_accepted_during_serialization_cannot_publish(screen, monkeypatch):
    import app.services.cash_cow as module
    run(screen)
    saved = screen.latest()['snapshot']
    dumps = module.json.dumps
    accepted = []

    def during_serialization(value, *args, **kwargs):
        if isinstance(value, dict) and 'snapshot' in value and 'items' in value:
            accepted.append(screen.cancel())
        return dumps(value, *args, **kwargs)

    monkeypatch.setattr(module.json, 'dumps', during_serialization)
    run(screen)
    assert accepted == [True]
    assert screen.latest()['snapshot'] == saved
    assert screen.status()['stage'] == '已取消，保留上次结果'


def test_latest_completion_boundary_reads_finished_snapshot(screen, monkeypatch):
    run(screen)
    status = screen.status

    def completed_status():
        # Finish another refresh at exactly the point /latest asks for status.
        with screen.db() as db:
            db.execute('UPDATE snapshot SET payload=? WHERE id=1',
                       ('{"snapshot":{"as_of":"new"},"items":[]}',))
        return status()

    monkeypatch.setattr(screen, 'status', completed_status)
    assert screen.latest()['snapshot']['as_of'] == 'new'


def test_no_automatic_network_read_and_api(client, monkeypatch):
    service = client.app.state.cash_cow[10]
    monkeypatch.setattr(service, 'refresh', lambda: True)
    assert client.get('/api/v1/cash-cow/latest').json()['snapshot'] is None
    assert client.get('/api/v1/cash-cow/status').json()['stage'] == '尚未筛选'
    # Existing auth tests exercise admin-only CSRF mutation middleware.
    response = client.post('/api/v1/cash-cow/refresh')
    assert response.status_code == 202
    assert response.json() == {'started': True}
    for period in (3, 5, 10):
        response = client.get(f'/api/v1/cash-cow/latest?years={period}')
        assert response.status_code == 200
        assert response.json()['years'] == period
    assert client.get('/api/v1/cash-cow/latest?years=4').status_code == 422


def test_periods_screen_full_universe_independently_and_keep_separate_snapshots(tmp_path):
    class DifferentWindows(Source):
        def financials(self, year, codes):
            return {code: dict(adjusted_profit=(-1 if (code == '000002' and year == 2022)
                                               or (code == '920001' and year == 2018) else 80),
                               net_profit=100, operating_cashflow=60) for code in codes}

    settings = Settings(_env_file=None, log_dir=tmp_path)
    screens = {years: CashCowScreen(settings, years=years, source_factory=DifferentWindows,
                                    client_factory=lambda: nullcontext(None)) for years in (3, 5, 10)}
    for period, screen in screens.items():
        assert run(screen)
        result = screen.latest()
        assert result['snapshot']['matched_count'] == {3: 3, 5: 2, 10: 1}[period]
        assert result['snapshot']['universe_count'] == 3
        assert result['snapshot']['start_year'] == 2026 - period
        assert all(len(item['years']) == period for item in result['items'])
    # Running another window did not overwrite the first window's result.
    assert screens[3].latest()['snapshot']['matched_count'] == 3
    assert len({screen.root for screen in screens.values()}) == 3


def test_scheduled_success_slot_only_written_after_success(screen):
    screen.refresh(background=False, moment=datetime(2026, 9, 30, tzinfo=timezone.utc), slot='2026-09-30')
    assert screen.read_state()['scheduled_success_slot'] == '2026-09-30'
    assert screen.read_state()['scheduled_slot'] == '2026-09-30'
    run(screen)
    assert screen.read_state()['scheduled_success_slot'] == '2026-09-30'


def test_success_history_three_updates_and_restart(screen, monkeypatch):
    import app.services.cash_cow as module
    moments = [datetime(2026, month, 28, 13, tzinfo=timezone.utc) for month in (8, 9, 10)]
    for index, moment in enumerate(moments):
        monkeypatch.setattr(module, 'now', lambda: moment)
        run(screen)
        result = screen.latest()['snapshot']
        assert result['updated_at'] == moment.isoformat()
        assert result['previous_updated_at'] == (moments[index - 1].isoformat() if index else None)
        assert result['previous_previous_updated_at'] == (moments[index - 2].isoformat() if index > 1 else None)
    # A new service instance reads the same persisted update history.
    settings = Settings(_env_file=None, log_dir=screen.root.parents[2])
    restarted = CashCowScreen(settings)
    assert restarted.latest()['snapshot'] == screen.latest()['snapshot']


def test_failed_snapshot_commit_does_not_advance_success_history(screen, monkeypatch):
    from contextlib import contextmanager
    import sqlite3
    run(screen)
    previous = screen.latest()['snapshot']
    old_success = screen.read_state()['last_success_at']
    database = screen.db

    @contextmanager
    def reject_snapshot():
        with database() as db:
            class Connection:
                def execute(self, sql, params=()):
                    if sql.startswith('INSERT OR REPLACE INTO snapshot'):
                        raise sqlite3.OperationalError('test commit failure')
                    return db.execute(sql, params)
            yield Connection()

    monkeypatch.setattr(screen, 'db', reject_snapshot)
    run(screen)
    assert screen.latest()['snapshot'] == previous
    assert screen.read_state()['last_success_at'] == old_success
    assert screen.read_state().get('previous_success_at') is None
