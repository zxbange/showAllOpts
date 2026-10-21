import ast
import datetime as dt
import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
import pandas as pd
import pytest
from sqlalchemy import Column, Date, Integer, String, Float, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from app.models.ownership import OwnershipStock, OwnershipReport, OwnershipHolders

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT/'collectors'/'个股机构持仓_数据库版.py'

@pytest.fixture
def mod(monkeypatch):
    monkeypatch.setitem(sys.modules,'jqdata',ModuleType('jqdata'))
    spec=importlib.util.spec_from_file_location('ownership_collector',SOURCE)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.PROGRESS_ENABLED=False
    return module

@pytest.fixture
def ns(mod):
    return mod._ownership_strategy({'finance':None,'query':None},mod.DB_CONFIG)

def member(identity='social1', name='全国社保基金一零一组合', kind=307004):
    return dict(id=identity,name=name,class_id=kind,shares=100,ratio=.1,market_value=200)

def payload(members,complete=True):
    return dict(float_members=members,float_complete=complete)

def test_social_requires_adjacent_complete_and_matches_identity_or_name(ns):
    current=payload([member()])
    assert ns.social_entry(current,None)['status']=='unknown'
    assert ns.social_entry(current,payload([],False))['status']=='unknown'
    first=ns.social_entry(current,payload([member('other','其他公司',307099)]))
    assert first['first_appearance'] and len(first['new_combinations'])==1
    assert first['new_market_value']==200
    assert not ns.social_entry(current,payload([member(name='旧名称')]))['new_combinations']
    assert not ns.social_entry(current,payload([member(identity='changed-id')]))['new_combinations']
    assert not ns.social_entry(current,payload([member(kind=307099)]))['new_combinations']
    addition=ns.social_entry(payload([member(),member('social2','全国社保基金二零二组合')]),current)
    assert len(addition['new_combinations'])==1 and not addition['first_appearance']

def test_missing_not_zero_and_schema(ns):
    assert ns.aggregate([], 'x','y') is None
    assert ns.aggregate([dict(shares=10,market_value=None)],'x','y')['market_value'] is None
    for collector,web in [(ns.OwnershipStock,OwnershipStock),(ns.OwnershipReport,OwnershipReport),(ns.OwnershipHolders,OwnershipHolders)]:
        assert [(c.name,str(c.type),c.primary_key) for c in collector.__table__.columns]==[(c.name,str(c.type),c.primary_key) for c in web.__table__.columns]

def test_float_snapshot_requires_all_ten_and_latest_disclosure(ns):
    rows=[dict(id=i,shareholder_rank=i,pub_date=dt.date(2026,10,1),share_number=10,
               shareholder_id=i,shareholder_name='name'+str(i),shareholder_class_id=307099,share_ratio=1) for i in range(1,11)]
    members,complete,_=ns.float_snapshot(rows,2)
    assert complete and members[0]['market_value']==20
    assert not ns.float_snapshot(rows[:-1],2)[1]
    rows.append(dict(rows[0],id=100,pub_date=dt.date(2026,10,2)))
    assert not ns.float_snapshot(rows,2)[1]  # do not mix revisions

def test_factories_match_and_python36():
    def factory(path):
        tree=ast.parse(path.read_text(encoding='utf-8'),feature_version=(3,6))
        return next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_ownership_strategy')
    assert ast.dump(factory(SOURCE))==ast.dump(factory(ROOT/'collectors'/'策略批量采集_数据库版.py'))

