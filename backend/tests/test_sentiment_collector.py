"""Offline tests with real pandas and SQLite; no JoinQuant or MySQL access."""
import ast
import importlib.util
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[2]
FILES = ['市场情绪跟踪_数据库版.py', '策略批量采集_数据库版.py']


@pytest.fixture(params=FILES)
def collector(request, monkeypatch, tmp_path):
    jq = ModuleType('jqdata')
    monkeypatch.setitem(sys.modules, 'jqdata', jq)
    spec = importlib.util.spec_from_file_location('sentiment_fixture', ROOT/'collectors'/request.param)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine('sqlite:///' + (tmp_path/'result.sqlite').as_posix())
    monkeypatch.setattr(module, 'connect_database', lambda config: engine)
    monkeypatch.setattr(module, 'china_now', lambda: datetime(2026, 10, 23, 18))
    module.PROGRESS_ENABLED = False
    yield module, engine
    engine.dispose()


def days_between(start, end):
    return list(pd.bdate_range(start, end).date)


class Platform:
    def __init__(self):
        self.prices = []
        self.stock_reads = 0
        self.fail_at = None

    def get_trade_days(self, start_date=None, end_date=None, count=None):
        days = days_between(start_date or end_date-timedelta(days=60), end_date)
        return days[-count:] if count else days

    def get_all_securities(self, **kwargs):
        assert kwargs == {'types': ['stock'], 'date': None}
        self.stock_reads += 1
        return pd.DataFrame({'start_date': [date(2000,1,1)], 'end_date': [date(2200,1,1)],
                             'display_name': ['*ST测试']}, index=['000001.XSHE'])

    def get_price(self, **kwargs):
        self.prices.append(kwargs)
        if self.fail_at == len(self.prices):
            raise RuntimeError('fixture interrupted')
        assert kwargs['fq'] == 'pre' and kwargs['skip_paused'] is False
        return pd.DataFrame([{'code': code, 'time': day, 'close': float(day.toordinal()), 'paused': 0}
                             for code in kwargs['security']
                             for day in days_between(kwargs['start_date'], kwargs['end_date'])])

    def apis(self):
        return {key: getattr(self, key) for key in ('get_trade_days','get_all_securities','get_price')}


def test_above_ma20_scope_pause_new_listings_delist_and_equal(collector):
    module, _ = collector
    strategy = module._sentiment_strategy({}, {})
    days = days_between(date(2026,9,1), date(2026,10,23))[-21:]
    codes = ['600001.XSHG','688001.XSHG','300001.XSHE','920001.XBSE','000001.XSHE','000002.XSHE']
    metadata = {code: (date(2000,1,1),date(2200,1,1)) for code in codes}
    metadata[codes[3]] = (days[-5],date(2200,1,1))  # IPO
    metadata[codes[5]] = (date(2000,1,1),days[-2])  # delisted
    frame = pd.DataFrame([dict(code=code,time=day,close=(0.1 if index==2 else 10+i),
                              paused=int(index==4 and day==days[-1]))
                          for index,code in enumerate(codes) for i,day in enumerate(days)])
    counts = strategy.batch_counts(frame, codes, days, metadata)
    assert list(counts[-2]) == [6,5,4]  # later-delisted included historically; flat price not above
    assert list(counts[-1]) == [5,3,2]  # IPO and currently paused excluded, STAR/ChiNext retained
    broken = frame.drop(frame[(frame.code==codes[0]) & (frame.time==days[-3])].index)
    with pytest.raises(ValueError, match='缺失'):
        strategy.batch_counts(broken,codes,days,metadata)
    with pytest.raises(ValueError, match='重复'):
        strategy.batch_counts(pd.concat([frame,frame.iloc[:1]]),codes,days,metadata)


