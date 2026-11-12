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

@pytest.fixture(params=['市场情绪历史补采_聚宽研究版.py', '估值标尺历史补采_聚宽研究版.py'])
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

def test_valuation_signed_members_and_precision(monkeypatch):
    m = load('估值标尺历史补采_聚宽研究版.py', monkeypatch)
    members = [('{:06d}.XSHG'.format(600000+i), Decimal('-2') if i == 0 else Decimal('10')) for i in range(300)]
    day = dt.date(2020, 1, 2)
    daily, raw, cal = m.summarize(day, '1234.12345678', members)
    assert daily['positive_count'] == 299 and daily['loss_count'] == 1
    assert daily['mean_pe_ttm'] == Decimal('10') and daily['pe_sum'] == Decimal('2990')
    assert raw[0]['pe_ttm'] == Decimal('-2')
    assert cal['source'] == m.CALENDAR_SOURCE
    for invalid in ['0', None, 'NaN', '1.0000000000001']:
        with pytest.raises(ValueError):
            m.summarize(day, '1234', [(members[0][0], invalid)] + members[1:])
    with pytest.raises(ValueError):
        m.summarize(day, '1234.000000001', members)
    with pytest.raises(ValueError):
        m.summarize(day, '1234', members[:-1])

@pytest.mark.parametrize('code,valid', [
    ('600000.XSHG', True), ('688001.XSHG', True), ('689009.XSHG', True),
    ('000001.XSHE', True), ('300001.XSHE', True), ('301001.XSHE', True),
    ('302001.XSHE', True), ('000001.XSHG', False), ('600000.XSHE', False),
    ('900001.XSHG', False), ('600000.XSHG\n', False), ('600000', False),
])
def test_valuation_collector_importer_code_contract(monkeypatch, code, valid):
    from app.services.valuation_ruler_model import valid_code
    m = load('估值标尺历史补采_聚宽研究版.py', monkeypatch)
    assert bool(m.valid_code(code)) is valid
    assert bool(valid_code(code)) is valid
    members = [('{:06d}.XSHG'.format(600001+i), '10') for i in range(299)] + [(code, '-2')]
    if valid:
        assert m.summarize(dt.date(2020, 1, 2), '4000', members)[0]['member_count'] == 300
    else:
        with pytest.raises(m.HistoryError):
            m.summarize(dt.date(2020, 1, 2), '4000', members)