@pytest.mark.parametrize('batch_size', [1, 50])
def test_full_collection_repeat_correction_future_filter_and_social(mod,monkeypatch,tmp_path,batch_size):
    base=declarative_base()
    class Fund(base):
        __tablename__='fund'
        id=Column(Integer,primary_key=True);code=Column(String);symbol=Column(String)
        period_end=Column(Date);pub_date=Column(Date);report_type_id=Column(Integer);report_type=Column(String)
        shares=Column(Float);market_cap=Column(Float)
    class Floats(base):
        __tablename__='floats'
        id=Column(Integer,primary_key=True);code=Column(String);end_date=Column(Date);pub_date=Column(Date)
        change_reason_id=Column(Integer);sharesnature_id=Column(Integer);shareholder_rank=Column(Integer)
        shareholder_id=Column(Integer);shareholder_name=Column(String);shareholder_class_id=Column(Integer)
        share_number=Column(Float);share_ratio=Column(Float)
    class Holder(base):
        __tablename__='holder'
        id=Column(Integer,primary_key=True);code=Column(String);end_date=Column(Date);pub_date=Column(Date)
        share_holders=Column(Integer);a_share_holders=Column(Integer);b_share_holders=Column(Integer);h_share_holders=Column(Integer)
    class Main(base):
        __tablename__='main'
        id=Column(Integer,primary_key=True);main_code=Column(String);statistics_main_code=Column(String)
    source=create_engine('sqlite://');base.metadata.create_all(source);source_session=sessionmaker(bind=source)()
    code='600001.XSHG';other='600002.XSHG';asof=dt.date(2026,10,21)
    source_session.add_all([Main(id=1,main_code='A',statistics_main_code='A'),Main(id=2,main_code='C',statistics_main_code='A')])
    for i,fund in enumerate(['A','C'],1):
        source_session.add(Fund(id=i,code=fund,symbol='600001',period_end=dt.date(2026,9,30),pub_date=dt.date(2026,10,20),report_type_id=403003,report_type='第三季度',shares=100,market_cap=200))
    source_session.add(Fund(id=3,code='C',symbol='600002',period_end=dt.date(2026,9,30),pub_date=dt.date(2026,10,20),report_type_id=403003,report_type='第三季度',shares=500,market_cap=1000))
    for p,period in enumerate([dt.date(2026,6,30),dt.date(2026,9,30)]):
        for i in range(1,11):
            social=p==1 and i==1
            source_session.add(Floats(id=p*10+i,code=code,end_date=period,pub_date=period+dt.timedelta(days=10),
                change_reason_id=306019,sharesnature_id=308007,shareholder_rank=i,shareholder_id=99 if social else i,
                shareholder_name='社保组合' if social else '公司'+str(i),shareholder_class_id=307004 if social else 307099,
                share_number=100,share_ratio=1))
    source_session.add_all([Holder(id=1,code=code,end_date=dt.date(2026,6,30),pub_date=dt.date(2026,7,20),share_holders=110,a_share_holders=100),
        Holder(id=2,code=code,end_date=dt.date(2026,9,30),pub_date=dt.date(2026,10,22),share_holders=80,a_share_holders=70)])
    source_session.commit()
    fail_late = [False]
    def run_query(q):
        params = q.statement.compile().params
        if fail_late[0] and params.get('period_end_1') == dt.date(2026,9,30) and params.get('symbol_1') == ['600002',other]:
            raise RuntimeError('synthetic late batch failure')
        with source.connect() as c:
            result=c.execute(q.statement)
            return pd.DataFrame(result.fetchall(),columns=result.keys())
    finance=SimpleNamespace(FUND_MAIN_INFO=Main,FUND_PORTFOLIO_STOCK=Fund,STK_SHAREHOLDER_FLOATING_TOP10=Floats,STK_HOLDER_NUM=Holder,run_query=run_query)
    api=dict(finance=finance,query=source_session.query,get_all_securities=lambda **kw:pd.DataFrame({'display_name':['示例','示例二']},index=[code,other]),
        get_industry=lambda codes,**kw:{code:{'sw_l2':{'industry_name':'行业'}}},get_trade_days=lambda **kw:[kw['end_date']],
        get_price=lambda codes,**kw:pd.DataFrame({'code':codes,'close':[2]*len(codes)}))
    target=create_engine('sqlite:///'+str(tmp_path/'target.sqlite'))
    @contextmanager
    def scope(Base,config):
        Base.metadata.create_all(target)
        yield sessionmaker(bind=target)
    monkeypatch.setattr(mod,'database_scope',scope)
    monkeypatch.setattr(mod,'china_now',lambda:dt.datetime(2026,10,21,15))
    mod.STRATEGY_CONFIG['个股机构持仓']['history_years']=1
    mod.STRATEGY_CONFIG['个股机构持仓']['stock_batch_size']=batch_size
    mod.STRATEGY_CONFIG['个股机构持仓']['page_size']=2
    n=mod._ownership_strategy(api,mod.DB_CONFIG)
    assert len(n.run())>0
    with sessionmaker(bind=target)() as session:
        report=session.query(n.OwnershipReport).filter_by(report_date=dt.date(2026,9,30)).one()
        assert report.payload['categories']['public_fund']['holder_count']==1
        assert report.payload['social_entry']['first_appearance']
        assert session.query(n.OwnershipHolders).count()==1
        stamp=report.updated_at
    assert n.run().empty
    source_session.query(Floats).filter_by(id=11).update({'share_number':150});source_session.commit()
    assert len(n.run())==1
    with sessionmaker(bind=target)() as session:
        report=session.query(n.OwnershipReport).filter_by(report_date=dt.date(2026,9,30)).one()
        assert report.payload['social_entry']['new_shares']==150
        assert session.query(n.OwnershipReport).count()==2
    if batch_size == 1:
        source_session.query(Floats).filter_by(id=11).update({'share_number':999});source_session.commit()
        fail_late[0] = True
        with pytest.raises(RuntimeError,match='late batch failure'):
            n.run()
        with sessionmaker(bind=target)() as session:
            report=session.query(n.OwnershipReport).filter_by(report_date=dt.date(2026,9,30)).one()
            assert report.payload['social_entry']['new_shares']==150  # flushed first batch was rolled back
        fail_late[0] = False
        assert len(n.run())==1
        assert n.run().empty
    source_session.close();source.dispose();target.dispose()


def test_source_pages_are_lazy_and_memory_is_bounded(mod):
    import tracemalloc
    base=declarative_base()
    class Rows(base):
        __tablename__='memory_rows'
        id=Column(Integer,primary_key=True)
        payload=Column(String)
    session=sessionmaker()()
    calls=[]
    count=20000
    def run_query(q):
        params=q.statement.compile().params
        cursor=params['id_1'];size=params['param_1']
        calls.append(cursor)
        return pd.DataFrame([{'id':i,'payload':str(i)+'x'*4096}
                             for i in range(cursor+1,min(cursor+size,count)+1)],columns=['id','payload'])
    mod.STRATEGY_CONFIG['个股机构持仓']['page_size']=128
    ns=mod._ownership_strategy({'finance':SimpleNamespace(run_query=run_query),'query':session.query},mod.DB_CONFIG)
    stream=ns.pages(Rows)
    assert not calls
    tracemalloc.start()
    try:
        assert next(stream)['id']==1 and len(calls)==1
        seen=1
        for row in stream:
            seen+=1
        _,peak=tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop();session.close()
    assert seen==count and len(calls)>100
    # Input payloads alone exceed 78 MiB; iterator must not retain all prior pages.
    assert peak < 8*1024*1024


@pytest.mark.parametrize('value',[0,-1,True,301])
def test_invalid_stock_batch_size(mod,value):
    mod.STRATEGY_CONFIG['个股机构持仓']['stock_batch_size']=value
    ns=mod._ownership_strategy({'finance':None,'query':None},mod.DB_CONFIG)
    with pytest.raises(ValueError,match='stock_batch_size'):
        ns.run()
