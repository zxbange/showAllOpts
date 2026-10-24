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
@pytest.mark.parametrize('missing_price', [False, True])
def test_full_collection_repeat_correction_future_filter_and_social(mod,monkeypatch,tmp_path,capsys,batch_size,missing_price):
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
    source_session.add(Main(id=3,main_code='UNRELATED',statistics_main_code='UNRELATED'))
    source_session.add(Fund(id=100,code='UNRELATED',symbol='999999',period_end=dt.date(2026,9,30),
                            pub_date=dt.date(2026,10,20),report_type_id=403003,report_type='第三季度',shares=1,market_cap=1))
    source_session.commit()
    fail_late = [False]
    fail_holder = [False]
    source_calls = []
    def run_query(q):
        params = q.statement.compile().params
        source_calls.append(dict(params, _tables=tuple(t.name for t in q.statement.get_final_froms())))
        if fail_holder[0] and source_calls[-1]['_tables'] == ('holder',) and 'code_1' in params:
            raise RuntimeError('synthetic holder failure')
        if fail_late[0] and params.get('period_end_1') == dt.date(2026,9,30) and params.get('symbol_1') == ['600002',other]:
            raise RuntimeError('synthetic late batch failure')
        with source.connect() as c:
            result=c.execute(q.statement)
            return pd.DataFrame(result.fetchall(),columns=result.keys())
    finance=SimpleNamespace(FUND_MAIN_INFO=Main,FUND_PORTFOLIO_STOCK=Fund,STK_SHAREHOLDER_FLOATING_TOP10=Floats,STK_HOLDER_NUM=Holder,run_query=run_query)
    def get_price(codes, **kwargs):
        if any(c not in (code, other) for c in codes):
            raise ValueError('找不到标的K0470.XSHG')
        return pd.DataFrame({'code': codes, 'close': [2]*len(codes)}) if not missing_price else pd.DataFrame(columns=['code','close'])
    api=dict(finance=finance,query=source_session.query,get_all_securities=lambda **kw:pd.DataFrame({'display_name':['示例','示例二']},index=[code,other]),
        get_industry=lambda codes,**kw:{code:{'sw_l2':{'industry_name':'行业'}}},get_trade_days=lambda **kw:[kw['end_date']],
        get_price=get_price)
    target=create_engine('sqlite:///'+str(tmp_path/'target.sqlite'))
    scopes = []
    @contextmanager
    def scope(Base,config):
        scopes.append('open')
        Base.metadata.create_all(target)
        try:
            yield sessionmaker(bind=target)
        finally:
            scopes.append('close')
    monkeypatch.setattr(mod,'database_scope',scope)
    monkeypatch.setattr(mod,'china_now',lambda:dt.datetime(2026,10,21,15))
    mod.STRATEGY_CONFIG['个股机构持仓']['history_years']=1
    mod.STRATEGY_CONFIG['个股机构持仓']['stock_batch_size']=batch_size
    mod.STRATEGY_CONFIG['个股机构持仓']['page_size']=2
    n=mod._ownership_strategy(api,mod.DB_CONFIG)
    def run(**kwargs):
        before = len(scopes)
        try:
            return n.run(**kwargs)
        finally:
            assert scopes[before:] == ['open', 'close']
    assert len(run())>0
    with sessionmaker(bind=target)() as session:
        report=session.query(n.OwnershipReport).filter_by(report_date=dt.date(2026,9,30)).one()
        assert report.payload['categories']['public_fund']['holder_count']==1
        assert report.payload['social_entry']['first_appearance']
        assert report.payload['social_entry']['new_shares'] == 100
        assert report.payload['social_entry']['new_market_value'] == (None if missing_price else 200)
        assert session.query(n.OwnershipHolders).count()==1
        stamp=report.updated_at
    with sessionmaker(bind=target)() as session:
        revision_before=session.query(n.SyncHead).one().revision
    assert run().empty
    with sessionmaker(bind=target)() as session:
        assert session.query(n.SyncHead).one().revision==revision_before
    monkeypatch.setattr(mod,'china_now',lambda:dt.datetime(2026,10,22,15))
    source_calls.clear()
    capsys.readouterr()
    assert len(run()) == 1  # Previously future-dated disclosure now becomes visible.
    output = capsys.readouterr().out
    assert '触发来源 股东户数' in output
    assert '无持仓新增披露，跳过报告期、持仓明细和社保比较' in output
    assert '股东户数更新范围 1 只股票' in output
    # A holder-count disclosure must not read fund metadata or quarterly holdings.
    assert source_calls[6:] and all(q['_tables'] == ('holder',) for q in source_calls[6:])
    with sessionmaker(bind=target)() as session:
        report = session.query(n.OwnershipReport).filter_by(code=code,report_date=dt.date(2026,9,30)).one()
        assert report.updated_at == stamp
        assert session.query(n.Checkpoint).filter_by(key='report:2026-09-30').one().as_of == dt.date(2026,10,21)
        assert session.query(n.DisclosureProbe).one().as_of == dt.date(2026,10,22)

    monkeypatch.setattr(mod,'china_now',lambda:dt.datetime(2026,10,23,15))
    assert run().empty  # Another day without disclosures still skips.
    source_session.query(Floats).filter_by(id=11).update({'share_number':150});source_session.commit()
    assert run().empty  # In-place corrections alone do not trigger automatic review.
    assert len(run(full_refresh=True))==1
    with sessionmaker(bind=target)() as session:
        report=session.query(n.OwnershipReport).filter_by(report_date=dt.date(2026,9,30)).one()
        assert report.payload['social_entry']['new_shares']==150
        assert session.query(n.OwnershipReport).count()==2
    if batch_size == 1:
        source_session.query(Floats).filter_by(id=11).update({'share_number':999});source_session.commit()
        fail_late[0] = True
        with pytest.raises(RuntimeError,match='late batch failure'):
            run(full_refresh=True)
        with sessionmaker(bind=target)() as session:
            report=session.query(n.OwnershipReport).filter_by(report_date=dt.date(2026,9,30)).one()
            assert report.payload['social_entry']['new_shares']==150  # flushed first batch was rolled back
        fail_late[0] = False
        assert len(run(full_refresh=True))==1
        assert run().empty
    # A new A-class report drops the old stock and adds another: both need review.
    source_session.add(Fund(id=101,code='A',symbol='600002',period_end=dt.date(2026,9,30),
                           pub_date=dt.date(2026,10,23),report_type_id=403003,report_type='第三季度',shares=75,market_cap=150))
    source_session.commit()
    source_calls.clear()
    assert len(run()) >= 2
    assert all(q['_tables'] != ('holder',) for q in source_calls[6:])
    # Fund refresh still finds removed constituents and remains stock/fund-scoped.
    quarterly_pages = [q for q in source_calls[6:] if q.get('period_end_1') in
                       [dt.date(2025,12,31),dt.date(2026,3,31),dt.date(2026,6,30),dt.date(2026,9,30)]
                       and 'id_1' in q]
    assert quarterly_pages
    assert all('symbol_1' in q or 'code_1' in q for q in quarterly_pages)
    assert all('UNRELATED' not in q.get('code_1', []) for q in quarterly_pages)
    with sessionmaker(bind=target)() as session:
        old = session.query(n.OwnershipReport).filter_by(code=code,report_date=dt.date(2026,9,30)).one()
        new = session.query(n.OwnershipReport).filter_by(code=other,report_date=dt.date(2026,9,30)).one()
        assert old.payload['categories']['public_fund'] is None
        assert new.payload['categories']['public_fund']['shares'] == 75
    assert run().empty
    # Float-holder disclosure alone updates holdings/social results, not holder counts.
    def add_float_version(first_id, shares):
        for rank in range(1,11):
            old = source_session.query(Floats).filter_by(id=10+rank).one()
            values = {column.name: getattr(old, column.name) for column in Floats.__table__.columns}
            values.update(id=first_id+rank, pub_date=dt.date(2026,10,23), share_number=shares if rank == 1 else 100)
            source_session.add(Floats(**values))
    add_float_version(200, 300)
    source_session.commit()
    source_calls.clear()
    assert len(run()) == 1
    assert all(q['_tables'] != ('holder',) for q in source_calls[6:])
    with sessionmaker(bind=target)() as session:
        assert session.query(n.OwnershipReport).filter_by(code=code,report_date=dt.date(2026,9,30)).one().payload['social_entry']['new_shares'] == 300
    # An unrelated disclosure identifier must never enter the stock/price scope.
    # Keep a valid disclosure in the same run to ensure it is still updated.
    invalid = ['K0470.XSHG', '600001.XSHE']
    for i, bad_code in enumerate(invalid, 1000):
        source_session.add(Floats(id=i,code=bad_code,end_date=dt.date(2026,9,30),pub_date=dt.date(2026,10,23),
            change_reason_id=306019,sharesnature_id=308007,shareholder_rank=1,shareholder_id=77,
            shareholder_name='其他主体',shareholder_class_id=307099,share_number=100,share_ratio=1))
        source_session.add(Holder(id=i,code=bad_code,end_date=dt.date(2026,9,30),pub_date=dt.date(2026,10,23),share_holders=100))
    source_session.add(Holder(id=3,code=code,end_date=dt.date(2026,9,30),pub_date=dt.date(2026,10,23),share_holders=65,a_share_holders=65))
    source_session.commit()
    source_calls.clear()
    assert len(run()) == 1
    with sessionmaker(bind=target)() as session:
        assert session.query(n.OwnershipHolders).filter_by(code=code,end_date=dt.date(2026,9,30)).one().payload['share_holders'] == 65
        assert session.query(n.OwnershipReport).filter(n.OwnershipReport.code.in_(invalid)).count() == 0
        assert session.query(n.OwnershipHolders).filter(n.OwnershipHolders.code.in_(invalid)).count() == 0
    scopes_read = [q['code_1'] for q in source_calls if isinstance(q.get('code_1'), list)]
    assert scopes_read and all(not set(values).intersection(invalid) for values in scopes_read)
    assert run().empty
    # An unknown-only disclosure advances the probe without scanning four quarters.
    source_session.add(Holder(id=1002,code=invalid[0],end_date=dt.date(2026,9,30),pub_date=dt.date(2026,10,23),share_holders=101))
    source_session.commit()
    source_calls.clear()
    assert run().empty
    assert not any('period_end_1' in q or 'symbol_1' in q for q in source_calls[6:])
    source_calls.clear()
    assert run().empty and len(source_calls) == 6
    # A newly listed stock with only a holder disclosure still gets stock metadata.
    original_universe = api['get_all_securities']
    new_code = '600003.XSHG'
    api['get_all_securities'] = lambda **kw: pd.concat([original_universe(**kw),
        pd.DataFrame({'display_name':['新股']},index=[new_code])])
    source_session.add(Holder(id=1003,code=new_code,end_date=dt.date(2026,9,30),pub_date=dt.date(2026,10,23),share_holders=70))
    source_session.commit()
    source_calls.clear()
    assert len(run()) == 1
    assert all(q['_tables'] == ('holder',) for q in source_calls[6:])
    with sessionmaker(bind=target)() as session:
        assert session.query(n.OwnershipStock).filter_by(code=new_code).one().name == '新股'
        assert session.query(n.OwnershipHolders).filter_by(code=new_code).one().payload['share_holders'] == 70
        assert session.query(n.OwnershipReport).filter_by(code=new_code).count() == 0
    # Existing history remains eligible even after the stock leaves today's list.
    original_securities = api['get_all_securities']
    api['get_all_securities'] = lambda **kw: (pd.DataFrame({'display_name':['示例']}, index=[code])
        if kw['date'] == dt.date(2026,10,23) else original_securities(**kw))
    source_session.add(Holder(id=1004,code=other,end_date=dt.date(2026,9,30),pub_date=dt.date(2026,10,23),share_holders=88))
    source_session.commit()
    assert len(run()) == 1
    with sessionmaker(bind=target)() as session:
        assert session.query(n.OwnershipStock).filter_by(code=other).count() == 1
        assert session.query(n.OwnershipHolders).filter_by(code=other).one().payload['share_holders'] == 88
    # Mixed changes: do not advance the global probe if holder collection fails
    # after the holdings transactions committed; retry must finish both sources.
    with sessionmaker(bind=target)() as session:
        old_probe = session.query(n.DisclosureProbe).one().payload
    add_float_version(2000, 350)
    source_session.add(Holder(id=2000,code=code,end_date=dt.date(2026,9,30),pub_date=dt.date(2026,10,23),share_holders=60))
    source_session.commit()
    fail_holder[0] = True
    with pytest.raises(RuntimeError, match='synthetic holder failure'):
        run()
    with sessionmaker(bind=target)() as session:
        assert session.query(n.DisclosureProbe).one().payload == old_probe
        assert session.query(n.OwnershipReport).filter_by(code=code,report_date=dt.date(2026,9,30)).one().payload['social_entry']['new_shares'] == 350
        assert session.query(n.OwnershipHolders).filter_by(code=code,end_date=dt.date(2026,9,30)).one().payload['share_holders'] == 65
    fail_holder[0] = False
    assert len(run()) == 1
    with sessionmaker(bind=target)() as session:
        assert session.query(n.DisclosureProbe).one().payload != old_probe
        assert session.query(n.OwnershipHolders).filter_by(code=code,end_date=dt.date(2026,9,30)).one().payload['share_holders'] == 60
    source_calls.clear()
    assert run().empty and len(source_calls) == 6
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