@pytest.mark.parametrize('api_location', ['module', 'notebook', 'builtins'])
def test_valuation_dated_collector_to_importer_to_sqlite_contract(monkeypatch, tmp_path, api_location):
    """Real collector schema/rows and importer; only the external JQ API is synthetic."""
    from app.services import valuation_ruler_import as importer
    from app.services.valuation_ruler import ValuationRuler, CHINA
    m = load('估值标尺历史补采_聚宽研究版.py', monkeypatch)
    day = dt.date(2020, 1, 2)
    codes = (['{:06d}.XSHG'.format(600000+i) for i in range(100)] +
             ['{:06d}.XSHE'.format(1+i) for i in range(100)] +
             ['{:06d}.XSHE'.format(300001+i) for i in range(98)] +
             ['689009.XSHG', '302001.XSHE'])
    calls = []
    class Field:
        def in_(self, selected):
            assert selected == codes
            return self
    m.jq.valuation = types.SimpleNamespace(code=Field(), day=Field(), pe_ratio=Field())
    m.jq.query = lambda *args: types.SimpleNamespace(filter=lambda condition: 'dated-query')
    def members(index, date):
        assert index == m.INDEX and date == day
        calls.append(('members', date))
        return codes
    def fundamentals(query, date):
        assert query == 'dated-query' and date == day
        calls.append(('pe', date))
        return pd.DataFrame({'code': codes, 'day': [day]*300,
                             'pe_ratio': [-3.5]+[12.25]*299})
    def price(index, **kwargs):
        assert index == m.INDEX
        assert kwargs['start_date'] == kwargs['end_date'] == day and kwargs['fq'] is None
        return pd.DataFrame({'close': [4000.125]}, index=[day])
    m.jq.get_index_stocks, m.jq.get_fundamentals, m.jq.get_price = members, fundamentals, price
    m.jq.get_all_trade_days = lambda: [day]
    m.jq.get_trade_days = lambda **kwargs: [day]
    if api_location != 'module':
        import builtins
        target = m if api_location == 'notebook' else builtins
        for name in ('get_index_stocks', 'get_fundamentals', 'get_price', 'get_all_trade_days',
                     'get_trade_days', 'query', 'valuation'):
            monkeypatch.setattr(target, name, getattr(m.jq, name), raising=False)
            delattr(m.jq, name)
    db_path = tmp_path/'collector.sqlite3'
    config = types.ModuleType('mysql_connector')
    config.create_mysql_engine = lambda: create_engine('sqlite:///' + str(db_path))
    monkeypatch.setitem(sys.modules, 'mysql_connector', config)
    assert len(m.run(start_date=day, end_date=day, max_days=1)) == 1
    assert calls == [('members', day), ('pe', day)]
    source = create_engine('sqlite:///' + str(db_path))
    service = ValuationRuler(types.SimpleNamespace(log_path=tmp_path/'published',
                                                   valuation_ruler_worker_enabled=False))
    moment = dt.datetime(2020, 1, 2, 20, tzinfo=CHINA)
    assert importer.start_import(service, background=False, moment=moment, engine_factory=lambda: source)
    history = service.history(moment)
    assert service.read_state()['phase'] == 'complete'
    assert history['latest']['member_count'] == 300
    assert history['latest']['positive_count'] == 299 and history['latest']['loss_count'] == 1
    assert history['latest']['mean_pe_ttm'] == 12.25
    assert history['latest']['index_close'] == 4000.125
    assert history['benchmarks']['p20'] == history['benchmarks']['p80'] == 12.25
    with service.db() as db:
        actual = {row['code']: row['pe_ttm'] for row in db.execute('SELECT code,pe_ttm FROM members')}
    assert set(actual) == set(codes)
    assert Decimal(actual[codes[0]]) == Decimal('-3.5')


def test_valuation_notebook_builtin_shadowing(monkeypatch):
    import numpy as np
    m = load('估值标尺历史补采_聚宽研究版.py', monkeypatch)
    for name, value in dict(any=np.any, all=np.all, sum=np.sum, max=np.max,
                            bool=np.bool_, int=np.int64, str=np.str_).items():
        monkeypatch.setattr(m, name, value, raising=False)
    day = dt.date(2020, 1, 2)
    m.jq.get_all_trade_days = lambda: [day]
    m.jq.get_trade_days = lambda **kw: [day]
    m.source_day = lambda day: m.summarize(day, '100', [
        ('{:06d}.XSHG'.format(600000+i), '10') for i in range(300)])
    result = m.run(start_date=day, end_date=day, write_database=False, max_days=1)
    assert result[0]['mean_pe_ttm'] == Decimal(10)


def test_valuation_missing_api_has_explicit_name(monkeypatch):
    m = load('估值标尺历史补采_聚宽研究版.py', monkeypatch)
    with pytest.raises(m.HistoryError, match='get_index_stocks'):
        m.source_day(dt.date(2020, 1, 2))


@pytest.mark.parametrize('fail', [False, True])
def test_valuation_progress_tracks_completed_days_and_failure(monkeypatch, capsys, fail):
    m = load('估值标尺历史补采_聚宽研究版.py', monkeypatch)
    days = [dt.date(2020, 1, 2), dt.date(2020, 1, 3)]
    m.range_days = lambda *a: days
    def source(day):
        if fail and day == days[1]:
            raise m.HistoryError('测试源缺失')
        return {'trade_date': day}, [], {}
    m.source_day = source
    if fail:
        with pytest.raises(m.HistoryError):
            m.run(write_database=False)
    else:
        assert len(m.run(write_database=False)) == 2
    output = capsys.readouterr().out
    assert '\r' in output and '已用' in output and '2020-01-03' in output
    if fail:
        assert '1/2 日 50%' in output and '2/2 日 100%' not in output
        assert '失败：本日未提交' in output
    else:
        assert '2/2 日 100%' in output and '预览完成（未入库）' in output


