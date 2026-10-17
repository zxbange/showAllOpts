"""Offline collector tests: no jqdata import or real database connection."""
import ast
import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def collector(tmp_path):
    source = Path(__file__).resolve().parents[2] / 'collectors' / '抱团拥挤度_数据库版.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    tree.body = [node for node in tree.body
                 if not (isinstance(node, ast.ImportFrom) and node.module == 'jqdata')
                 and not (isinstance(node, ast.Import) and node.names[0].name == 'pandas')]
    namespace = {'__name__': 'offline_collector_test',
                 'pd': SimpleNamespace(DataFrame=lambda data=None, columns=None: data or [])}
    exec(compile(tree, str(source), 'exec'), namespace)
    namespace['china_now'] = lambda: datetime.datetime(2026, 10, 17, 10)
    engine = create_engine('sqlite:///' + str(tmp_path / 'crowding.sqlite'))
    namespace['Base'].metadata.create_all(engine)
    namespace['MysqlCrowding'] = SimpleNamespace(connect_to_mysql=lambda: engine)
    days = [datetime.date(2020, 1, 2), datetime.date(2020, 1, 3), datetime.date(2026, 10, 16)]
    namespace['get_trade_days'] = lambda start_date, end_date: [
        d for d in days if start_date <= d <= end_date]
    calculated = []
    def calculate(day, top_pct):
        calculated.append(day)
        return record(day, top_pct)
    namespace['calculate_crowding_original'] = namespace['calculate_crowding']
    namespace['calculate_crowding'] = calculate
    yield SimpleNamespace(env=namespace, engine=engine, Session=sessionmaker(bind=engine),
                          days=days, calculated=calculated)
    engine.dispose()


def record(day, pct=.05, ratio=.4):
    return dict(trade_date=day, top_pct=Decimal(str(pct)), ratio=Decimal(str(ratio)),
                top_money=400, total_money=1000, stock_count=100, top_stock_count=5,
                updated_at=datetime.datetime(2026, 10, 17, 10))


def rows(c):
    with c.Session() as session:
        return session.query(c.env['MarketCrowding']).order_by(c.env['MarketCrowding'].trade_date).all()


def seed(c, day, pct=.05, ratio=.4):
    c.env['save_day'](c.Session, day, pct, record(day, pct, ratio))


def test_formula_floor_zero_turnover_and_original_top_pct(collector):
    c = collector
    assert c.env['START_DATE'] == '2020-01-01'
    assert c.env['END_DATE'] is None and c.env['TOP_PCT'] == .05
    # 101 stocks -> floor(101 * .05) = 5; 96 zero-turnover stocks still count.
    result = c.env['summarize_money']([(str(i), 100 if i < 5 else 0) for i in range(101)])
    assert result['stock_count'] == 101 and result['top_stock_count'] == 5
    assert result['ratio'] == 1 and result['total_money'] == 500
    result = c.env['summarize_money']([(str(i), 1) for i in range(100)])
    assert result['ratio'] == Decimal('.05')


@pytest.mark.parametrize('bad', [None, float('nan'), float('inf'), -1, 'bad'])
def test_invalid_amount_is_not_silently_skipped(collector, bad):
    pairs = [(str(i), 1) for i in range(100)]
    pairs[0] = ('0', bad)
    with pytest.raises(ValueError):
        collector.env['summarize_money'](pairs)


def test_zero_total_small_universe_duplicate_and_invalid_pct(collector):
    fn = collector.env['summarize_money']
    for pairs in ([('x', 1)], [(str(i), 0) for i in range(100)], [('x', 1)] * 100):
        with pytest.raises(ValueError):
            fn(pairs)
    for pct in (0, 2, float('nan'), .0500001):
        with pytest.raises(ValueError):
            fn([(str(i), 1) for i in range(100)], pct)