def test_grouped_index_returns_one_row_per_document_across_capped_pages(mod):
    base = declarative_base()
    class Holdings(base):
        __tablename__ = 'holdings'
        id = Column(Integer, primary_key=True)
        code = Column(String)
        pub_date = Column(Date)
        report_type_id = Column(Integer)
        report_type = Column(String)
        symbol = Column(String)
    engine = create_engine('sqlite://')
    base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    rows = []
    for i in range(12000):
        rows.append(dict(id=i+1, code='F'+str(i%6), pub_date=dt.date(2026,10,21),
                         report_type_id=1, report_type=None, symbol=str(i)))
    session.execute(Holdings.__table__.insert(), rows)
    session.commit()
    returned, sqls = [], []
    def run_query(q):
        # Simulate a source cap smaller than the requested page size.
        q = q.limit(2)
        sqls.append(str(q.statement))
        with engine.connect() as connection:
            result = connection.execute(q.statement)
            values = result.fetchall()
            returned.append(len(values))
            return pd.DataFrame(values, columns=result.keys())
    ns = mod._ownership_strategy({'finance': SimpleNamespace(run_query=run_query), 'query': session.query}, mod.DB_CONFIG)
    result = list(ns.grouped_pages(Holdings, [Holdings.code, Holdings.pub_date,
                        Holdings.report_type_id, Holdings.report_type], Holdings.code != 'F5'))
    assert len(result) == 5 and sum(returned) == 5  # 10,000 scoped detail rows -> five versions.
    assert [r['id'] for r in result] == [1,2,3,4,5]
    assert returned == [2,2,1,0]
    assert all('GROUP BY' in sql and 'HAVING min(' in sql and 'WHERE holdings.code' in sql for sql in sqls)
    session.close()
    engine.dispose()


