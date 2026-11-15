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
    return dict(dict(trade_date=DAY, rule_version='dividend_value_pe12_v6', universe_count=5000, eligible_count=4000,
                value_candidates=2, value_count=2), **changes)


def row(method='value', code=None, rank=1, **changes):
    code = code or ('600000.XSHG' if method == 'bank' else '000001.XSHE')
    data = dict(method=method,code=code,trade_date=DAY,rank=rank,name='示例股票',industry_code='801010',industry_name='农业',
                industry_level='sw_l3',dividend_yield_1y=Decimal('5'),
                dividend_cash_1y=Decimal('50000'),
                dividend_yield_history=[dict(fiscal_year=year,yield_pct=value) for year,value in [(2025,5),(2024,4),(2023,3)]],
                pe_ttm=Decimal('7'),pb=Decimal('1'),market_cap=Decimal('100'),close=Decimal('10'))
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


def test_bank_and_nonbank_rank_independently_before_limits(ns):
    codes=['{:06d}.XSHE'.format(i) for i in range(100)]
    caps={c:dict(market_cap=100,pe_ratio=7) for c in codes}
    cash={c:dict(cash_1y=Decimal(50000),cash_3y=Decimal(0)) for c in codes}
    candidates,yields=ns.candidates(caps,cash)
    selected=ns.select(candidates,set(codes[:40]))
    assert selected['bank']==codes[:10]
    assert selected['value']==codes[40:65]
    assert not set(selected['bank']) & set(selected['value'])
    assert len(candidates)==100 and yields[codes[0]]==5
    assert ns.select(ns.candidates(caps,cash)[0],set(codes))['value']==[]


def test_industry_uses_first_level_code_and_missing_classification_fails(module):
    data={'a': {'sw_l1': {'industry_code':'801780','industry_name':'银行'},
                'sw_l3': {'industry_code':'850701','industry_name':'城商行'}},
          'b': {'sw_l1': {'industry_code':'801790','industry_name':'非银金融'}}}
    ns=module._dividend_strategy({'get_industry':lambda codes,date:{c:data[c] for c in codes}},module.DB_CONFIG)
    industries,banks=ns.industries_for(['a','b'],DAY)
    assert banks=={'a'} and set(industries)=={'a','b'}
    data['b']={}
    with pytest.raises(ValueError,match='一级行业缺失'):
        ns.industries_for(['a','b'],DAY)


def test_two_group_write_limits_and_snapshot_replacement(ns):
    engine=create_engine('sqlite://');ns.Base.metadata.create_all(engine);Session=sessionmaker(bind=engine)
    try:
        ns.save_latest(Session,meta(),[row(),row('bank')])
        data=[row('value',code=str(i),rank=i+1) for i in range(25)]
        data += [row('bank',code=str(100+i),rank=i+1) for i in range(10)]
        head=meta(value_count=35,value_candidates=60)
        assert ns.save_latest(Session,head,data)
        assert not ns.save_latest(Session,head,data)
        with Session() as db:
            assert db.query(ns.DividendStock).count()==35
        with pytest.raises(ValueError):
            ns.save_latest(Session,meta(value_count=36),data+[row('bank',code='bad',rank=11)])
        with pytest.raises(ValueError,match='银行分类'):
            ns.save_latest(Session,head,[dict(r,code='0') if r['method']=='bank' and r['rank']==1 else r for r in data])
    finally:
        engine.dispose()


@pytest.mark.parametrize('pe,cash1,accepted', [(0,50000,False),(-1,50000,False),(8,50000,True),(Decimal('11.999'),50000,True),(12,50000,False),(13,50000,False),(None,50000,False),(7,30000,False),(7,30001,True)])
def test_value_strict_boundaries_and_no_growth_requirement(ns,pe,cash1,accepted):
    caps={'a':dict(market_cap=100,pe_ratio=pe)}
    cash={'a':dict(cash_1y=Decimal(cash1),cash_3y=Decimal(0),has_3y=False)}
    codes,_=ns.candidates(caps,cash)
    selected=ns.select(codes,set())
    assert selected['value']==(['a'] if accepted else [])