def test_valuation_progress_throttles_repeated_stage(monkeypatch, capsys):
    m = load('估值标尺历史补采_聚宽研究版.py', monkeypatch)
    monkeypatch.setattr(m.time, 'monotonic', lambda: 10)
    progress = m.Progress()
    for i in range(100):
        progress.update('检查已有记录', i+1, 100)
    progress.finish('检查完成')
    output = capsys.readouterr().out
    assert output.count('\r') == 2
    assert '100/100 日 100%' in output


def test_valuation_failure_diagnostics_keep_date_stage_and_mysql_code_not_secrets(monkeypatch):
    from sqlalchemy.exc import OperationalError
    m = load('估值标尺历史补采_聚宽研究版.py', monkeypatch)
    day = dt.date(2020, 1, 2)
    m.range_days = lambda *a: [day]
    def fail(day):
        raise OperationalError('SELECT secret_sql', {'password': 'secret_password'},
                               Exception(2013, 'mysql://secret_user:secret_password@private-host/db'))
    m.source_day = fail
    with pytest.raises(RuntimeError) as error:
        m.run(write_database=False)
    message = str(error.value)
    assert '2020-01-02' in message and '读取成分、PE和指数行情' in message
    assert 'OperationalError' in message and '2013' in message and 'run:' in message
    assert all(secret not in message for secret in ('secret', 'private-host', 'SELECT', 'mysql://'))
    assert error.value.__suppress_context__


def test_valuation_existing_full_integrity(monkeypatch):
    m = load('估值标尺历史补采_聚宽研究版.py', monkeypatch)
    e = create_engine('sqlite:///:memory:')
    m.Base.metadata.create_all(e)
    session = sessionmaker(bind=e)()
    day = dt.date(2020, 1, 2)
    assert not m.existing_day(session, day)
    daily, raw, cal = m.summarize(day, '100', [('{:06d}.XSHG'.format(600000+i), '10') for i in range(300)])
    session.add(m.ValuationDaily(**daily)); session.commit()
    with pytest.raises(ValueError):
        m.existing_day(session, day)
    session.add_all([m.ValuationMember(**r) for r in raw]); session.add(m.ValuationCalendar(**cal)); session.commit()
    assert m.existing_day(session, day)
    session.query(m.ValuationMember).first().pe_ttm = Decimal('11'); session.commit()
    with pytest.raises(ValueError):
        m.existing_day(session, day)
    session.close(); e.dispose()

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

def test_source_failure_commits_only_prior_days(monkeypatch, tmp_path):
    m = load('估值标尺历史补采_聚宽研究版.py', monkeypatch)
    db = str(tmp_path/'valuation.db')
    config = types.ModuleType('mysql_connector')
    config.create_mysql_engine = lambda: create_engine('sqlite:///' + db)
    monkeypatch.setitem(sys.modules, 'mysql_connector', config)
    days = [dt.date(2020, 1, 2), dt.date(2020, 1, 3)]
    m.range_days = lambda *a: days
    def source(day):
        if day == days[1]:
            raise m.HistoryError('测试源缺失。')
        return m.summarize(day, '100', [('{:06d}.XSHG'.format(600000+i), '10') for i in range(300)])
    m.source_day = source
    with pytest.raises(m.HistoryError, match='测试源缺失'):
        m.run()
    engine = create_engine('sqlite:///' + db)
    session = sessionmaker(bind=engine)()
    assert session.query(m.ValuationDaily).count() == 1
    assert session.query(m.ValuationMember).count() == 300
    assert m.existing_day(session, days[0])
    assert not m.existing_day(session, days[1])
    session.close(); engine.dispose()

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