def test_prices_use_report_date_universe_and_keep_missing_values(mod):
    period = dt.date(2025, 12, 31)
    retired, current, future = '600010.XSHG', '000001.XSHE', '600020.XSHG'
    universe_calls, price_calls = [], []
    def securities(**kwargs):
        universe_calls.append(kwargs)
        assert kwargs == {'types': ['stock'], 'date': period}
        return pd.DataFrame(index=[retired, current])
    def get_price(codes, **kwargs):
        price_calls.append((codes, kwargs))
        assert set(codes) <= {retired, current}
        assert kwargs['start_date'] == kwargs['end_date'] == period
        assert kwargs['fq'] is None
        return pd.DataFrame({'code': codes, 'close': [2.0]*len(codes)})
    n = mod._ownership_strategy(dict(get_all_securities=securities, get_price=get_price,
        get_trade_days=lambda **kwargs: [period]), mod.DB_CONFIG)
    assert n.prices([], period) == {} and not universe_calls
    assert n.prices([retired, future, 'K0470.XSHG'], period) == {retired: 2.0}
    assert n.prices([current], period) == {current: 2.0}
    assert len(universe_calls) == 1  # Once per report date, not once per stock batch.
    assert n.prices([future, 'K0470.XSHG'], period) == {} and len(price_calls) == 2


@pytest.mark.parametrize('failure', [None, pd.DataFrame(), RuntimeError('network unavailable')])
def test_prices_do_not_swallow_universe_failure(mod, failure):
    def securities(**kwargs):
        if isinstance(failure, Exception):
            raise failure
        return failure
    n = mod._ownership_strategy(dict(get_all_securities=securities,
        get_trade_days=lambda **kwargs: [dt.date(2026,9,30)],
        get_price=lambda *args,**kwargs: pytest.fail('must validate universe first')), mod.DB_CONFIG)
    with pytest.raises((ValueError, RuntimeError), match='股票清单|network unavailable'):
        n.prices(['600001.XSHG'], dt.date(2026,9,30))


def test_prices_preserve_real_api_errors(mod):
    def get_price(*args, **kwargs):
        raise RuntimeError('network unavailable')
    n = mod._ownership_strategy(dict(get_all_securities=lambda **kwargs: pd.DataFrame(index=['600001.XSHG']),
        get_trade_days=lambda **kwargs: [dt.date(2026,9,30)], get_price=get_price), mod.DB_CONFIG)
    with pytest.raises(RuntimeError, match='network unavailable'):
        n.prices(['600001.XSHG'], dt.date(2026,9,30))