def test_latest_replacement_no_duplicates_unchanged_no_dml_and_empty_results(ns):
    engine=create_engine('sqlite://');ns.Base.metadata.create_all(engine);Session=sessionmaker(bind=engine)
    values=[row(),row('bank')]
    assert ns.save_latest(Session,meta(),values)
    statements=[]
    event.listen(engine,'before_cursor_execute',lambda conn,cursor,sql,*args:statements.append(sql))
    assert ns.save_latest(Session,meta(),values) is False
    assert all(sql.lstrip().upper().startswith(('SELECT', 'PRAGMA')) for sql in statements)
    revised=[row(pe_ttm=Decimal('6')),row('bank',code='000002.XSHE')]
    assert ns.save_latest(Session,meta(),revised)
    with Session() as session:
        assert session.query(ns.DividendStock).count()==2
        assert session.query(ns.DividendSnapshot).count()==1
        assert session.get(ns.DividendStock,('value','000001.XSHE')).pe_ttm==6
    with pytest.raises(ValueError,match='旧日期'):
        ns.save_latest(Session,meta(trade_date=DAY-dt.timedelta(days=1)),[dict(r,trade_date=DAY-dt.timedelta(days=1)) for r in revised])
    assert ns.save_latest(Session,meta(value_count=0,value_candidates=0),[])
    with Session() as session:
        assert session.query(ns.DividendStock).count()==0 and session.query(ns.DividendSnapshot).count()==1
    engine.dispose()


def test_failed_replacement_rolls_back_both_lists(ns):
    engine=create_engine('sqlite://');ns.Base.metadata.create_all(engine);Session=sessionmaker(bind=engine)
    original=[row(),row('bank')];ns.save_latest(Session,meta(),original)
    def broken():
        s=Session()
        def commit():
            s.flush()
            raise RuntimeError('simulated commit failure')
        s.commit=commit;return s
    with pytest.raises(RuntimeError,match='commit failure'):
        ns.save_latest(broken,meta(),[row(name='changed'),row('bank',name='changed')])
    with Session() as session:
        assert {r.name for r in session.query(ns.DividendStock)}=={'示例股票'}
        assert session.query(ns.DividendStock).count()==2
    engine.dispose()


def test_existing_database_gains_nullable_history_without_dropping_data(ns):
    from sqlalchemy import text, inspect
    engine=create_engine('sqlite://'); ns.Base.metadata.create_all(engine); Session=sessionmaker(bind=engine)
    ns.save_latest(Session,meta(rule_version='dividend_static_fy_v4'),[row(),row('bank')])
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE dividend_selection_stock DROP COLUMN dividend_yield_history'))
    assert ns.save_latest(Session,meta(),[row(),row('bank')])
    assert 'dividend_yield_history' in {col['name'] for col in inspect(engine).get_columns('dividend_selection_stock')}
    with Session() as session:
        assert session.query(ns.DividendStock).count() == 2
        assert session.query(ns.DividendStock).first().dividend_yield_history[0]['yield_pct'] == 5
    engine.dispose()


def test_dividend_dates_pagination_and_truncation_protection(module):
    table=Table('payout',MetaData(),Column('id',Integer),Column('code',String),Column('a_registration_date',Date),
                Column('a_xr_date',Date),Column('report_date',Date),Column('bonus_amount_rmb',Numeric))
    class Query:
        def filter(self, *conditions): return self
        def order_by(self, *columns): return self
        def limit(self, size): return size
    q = lambda *fields: Query()
    columns=['id','code','report_date','a_registration_date','a_xr_date','bonus_amount_rmb']
    source=[dict(id=1,code='a',report_date=dt.date(2024,12,31),a_registration_date=DAY-dt.timedelta(days=500),a_xr_date=DAY-dt.timedelta(days=499),bonus_amount_rmb=200),
            dict(id=2,code='a',report_date=dt.date(2025,12,31),a_registration_date=DAY-dt.timedelta(days=1),a_xr_date=DAY,bonus_amount_rmb=300),

            dict(id=4,code='a',report_date=dt.date(2025,6,30),a_registration_date=DAY-dt.timedelta(days=365),a_xr_date=DAY-dt.timedelta(days=365),bonus_amount_rmb=1000)]
    calls=[]
    pages=iter([Frame(source[:2],columns),Frame(source[2:],columns),Frame([],columns)])
    def fetch(query):calls.append(query);return next(pages)
    ns=module._dividend_strategy(dict(query=q,finance=SimpleNamespace(STK_XR_XD=table.c,run_query=fetch)),module.DB_CONFIG)
    result=ns.dividends(['a','b'],DAY)
    assert len(calls)==3  # even a short page is followed until confirmed empty
    assert result['a']['cash_1y']==1300 and result['a']['annual_cash'][2024]==200
    assert 'b' not in result
    pages=iter([Frame(source[:2],columns),Frame(source[:2],columns)])
    with pytest.raises(ValueError,match='分页'):ns.dividends(['a','b'],DAY)


