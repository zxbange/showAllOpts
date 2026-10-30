import ast
import datetime as dt
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Column, Date, Integer, Numeric, String, create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker, Query
from app.models.etf_heat import Heat as WebHeat, Pool as WebPool

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT/'collectors'/'ETF动量热度_数据库版.py'

@pytest.fixture
def mod(monkeypatch):
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    spec=importlib.util.spec_from_file_location('heat_collector', SOURCE)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.PROGRESS_ENABLED=False
    return module

@pytest.fixture
def ns(mod):
    return mod._etf_heat_strategy({}, mod.DB_CONFIG)


def prices(count=280, falling=False):
    close=np.exp(np.linspace(0, .12 if not falling else -.12, count))
    return pd.DataFrame(dict(open=close*.999, close=close, high=close*1.01, low=close*.99,
                             volume=np.full(count,100.), paused=np.zeros(count)),
                        index=pd.bdate_range(end='2026-10-20', periods=count).date)


def test_schema_shared_and_atomic_insert_only(ns, tmp_path):
    for collector, web in [(ns.Heat,WebHeat),(ns.Pool,WebPool)]:
        assert [(c.name,str(c.type),c.primary_key) for c in collector.__table__.columns]==[(c.name,str(c.type),c.primary_key) for c in web.__table__.columns]
    engine=create_engine('sqlite:///'+str(tmp_path/'heat.sqlite'));ns.Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine)
    row=dict(trade_date=dt.date(2026,10,20),code='A',name='A',updated_at=dt.datetime(2026,10,20),close=2,score=3,annual_return=4,r2=.75,reference_pass=None,details={'status':'ok'})
    assert len(ns.save_rows(Session,[row]))==1
    assert ns.save_rows(Session,[dict(row,close=999)])==[]
    with Session() as s: assert s.query(ns.Heat).one().close==row['close']
    with pytest.raises(ValueError):ns.save_rows(Session,[row,row])
    engine.dispose()


def test_same_factory_embedded_and_python36_syntax():
    def factory(path):
        tree=ast.parse(path.read_text(encoding='utf-8'),feature_version=(3,6))
        return next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_etf_heat_strategy')
    assert ast.dump(factory(SOURCE)) == ast.dump(factory(ROOT/'collectors'/'策略批量采集_数据库版.py'))


