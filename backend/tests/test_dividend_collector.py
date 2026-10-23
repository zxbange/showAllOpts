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
from app.models.dividend import DividendSnapshot as WebSnapshot, DividendStock as WebStock

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT/'collectors'/'高股息选股_数据库版.py'
DAY = dt.date(2026, 10, 16)


class Frame:
    def __init__(self, data=None, columns=None):
        self.data = data or []
        self.columns = columns if columns is not None else list(self.data[0]) if self.data else []
        self.empty = not self.data
    def __len__(self): return len(self.data)
    def __getitem__(self, key): return [row[key] for row in self.data]
    def to_dict(self, orient): return self.data


@pytest.fixture
def module(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    pd = ModuleType('pandas'); pd.DataFrame = Frame
    pd.isna = lambda v: v is None or str(v).lower() in ('nan', 'nat')
    monkeypatch.setitem(sys.modules, 'pandas', pd)
    spec = importlib.util.spec_from_file_location('offline_dividend', SOURCE)
    mod = importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, 'PROGRESS_ENABLED', False)
    monkeypatch.setattr(mod, 'china_now', lambda: dt.datetime(2026, 10, 18, 17))
    monkeypatch.setattr(mod, 'connect_database', lambda _: create_engine('sqlite:///'+str(tmp_path/'dividend.sqlite')))
    return mod


@pytest.fixture
def ns(module):
    return module._dividend_strategy({}, module.DB_CONFIG)


def meta(**changes):
    return dict(dict(trade_date=DAY, rule_version='dividend_top25_v1', universe_count=5000, eligible_count=4000,
                classic_candidates=1, value_candidates=1, classic_count=1, value_count=1), **changes)


def row(method='classic', code='000001.XSHE', rank=1, **changes):
    data = dict(method=method,code=code,trade_date=DAY,rank=rank,name='示例股票',industry_code='801010',industry_name='农业',
                industry_level='sw_l3',dividend_yield_1y=Decimal('5'),dividend_ratio_3y=Decimal('16'),
                dividend_cash_1y=Decimal('50000'),dividend_cash_3y=Decimal('160000'),
                pe_ttm=Decimal('7'),pb=Decimal('1'),market_cap=Decimal('100'),close=Decimal('10'),
                revenue_growth=Decimal('10'),profit_growth=Decimal('20'),adjusted_roe=Decimal('8'),peg=Decimal('.35'),
                report_period=dt.date(2026,6,30),publication_date=dt.date(2026,8,1))
    data.update(changes);return data