@pytest.mark.parametrize('day,annual_cash', [
    (dt.date(2026,11,13), Decimal('3103')),
    (dt.date(2027,1,4), Decimal('1511')),
])
def test_static_yield_sums_report_year_not_payment_window(module,day,annual_cash):
    from sqlalchemy import select
    metadata=MetaData()
    table=Table('payout',metadata,Column('id',Integer,primary_key=True),Column('code',String),
                Column('report_date',Date),Column('a_registration_date',Date),
                Column('a_xr_date',Date),Column('bonus_amount_rmb',Numeric))
    engine=create_engine('sqlite://')
    metadata.create_all(engine)
    payouts=[
        # One year contains three payments, but only two belong to FY2025.
        (1,dt.date(2025,6,30),dt.date(2025,12,15),1414),
        (2,dt.date(2025,12,31),dt.date(2026,5,13),1689),
        (3,dt.date(2026,6,30),dt.date(2026,10,21),1511),
        # A delayed older-year payment must not enter the static numerator.
        (4,dt.date(2024,12,31),dt.date(2026,6,1),9999),
        # Future payments / proposals are not observable as implemented dividends.
        (5,dt.date(2025,12,31),dt.date(2027,2,1),8888),
        (6,dt.date(2025,12,31),None,7777),
    ]
    with engine.begin() as connection:
        connection.execute(table.insert(), [dict(id=i,code='601398.XSHG',report_date=period,
            a_registration_date=ex-dt.timedelta(days=1) if ex else None,
            a_xr_date=ex,bonus_amount_rmb=cash) for i,period,ex,cash in payouts])
    def fetch(q):
        with engine.connect() as connection:
            result=connection.execute(q)
            columns=list(result.keys())
            return Frame([dict(zip(columns,r)) for r in result],columns)
    try:
        ns=module._dividend_strategy(dict(query=select,finance=SimpleNamespace(
            STK_XR_XD=table.c,run_query=fetch)),module.DB_CONFIG)
        cash=ns.dividends(['601398.XSHG'],day)
        assert cash['601398.XSHG']['cash_1y']==annual_cash
        codes,ratios=ns.candidates({'601398.XSHG':dict(market_cap=Decimal('8.85'),pe_ratio=7)},cash)
        selected=ns.select(codes,{'601398.XSHG'})
        assert ratios['601398.XSHG']==annual_cash/100/Decimal('8.85')
        assert selected['bank']==(['601398.XSHG'] if day.year == 2026 else [])
        assert cash['601398.XSHG']['annual_cash'][day.year-1] == annual_cash
    finally:
        engine.dispose()


def test_missing_dividend_report_year_is_not_silently_used(module):
    from sqlalchemy import select
    table=Table('payout',MetaData(),Column('id',Integer),Column('code',String),
                Column('report_date',Date),Column('a_registration_date',Date),
                Column('a_xr_date',Date),Column('bonus_amount_rmb',Numeric))
    record=dict(id=1,code='a',report_date=None,a_registration_date=DAY,
                a_xr_date=DAY,bonus_amount_rmb=50000)
    ns=module._dividend_strategy(dict(query=select,finance=SimpleNamespace(
        STK_XR_XD=table.c,run_query=lambda q:Frame([record]))),module.DB_CONFIG)
    with pytest.raises(ValueError,match='分红报告期缺失'):
        ns.dividends(['a'],DAY)


@pytest.mark.parametrize('old_rule', ['dividend_split_bank_v3', 'dividend_static_3pct_v5'])
def test_rule_upgrade_replaces_old_yields_even_on_same_day(ns, old_rule):
    engine=create_engine('sqlite://')
    ns.Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine)
    try:
        old=meta(rule_version=old_rule)
        ns.save_latest(Session,old,[row(),row('bank')])
        current=meta(value_count=1,value_candidates=1)
        assert ns.save_latest(Session,current,[row(dividend_yield_1y=Decimal('3.51'))])
        with Session() as db:
            assert db.get(ns.DividendSnapshot,1).rule_version=='dividend_value_pe12_v6'
            assert db.query(ns.DividendStock).count()==1
            assert db.query(ns.DividendStock).first().dividend_yield_1y==Decimal('3.51')
    finally:
        engine.dispose()