def test_first_run_backfills_2020_to_today_and_rerun_refreshes_last(collector):
    c = collector
    assert len(c.env['run']()) == 3
    assert [r.trade_date for r in rows(c)] == c.days
    c.calculated.clear()
    c.env['run']()
    assert c.calculated == [c.days[-1]]
    assert len(rows(c)) == 3


def test_historical_gap_repaired_without_deleting_other_top_pct(collector):
    c = collector
    seed(c, c.days[0])
    seed(c, c.days[-1], ratio=.1)
    seed(c, c.days[-1], pct=.1)
    c.env['run']()
    assert c.calculated == c.days[1:]
    assert len(rows(c)) == 4
    assert rows(c)[0].trade_date.year == 2020


def test_failure_stops_and_next_run_resumes(collector):
    c = collector
    calculate = c.env['calculate_crowding']
    def fail(day, pct):
        if day == c.days[1]:
            raise ValueError('Incomplete prices')
        return calculate(day, pct)
    c.env['calculate_crowding'] = fail
    with pytest.raises(ValueError):
        c.env['run']()
    assert len(rows(c)) == 1
    c.env['calculate_crowding'] = calculate
    c.env['run']()
    assert len(rows(c)) == 3


def test_replacement_rollback_preserves_old_value(collector):
    c = collector
    seed(c, c.days[0], ratio=.2)
    class FailCommit(c.Session.class_):
        def commit(self):
            raise RuntimeError('Injected failure')
    failing = sessionmaker(bind=c.engine, class_=FailCommit)
    with pytest.raises(RuntimeError):
        c.env['save_day'](failing, c.days[0], .05, record(c.days[0]))
    assert rows(c)[0].ratio == Decimal('.2')
    with pytest.raises(ValueError):
        c.env['save_day'](c.Session, c.days[1], .05, record(c.days[0]))
    assert len(rows(c)) == 1


@pytest.mark.parametrize('bad_days', [None, ['2019-12-31'], ['2026-10-18'], ['2020-01-03', '2020-01-02'], ['2020-01-02'] * 2])
def test_bad_calendar_never_connects_to_database(collector, bad_days):
    c = collector
    c.env['get_trade_days'] = lambda **kwargs: bad_days
    c.env['MysqlCrowding'].connect_to_mysql = lambda: pytest.fail('Unexpected DB connection')
    with pytest.raises(ValueError):
        c.env['run']()


def test_runtime_today_and_no_trade_day(collector):
    c = collector
    dates = []
    def calendar(start_date, end_date):
        dates.append(end_date)
        return []
    c.env['get_trade_days'] = calendar
    c.env['MysqlCrowding'].connect_to_mysql = lambda: pytest.fail('Unexpected DB connection')
    assert c.env['run']() == []
    c.env['china_now'] = lambda: datetime.datetime(2026, 10, 18)
    assert c.env['run']() == []
    assert dates == [datetime.date(2026, 10, 17), datetime.date(2026, 10, 18)]
    with pytest.raises(ValueError):
        c.env['run'](end_date='2026-10-19')


def test_price_adapter_validates_universe_dates_and_missing_fields(collector):
    c = collector
    codes = [str(i) for i in range(100)]
    c.env['get_all_securities'] = lambda **kw: SimpleNamespace(
        empty=False, index=SimpleNamespace(tolist=lambda: codes))
    class Frame(dict):
        empty = False
        @property
        def columns(self):
            return list(self)
    frame = Frame(code=codes, time=[c.days[0]] * 100, money=[1] * 100)
    c.env['get_price'] = lambda **kw: frame
    calc = c.env['calculate_crowding_original']
    assert calc(c.days[0])['ratio'] == Decimal('.05')
    frame['code'] = codes[:-1]
    with pytest.raises(ValueError):
        calc(c.days[0])
    frame['code'] = codes
    frame['time'][0] = c.days[1]
    with pytest.raises(ValueError):
        calc(c.days[0])
    del frame['time']
    with pytest.raises(ValueError):
        calc(c.days[0])