@pytest.mark.parametrize('fault', ['299', 'duplicate', 'badcode', 'pe_missing', 'pe_zero', 'pe_nan', 'wrong_day', 'index_day', 'index_missing', 'pe_empty'])
def test_valuation_real_source_path(monkeypatch, fault):
    m = load('估值标尺历史补采_聚宽研究版.py', monkeypatch)
    day = dt.date(2020, 1, 2)
    codes = ['{:06d}.XSHG'.format(600000+i) for i in range(300)]
    if fault == '299':
        codes.pop()
    if fault == 'duplicate':
        codes[-1] = codes[0]
    if fault == 'badcode':
        codes[0] = '600000'
    class Field:
        def in_(self, value):
            assert value == codes
            return self
    m.jq.valuation = types.SimpleNamespace(code=Field(), day=Field(), pe_ratio=Field())
    m.jq.query = lambda *args: types.SimpleNamespace(filter=lambda f: 'dated-query')
    m.jq.get_index_stocks = lambda index, date: codes if index == m.INDEX and date == day else None
    frame = pd.DataFrame({'code': codes, 'day': [day]*len(codes), 'pe_ratio': [10]*len(codes)})
    if fault in ('pe_missing', 'pe_zero', 'pe_nan'):
        frame.loc[0, 'pe_ratio'] = {'pe_missing': None, 'pe_zero': 0, 'pe_nan': float('nan')}[fault]
    if fault == 'wrong_day':
        frame.loc[0, 'day'] = dt.date(2020, 1, 1)
    if fault == 'pe_empty':
        frame = frame.iloc[:0]
    def fundamentals(query, date):
        assert query == 'dated-query' and date == day
        return frame
    m.jq.get_fundamentals = fundamentals
    def price(index, **kw):
        assert index == m.INDEX and kw['fq'] is None and kw['start_date'] == day and kw['end_date'] == day
        return None if fault == 'index_missing' else pd.DataFrame({'close': [100]}, index=[day-dt.timedelta(days=1) if fault == 'index_day' else day])
    m.jq.get_price = price
    with pytest.raises(ValueError):
        m.source_day(day)

@pytest.mark.parametrize('fault', ['source', 'calendar_source', 'hash', 'mean', 'partial', 'null_pe'])
def test_existing_valuation_corruption_not_skipped(monkeypatch, fault):
    m = load('估值标尺历史补采_聚宽研究版.py', monkeypatch)
    engine = create_engine('sqlite:///:memory:'); m.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    day = dt.date(2020, 1, 2)
    daily, rows, cal = m.summarize(day, '100', [('{:06d}.XSHG'.format(600000+i), '-1' if i == 0 else '10') for i in range(300)])
    if fault == 'source': daily['source'] = 'OTHER'
    if fault == 'calendar_source': cal['source'] = 'OTHER'
    if fault == 'hash': daily['members_sha256'] = '0'*64
    if fault == 'mean': daily['mean_pe_ttm'] += 1
    if fault == 'partial': rows.pop()
    session.add(m.ValuationDaily(**daily)); session.add(m.ValuationCalendar(**cal)); session.add_all([m.ValuationMember(**r) for r in rows]); session.commit()
    if fault == 'null_pe':
        # Deliberately avoid flush: simulate a corrupt external row despite NOT NULL.
        session.query(m.ValuationMember).first().pe_ttm = None
    with session.no_autoflush:
        with pytest.raises(ValueError):
            m.existing_day(session, day)
    session.rollback(); session.close(); engine.dispose()

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

