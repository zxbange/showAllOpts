"""Offline JoinQuant adapters and incremental continuity; no real market or DB calls."""
import ast
import datetime
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class Frame:
    def __init__(self, records=None, index_data=None, dates=None, columns=None):
        self.records = records or []
        self.index_data = index_data
        self.index = dates if dates is not None else list(index_data or {})
        self.empty = not (self.records or self.index_data)
        self.columns = columns if columns is not None else list(
            (self.records[0] if self.records else next(iter((index_data or {}).values()), {})).keys())

    def to_dict(self, orient):
        return self.index_data if orient == 'index' else self.records


@pytest.fixture
def collector(tmp_path):
    source = Path(__file__).resolve().parents[2] / 'collectors' / '微盘温度计_数据库版.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    tree.body = [node for node in tree.body
                 if not (isinstance(node, ast.ImportFrom) and node.module == 'jqdata')
                 and not (isinstance(node, ast.Import) and node.names[0].name == 'pandas')]
    env = {'__name__': 'offline_microcap', 'pd': SimpleNamespace(DataFrame=lambda data=None, columns=None: data or [])}
    exec(compile(tree, str(source), 'exec'), env)
    env['china_now'] = lambda: datetime.datetime(2021, 1, 29, 17)
    engine = create_engine('sqlite:///' + str(tmp_path / 'microcap.sqlite'))
    env['Base'].metadata.create_all(engine)
    env['MysqlMicrocap'] = SimpleNamespace(connect_to_mysql=lambda: engine)
    days = [datetime.date(2021, 1, i) for i in range(4, 30) if datetime.date(2021, 1, i).weekday() < 5]
    def calendar(start_date=None, end_date=None, count=None):
        end = env['as_date'](end_date)
        values = [day for day in days if day <= end]
        return values[-count:] if count is not None else [day for day in values if day >= env['as_date'](start_date)]
    env['get_trade_days'] = calendar
    calculations = []
    real_calculate = env['calculate_period']
    def calculate(day, previous, micro_num):
        calculations.append(day)
        value = Decimal(10 + (days.index(day) // 5) % 3)
        return dict(
            trade_date=day, micro_num=micro_num, universe_count=4, profit_growth_count=1,
            micro_median=value, all_median=30, relative_ratio=value / 30,
            crowding_ratio=Decimal('1.2'), amount_crowding_ratio=value / 100,
            pe_median=-5, pb_median=2, avg_np_growth=None,
            micro_index=Decimal('1000') if previous is None else Decimal(previous['micro_index']) * Decimal('1.1'),
            period_return=Decimal('0') if previous is None else Decimal('.1'),
            member_codes=json.dumps(['A', 'B']), updated_at=env['china_now'](),
        )
    env['calculate_period'] = calculate
    yield SimpleNamespace(env=env, engine=engine, Session=sessionmaker(bind=engine),
                          days=days, calculated=calculations, real_calculate=real_calculate)
    engine.dispose()


def saved(c):
    with c.Session() as session:
        return [{col.name: getattr(row, col.name) for col in c.env['MicrocapTemperature'].__table__.columns}
                for row in session.query(c.env['MicrocapTemperature']).order_by(c.env['MicrocapTemperature'].trade_date)]


def test_original_parameters_and_first_run_grid(collector):
    c = collector
    assert c.env['START_DATE'] == '2021-01-01'
    assert c.env['MICRO_NUM'] == 1000 and c.env['SAMPLE_STEP'] == 5
    c.env['run'](micro_num=2, skip_existing=False)
    rows = saved(c)
    assert [row['trade_date'] for row in rows] == c.days[::5]
    assert [row['sample_count'] for row in rows] == [1, 2, 3, 4]
    assert [row['micro_index'] for row in rows] == [1000, 1100, 1210, 1331]


def test_incremental_equals_full_and_latest_refresh_is_idempotent(collector):
    c = collector
    c.env['run'](micro_num=2, end_date=c.days[6], skip_existing=False)
    c.env['run'](micro_num=2, end_date=c.days[12], skip_existing=False)
    c.env['run'](micro_num=2, skip_existing=False)
    incremental = saved(c)
    c.calculated.clear()
    c.env['run'](micro_num=2, skip_existing=False)
    assert c.calculated == [c.days[15]]
    assert saved(c) == incremental
    with c.Session() as session:
        session.query(c.env['MicrocapTemperature']).delete()
        session.commit()
    c.env['run'](micro_num=2, skip_existing=False)
    assert saved(c) == incremental


def test_refresh_before_next_sampling_day_does_not_shift_schedule(collector):
    c = collector
    c.env['run'](micro_num=2, end_date=c.days[7], skip_existing=False)
    c.calculated.clear()
    c.env['run'](micro_num=2, end_date=c.days[9], skip_existing=False)
    assert c.calculated == [c.days[5]]
    c.env['run'](micro_num=2, end_date=c.days[10], skip_existing=False)
    assert [row['trade_date'] for row in saved(c)] == c.days[:11:5]


def test_failure_stops_and_resume_preserves_prior_index(collector):
    c = collector
    calculate = c.env['calculate_period']
    def fail(day, previous, micro_num):
        if day == c.days[5]:
            raise ValueError('Missing endpoint')
        return calculate(day, previous, micro_num)
    c.env['calculate_period'] = fail
    with pytest.raises(ValueError):
        c.env['run'](micro_num=2, skip_existing=False)
    assert len(saved(c)) == 1
    c.env['calculate_period'] = calculate
    c.env['run'](micro_num=2, skip_existing=False)
    assert saved(c)[-1]['micro_index'] == 1331


def test_rollback_preserves_whole_snapshot_and_members(collector):
    c = collector
    c.env['run'](micro_num=2, end_date=c.days[0], skip_existing=False)
    old = saved(c)[0]
    class FailCommit(c.Session.class_):
        def commit(self):
            raise RuntimeError('Injected failure')
    record = dict(old, micro_index=2000, member_codes='["C", "D"]')
    with pytest.raises(RuntimeError):
        c.env['save_period'](sessionmaker(bind=c.engine, class_=FailCommit), record)
    assert saved(c)[0] == old


def test_rank_ties_and_weighting_include_current_value(collector):
    env = collector.env
    histories = {field: [] for field in env['RAW_FIELDS']}
    results = []
    for value in (1, 2, 2, 0):
        record = {field: Decimal(value) for field in env['RAW_FIELDS']}
        env['append_percentiles'](record, histories)
        results.append(record['score'])
    assert results == [Decimal(1), Decimal(1), Decimal('0.8333333333'), Decimal('.25')]
    assert len(histories['micro_median']) == 4


def test_index_uses_all_previous_members_and_same_price_request(collector):
    c = collector
    previous = dict(trade_date=c.days[0], micro_index=1000, member_codes='["A", "B"]')
    calls = []
    def price(**kwargs):
        calls.append(kwargs)
        return Frame(records=[
            {'code': 'A', 'time': c.days[0], 'close': 10},
            {'code': 'A', 'time': c.days[5], 'close': 12},
            {'code': 'B', 'time': c.days[0], 'close': 20},
            {'code': 'B', 'time': c.days[5], 'close': 18},
        ])
    c.env['get_price'] = price
    index, change = c.env['index_return'](previous, c.days[5], 2)
    assert index == 1050 and change == Decimal('.05')
    assert len(calls) == 1 and calls[0]['security'] == ['A', 'B']
    assert calls[0]['start_date'] == c.days[0].isoformat()
    assert calls[0]['end_date'] == c.days[5].isoformat()
    assert calls[0]['fq'] == 'pre' and calls[0]['panel'] is False
    c.env['get_price'] = lambda **kwargs: Frame(records=[
        {'code': 'A', 'time': c.days[0], 'close': 10},
        {'code': 'A', 'time': c.days[5], 'close': 12},
    ])
    with pytest.raises(ValueError, match='端点价格缺失'):
        c.env['index_return'](previous, c.days[5], 2)


def test_universe_formula_matches_original_and_includes_zero_money(collector):
    env = collector.env
    rows = {
        code: dict(market_cap=cap, pe_ratio=pe, pb_ratio=pb, turnover_ratio=1)
        for code, cap, pe, pb in [('A', 10, -10, 1), ('B', 20, 20, 3), ('C', 30, 30, 4), ('D', 40, 40, 5)]
    }
    metrics, members = env['summarize_universe'](
        rows, {'A': [Decimal(2)]*5, 'B': [Decimal(4)]*5, 'C': [Decimal(1)]*5, 'D': [Decimal(1)]*5},
        {'A': Decimal(0), 'B': Decimal(20), 'C': Decimal(30), 'D': Decimal(50)}, 2)
    assert members == ['A', 'B']
    assert metrics['micro_median'] == 15 and metrics['all_median'] == 25
    assert metrics['relative_ratio'] == Decimal('.6')
    assert metrics['crowding_ratio'] == Decimal('1.5')
    assert metrics['amount_crowding_ratio'] == Decimal('.2')
    assert metrics['pe_median'] == 5 and metrics['pb_median'] == 2


def test_listing_date_st_and_new_bse_prefix_filter(collector):
    c = collector
    day = c.days[5]
    old = day - datetime.timedelta(days=250)
    c.env['get_all_securities'] = lambda **kwargs: Frame(index_data={
        'A': {'start_date': old}, 'B': {'start_date': old},
        'C': {'start_date': old + datetime.timedelta(days=1)},
        '400001': {'start_date': old}, '800001': {'start_date': old}, '920001': {'start_date': old},
    })
    requests = []
    def extras(info, codes, **kwargs):
        requests.append(codes)
        return Frame(records=[{'A': False, 'B': True}], dates=[day])
    c.env['get_extras'] = extras
    assert c.env['eligible_codes'](day) == ['A']
    assert requests == [['A', 'B']]
    c.env['get_extras'] = lambda *args, **kwargs: Frame(records=[{'A': None, 'B': False}], dates=[day])
    with pytest.raises(ValueError, match='ST'):
        c.env['eligible_codes'](day)


def test_calculation_adapters_five_day_turnover_and_nullable_profit(collector):
    c = collector
    c.env['eligible_codes'] = lambda day: ['A', 'B']
    valuation, indicator = object(), object()
    c.env['valuation'], c.env['indicator'] = valuation, indicator
    calls = []
    def fundamentals(codes, table, fields, day):
        calls.append((table, fields, day))
        if table is indicator:
            return {'A': {'inc_net_profit_to_shareholders_year_on_year': None}}
        return {code: dict(market_cap=10 if code == 'A' else 20, pe_ratio=-5, pb_ratio=1, turnover_ratio=2)
                for code in codes}
    c.env['fundamental_rows'] = fundamentals
    c.env['price_rows'] = lambda codes, start, end, field: {(code, end): Decimal(10) for code in codes}
    record = c.real_calculate(c.days[5], None, 2)
    assert record['micro_index'] == 1000
    assert record['avg_np_growth'] is None and record['profit_growth_count'] == 0
    assert record['relative_ratio'] == 1 and record['amount_crowding_ratio'] == 1
    assert len([call for call in calls if call[0] is valuation]) == 5


def test_missing_old_sample_is_detected(collector):
    c = collector
    c.env['run'](micro_num=2, skip_existing=False)
    with c.Session() as session:
        session.query(c.env['MicrocapTemperature']).filter_by(trade_date=c.days[5]).delete()
        session.commit()
    with pytest.raises(ValueError, match='历史样本不连续'):
        c.env['run'](micro_num=2, skip_existing=False)


@pytest.mark.parametrize('bad_days', [None, ['2020-12-31'], ['2021-02-01'], ['2021-01-04']*2])
def test_bad_calendar_prevents_writes(collector, bad_days):
    c = collector
    c.env['get_trade_days'] = lambda **kwargs: bad_days
    with pytest.raises(ValueError):
        c.env['run'](micro_num=2, skip_existing=False)
    assert saved(c) == []


def test_future_end_and_nonpositive_parameters_never_connect(collector):
    c = collector
    c.env['MysqlMicrocap'].connect_to_mysql = lambda: pytest.fail('Should not connect')
    for kwargs in ({'end_date': '2021-02-01'}, {'micro_num': 0}, {'sample_step': True}):
        with pytest.raises(ValueError):
            c.env['run'](**kwargs, skip_existing=False)


def test_fundamentals_batching_and_requested_fields(collector):
    c = collector
    class Field:
        def __init__(self, name):
            self.name = name
        def in_(self, values):
            return values
    class Query:
        def __init__(self, fields):
            self.fields = fields
        def filter(self, codes):
            self.codes = codes
            return self
    table = SimpleNamespace(code=Field('code'), turnover_ratio=Field('turnover_ratio'))
    c.env['query'] = lambda *fields: Query(fields)
    c.env['BATCH_SIZE'] = 2
    calls = []
    def fundamentals(query, date):
        calls.append((query.codes, date))
        return Frame(records=[{'code': code, 'turnover_ratio': 2} for code in query.codes])
    c.env['get_fundamentals'] = fundamentals
    result = c.env['fundamental_rows'](['A', 'B', 'C'], table, ('turnover_ratio',), c.days[0])
    assert list(result) == ['A', 'B', 'C']
    assert calls == [(['A', 'B'], '2021-01-04'), (['C'], '2021-01-04')]
    c.env['get_fundamentals'] = lambda *args, **kwargs: Frame(records=[{'code': 'unexpected', 'turnover_ratio': 1}])
    with pytest.raises(ValueError, match='股票重复或越界'):
        c.env['fundamental_rows'](['A'], table, ('turnover_ratio',), c.days[0])


def test_latest_query_is_index_bounded_and_baseline_has_only_four_metrics(collector):
    from sqlalchemy import event
    c = collector
    c.env['run'](micro_num=2, skip_existing=False)
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith('SELECT'):
            statements.append(statement)
    event.listen(c.engine, 'before_cursor_execute', capture)
    try:
        last, previous, histories = c.env['load_resume'](
            c.Session, c.env['series_key']('2021-01-01', 2, 5))
    finally:
        event.remove(c.engine, 'before_cursor_execute', capture)
    assert last == c.days[15] and previous['trade_date'] == c.days[10]
    assert len(statements) == 3
    assert 'LIMIT' in statements[0] and 'DESC' in statements[0]
    assert 'member_codes' not in statements[1] and 'micro_index' not in statements[1]
    assert all(len(values) == 3 for values in histories.values())


def test_insert_only_microcap_preserves_sampling_index_and_noop_writes(collector):
    from sqlalchemy import event
    c=collector
    c.env['run'](micro_num=2,end_date=c.days[5],skip_existing=True)
    original=saved(c)
    c.calculated.clear()
    result=c.env['run'](micro_num=2,skip_existing=True)
    assert len(result)==2 and c.calculated==c.days[10::5]
    assert saved(c)[:2]==original
    assert [r['sample_count'] for r in saved(c)]==[1,2,3,4]
    assert [r['micro_index'] for r in saved(c)]==[1000,1100,1210,1331]
    before=saved(c)
    c.calculated.clear()
    statements=[]
    def capture(conn,cursor,statement,*args):
        if statement.lstrip().split()[0].upper() in ('INSERT','UPDATE','DELETE'):
            statements.append(statement)
    event.listen(c.engine,'before_cursor_execute',capture)
    try:
        assert c.env['run'](micro_num=2,skip_existing=True)==[]
        assert not c.env['save_period'](c.Session,dict(before[-1],micro_index=9999),skip_existing=True)
    finally:
        event.remove(c.engine,'before_cursor_execute',capture)
    assert not statements and not c.calculated and saved(c)==before


def test_insert_only_microcap_failure_resumes_after_committed_sample(collector):
    c=collector
    c.env['run'](micro_num=2,end_date=c.days[5],skip_existing=True)
    calculate=c.env['calculate_period']
    def fail(day,previous,micro_num):
        if day==c.days[15]:
            raise ValueError('synthetic source failure')
        return calculate(day,previous,micro_num)
    c.env['calculate_period']=fail
    with pytest.raises(ValueError):
        c.env['run'](micro_num=2,skip_existing=True)
    assert len(saved(c))==3
    c.env['calculate_period']=calculate
    c.calculated.clear()
    result=c.env['run'](micro_num=2,skip_existing=True)
    assert len(result)==1 and c.calculated==[c.days[15]]
    assert saved(c)[-1]['micro_index']==1331


def test_insert_only_microcap_defers_intraday_sample(collector):
    c=collector
    c.env['china_now']=lambda: datetime.datetime.combine(c.days[5],datetime.time(15,30))
    c.env['run'](micro_num=2,skip_existing=True)
    assert c.calculated==[c.days[0]]
    c.calculated.clear()
    c.env['china_now']=lambda: datetime.datetime.combine(c.days[5],datetime.time(16))
    c.env['run'](micro_num=2,skip_existing=True)
    assert c.calculated==[c.days[5]]
