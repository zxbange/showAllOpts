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
from app.models.etf_heat import Bar as WebBar, Pool as WebPool

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
    for collector, web in [(ns.Bar,WebBar),(ns.Pool,WebPool)]:
        assert [(c.name,str(c.type),c.primary_key) for c in collector.__table__.columns]==[(c.name,str(c.type),c.primary_key) for c in web.__table__.columns]
    engine=create_engine('sqlite:///'+str(tmp_path/'heat.sqlite'));ns.Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine)
    row=dict(trade_date=dt.date(2026,10,20),code='A',name='A',updated_at=dt.datetime(2026,10,20),close=2,adjusted_close=3,paused=0,observation_start=dt.date(2026,10,20))
    assert len(ns.save_rows(Session,[row]))==1
    assert ns.save_rows(Session,[dict(row,close=999)])==[]
    with Session() as s: assert s.query(ns.Bar).one().close==row['close']
    with pytest.raises(ValueError):ns.save_rows(Session,[row,row])
    engine.dispose()


def test_same_factory_embedded_and_python36_syntax():
    def factory(path):
        tree=ast.parse(path.read_text(encoding='utf-8'),feature_version=(3,6))
        return next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_etf_heat_strategy')
    assert ast.dump(factory(SOURCE)) == ast.dump(factory(ROOT/'collectors'/'策略批量采集_数据库版.py'))


def test_full_run_repeat_and_partial_latest_preserve_head(mod, monkeypatch, tmp_path):
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
    mod.STRATEGY_CONFIG['ETF动量热度']['initial_trade_days']=3
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
    assert len(strategy.run())==28
    assert len(strategy.run())==0
    assert len(price_calls)==2
    with sessionmaker(bind=database)() as s:
        before=s.query(strategy.Pool).one().updated_at
        assert s.query(strategy.Bar).count()==28
    now[0]=dt.datetime(2026,10,21,17);partial[0]=True
    with pytest.raises(ValueError,match='行情缺失'): strategy.run()
    with sessionmaker(bind=database)() as s:
        assert s.query(strategy.Pool).one().as_of_date==dt.date(2026,10,20)
        assert s.query(strategy.Pool).one().updated_at==before
    partial[0]=False
    assert len(strategy.run())==1
    with sessionmaker(bind=database)() as s: assert s.query(strategy.Bar).count()==29
    source.dispose();database.dispose()


def test_selector_logic_is_shared_with_research_script():
    research=ast.parse((ROOT/'collectors'/'ETF分类去重_研究版.py').read_text(encoding='utf-8'))
    collector=ast.parse(SOURCE.read_text(encoding='utf-8'))
    factory=next(n for n in collector.body if isinstance(n,ast.FunctionDef) and n.name=='_etf_heat_strategy')
    originals={n.name:n for n in research.body if isinstance(n,ast.FunctionDef)}
    for name in ['classify','permitted','commodity_direction','target_catalog','load_sizes','load_lof_reports','select_largest','lof_catalog']:
        actual=next(n for n in factory.body if isinstance(n,ast.FunctionDef) and n.name==name)
        assert ast.dump(actual)==ast.dump(originals[name]), name