@pytest.mark.parametrize('legacy_empty', [False, True])
@pytest.mark.parametrize('retention', [180, 3])
def test_full_run_repeat_and_partial_latest_preserve_head(mod, monkeypatch, tmp_path, legacy_empty, retention):
    Base=declarative_base()
    class Target(Base):
        __tablename__='target'
        id=Column(Integer,primary_key=True);code=Column(String);pub_date=Column(Date);start_date=Column(Date);end_date=Column(Date)
        traced_index_name=Column(String);traced_index_code=Column(String)
    class Shares(Base):
        __tablename__='shares'
        id=Column(Integer,primary_key=True);code=Column(String);date=Column(Date);shares=Column(Numeric)
    source=create_engine('sqlite://');Base.metadata.create_all(source);Source=sessionmaker(bind=source)
    with Source() as s:
        s.add(Target(id=1,code='518880.XSHG',pub_date=dt.date(2020,1,1),start_date=dt.date(2020,1,1),traced_index_name='黄金现货',traced_index_code='AU'))
        s.add_all(Shares(code='518880.XSHG',date=d.date(),shares=400000000) for d in pd.bdate_range('2026-09-01','2026-10-21'));s.commit()
    def query_data(q):
        with Source() as s:
            rows=s.execute(q.statement).mappings().all()
            # Query mapped classes executes ORM entity objects; flatten those.
            if rows and len(rows[0])==1:
                values=[next(iter(r.values())) for r in rows]
                return pd.DataFrame([{c.name:getattr(v,c.name) for c in v.__table__.columns} for v in values])
            return pd.DataFrame(rows)
    finance=SimpleNamespace(FUND_INVEST_TARGET=Target,FUND_SHARE_DAILY=Shares,run_query=query_data)
    now=[dt.datetime(2026,10,20,17)]; price_calls=[]; partial=[False]
    monkeypatch.setattr(mod,'china_now',lambda:now[0])
    mod.STRATEGY_CONFIG['ETF动量热度'].update(initial_trade_days=5 if retention==3 else 3, retention_trade_days=retention)
    database=create_engine('sqlite:///'+str(tmp_path/'output.sqlite'))
    monkeypatch.setattr(mod,'connect_database',lambda config:database)
    def get_price(code,**kwargs):
        price_calls.append(code)
        frame=prices(500);frame.index=pd.bdate_range(end='2026-10-21',periods=500).date
        frame=frame.loc[(frame.index >= kwargs['start_date']) & (frame.index <= kwargs['end_date'])]
        return frame.iloc[:-1] if partial[0] else frame
    apis=dict(finance=finance, query=lambda *entities:Query(entities),
              get_trade_days=lambda end_date,count=None,start_date=None:pd.bdate_range(start=start_date,end=end_date,periods=count).date,
              get_all_securities=lambda types,date:pd.DataFrame({'display_name':['黄金ETF']},index=['518880.XSHG']) if types==['etf'] else pd.DataFrame(columns=['display_name']),
              get_extras=lambda info,batch,**kw:pd.DataFrame({c:10. for c in batch},index=pd.bdate_range(kw['start_date'],kw['end_date'])),
              get_price=get_price)
    strategy=mod._etf_heat_strategy(apis,mod.DB_CONFIG)
    strategy.Base.metadata.create_all(database)
    with sessionmaker(bind=database)() as s:
        # Expired NULLs must be deleted, never reintroduced by upgrade repair.
        s.add_all(strategy.Heat(trade_date=dt.date(2025,1,1),code=code,name=code,score=None,
                  details={},updated_at=dt.datetime(2025,1,1)) for code in ['518880.XSHG','RETIRED'])
        s.add(strategy.Pool(id=1,as_of_date=dt.date(2026,10,20),rule_version='etf_close_momentum_v2' if legacy_empty else 'etf_raw_market_v1',
                            members=[{'code':'518880.XSHG'}],updated_at=dt.datetime(2026,10,19)))
        if legacy_empty:
            s.add_all(strategy.Heat(trade_date=d.date(),code='518880.XSHG',name='黄金ETF',close=1,
                      score=None,details={'status':'missing'},updated_at=dt.datetime(2026,10,19))
                      for d in pd.bdate_range(end='2026-10-20',periods=3))
        s.commit()
    # Reproduce pasted Notebook globals from jqdata/numpy wildcard imports.
    monkeypatch.setattr(mod, 'any', np.any, raising=False)
    monkeypatch.setattr(mod, 'sum', np.sum, raising=False)
    assert len(strategy.run())==3
    from sqlalchemy import inspect
    assert set(inspect(database).get_table_names())=={'etf_momentum_heat','etf_momentum_pool','result_sync_head','result_sync_change'}
    with sessionmaker(bind=database)() as s:
        assert s.query(strategy.Pool).one().rule_version=='etf_close_momentum_v3'
        assert all(r.score is not None for r in s.query(strategy.Heat))
    assert len(strategy.run())==0
    assert len(price_calls)==2
    with sessionmaker(bind=database)() as s:
        before=s.query(strategy.Pool).one().updated_at
        assert s.query(strategy.Heat).count()==3
    now[0]=dt.datetime(2026,10,21,17);partial[0]=True
    with pytest.raises(ValueError,match='行情缺失'): strategy.run()
    with sessionmaker(bind=database)() as s:
        assert s.query(strategy.Pool).one().as_of_date==dt.date(2026,10,20)
        assert s.query(strategy.Pool).one().updated_at==before
    partial[0]=False
    assert len(strategy.run())==1
    with sessionmaker(bind=database)() as s:
        assert s.query(strategy.Heat).count()==min(4,retention)
        assert min(r.trade_date for r in s.query(strategy.Heat)) >= pd.bdate_range(end=now[0].date(),periods=retention).date[0]
    with sessionmaker(bind=database)() as s:
        s.query(strategy.Heat).filter(strategy.Heat.trade_date==dt.date(2026,10,20)).update({'score':None})
        s.commit()
    assert len(strategy.run())==0  # v3 normally appends only.
    assert len(strategy.run(repair_missing=True))==1
    assert len(strategy.run(repair_missing=True))==0
    source.dispose();database.dispose()


def test_selector_logic_is_shared_with_research_script():
    research=ast.parse((ROOT/'collectors'/'ETF分类去重_研究版.py').read_text(encoding='utf-8'))
    collector=ast.parse(SOURCE.read_text(encoding='utf-8'))
    factory=next(n for n in collector.body if isinstance(n,ast.FunctionDef) and n.name=='_etf_heat_strategy')
    originals={n.name:n for n in research.body if isinstance(n,ast.FunctionDef)}
    for name in ['classify','permitted','commodity_direction','target_catalog','load_sizes','load_lof_reports','select_largest','lof_catalog']:
        actual=next(n for n in factory.body if isinstance(n,ast.FunctionDef) and n.name==name)
        assert ast.dump(actual)==ast.dump(originals[name]), name


@pytest.mark.parametrize('direction', [1, -1, 0])
def test_collector_score_matches_weighted_regression(ns, direction):
    values=np.exp(np.linspace(0,.1*direction,26))*(1+.003*np.sin(np.arange(26)))
    y=np.log(values); x=np.arange(26); w=np.linspace(1,2,26)
    slope, intercept=np.polyfit(x,y,1,w=w)
    expected=np.expm1(slope*250)*(1-np.sum(w*(y-slope*x-intercept)**2)/np.sum(w*(y-y.mean())**2))
    assert float(ns.metrics(list(values))['score'])==pytest.approx(expected,abs=1e-9)
    assert ns.metrics(list(values))['score']==ns.metrics(list(values*100))['score']


def test_collector_invalid_and_flat_prices(ns):
    assert ns.metrics([1]*26)['score']==0
    assert ns.metrics([1]*25)['score'] is None
    assert ns.metrics([1]*25+[None])['score'] is None
    assert ns.metrics([1]*26,True)['score'] is None
    assert ns.metrics([1]*25+[.9])['score']<0


