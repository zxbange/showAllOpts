"""PIT recovery must not mutate the mixed live chart."""
import datetime as dt
from types import SimpleNamespace
import pandas as pd
import pytest
from sqlalchemy import Column, Date, Integer, Numeric, String, create_engine, inspect
from sqlalchemy.orm import declarative_base, sessionmaker, Query
from test_etf_heat_collector import mod

@pytest.fixture
def history(mod, monkeypatch, tmp_path):
    Base = declarative_base()
    class Target(Base):
        __tablename__ = 'targets'
        id = Column(Integer, primary_key=True)
        code = Column(String); pub_date = Column(Date); start_date = Column(Date); end_date = Column(Date)
        traced_index_name = Column(String); traced_index_code = Column(String)
    class Shares(Base):
        __tablename__ = 'shares'
        id = Column(Integer, primary_key=True)
        code = Column(String); date = Column(Date); shares = Column(Numeric)
    source = create_engine('sqlite://'); Base.metadata.create_all(source); Source = sessionmaker(bind=source)
    days = [dt.date(2020, 1, 6), dt.date(2020, 1, 7), dt.date(2020, 1, 8)]
    codes = ['510001.XSHG', '510002.XSHG']
    with Source() as s:
        for code in codes:
            s.add(Target(code=code, pub_date=dt.date(2019,1,1), start_date=dt.date(2019,1,1), traced_index_name='沪深300', traced_index_code='000300'))
            for day in pd.bdate_range('2019-11-01', '2020-01-08').date:
                s.add(Shares(code=code, date=day, shares=400000000 if code == codes[0] else 500000000))
        s.commit()
    def fetch(q):
        with Source() as s:
            rows = s.execute(q.statement).scalars().all()
            return pd.DataFrame([{c.name: getattr(row,c.name) for c in row.__table__.columns} for row in rows])
    seen, broken = [], set()
    def universe(types, date):
        assert types == ['etf'], 'Must not use current LOF classification'
        seen.append(date)
        selected = codes[:1] if date == days[0] else codes
        return pd.DataFrame({'display_name': ['今天改名成债券ETF']*len(selected), 'start_date': [dt.date(2019,1,1)]*len(selected)}, index=selected)
    def prices(code, start_date, end_date, fields, **kwargs):
        dates = pd.bdate_range(start_date, end_date)
        if end_date in broken: dates = dates[:-1]
        return pd.DataFrame({'close': [1+i*.001 for i in range(len(dates))], 'paused': 0}, index=dates)[fields]
    apis = dict(finance=SimpleNamespace(FUND_INVEST_TARGET=Target,FUND_SHARE_DAILY=Shares,run_query=fetch),
                query=lambda *entities: Query(entities), get_all_securities=universe,
                get_trade_days=lambda end_date,count=None,start_date=None: pd.bdate_range(start=start_date,end=end_date,periods=count).date,
                get_all_trade_days=lambda: days,
                get_extras=lambda field,batch,start_date,end_date,**kw: pd.DataFrame({c:10 for c in batch},index=pd.bdate_range(start_date,end_date)), get_price=prices)
    engine = create_engine('sqlite:///'+str(tmp_path/'history.sqlite'))
    monkeypatch.setattr(mod,'connect_database',lambda config:engine)
    monkeypatch.setattr(mod,'china_now',lambda:dt.datetime(2026,10,20,17))
    strategy = mod._etf_heat_strategy(apis,mod.DB_CONFIG)
    return SimpleNamespace(strategy=strategy,engine=engine,Session=sessionmaker(bind=engine),days=days,codes=codes,broken=broken,seen=seen,Source=Source,Target=Target,Shares=Shares,mod=mod)

def run(h, **kw):
    return h.strategy.run(history_mode=True,include_lof=False,start_date=h.days[0],end_date=h.days[-1],**kw)

