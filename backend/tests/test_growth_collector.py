"""Offline tests: synthetic JoinQuant data and SQLite only."""
import ast
import datetime as dt
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import Column, Date, Integer, MetaData, Numeric, String, Table, create_engine, event
from sqlalchemy.orm import sessionmaker
from app.models.growth import GrowthSnapshot as WebSnapshot, GrowthStock as WebStock

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT/'collectors'/'高增长_数据库版.py'
DAY = dt.date(2026, 10, 16)


class Frame:
    def __init__(self, data=None, columns=None):
        self.data = data or []
        self.columns = columns if columns is not None else list(self.data[0]) if self.data else []
        self.empty = not self.data
    def __len__(self): return len(self.data)
    def to_dict(self, orient): return self.data


@pytest.fixture
def module(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    pd = ModuleType('pandas'); pd.DataFrame = Frame
    pd.isna = lambda v: v is None or str(v).lower() in ('nan', 'nat')
    monkeypatch.setitem(sys.modules, 'pandas', pd)
    spec = importlib.util.spec_from_file_location('offline_growth', SOURCE)
    mod = importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, 'PROGRESS_ENABLED', False)
    monkeypatch.setattr(mod, 'china_now', lambda: dt.datetime(2026, 10, 18, 17))
    monkeypatch.setattr(mod, 'connect_database', lambda _: create_engine('sqlite:///'+str(tmp_path/'growth.sqlite')))
    return mod


@pytest.fixture
def ns(module):
    return module._growth_strategy({}, module.DB_CONFIG)


def meta(**changes):
    return dict(dict(as_of_date=DAY, valuation_date=DAY, rule_version='growth_cumulative_parent_eps_v1',
                     universe_count=5000,evaluated_count=4000,matched_count=1), **changes)


def source(profit=(100,120,144), eps=(1,1.2,1.44)):
    return [dict(report_period=dt.date(year,month,day),publication_date=dt.date(year+1,2,1) if month==12 else dt.date(year,month+1,20),net_profit=p,eps=e)
            for month,day in [(12,31),(3,31),(6,30),(9,30)]
            for year,p,e in zip([2023,2024,2025],profit,eps)]


def row(ns, **changes):
    data=dict(code='000001.XSHE',as_of_date=DAY,rank=1,name='示例股票',industry_code=None,industry_name=None,industry_level=None,
              pe_ttm=Decimal(20),pb=Decimal(2),market_cap=Decimal(100),annual_profit_growth=Decimal(20),
              reports=ns.evaluate(ns.latest_reports(source(),DAY))[1])
    data.update(changes);return data