def test_backfill_incremental_gaps_retention_and_no_overwrite(collector):
    module, engine = collector
    platform = Platform()
    strategy = module._sentiment_strategy(platform.apis(), {})
    first = strategy.run(start_date='2026-10-01', end_date='2026-10-22', window_size=4)
    assert len(first) == len(days_between(date(2026,10,1),date(2026,10,22)))
    assert set(first.ratio.astype(float)) == {1.0}
    assert max(len(days_between(p['start_date'],p['end_date'])) for p in platform.prices) <= 23
    assert strategy.run(start_date='2026-10-01',end_date='2026-10-22').empty
    calls = len(platform.prices)
    assert platform.stock_reads == 1
    delta = strategy.run(start_date='2026-10-01')
    assert list(delta.trade_date) == [date(2026,10,23)]
    assert len(platform.prices) == calls+1
    assert len(days_between(platform.prices[-1]['start_date'],platform.prices[-1]['end_date'])) == 20
    Model = strategy.MarketSentiment
    with Session(engine) as session:
        stamp = session.get(Model,date(2026,10,1)).updated_at
        session.delete(session.get(Model,date(2026,10,6)))
        session.add(Model(trade_date=date(2023,10,22),ratio=0.5,above_count=1,eligible_count=2,
                          universe_count=2,updated_at=datetime(2023,10,22,18)))
        session.commit()
    gap = strategy.run(start_date='2026-10-01')
    assert list(gap.trade_date) == [date(2026,10,6)]
    with Session(engine) as session:
        assert session.get(Model,date(2023,10,22)) is None
        assert session.get(Model,date(2026,10,1)).updated_at == stamp
    with pytest.raises(ValueError,match='仅新增'):
        strategy.run(skip_existing=False)


def test_interrupt_keeps_committed_windows_and_retry_only_fills_remaining(collector):
    module, engine = collector
    platform=Platform()
    platform.fail_at=2
    strategy=module._sentiment_strategy(platform.apis(),{})
    with pytest.raises(RuntimeError,match='interrupted'):
        strategy.run(start_date='2026-10-01',window_size=4)
    with Session(engine) as session:
        saved=list(session.scalars(select(strategy.MarketSentiment.trade_date)))
    assert len(saved)==4
    platform.fail_at=None
    result=strategy.run(start_date='2026-10-01',window_size=4)
    assert not set(saved).intersection(result.trade_date)
    assert len(saved)+len(result)==len(days_between(date(2026,10,1),date(2026,10,23)))


def test_initial_three_years_bounds_and_before_close(collector,monkeypatch):
    module,_=collector
    platform=Platform()
    strategy=module._sentiment_strategy(platform.apis(),{})
    monkeypatch.setattr(module,'china_now',lambda: datetime(2026,10,23,15))
    result=strategy.run(window_size=40)
    assert result.trade_date.min()==date(2023,10,23)
    assert result.trade_date.max()==date(2026,10,22)
    assert max(len(days_between(p['start_date'],p['end_date'])) for p in platform.prices)<=59
    assert strategy.three_years_ago(date(2024,2,29))==date(2021,2,28)


def test_bad_metadata_and_calendar_fail_instead_of_biased_results(collector):
    module,_=collector
    platform=Platform()
    apis=platform.apis()
    apis['get_all_securities']=lambda **kw: pd.DataFrame({'start_date':[pd.NaT],'end_date':[date(2200,1,1)]},index=['000001.XSHE'])
    strategy=module._sentiment_strategy(apis,{})
    with pytest.raises(ValueError,match='日期缺失'):
        strategy.run(start_date='2026-10-23')
    with pytest.raises(ValueError,match='交易日历'):
        strategy.checked_days([date(2026,10,23)]*2,date(2026,10,1),date(2026,10,23))
    with pytest.raises(ValueError,match='交易日历'):
        strategy.checked_days(None,date(2026,10,1),date(2026,10,23))


def test_single_file_python36_compatible_and_same_algorithm():
    factories=[]
    for filename in FILES:
        source=(ROOT/'collectors'/filename).read_text(encoding='utf-8')
        tree=ast.parse(source,feature_version=(3,6))
        factory=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_sentiment_strategy')
        assert '.to_numpy(' not in ast.get_source_segment(source,factory)
        factories.append(ast.dump(factory))
    assert factories[0]==factories[1]