def test_schemas_and_embedded_factory_match(ns):
    for local,web in ((ns.DividendSnapshot,WebSnapshot),(ns.DividendStock,WebStock)):
        assert [(c.name,str(c.type),c.nullable,c.primary_key) for c in local.__table__.columns] == [
            (c.name,str(c.type),c.nullable,c.primary_key) for c in web.__table__.columns]
    nodes=[]
    for path in (SOURCE,ROOT/'collectors/策略批量采集_数据库版.py'):
        tree=ast.parse(path.read_text(encoding='utf-8'),feature_version=(3,6))
        nodes.append(ast.dump(next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_dividend_strategy')))
    assert nodes[0]==nodes[1]
    assert '__file__' not in SOURCE.read_text(encoding='utf-8')


def test_top10_then_fundamentals_then_top25_and_tie_breaking(ns):
    codes=['{:06d}.XSHE'.format(i) for i in range(1000)]
    caps={c:dict(market_cap=100,pe_ratio=7,pb_ratio=1) for c in reversed(codes)}
    cash={c:dict(cash_1y=Decimal(50000),cash_3y=Decimal(100000+i*100),has_3y=True) for i,c in enumerate(codes)}
    fin={c:dict(inc_total_revenue_year_on_year=10,inc_net_profit_year_on_year=20,inc_return=8) for c in codes}
    selected,ratios,counts=ns.select(caps,cash,fin)
    assert selected['classic']==list(reversed(codes))[:25]
    assert selected['value']==codes[:25]  # equal yield, code ascending
    assert counts==dict(classic=100,value=1000)
    assert ratios[codes[0]]==(Decimal(5),Decimal(10))
    # Failure of the top decile never admits lower-ranked stocks to make 25.
    for c in codes[-100:]:fin[c]['inc_net_profit_year_on_year']=8
    assert ns.select(caps,cash,fin)[0]['classic']==[]


@pytest.mark.parametrize('pe,cash1,accepted', [(0,50000,False),(-1,50000,False),(8,50000,False),(7,40000,False),(7,40001,True)])
def test_value_strict_boundaries_and_no_growth_requirement(ns,pe,cash1,accepted):
    caps={'a':dict(market_cap=100,pe_ratio=pe)}
    cash={'a':dict(cash_1y=Decimal(cash1),cash_3y=Decimal(0),has_3y=False)}
    selected,_,_=ns.select(caps,cash,{})
    assert selected['value']==(['a'] if accepted else [])
    assert selected['classic']==[]


@pytest.mark.parametrize('field,value', [('inc_total_revenue_year_on_year',3),('inc_net_profit_year_on_year',8),('inc_return',4.5)])
def test_classic_growth_boundaries(ns,field,value):
    caps={str(i):dict(market_cap=100,pe_ratio=7) for i in range(10)}
    cash={str(i):dict(cash_1y=Decimal(0),cash_3y=Decimal(1000+i),has_3y=True) for i in range(10)}
    fin={'9':dict(inc_total_revenue_year_on_year=10,inc_net_profit_year_on_year=20,inc_return=8)}
    assert ns.select(caps,cash,fin)[0]['classic']==['9']
    fin['9'][field]=value
    assert ns.select(caps,cash,fin)[0]['classic']==[]


def test_latest_replacement_no_duplicates_unchanged_no_dml_and_empty_results(ns):
    engine=create_engine('sqlite://');ns.Base.metadata.create_all(engine);Session=sessionmaker(bind=engine)
    values=[row(),row('value')]
    assert ns.save_latest(Session,meta(),values)
    statements=[]
    event.listen(engine,'before_cursor_execute',lambda conn,cursor,sql,*args:statements.append(sql))
    assert ns.save_latest(Session,meta(),values) is False
    assert all(sql.lstrip().upper().startswith('SELECT') for sql in statements)
    revised=[row(pe_ttm=Decimal('6')),row('value',code='000002.XSHE')]
    assert ns.save_latest(Session,meta(),revised)
    with Session() as session:
        assert session.query(ns.DividendStock).count()==2
        assert session.query(ns.DividendSnapshot).count()==1
        assert session.get(ns.DividendStock,('classic','000001.XSHE')).pe_ttm==6
    with pytest.raises(ValueError,match='旧日期'):
        ns.save_latest(Session,meta(trade_date=DAY-dt.timedelta(days=1)),[dict(r,trade_date=DAY-dt.timedelta(days=1)) for r in revised])
    assert ns.save_latest(Session,meta(classic_count=0,value_count=0,classic_candidates=0,value_candidates=0),[])
    with Session() as session:
        assert session.query(ns.DividendStock).count()==0 and session.query(ns.DividendSnapshot).count()==1
    engine.dispose()


def test_failed_replacement_rolls_back_both_lists(ns):
    engine=create_engine('sqlite://');ns.Base.metadata.create_all(engine);Session=sessionmaker(bind=engine)
    original=[row(),row('value')];ns.save_latest(Session,meta(),original)
    def broken():
        s=Session()
        def commit():
            s.flush()
            raise RuntimeError('simulated commit failure')
        s.commit=commit;return s
    with pytest.raises(RuntimeError,match='commit failure'):
        ns.save_latest(broken,meta(),[row(name='changed'),row('value',name='changed')])
    with Session() as session:
        assert {r.name for r in session.query(ns.DividendStock)}=={'示例股票'}
        assert session.query(ns.DividendStock).count()==2
    engine.dispose()


def test_dividend_dates_pagination_and_truncation_protection(module):
    table=Table('payout',MetaData(),Column('id',Integer),Column('code',String),Column('a_registration_date',Date),
                Column('a_xr_date',Date),Column('bonus_amount_rmb',Numeric))
    class Query:
        def filter(self, *conditions): return self
        def order_by(self, *columns): return self
        def limit(self, size): return size
    q = lambda *fields: Query()
    columns=['id','code','a_registration_date','a_xr_date','bonus_amount_rmb']
    source=[dict(id=1,code='a',a_registration_date=DAY-dt.timedelta(days=500),a_xr_date=DAY-dt.timedelta(days=499),bonus_amount_rmb=200),
            dict(id=2,code='a',a_registration_date=DAY-dt.timedelta(days=1),a_xr_date=DAY,bonus_amount_rmb=300),
            dict(id=3,code='b',a_registration_date=DAY,a_xr_date=DAY+dt.timedelta(days=1),bonus_amount_rmb=400),
            dict(id=4,code='a',a_registration_date=DAY-dt.timedelta(days=365),a_xr_date=DAY-dt.timedelta(days=365),bonus_amount_rmb=1000)]
    calls=[]
    pages=iter([Frame(source[:2],columns),Frame(source[2:],columns),Frame([],columns)])
    def fetch(query):calls.append(query);return next(pages)
    ns=module._dividend_strategy(dict(query=q,finance=SimpleNamespace(STK_XR_XD=table.c,run_query=fetch)),module.DB_CONFIG)
    result=ns.dividends(['a','b'],DAY)
    assert len(calls)==3  # even a short page is followed until confirmed empty
    assert result['a']['cash_1y']==300 and result['a']['cash_3y']==1500
    assert result['b']['cash_1y']==0 and result['b']['cash_3y']==400
    pages=iter([Frame(source[:2],columns),Frame(source[:2],columns)])
    with pytest.raises(ValueError,match='分页'):ns.dividends(['a','b'],DAY)


@pytest.mark.parametrize('preferred', ['000002.XSHE', '000003.XSHE', '000004.XSHE', '688001.XSHG'])
def test_end_to_end_source_dates_filters_recompute_and_failure_keeps_snapshot(module,monkeypatch,preferred):
    from sqlalchemy import select
    metadata=MetaData()
    val=Table('val',metadata,Column('code',String),Column('market_cap',Numeric),Column('pe_ratio',Numeric),Column('pb_ratio',Numeric))
    ind=Table('ind',metadata,Column('code',String),Column('inc_total_revenue_year_on_year',Numeric),
              Column('inc_net_profit_year_on_year',Numeric),Column('inc_return',Numeric),Column('statDate',Date),Column('pubDate',Date))
    payout=Table('payout',metadata,Column('id',Integer),Column('code',String),Column('a_registration_date',Date),
                 Column('a_xr_date',Date),Column('bonus_amount_rmb',Numeric))
    codes=['{:06d}.XSHE'.format(i) for i in range(40)]+['688001.XSHG','800001.XBSE']
    class Universe(Frame):
        def iterrows(self):
            return iter((c,dict(display_name='ST示例' if c==codes[0] else '示例'+c[:6])) for c in codes)
    dates=[];state={'fail':False,'price_calls':0}
    def quote(batch,start_date,end_date,**kwargs):
        dates.append(end_date);state['price_calls']+=1
        if state['fail']:raise RuntimeError('source unavailable')
        assert kwargs['fields']==['close'] and kwargs['skip_paused'] is False
        # Paused, upper-limit and lower-limit stocks remain available. Only requested fields are returned.
        bars=[dict(code=c,time=end_date,open=11 if c==codes[3] else 9 if c==codes[4] else 10,
                   close=10,high_limit=11,low_limit=9,paused=1 if c==codes[2] else 0) for c in batch]
        return Frame([{k:bar[k] for k in ['code','time']+kwargs['fields']} for bar in bars])
    def fundamentals(q,date):
        dates.append(date)
        batch=next(v for v in q.compile().params.values() if isinstance(v,list))
        if q.column_descriptions[1]['name']=='market_cap':
            return Frame([dict(code=c,market_cap=100,pe_ratio=7,pb_ratio=1) for c in batch])
        return Frame([dict(code=c,inc_total_revenue_year_on_year=10,inc_net_profit_year_on_year=20,
                           inc_return=8,statDate=dt.date(2026,6,30),pubDate=dt.date(2026,8,1)) for c in batch])
    def dividends(q):
        params=q.compile().params
        batch=next(v for v in params.values() if isinstance(v,list))
        after=next(v for k,v in params.items() if k.startswith('id_'))
        columns=['id','code','a_registration_date','a_xr_date','bonus_amount_rmb']
        return Frame([dict(id=i+1,code=c,a_registration_date=DAY-dt.timedelta(days=1),a_xr_date=DAY,bonus_amount_rmb=100000 if c==preferred else 50000)
                      for i,c in enumerate(batch) if i+1>after],columns)
    apis=dict(query=select,valuation=val.c,indicator=ind.c,
              finance=SimpleNamespace(STK_XR_XD=payout.c,run_query=dividends),
              get_all_securities=lambda **kw:Universe([{} for _ in codes],['display_name']),
              get_trade_days=lambda end_date,count:[DAY],
              get_extras=lambda _,batch,**kw:SimpleNamespace(index=[DAY],columns=batch,iloc=[{c:c==codes[1] for c in batch}]),
              get_price=quote,get_fundamentals=fundamentals,
              get_industry=lambda selected,date:{c:{'sw_l3':dict(industry_code='test',industry_name='测试行业')} for c in selected})
    ns=module._dividend_strategy(apis,module.DB_CONFIG)
    result=ns.run()
    assert len(result)==28  # 39 eligible -> top-decile 3 + value top25
    assert {r['trade_date'] for r in result.data}=={DAY}
    assert all(d==DAY for d in dates)
    assert all(not r['code'].startswith(('4','8')) and r['code'] not in codes[:2] for r in result.data)
    assert {r['method'] for r in result.data if r['code']==preferred}=={'classic','value'}
    assert all(r['rank']==1 for r in result.data if r['code']==preferred)
    assert [r['rank'] for r in result.data if r['method']=='value']==list(range(1,26))
    assert all(r['dividend_yield_1y']==(10 if r['code']==preferred else 5) for r in result.data)
    before=state['price_calls'];again=ns.run()
    assert state['price_calls']>before and again.data==result.data  # same date always recomputes
    state['fail']=True
    with pytest.raises(RuntimeError,match='source unavailable'):ns.run()
    with module.database_scope(ns.Base,module.DB_CONFIG) as Session:
        with Session() as session:assert session.query(ns.DividendStock).count()==28
    # Before 16:00 request at most yesterday, and never request a future cutoff.
    monkeypatch.setattr(module,'china_now',lambda:dt.datetime(2026,10,16,15))
    cutoff=[]
    apis['get_trade_days']=lambda end_date,count:(cutoff.append(end_date) or [dt.date(2026,10,15)])
    state['fail']=False
    earlier=module._dividend_strategy(apis,module.DB_CONFIG)
    with pytest.raises(ValueError):earlier.run()  # fixture ST date intentionally mismatches the prior date
    assert cutoff==[dt.date(2026,10,15)]
    with pytest.raises(ValueError,match='晚于今天'):earlier.run(end_date='2026-10-17')
