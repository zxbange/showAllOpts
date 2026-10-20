import ast
import datetime as dt
import importlib.util
import math
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from app.models.breadth import MicrocapBreadth as WebModel

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT/'collectors'/'legacy'/'微盘新高新低数量统计_数据库版.py'

class Frame:
    def __init__(self, rows=None, columns=None):
        self.rows = rows or []
        self.columns = columns if columns is not None else list(self.rows[0]) if self.rows else []
        self.empty = not self.rows
    def __len__(self): return len(self.rows)
    def to_dict(self, orient): return self.rows

@pytest.fixture
def module(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    pd = ModuleType('pandas')
    pd.DataFrame = Frame
    pd.isna = lambda value: value is None or isinstance(value, float) and math.isnan(value)
    monkeypatch.setitem(sys.modules, 'pandas', pd)
    spec = importlib.util.spec_from_file_location('offline_breadth', SOURCE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, 'china_now', lambda: dt.datetime(2026, 10, 18, 17))
    path = str(tmp_path/'breadth.sqlite')
    monkeypatch.setattr(mod, 'connect_database', lambda _: create_engine('sqlite:///'+path))
    return mod

@pytest.fixture
def scenario(module):
    calendar = [dt.date(2026, 7, 1)+dt.timedelta(days=i) for i in range(110)]
    calendar = [d for d in calendar if d.weekday()<5]
    state = {'missing': None, 'fail': None, 'priced': []}
    codes = ['000001.XSHE', '000002.XSHE', '688001.XSHG', '000003.XSHE']
    securities = SimpleNamespace(empty=False, columns=['start_date'], iterrows=lambda: iter([
        (code, {'start_date': dt.date(2020, 1, 1) if i < 3 else dt.date(2026, 10, 1)}) for i,code in enumerate(codes)]))
    def st(_, batch, start_date, end_date, df):
        return SimpleNamespace(columns=batch, index=[end_date], iloc=[{c: False for c in batch}])
    def prices(batch, start_date, end_date, **kwargs):
        state['priced'].append(end_date)
        if state['fail'] == end_date: raise RuntimeError('source unavailable')
        dates = [d for d in calendar if start_date <= d <= end_date]
        rows = [dict(code=code,time=day,close=float(i+1 if code==codes[0] else 100-i))
                for code in batch for i,day in enumerate(dates)]
        if state['missing'] == end_date: rows.pop()
        return Frame(rows)
    apis = dict(get_all_securities=lambda **kw: securities, get_extras=st,
                query=lambda *args: SimpleNamespace(filter=lambda codes: codes),
                valuation=SimpleNamespace(code=SimpleNamespace(in_=lambda codes: codes), market_cap='market_cap'),
                get_fundamentals=lambda codes,date: Frame([dict(code=c,market_cap=10) for c in reversed(codes)]),
                get_price=prices,
                get_trade_days=lambda end_date,start_date=None,count=None: (
                    [d for d in calendar if d <= end_date][-count:] if count is not None else
                    [d for d in calendar if start_date <= d <= end_date]))
    ns = module._breadth_strategy(apis, module.DB_CONFIG)
    return ns, state, apis


def test_schema_and_single_file_factory_parity(module, scenario):
    ns,_,_=scenario
    assert [(c.name,str(c.type),c.primary_key,c.nullable) for c in ns.MicrocapBreadth.__table__.columns] == [
        (c.name,str(c.type),c.primary_key,c.nullable) for c in WebModel.__table__.columns]
    class WithoutProgress(ast.NodeTransformer):
        def visit_Expr(self, node):
            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == '_progress_note':
                return None
            return self.generic_visit(node)
        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id == '_progress_items':
                return self.visit(node.args[0])
            return self.generic_visit(node)
    nodes=[]
    for path in (SOURCE, ROOT/'collectors'/'策略批量采集_数据库版.py'):
        tree=ast.parse(path.read_text(encoding='utf-8'),feature_version=(3,6))
        nodes.append(ast.dump(WithoutProgress().visit(next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_breadth_strategy')),include_attributes=False))
    assert nodes[0]==nodes[1]
    assert '__file__' not in SOURCE.read_text(encoding='utf-8')


@pytest.mark.parametrize('prices,high,low', [(list(range(1,32)),1,0),(list(range(31,0,-1)),0,1),([10]*31,1,1)])
def test_counts_and_ties(scenario,prices,high,low):
    ns,_,_=scenario
    result=ns.high_low_counts(prices)
    for p in (5,10,20,30):
        assert result[f'high_{p}']==high and result[f'low_{p}']==low


def test_same_day_counts_use_previous_window_not_current(scenario):
    ns,_,_=scenario
    result=ns.high_low_counts([100]+[10]*29+[20])
    assert result['high_5']==1 and result['high_30']==0


def test_constituents_filters_tie_breaking_and_complete_prices(scenario):
    ns,state,_=scenario
    day=dt.date(2026,10,16)
    assert ns.choose_stocks(day,2)==['000001.XSHE','000002.XSHE']
    row=ns.calculate_day(day,2)
    assert row['sample_count']==2 and row['high_5']==row['low_30']==1
    assert state['priced']==[day]  # One batch, all four periods.
    state['missing']=day
    with pytest.raises(ValueError,match='未完整'): ns.calculate_day(day,2)


def test_incremental_resume_and_no_repeated_writes(module,scenario,monkeypatch):
    ns,state,_=scenario
    start,end=dt.date(2026,10,14),dt.date(2026,10,16)
    state['fail']=dt.date(2026,10,15)
    with pytest.raises(RuntimeError): ns.run(start_date=start,end_date=end,micro_num=2)
    state['fail']=None;state['priced'].clear()
    assert len(ns.run(start_date=start,end_date=end,micro_num=2))==2
    assert state['priced']==[dt.date(2026,10,15),end]
    state['priced'].clear()
    writes=[]
    engine=module.connect_database({})
    monkeypatch.setattr(module,'connect_database',lambda _:engine)
    def capture(conn,cursor,statement,*args):
        if statement.lstrip().split()[0].upper() in ('INSERT','UPDATE','DELETE'): writes.append(statement)
    event.listen(engine,'before_cursor_execute',capture)
    assert len(ns.run(start_date=start,end_date=end,micro_num=2))==0
    assert not state['priced'] and not writes
    with engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM microcap_breadth').scalar()==3
    engine.dispose()


def test_before_16_does_not_freeze_intraday_data(module,scenario,monkeypatch):
    ns,state,_=scenario
    monkeypatch.setattr(module,'china_now',lambda:dt.datetime(2026,10,16,15,30))
    assert len(ns.run(start_date='2026-10-16',micro_num=2))==0
    assert not state['priced']


def test_bad_stock_source_cannot_produce_partial_snapshot(module,scenario):
    ns,state,apis=scenario
    apis['get_fundamentals']=lambda codes,date:Frame([dict(code=codes[0],market_cap=10)])
    broken=module._breadth_strategy(apis,module.DB_CONFIG)
    with pytest.raises(ValueError,match='市值数据不完整'): broken.run(start_date='2026-10-16',end_date='2026-10-16',micro_num=2)
    assert not state['priced']


def test_save_day_atomic_and_existing_snapshot_preserved(module,scenario):
    ns,_,_=scenario
    record=ns.calculate_day(dt.date(2026,10,16),2)
    engine=module.connect_database({});ns.Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine)
    assert ns.save_day(Session,record)
    assert not ns.save_day(Session,dict(record,high_5=0,high_10=0,high_20=0,high_30=0))
    with Session() as session: assert session.query(ns.MicrocapBreadth).one().high_5==1
    class FailCommit(Session.class_):
        def commit(self):
            self.flush()
            raise RuntimeError('commit failed')
    with pytest.raises(RuntimeError): ns.save_day(sessionmaker(bind=engine,class_=FailCommit),dict(record,trade_date=dt.date(2026,10,15)))
    with Session() as session: assert session.query(ns.MicrocapBreadth).count()==1
    engine.dispose()
