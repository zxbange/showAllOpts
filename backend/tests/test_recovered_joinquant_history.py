"""Offline protocol/source fixtures; never authenticates to JoinQuant or MySQL."""
import ast
import datetime as dt
import importlib.util
import sys
import types
from decimal import Decimal
from pathlib import Path
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[2]

@pytest.fixture(params=['市场情绪历史补采_聚宽研究版.py'])
def script(request, monkeypatch):
    return load(request.param, monkeypatch)

def load(name, monkeypatch):
    monkeypatch.setitem(sys.modules, 'jqdata', types.ModuleType('jqdata'))
    # A preview/import must not import the configuration module at all.
    monkeypatch.setitem(sys.modules, 'mysql_connector', None)
    spec = importlib.util.spec_from_file_location('history_' + name, ROOT/'collectors'/name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def test_python36_import_lazy_config(script):
    ast.parse(Path(script.__file__).read_text(), feature_version=(3, 6))
    assert script.number('1.230000', 12, 2) == Decimal('1.23')
    for value in [None, True, 'NaN', 'Infinity', '0.001', '10000000000']:
        with pytest.raises(ValueError):
            script.number(value, 12, 2)
    assert script.number('999999999999999999.123456789012', 30, 12) == Decimal('999999999999999999.123456789012')

def test_calendar_full_coverage(script):
    days = [dt.date(2020, 1, i) for i in [2, 3, 6]]
    script.jq.get_all_trade_days = lambda: days
    script.jq.get_trade_days = lambda **kw: days[1:]
    with pytest.raises(ValueError):
        script.calendar(None, days[-1])
    script.jq.get_trade_days = lambda **kw: days
    assert script.calendar(None, days[-1]) == days


def test_market_crowding_paused_universe_and_missing(monkeypatch):
    m = load('市场情绪历史补采_聚宽研究版.py', monkeypatch)
    day = dt.date(2020, 1, 2)
    codes = ['{:06d}.XSHG'.format(600000+i) for i in range(100)]
    m.jq.get_all_securities = lambda **kw: pd.DataFrame({'start_date': [day]*100}, index=codes)
    frame = pd.DataFrame({'code': codes, 'time': [day]*100, 'money': [0]*10+[10]*90})
    m.jq.get_price = lambda **kw: frame
    result = m.source_day(day, heat=False)['crowding']
    assert result['stock_count'] == 100 and result['top_stock_count'] == 5
    assert result['total_money'] == Decimal('900')
    frame.loc[0, 'money'] = None
    with pytest.raises(ValueError):
        m.source_day(day, heat=False)

def test_market_preview_gap_bound(monkeypatch):
    m = load('市场情绪历史补采_聚宽研究版.py', monkeypatch)
    days = [dt.date(2020, 1, i) for i in range(1, 5)]
    m.range_days = lambda *args: days
    m.calendar = lambda *args: days
    calls = []
    m.source_day = lambda day, **kw: calls.append((day, kw)) or {'day': day}
    assert len(m.run(write_database=False, max_days=2)) == 2
    assert [x[0] for x in calls] == days[:2]
    assert all(not x[1]['heat'] for x in calls)

def test_market_write_journal_resume_independent_holes(monkeypatch, tmp_path):
    m = load('市场情绪历史补采_聚宽研究版.py', monkeypatch)
    db = str(tmp_path/'market.db')
    config = types.ModuleType('mysql_connector')
    config.create_mysql_engine = lambda: create_engine('sqlite:///' + db)
    monkeypatch.setitem(sys.modules, 'mysql_connector', config)
    days = [dt.date(2020, 1, 1) + dt.timedelta(days=i) for i in range(22)]
    wanted = days[-3:]
    m.range_days = lambda *a: wanted
    m.calendar = lambda *a: days
    calls = []
    def source(day, heat=True, crowding=True):
        calls.append((day, heat, crowding))
        result = {}
        if heat:
            result['heat'] = dict(trade_date=day, ratio=Decimal('0.5'), above_count=1,
                eligible_count=2, universe_count=3, updated_at=dt.datetime(2020, 2, 1))
        if crowding:
            result['crowding'] = dict(trade_date=day, top_pct=Decimal('0.05'), ratio=Decimal('0.05'),
                top_money=Decimal('5'), total_money=Decimal('100'), stock_count=100,
                top_stock_count=5, updated_at=dt.datetime(2020, 2, 1))
        return result
    m.source_day = source
    assert len(m.run(max_days=2)) == 2
    assert len(m.run(max_days=2)) == 1
    assert m.run(max_days=2) == []
    engine = create_engine('sqlite:///' + db)
    session = sessionmaker(bind=engine)()
    # Independent older hole, below the existing maximum date.
    session.query(m.MarketCrowding).filter_by(trade_date=wanted[0]).delete(); session.commit(); session.close()
    calls[:] = []
    assert len(m.run(max_days=2)) == 1
    assert calls == [(wanted[0], False, True)]
    from sqlalchemy import text
    with engine.connect() as c:
        payloads = [r[0] for r in c.execute(text('SELECT payload FROM result_sync_change'))]
    assert payloads and all('market_crowding' not in p for p in payloads)
    engine.dispose()


def test_market_heat_paused_new_listing_and_missing(monkeypatch):
    m = load('市场情绪历史补采_聚宽研究版.py', monkeypatch)
    days = [dt.date(2020, 1, 1)+dt.timedelta(days=i) for i in range(20)]
    codes = ['000001.XSHE', '000002.XSHE', '000003.XSHE']
    m.jq.get_trade_days = lambda **kw: days
    m.jq.get_all_trade_days = lambda: days
    m.jq.get_all_securities = lambda **kw: pd.DataFrame({'start_date': [days[0], days[0], days[-1]]}, index=codes)
    frame = pd.DataFrame([dict(code=c, time=d, close=10 if d != days[-1] else 20,
                             paused=int(c == codes[1] and d == days[-1]))
                          for c in codes for d in days if c != codes[2] or d == days[-1]])
    m.jq.get_price = lambda **kw: frame
    row = m.source_day(days[-1], crowding=False)['heat']
    assert row['universe_count'] == 3 and row['eligible_count'] == 1 and row['above_count'] == 1
    frame.loc[0, 'close'] = None
    with pytest.raises(ValueError):
        m.source_day(days[-1], crowding=False)

def test_closed_date_and_limits(script):
    script.china_now = lambda: dt.datetime(2020, 1, 2, 18, 29)
    script.calendar = lambda start, end: [end]
    assert script.range_days(None, None, 20) == [dt.date(2020, 1, 1)]
    with pytest.raises(ValueError):
        script.range_days(None, '2020-01-02', 20)
    for limit in [True, 0, 251, 1.5]:
        with pytest.raises(ValueError):
            script.range_days(None, None, limit)
    script.china_now = lambda: dt.datetime(2020, 1, 2, 18, 30)
    assert script.range_days(None, None, 20) == [dt.date(2020, 1, 2)]

@pytest.mark.parametrize('fault', ['missing_column', 'extra_column', 'pk', 'nullable', 'numeric_precision', 'numeric_scale', 'date_type', 'engine'])
def test_actual_schema_gate(script, monkeypatch, fault):
    from sqlalchemy import inspect, Numeric, String
    engine = create_engine('sqlite:///:memory:')
    script.Base.metadata.create_all(engine)
    real = inspect(engine)
    models = tuple(script.Base.registry.mappers)
    models = tuple(map(lambda mapper: mapper.class_, models))
    class Inspector:
        def get_columns(self, name):
            cols = [dict(c) for c in real.get_columns(name)]
            if fault == 'missing_column':
                return cols[:-1]
            if fault == 'extra_column':
                return cols + [dict(name='unexpected', nullable=True, type=String(1))]
            if fault == 'nullable':
                cols[0]['nullable'] = not cols[0]['nullable']
            if fault == 'date_type':
                next(c for c in cols if c['name'] == 'trade_date')['type'] = String(10)
            for c in cols:
                if isinstance(c['type'], Numeric):
                    if fault == 'numeric_precision':
                        c['type'] = Numeric(c['type'].precision+1, c['type'].scale)
                    if fault == 'numeric_scale':
                        c['type'] = Numeric(c['type'].precision, c['type'].scale+1)
            return cols
        def get_pk_constraint(self, name):
            return {'constrained_columns': []} if fault == 'pk' else real.get_pk_constraint(name)
        def get_table_options(self, name):
            return {'mysql_engine': 'MyISAM' if fault == 'engine' else 'InnoDB'}
    monkeypatch.setattr(script, 'inspect', lambda unused: Inspector())
    fake_mysql = types.SimpleNamespace(dialect=types.SimpleNamespace(name='mysql'))
    with pytest.raises(script.HistoryError):
        script.validate_schema(fake_mysql, models)
    engine.dispose()

@pytest.mark.parametrize('fault', ['none', 'empty', 'duplicate', 'unordered', 'range_gap'])
def test_calendar_rejects_corruption(script, fault):
    days = [dt.date(2020, 1, i) for i in [2, 3, 6]]
    raw = {'none': None, 'empty': [], 'duplicate': days+days[-1:], 'unordered': days[::-1], 'range_gap': days}[fault]
    script.jq.get_all_trade_days = lambda: raw
    script.jq.get_trade_days = lambda **kw: days[:1] if fault == 'range_gap' else days
    with pytest.raises((ValueError, TypeError)):
        script.calendar(None, days[-1])

@pytest.mark.parametrize('value', ['2020-01-02 extra', '2020-2-2', '2020-02-30', '', None, 20200102])
def test_strict_date(script, value):
    with pytest.raises(ValueError):
        script.as_date(value)


@pytest.mark.parametrize('fault', ['empty', 'missing_code', 'duplicate', 'wrong_day', 'infinite', 'negative', 'overprecision'])
def test_market_crowding_source_rejects_partial(monkeypatch, fault):
    m = load('市场情绪历史补采_聚宽研究版.py', monkeypatch)
    day = dt.date(2020, 1, 2)
    codes = ['{:06d}.XSHG'.format(600000+i) for i in range(100)]
    m.jq.get_all_securities = lambda **kw: pd.DataFrame({'start_date': [day]*100}, index=codes)
    frame = pd.DataFrame({'code': codes, 'time': [day]*100, 'money': [10.0]*100})
    if fault == 'empty': frame = frame.iloc[:0]
    if fault == 'missing_code': frame = frame.iloc[:-1]
    if fault == 'duplicate': frame.loc[0, 'code'] = codes[1]
    if fault == 'wrong_day': frame.loc[0, 'time'] = day-dt.timedelta(days=1)
    if fault in ['infinite', 'negative', 'overprecision']:
        frame.loc[0, 'money'] = {'infinite': float('inf'), 'negative': -1, 'overprecision': 1.001}[fault]
    m.jq.get_price = lambda **kw: frame
    with pytest.raises(ValueError):
        m.source_day(day, heat=False)

@pytest.mark.parametrize('value', ['Infinity', '-Infinity', 'NaN', '-NaN', 'sNaN', '1e100', '1e-100', [], {}, True])
def test_all_nonfinite_and_unrepresentable_numeric_inputs(script, value):
    with pytest.raises(ValueError):
        script.number(value, 30, 12)

@pytest.mark.parametrize('failure_stage', ['source', 'database'])
def test_secret_not_in_public_exception(script, monkeypatch, failure_stage, capsys):
    secret = 'mysql://alice:TOP_SECRET_PASSWORD@db/private'
    script.range_days = lambda *args: [dt.date(2020, 1, 2)]
    script.calendar = lambda *args: [dt.date(2020, 1, 2)]
    def fail(*args, **kwargs):
        raise ValueError(secret)
    if failure_stage == 'source':
        script.source_day = fail
    else:
        config = types.ModuleType('mysql_connector'); config.create_mysql_engine = fail
        monkeypatch.setitem(sys.modules, 'mysql_connector', config)
    with pytest.raises(RuntimeError) as caught:
        script.run(write_database=failure_stage == 'database')
    assert secret not in str(caught.value)
    assert 'TOP_SECRET_PASSWORD' not in str(caught.value)
    assert 'TOP_SECRET_PASSWORD' not in capsys.readouterr().out


@pytest.mark.parametrize('fault', ['zero_eligible', 'above_exceeds', 'bad_ratio', 'crowding_count', 'crowding_ratio', 'crowding_amount'])
def test_market_existing_invalid_not_complete(monkeypatch, fault):
    m = load('市场情绪历史补采_聚宽研究版.py', monkeypatch)
    if fault.startswith('crowding'):
        model = m.MarketCrowding
        row = model(trade_date=dt.date(2020, 1, 2), top_pct=Decimal('0.05'), ratio=Decimal('0.05'),
                    top_money=Decimal('5'), total_money=Decimal('100'), stock_count=100, top_stock_count=5, updated_at=dt.datetime(2020, 1, 3))
        if fault == 'crowding_count': row.top_stock_count = 6
        if fault == 'crowding_ratio': row.ratio = Decimal('0.5')
        if fault == 'crowding_amount': row.total_money = Decimal('0')
    else:
        model = m.MarketSentiment
        row = model(trade_date=dt.date(2020, 1, 2), ratio=Decimal('0.5'), above_count=1, eligible_count=2, universe_count=3, updated_at=dt.datetime(2020, 1, 3))
        if fault == 'zero_eligible': row.eligible_count = 0
        if fault == 'above_exceeds': row.above_count = 4
        if fault == 'bad_ratio': row.ratio = Decimal('0.9')
    with pytest.raises(ValueError):
        m.validate_existing(row, model)

@pytest.mark.parametrize('fault', ['short_warmup', 'missing_close', 'missing_paused', 'nonfinite_close', 'duplicate_row', 'wrong_day'])
def test_market_heat_source_guards(monkeypatch, fault):
    m = load('市场情绪历史补采_聚宽研究版.py', monkeypatch)
    days = [dt.date(2020, 1, 1)+dt.timedelta(days=i) for i in range(20)]
    code = '000001.XSHE'
    m.jq.get_all_securities = lambda **kw: pd.DataFrame({'start_date': [days[0]]}, index=[code])
    m.jq.get_all_trade_days = lambda: days
    m.jq.get_trade_days = lambda **kw: days[:-1] if fault == 'short_warmup' else days
    frame = pd.DataFrame(dict(code=[code]*20, time=days, close=[10.0]*20, paused=[0]*20))
    if fault == 'missing_close': frame = frame.iloc[:-1]
    if fault == 'missing_paused': frame = frame.drop(columns=['paused'])
    if fault == 'nonfinite_close': frame.loc[0, 'close'] = float('inf')
    if fault == 'duplicate_row': frame.loc[0, 'time'] = days[1]
    if fault == 'wrong_day': frame.loc[0, 'time'] = days[0]-dt.timedelta(days=1)
    m.jq.get_price = lambda **kw: frame
    with pytest.raises(ValueError):
        m.source_day(days[-1], crowding=False)


@pytest.mark.parametrize('write_database', [False, True])
def test_full_span_none_handles_over_250_days_and_resumes(script, monkeypatch, tmp_path, write_database):
    days = list(pd.bdate_range('2010-01-04', periods=251).date)
    clock_calls = []
    def fixed_now():
        clock_calls.append(1)
        # Any repeated clock read after planning would extend the requested window.
        return dt.datetime.combine(days[-1], dt.time(18, 31)) + dt.timedelta(days=len(clock_calls)-1)
    script.china_now = fixed_now
    script.jq.get_all_trade_days = lambda: days
    script.jq.get_trade_days = lambda **kw: [d for d in days if kw['start_date'] <= d <= kw['end_date']]
    calls = []
    valuation = hasattr(script, 'ValuationDaily')
    if valuation:
        def source(day):
            calls.append(day)
            # summarize's timestamps should not affect the already fixed date plan.
            return script.summarize(day, '4000', [('{:06d}.XSHG'.format(600000+i), '10') for i in range(300)])
    else:
        def source(day, heat=True, crowding=True):
            calls.append(day)
            rows = {}
            if heat:
                rows['heat'] = dict(trade_date=day, ratio=Decimal('0.5'), above_count=1,
                    eligible_count=2, universe_count=3, updated_at=dt.datetime(2011, 1, 1))
            if crowding:
                rows['crowding'] = dict(trade_date=day, top_pct=Decimal('0.05'), ratio=Decimal('0.05'),
                    top_money=Decimal('5'), total_money=Decimal('100'), stock_count=100,
                    top_stock_count=5, updated_at=dt.datetime(2011, 1, 1))
            return rows
    script.source_day = source
    if write_database:
        config = types.ModuleType('mysql_connector')
        db_path = tmp_path/'full-span.sqlite3'
        config.create_mysql_engine = lambda: create_engine('sqlite:///' + str(db_path))
        monkeypatch.setitem(sys.modules, 'mysql_connector', config)
    result = script.run(write_database=write_database)
    assert len(result) == 251 and calls == days
    assert clock_calls  # A frozen requested range, not a loop extending through future dates.
    if write_database:
        script.source_day = lambda *args, **kw: pytest.fail('Complete historical rows must not be downloaded again')
        assert script.run(end_date=days[-1], write_database=True) == []