def test_numpy_global_functions_do_not_change_metrics(mod, monkeypatch):
    clean=mod._etf_heat_strategy({},mod.DB_CONFIG)
    values=[1+i*.01 for i in range(26)]
    expected=clean.metrics(values)
    monkeypatch.setattr(mod,'any',np.any,raising=False)
    monkeypatch.setattr(mod,'sum',np.sum,raising=False)
    assert clean.metrics(values)==expected and expected['score'] is not None


def test_null_repair_never_overwrites_valid_or_repeats_writes(ns):
    engine=create_engine('sqlite://');ns.Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine)
    stamp=dt.datetime(2026,10,20)
    original=dict(trade_date=stamp.date(),code='A',name='A',score=None,close=2,
                  details={'status':'missing'},updated_at=stamp)
    ns.save_rows(Session,[original,dict(original,code='B',score=3)])
    fixed=dict(original,score=9,annual_return=10,r2=.9,details={'status':'ok'},updated_at=dt.datetime(2026,10,21))
    assert ns.save_rows(Session,[fixed])==[]
    assert len(ns.save_rows(Session,[fixed,dict(fixed,code='B')],repair_missing=True))==1
    with Session() as session:
        assert session.get(ns.Heat,(stamp.date(),'A')).score==9
        assert session.get(ns.Heat,(stamp.date(),'B')).score==3
        assert session.get(ns.Heat,(stamp.date(),'A')).updated_at==fixed['updated_at']
    writes=[]
    def observe(c,cur,sql,*a):
        if sql.lstrip().split()[0].upper() in ('INSERT','UPDATE','DELETE'):writes.append(sql)
    event.listen(engine,'before_cursor_execute',observe)
    assert ns.save_rows(Session,[dict(original,code='A')],repair_missing=True)==[]
    assert ns.save_rows(Session,[fixed,dict(fixed,code='B')],repair_missing=True)==[]
    assert not writes
    engine.dispose()


@pytest.mark.parametrize('case', ['none', 'short', 'duplicate', 'reversed', 'future'])
def test_retention_bad_calendar_never_opens_database(mod, monkeypatch, case):
    today=dt.date(2026,10,20)
    days=list(pd.bdate_range(end=today,periods=180).date)
    variants={'none':None,'short':days[1:],'duplicate':days[:-1]+[days[-2]],
              'reversed':days[::-1],'future':days[1:]+[today+dt.timedelta(days=1)]}
    monkeypatch.setattr(mod,'china_now',lambda:dt.datetime(2026,10,20,17))
    def forbidden(*args):raise AssertionError('Invalid calendar must not open database')
    monkeypatch.setattr(mod,'connect_database',forbidden)
    strategy=mod._etf_heat_strategy({'get_trade_days':lambda **kw:variants[case]},mod.DB_CONFIG)
    with pytest.raises(ValueError,match='未清理数据库'):strategy.run()


@pytest.mark.parametrize('historical', [True, False])
def test_retention_cleanup_precedes_selection_and_prevents_expired_backfill(mod,monkeypatch,tmp_path,historical):
    now=dt.datetime(2026,10,20,17)
    monkeypatch.setattr(mod,'china_now',lambda:now)
    calendar=lambda end_date,count=None,start_date=None:pd.bdate_range(start=start_date,end=end_date,periods=count).date
    cutoff=calendar(now.date(),180)[0]
    engine=create_engine('sqlite:///'+str(tmp_path/'retention.sqlite'))
    monkeypatch.setattr(mod,'connect_database',lambda config:engine)
    calls=[]
    def unavailable(**kwargs):
        calls.append('selection')
        raise RuntimeError('selection unavailable')
    strategy=mod._etf_heat_strategy({'get_trade_days':calendar,'get_all_securities':unavailable},mod.DB_CONFIG)
    strategy.Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine)
    with Session() as s:
        for day in [cutoff-dt.timedelta(days=1),cutoff,now.date()]:
            for code in ['CURRENT','RETIRED']:
                s.add(strategy.Heat(trade_date=day,code=code,name=code,score=1,details={},updated_at=now))
        s.commit()
    if historical:
        assert strategy.run(end_date=cutoff-dt.timedelta(days=1),repair_missing=True).empty
        assert not calls
    else:
        with pytest.raises(RuntimeError,match='selection unavailable'):strategy.run()
        assert calls==['selection']
    with Session() as s:
        assert {(r.trade_date,r.code) for r in s.query(strategy.Heat)}=={
            (day,code) for day in [cutoff,now.date()] for code in ['CURRENT','RETIRED']}
    assert strategy.cleanup_old_data(Session,cutoff)==0
    engine.dispose()


def test_retention_weekend_keeps_same_boundary(mod):
    strategy=mod._etf_heat_strategy({'get_trade_days':lambda end_date,count:pd.bdate_range(end=end_date,periods=count).date},mod.DB_CONFIG)
    assert strategy.get_retention_start(dt.date(2026,10,23))==strategy.get_retention_start(dt.date(2026,10,25))