def test_mid_transaction_failure_rolls_back_daily_members_calendar_and_journal(monkeypatch, tmp_path):
    from sqlalchemy import event, text
    m = load('估值标尺历史补采_聚宽研究版.py', monkeypatch)
    db = str(tmp_path/'atomic.db')
    config = types.ModuleType('mysql_connector')
    config.create_mysql_engine = lambda: create_engine('sqlite:///' + db)
    monkeypatch.setitem(sys.modules, 'mysql_connector', config)
    day = dt.date(2020, 1, 2)
    m.range_days = lambda *args: [day]
    m.source_day = lambda d: m.summarize(d, '100', [('{:06d}.XSHG'.format(600000+i), '10') for i in range(300)])
    def crash(mapper, connection, target):
        if target.code == '600100.XSHG':
            raise RuntimeError('TOP_SECRET_PASSWORD')
    event.listen(m.ValuationMember, 'before_insert', crash)
    with pytest.raises(RuntimeError) as caught:
        m.run()
    assert 'TOP_SECRET_PASSWORD' not in str(caught.value)
    engine = create_engine('sqlite:///' + db)
    session = sessionmaker(bind=engine)()
    assert session.query(m.ValuationDaily).count() == 0
    assert session.query(m.ValuationMember).count() == 0
    assert session.query(m.ValuationCalendar).count() == 0
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM result_sync_change')).scalar() == 0
        assert c.execute(text('SELECT revision FROM result_sync_head')).scalar() == 0
    event.remove(m.ValuationMember, 'before_insert', crash)
    assert len(m.run()) == 1
    m.source_day = lambda d: pytest.fail('Existing complete day must not be fetched/replaced')
    assert m.run() == []
    session.close(); engine.dispose()

def test_valuation_hash_signed_provenance_and_half_even(monkeypatch):
    import hashlib
    m = load('估值标尺历史补采_聚宽研究版.py', monkeypatch)
    day = dt.date(2020, 1, 2)
    codes = ['{:06d}.XSHG'.format(600000+i) for i in range(300)]
    # 2 positive members: midpoint rounds toward the even 12th decimal.
    members = [(c, Decimal('-1')) for c in codes]
    members[0] = (codes[0], Decimal('1.000000000000'))
    members[1] = (codes[1], Decimal('1.000000000001'))
    daily, rows, cal = m.summarize(day, '100', list(reversed(members)))
    assert daily['members_sha256'] == hashlib.sha256('\n'.join(codes).encode('utf-8')).hexdigest()
    assert daily['mean_pe_ttm'] == Decimal('1.000000000000')
    assert daily['loss_count'] == 298
    assert all(r['source'] == 'JQ_PLATFORM.PE_TTM.V1' and r['trade_date'] == day for r in rows)
    assert daily['method_version'] == 'csi300_equal_positive_pe_ttm_v1'
    assert daily['index_code'] == '000300.XSHG'
    assert cal['source'] == 'JQDATA.GET_TRADE_DAYS'

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

@pytest.mark.parametrize('fault', ['unsigned', 'string_length', 'pk_order'])
def test_valuation_mysql_schema_exact_shape(monkeypatch, fault):
    from sqlalchemy import inspect, String
    from sqlalchemy.dialects.mysql import DECIMAL
    m = load('估值标尺历史补采_聚宽研究版.py', monkeypatch)
    engine = create_engine('sqlite:///:memory:'); m.Base.metadata.create_all(engine)
    real = inspect(engine)
    class Inspector:
        def get_columns(self, name):
            cols = [dict(c) for c in real.get_columns(name)]
            for col in cols:
                if col['name'] == 'index_close' and fault == 'unsigned':
                    col['type'] = DECIMAL(24, 8, unsigned=True)
                if col['name'] == 'source' and fault == 'string_length':
                    col['type'] = String(49)
            return cols
        def get_pk_constraint(self, name):
            result = real.get_pk_constraint(name)
            if fault == 'pk_order':
                result['constrained_columns'] = list(reversed(result['constrained_columns']))
            return result
        def get_table_options(self, name): return {'mysql_engine': 'InnoDB'}
    m.inspect = lambda unused: Inspector()
    with pytest.raises(ValueError):
        m.validate_schema(types.SimpleNamespace(dialect=types.SimpleNamespace(name='mysql')), m.MODELS)
    engine.dispose()

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
    result = script.run(max_days=None, write_database=write_database)
    assert len(result) == 251 and calls == days
    assert clock_calls  # A frozen requested range, not a loop extending through future dates.
    if write_database:
        script.source_day = lambda *args, **kw: pytest.fail('Complete historical rows must not be downloaded again')
        assert script.run(end_date=days[-1], max_days=None, write_database=True) == []