@pytest.mark.parametrize('preferred', ['000002.XSHE', '000003.XSHE', '000004.XSHE', '688001.XSHG'])
def test_end_to_end_source_dates_filters_recompute_and_failure_keeps_snapshot(module,monkeypatch,preferred):
    from sqlalchemy import select
    metadata=MetaData()
    val=Table('val',metadata,Column('code',String),Column('market_cap',Numeric),Column('pe_ratio',Numeric),Column('pb_ratio',Numeric))
    ind=Table('ind',metadata,Column('code',String),Column('inc_total_revenue_year_on_year',Numeric),
              Column('inc_net_profit_year_on_year',Numeric),Column('inc_return',Numeric),Column('statDate',Date),Column('pubDate',Date))
    payout=Table('payout',metadata,Column('id',Integer),Column('code',String),Column('a_registration_date',Date),
                 Column('a_xr_date',Date),Column('report_date',Date),Column('bonus_amount_rmb',Numeric))
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
        raise AssertionError('Unexpected obsolete financial-indicator query')
    def dividends(q):
        params=q.compile().params
        batch=next(v for v in params.values() if isinstance(v,list))
        after=next(v for k,v in params.items() if k.startswith('id_'))
        columns=['id','code','report_date','a_registration_date','a_xr_date','bonus_amount_rmb']
        return Frame([dict(id=i+1,code=c,report_date=dt.date(2025,12,31),a_registration_date=DAY-dt.timedelta(days=1),a_xr_date=DAY,bonus_amount_rmb=100000 if c==preferred else 50000)
                      for i,c in enumerate(batch) if i+1>after],columns)
    apis=dict(query=select,valuation=val.c,indicator=ind.c,
              finance=SimpleNamespace(STK_XR_XD=payout.c,run_query=dividends),
              get_all_securities=lambda **kw:Universe([{} for _ in codes],['display_name']),
              get_trade_days=lambda end_date,count:[DAY],
              get_extras=lambda _,batch,**kw:SimpleNamespace(index=[DAY],columns=batch,iloc=[{c:c==codes[1] for c in batch}]),
              get_price=quote,get_fundamentals=fundamentals,
              get_industry=lambda selected,date:{c:{'sw_l1':dict(industry_code='801010',industry_name='农林牧渔'), 'sw_l3':dict(industry_code='test',industry_name='测试行业')} for c in selected})
    ns=module._dividend_strategy(apis,module.DB_CONFIG)
    result=ns.run()
    assert len(result)==25  # Nonbank top25; no growth/PEG query
    assert {r['trade_date'] for r in result.data}=={DAY}
    assert all(d==DAY for d in dates)
    assert all(not r['code'].startswith(('4','8')) and r['code'] not in codes[:2] for r in result.data)
    assert {r['method'] for r in result.data if r['code']==preferred}=={'value'}
    assert all(r['rank']==1 for r in result.data if r['code']==preferred)
    assert [r['rank'] for r in result.data if r['method']=='value']==list(range(1,26))
    assert all(r['dividend_yield_1y']==(10 if r['code']==preferred else 5) for r in result.data)
    before=state['price_calls'];again=ns.run()
    assert state['price_calls']>before and again.data==result.data  # same date always recomputes
    state['fail']=True
    with pytest.raises(RuntimeError,match='source unavailable'):ns.run()
    with module.database_scope(ns.Base,module.DB_CONFIG) as Session:
        with Session() as session:assert session.query(ns.DividendStock).count()==25
    # Before 16:00 request at most yesterday, and never request a future cutoff.
    monkeypatch.setattr(module,'china_now',lambda:dt.datetime(2026,10,16,15))
    cutoff=[]
    apis['get_trade_days']=lambda end_date,count:(cutoff.append(end_date) or [dt.date(2026,10,15)])
    state['fail']=False
    earlier=module._dividend_strategy(apis,module.DB_CONFIG)
    with pytest.raises(ValueError):earlier.run()  # fixture ST date intentionally mismatches the prior date
    assert cutoff==[dt.date(2026,10,15)]
    with pytest.raises(ValueError,match='晚于今天'):earlier.run(end_date='2026-10-17')