def test_schemas_factory_and_python36_compatibility(ns):
    for local,web in ((ns.GrowthSnapshot,WebSnapshot),(ns.GrowthStock,WebStock)):
        assert [(c.name,str(c.type),c.nullable,c.primary_key) for c in local.__table__.columns]==[
            (c.name,str(c.type),c.nullable,c.primary_key) for c in web.__table__.columns]
    nodes=[]
    for p in (SOURCE,ROOT/'collectors/策略批量采集_数据库版.py'):
        tree=ast.parse(p.read_text(encoding='utf-8'),feature_version=(3,6))
        nodes.append(ast.dump(next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_growth_strategy')))
    assert nodes[0]==nodes[1]


@pytest.mark.parametrize('profit,eps,passed',[
    ((100,120,144),(1,1.2,1.44),True),
    ((100,120,143.999999999),(1,1.2,1.44),False),
    ((100,119,150),(1,1.2,1.44),False),
    ((100,120,144),(1,1.2,1.43999),False),
    ((100,120,144),(1,1.19,1.5),False),
    ((-100,-50,20),(-1,-.5,.2),True),
    ((-100,-50,-10),(1,1.2,1.44),False),
    ((100,120,144),(-1,-.5,0),False),
    ((0,100,150),(1,1.2,1.44),False),
    ((100,120,144),(0,1,2),False),
])
def test_sixteen_growth_checks_and_latest_positive(ns,profit,eps,passed):
    result=ns.evaluate(ns.latest_reports(source(profit,eps),DAY))
    assert (result is not None)==passed
    if passed:
        assert len(result[1])==4 and all(len(g)==3 for g in result[1].values())


@pytest.mark.parametrize('month', [3,6,9,12])
@pytest.mark.parametrize('field', ['net_profit','eps'])
def test_any_single_group_or_metric_failure_rejects_stock(ns,month,field):
    data=source()
    next(r for r in data if r['report_period']==dt.date(2025,month,31 if month in (3,12) else 30))[field]=Decimal('.1')
    assert ns.evaluate(ns.latest_reports(data,DAY)) is None


def test_latest_three_missing_values_gaps_and_revisions(ns):
    data=source()
    assert ns.latest_reports(data[:-1],DAY) is None
    missing=[dict(r,eps=None) if r['report_period']==dt.date(2025,6,30) else r for r in data]
    assert ns.latest_reports(missing,DAY) is None
    # A new declining Q1 supersedes the old passing triple.
    data.append(dict(report_period=dt.date(2026,3,31),publication_date=dt.date(2026,4,20),net_profit=100,eps=1))
    assert ns.evaluate(ns.latest_reports(data,DAY)) is None
    gap=[dict(r,report_period=r['report_period'].replace(year=2021),publication_date=dt.date(2022,2,1)) if r['report_period'].year==2023 else r for r in source()]
    assert ns.latest_reports(gap,DAY) is None
    revision=dict(source()[0],publication_date=dt.date(2024,3,1),net_profit=200)
    assert ns.evaluate(ns.latest_reports(source()+[revision],DAY)) is None
    with pytest.raises(ValueError,match='冲突'):
        ns.latest_reports(source()+[dict(source()[0],net_profit=200)],DAY)
    with pytest.raises(ValueError,match='日期'):
        ns.latest_reports(source()+[dict(source()[0],publication_date=DAY+dt.timedelta(days=1))],DAY)


def test_ranking_annual_rate_and_code_tie_break(ns):
    data={'b':source(),'a':source(),'c':source((100,120,180)),'d':source((100,110,150))}
    chosen,count=ns.select(data,DAY)
    assert count==4 and [c[0] for c in chosen]==['c','a','b']
    assert chosen[0][1]==50


def test_atomic_latest_no_duplicates_noop_zero_and_old_date(ns):
    engine=create_engine('sqlite://');ns.Base.metadata.create_all(engine);Session=sessionmaker(bind=engine)
    assert ns.save_latest(Session,meta(),[row(ns)])
    statements=[]
    event.listen(engine,'before_cursor_execute',lambda conn,cursor,sql,*args:statements.append(sql))
    assert not ns.save_latest(Session,meta(),[row(ns)])
    assert all(s.lstrip().startswith('SELECT') for s in statements)
    assert ns.save_latest(Session,meta(),[row(ns,name='更新股票',code='000002.XSHE')])
    with Session() as session:
        assert session.query(ns.GrowthStock).count()==1
        assert session.query(ns.GrowthStock).one().code=='000002.XSHE'
    with pytest.raises(ValueError,match='旧日期'):
        ns.save_latest(Session,meta(as_of_date=DAY-dt.timedelta(days=1)),[row(ns,as_of_date=DAY-dt.timedelta(days=1))])
    assert ns.save_latest(Session,meta(matched_count=0),[])
    with Session() as session:
        assert session.query(ns.GrowthStock).count()==0 and session.query(ns.GrowthSnapshot).count()==1
    engine.dispose()


def test_failed_replacement_preserves_previous_snapshot(ns):
    engine=create_engine('sqlite://');ns.Base.metadata.create_all(engine);Session=sessionmaker(bind=engine)
    ns.save_latest(Session,meta(),[row(ns)])
    def broken():
        s=Session()
        def commit():
            s.flush();raise RuntimeError('commit failed')
        s.commit=commit;return s
    with pytest.raises(RuntimeError,match='commit failed'):
        ns.save_latest(broken,meta(),[row(ns,name='覆盖')])
    with Session() as session:
        assert session.query(ns.GrowthStock).one().name=='示例股票'
    engine.dispose()


def make_finance(module, values, page_size=5):
    schema=MetaData()
    table=Table('reports',schema,Column('id',Integer,primary_key=True),Column('code',String),Column('pub_date',Date),
                Column('start_date',Date),Column('end_date',Date),Column('report_date',Date),Column('report_type',Integer),
                Column('np_parent_company_owners',Numeric),Column('basic_eps',Numeric))
    engine=create_engine('sqlite://');schema.create_all(engine)
    with engine.begin() as con:con.execute(table.insert(),values)
    calls=[]
    Session=sessionmaker(bind=engine)
    def fetch(q):
        with Session() as session:
            data=session.execute(q.statement).mappings().all()
            calls.append((str(q.statement),dict(q.statement.compile().params)))
            return Frame([dict(r) for r in data],[c.name for c in table.columns])
    apis=dict(query=lambda *fields:Session().query(*fields),finance=SimpleNamespace(STK_INCOME_STATEMENT=table.c,run_query=fetch))
    module.STRATEGY_CONFIG['高增长']['page_size']=page_size
    return apis,calls,engine


def raw_source(code='a'):
    return [dict(id=i+1,code=code,pub_date=r['publication_date'],start_date=dt.date(r['report_period'].year,1,1),
                 end_date=r['report_period'],report_date=r['report_period'],report_type=0,
                 np_parent_company_owners=r['net_profit'],basic_eps=r['eps']) for i,r in enumerate(source())]


def test_cumulative_source_keyset_paging_excludes_comparatives_and_single_quarters(module):
    values=raw_source()
    # A newer document's prior-year comparison must not replace the older report.
    values.append(dict(values[5],id=90,report_date=dt.date(2026,3,31),pub_date=dt.date(2026,4,1),np_parent_company_owners=1))
    # An additional stand-alone Q2 observation is not H1 cumulative data.
    values.append(dict(values[8],id=91,start_date=dt.date(2025,4,1),np_parent_company_owners=1))
    # Future publication must be filtered at the source query.
    values.append(dict(values[0],id=92,pub_date=DAY+dt.timedelta(days=1),np_parent_company_owners=1))
    apis,calls,engine=make_finance(module,values)
    ns=module._growth_strategy(apis,module.DB_CONFIG)
    result=ns.financial_reports(['a'],DAY)
    assert len(result['a'])==12
    chosen,evaluated=ns.select(result,DAY)
    assert evaluated==1 and chosen[0][1]==20
    assert len(calls)>2 and all('ORDER BY reports.end_date DESC, reports.id DESC' in sql for sql,_ in calls)
    assert any('reports.end_date <' in sql for sql,_ in calls[1:])
    engine.dispose()


def test_newest_three_do_not_scan_entire_history_and_load_revisions(module):
    values=raw_source()
    # A later restatement of a required report must win even across page boundaries.
    values.append(dict(values[0],id=200,pub_date=dt.date(2026,1,1),np_parent_company_owners=200))
    values += [dict(values[0],id=300+i,end_date=dt.date(year,12,31),start_date=dt.date(year,1,1),
                    report_date=dt.date(year,12,31),pub_date=dt.date(year+1,2,1)) for i,year in enumerate(range(1990,2023))]
    apis,calls,engine=make_finance(module,values,page_size=4)
    ns=module._growth_strategy(apis,module.DB_CONFIG)
    result=ns.financial_reports(['a'],DAY)
    assert ns.select(result,DAY)[0]==[]
    assert len(calls)<len(values)//4
    assert min(r['report_period'].year for r in result['a'])==2023
    engine.dispose()


def test_finance_missing_columns_and_mid_paging_failure_abort(module):
    apis,calls,engine=make_finance(module,raw_source())
    fetch=apis['finance'].run_query
    def failure(q):
        if calls:raise RuntimeError('source outage')
        return fetch(q)
    apis['finance'].run_query=failure
    with pytest.raises(RuntimeError,match='source outage'):
        module._growth_strategy(apis,module.DB_CONFIG).financial_reports(['a'],DAY)
    apis['finance'].run_query=lambda q:Frame([{'id':1}],['id'])
    with pytest.raises(ValueError,match='缺少字段'):
        module._growth_strategy(apis,module.DB_CONFIG).financial_reports(['a'],DAY)
    engine.dispose()


def test_end_to_end_latest_list_and_repeat_is_noop(module):
    apis,calls,engine=make_finance(module,raw_source('000001.XSHE'))
    class Names(Frame):
        def iterrows(self):return iter([('000001.XSHE',{'display_name':'示例公司'})])
    value_table=Table('valuation',MetaData(),Column('code',String),Column('pe_ratio',Numeric),Column('pb_ratio',Numeric),Column('market_cap',Numeric))
    apis.update(get_all_securities=lambda **kw:Names([{'display_name':'示例公司'}]),
                get_trade_days=lambda **kw:[DAY],valuation=value_table.c,
                get_fundamentals=lambda *a,**kw:Frame([dict(code='000001.XSHE',pe_ratio=20,pb_ratio=2,market_cap=100)]),
                get_industry=lambda codes,**kw:{c:{'sw_l3':dict(industry_code='x',industry_name='测试细分行业')} for c in codes})
    ns=module._growth_strategy(apis,module.DB_CONFIG)
    result=ns.run()
    assert len(result)==1 and result.data[0]['industry_name']=='测试细分行业'
    assert result.data[0]['as_of_date']==dt.date(2026,10,18)
    assert ns.run().data==result.data
    engine.dispose()