def test_historical_winners_names_and_isolated_tables(history):
    h=history; result=run(h)
    assert result.status.tolist()==['COMPLETE']*3
    assert h.seen==h.days
    assert set(inspect(h.engine).get_table_names())=={'etf_momentum_heat_etf_history','etf_momentum_pool_etf_history'}
    with h.Session() as s:
        rows=s.query(h.strategy.HistoryHeat).order_by(h.strategy.HistoryHeat.trade_date).all()
        assert [r.code for r in rows]==[h.codes[0],h.codes[1],h.codes[1]]
        assert all(r.name=='沪深300' and r.score is not None for r in rows)
        assert all(r.details['selection_scope']=='etf_only_point_in_time' for r in rows)
        assert s.query(h.strategy.HistoryPool).count()==3
    assert run(h).written.sum()==0

def test_middle_gap_and_later_head_do_not_prevent_resume(history):
    h=history; run(h)
    with h.Session() as s:
        s.query(h.strategy.HistoryHeat).filter_by(trade_date=h.days[1]).delete(); s.commit()
    assert run(h).written.tolist()==[0,1,0]

def test_failed_date_is_atomic_reported_and_retried(history):
    h=history; h.broken.add(h.days[1])
    with pytest.raises(ValueError,match='失败日期') as caught: run(h)
    assert caught.value.report.status.tolist()==['COMPLETE','FAILED','COMPLETE']
    with h.Session() as s:
        assert s.query(h.strategy.HistoryPool).count()==2
        assert s.query(h.strategy.HistoryHeat).count()==2
    h.broken.clear()
    assert run(h).written.tolist()==[0,1,0]

def test_missing_competitor_size_cannot_silently_change_winner(history):
    h=history
    with h.Source() as s: s.query(h.Shares).filter_by(code=h.codes[1]).delete(); s.commit()
    with pytest.raises(ValueError) as caught: run(h)
    assert caught.value.report.status.tolist()==['COMPLETE','FAILED','FAILED']
    assert '规模不完整' in caught.value.report.error.iloc[1]

def test_missing_historical_target_cannot_use_current_name(history):
    h=history
    with h.Source() as s: s.query(h.Target).filter_by(code=h.codes[1]).delete(); s.commit()
    with pytest.raises(ValueError) as caught: run(h)
    assert caught.value.report.status.tolist()==['COMPLETE','FAILED','FAILED']
    assert '历史跟踪标的不唯一或缺失' in caught.value.report.error.iloc[1]

def test_mixed_history_rejected_before_database(history,monkeypatch):
    monkeypatch.setattr(history.mod,'connect_database',lambda config:pytest.fail('Must not connect'))
    with pytest.raises(ValueError,match='历史LOF分类'): history.strategy.run(history_mode=True,start_date='2020-01-01')

def test_platform_earliest_has_no_retention(history):
    result=history.strategy.run(history_mode=True,include_lof=False,from_platform_earliest=True,end_date=history.days[-1])
    assert result.trade_date.tolist()==history.days
    assert result.written.sum()==3

def test_existing_snapshot_change_fails_closed(history):
    h=history; run(h)
    with h.Source() as s: s.query(h.Target).update({'traced_index_name':'中证500'}); s.commit()
    with pytest.raises(ValueError) as caught: run(h)
    assert caught.value.report.status.tolist()==['FAILED']*3
    with h.Session() as s: assert all(r.name=='沪深300' for r in s.query(h.strategy.HistoryHeat))

@pytest.mark.parametrize('kwargs',[dict(start_date='2020-01-01',from_platform_earliest=True),dict(start_date=None),dict(start_date='2099-01-01'),dict(start_date='2020-01-01',from_platform_earliest=1)])
def test_invalid_history_options_do_not_connect(history,monkeypatch,kwargs):
    monkeypatch.setattr(history.mod,'connect_database',lambda config:pytest.fail('Must not connect'))
    with pytest.raises(ValueError): history.strategy.run_history(include_lof=False,**kwargs)
