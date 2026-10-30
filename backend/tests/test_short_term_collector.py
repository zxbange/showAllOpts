import ast
import datetime as dt
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from contextlib import contextmanager
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'collectors' / '短线选股_数据库版.py'

@pytest.fixture
def mod(monkeypatch):
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    spec = importlib.util.spec_from_file_location('short_collector', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.PROGRESS_ENABLED = False
    return module

@pytest.fixture
def ns(mod):
    return mod._short_term_strategy({}, mod.DB_CONFIG)

def complete_meta(ns, bukou=0):
    return {'rule_version': ns.RULE_VERSION, 'counts': {'bukou': bukou, 'redhover': 0, 'xuli': 0, 'oversold': 0}}

def history():
    close = np.linspace(10, 20, 90)
    close[-4:] = [20, 21, 20.5, 21.5]
    df = pd.DataFrame({'date': pd.bdate_range('2026-01-01', periods=90), 'close': close,
                       'open': close - .1, 'low': close - .2, 'high': close + .2, 'volume': 100.0})
    df.loc[87, 'open'] = 20
    df.loc[88, 'open'] = 21
    df.loc[87:, 'volume'] = [130, 80, 130]
    df['pre_close'] = df.close.shift(1)
    df['pct_chg'] = (df.close / df.pre_close - 1) * 100
    return df

def test_original_thresholds_and_warmup(ns):
    frame = history()
    assert ns.analyze_one('600001.XSHG', frame)
    frame.loc[88, 'pct_chg'] = -4
    assert not ns.analyze_one('600001.XSHG', frame)
    bars = ns.chart_bars(history())
    assert len(bars) == 30
    assert bars[0]['ma']['60'] == pytest.approx(history().close.iloc[1:61].mean())
    red = pd.DataFrame({'date': pd.bdate_range('2026-01-01', periods=3),
        'pre_close': [10, 11, 11.2], 'pct_chg': [10, 1.82, .89],
        'low': [10, 11, 11], 'volume': [100, 50, 60]})
    assert ns.analyze_stock(red, '600001.XSHG')  # Actual original code has no 1.5x volume filter.
    red.loc[2, 'pct_chg'] = 9.5
    assert ns.analyze_stock(red, '600001.XSHG') is None

def test_schema_and_factory_parity(ns):
    from app.models.short_term import ShortTermRun, ShortTermStock
    for source, target in ((ns.ShortTermRun, ShortTermRun), (ns.ShortTermStock, ShortTermStock)):
        assert [(c.name, str(c.type), c.primary_key) for c in source.__table__.columns] == [(c.name, str(c.type), c.primary_key) for c in target.__table__.columns]
    def factory(path):
        tree = ast.parse(path.read_text(encoding='utf-8'), feature_version=(3,6))
        return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_short_term_strategy')
    assert ast.dump(factory(SOURCE)) == ast.dump(factory(ROOT/'collectors'/'策略批量采集_数据库版.py'))

def test_complete_day_idempotent_zero_results_and_atomicity(ns):
    engine = create_engine('sqlite://')
    ns.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    day = dt.date(2026, 10, 22)
    assert ns.save_day(Session, day, complete_meta(ns), [])
    assert not ns.save_day(Session, day, complete_meta(ns), [])
    record = {'trade_date': day, 'code': '600001.XSHG', 'name': 'example', 'bars': []}
    with pytest.raises(Exception):
        ns.save_day(Session, day + dt.timedelta(days=1), {}, [record, record])
    with Session() as session:
        assert session.query(ns.ShortTermRun).count() == 1
        assert session.query(ns.ShortTermStock).count() == 0
    engine.dispose()

def test_retention_uses_three_calendar_trading_days_and_skips_existing(mod, monkeypatch):
    engine = create_engine('sqlite://')
    days = [dt.date(2026, 10, 21), dt.date(2026, 10, 22), dt.date(2026, 10, 23)]
    monkeypatch.setattr(mod, 'china_now', lambda: dt.datetime(2026, 10, 23, 17))
    ns = mod._short_term_strategy({'get_trade_days': lambda **kw: days}, mod.DB_CONFIG)
    ns.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    old = dt.date(2026, 10, 20)
    for day in [old] + days:
        ns.save_day(Session, day, complete_meta(ns, 1), [{'trade_date': day, 'code': '600001.XSHG', 'methods': ['bukou']}])
    @contextmanager
    def database_scope(*args):
        yield Session
    monkeypatch.setattr(mod, 'database_scope', database_scope)
    assert ns.run().empty  # No market API available: completed days must not be recollected.
    with Session() as session:
        assert [d for d, in session.query(ns.ShortTermRun.trade_date).order_by(ns.ShortTermRun.trade_date)] == days
        assert session.query(ns.ShortTermStock).count() == 3
    engine.dispose()

def test_missing_price_dates_fail_instead_of_partial_success(ns):
    df = history().rename(columns={'date': 'time'})
    df['code'] = '600001.XSHG'
    days = [d.date() for d in df.time]
    frame = ns.clean_history(df, '600001.XSHG', days, days[0])
    assert len(frame) == 90
    with pytest.raises(ValueError):
        ns.clean_history(df.iloc[:-1], '600001.XSHG', days, days[0])


@pytest.mark.parametrize('market_cap,is_st,included', [(50, False, True), (30, False, True), (29.99, False, False), (50, True, False)])
def test_collect_scans_and_enriches_selected_stock(mod, market_cap, is_st, included):
    from types import SimpleNamespace
    frame = history().rename(columns={'date': 'time'})
    frame['code'] = '600001.XSHG'
    days = [d.date() for d in frame.time]
    class Field:
        def in_(self, value): return self
    fields = SimpleNamespace(**{k: Field() for k in ('code', 'market_cap', 'pe_ratio', 'pb_ratio')})
    apis = dict(get_trade_days=lambda **kw: days,
        get_all_securities=lambda **kw: pd.DataFrame([{'display_name': '测试股份', 'start_date': days[0]}], index=['600001.XSHG']),
        get_price=lambda *a, **kw: frame,
        get_extras=lambda *a, **kw: pd.DataFrame({'600001.XSHG': [is_st]}, index=[days[-1]]),
        get_industry=lambda *a, **kw: {'600001.XSHG': {'sw_l2': {'industry_name': '测试行业'}}},
        get_fundamentals=lambda *a, **kw: pd.DataFrame([{'code':'600001.XSHG','market_cap':market_cap,'pe_ratio':12,'pb_ratio':2}]),
        query=lambda *a: SimpleNamespace(filter=lambda *a: None), valuation=fields)
    ns = mod._short_term_strategy(apis, mod.DB_CONFIG)
    meta, stocks = ns.collect(days[-1])
    if not included:
        assert stocks == [] and meta['evaluated_count'] == 0
        return
    assert meta['counts']['xuli'] == 1 and len(stocks) == 1
    assert stocks[0]['industry'] == '测试行业' and stocks[0]['pe'] == 12
    assert len(stocks[0]['bars']) == 30 and stocks[0]['bars'][0]['ma']['60'] is not None


def test_bukou_short_rsv_dips_then_recovers_with_long_rsv_strong(ns):
    frame = history()
    frame['close'] = np.linspace(80, 104, 90)
    frame.loc[86:, 'close'] = [104, 105, 103, 106]
    frame['low'] = frame.close - .1
    frame.loc[75, 'low'] = 80
    frame['high'] = frame.close + .2
    assert 'bukou' in ns.evaluate(frame, '600001.XSHG', is_st=True)
    frame.loc[88, 'close'] = 105
    assert 'bukou' not in ns.evaluate(frame, '600001.XSHG', is_st=True)


def test_st_missing_batch_falls_back_and_never_uses_old_day(mod):
    day = dt.date(2026, 10, 22)
    calls = []
    def extra(info, codes, **kw):
        calls.append((codes, kw))
        if kw['df']:
            return pd.DataFrame({'a': [False], 'b': [False]}, index=[day-dt.timedelta(days=1)])
        return {codes[0]: [codes[0] == 'a']}
    ns = mod._short_term_strategy({'get_extras': extra}, mod.DB_CONFIG)
    assert ns.read_st_flags(['a', 'b'], day) == {'a': True, 'b': False}
    assert len(calls) == 3
    ns = mod._short_term_strategy({'get_extras': lambda *a, **kw: pd.DataFrame() if kw['df'] else {}}, mod.DB_CONFIG)
    with pytest.raises(ValueError, match='2026-10-22.*a'):
        ns.read_st_flags(['a'], day)
    assert ns.read_st_flags([], day) == {}


def test_rule_upgrade_replaces_snapshot_atomically(ns):
    engine = create_engine('sqlite://')
    ns.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    day = dt.date(2026, 10, 22)
    record = {'code': 'a', 'trade_date': day}
    with Session() as session:
        session.add(ns.ShortTermRun(trade_date=day, payload={'rule_version': 'old'}, updated_at=dt.datetime.now()))
        session.add(ns.ShortTermStock(trade_date=day, code='a', payload={'code': 'a'}, updated_at=dt.datetime.now()))
        session.commit()
    with pytest.raises(Exception):
        ns.save_day(Session, day, {'rule_version': ns.RULE_VERSION}, [record, record])
    with Session() as session:
        assert session.query(ns.ShortTermRun).one().payload['rule_version'] == 'old'
        assert session.query(ns.ShortTermStock).count() == 1
    assert ns.save_day(Session, day, complete_meta(ns), [])
    with Session() as session:
        assert session.query(ns.ShortTermStock).count() == 0
        assert session.query(ns.ShortTermRun).one().payload['rule_version'] == ns.RULE_VERSION
    assert not ns.save_day(Session, day, complete_meta(ns), [])
    engine.dispose()


def test_correct_rule_missing_oversold_is_recomputed_and_never_zero_filled(ns):
    engine = create_engine('sqlite://')
    ns.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    day = dt.date(2026, 10, 22)
    incomplete = complete_meta(ns)
    del incomplete['counts']['oversold']
    with Session.begin() as session:
        session.add(ns.ShortTermRun(trade_date=day, payload=incomplete, updated_at=dt.datetime.now()))
    with pytest.raises(ValueError):
        ns.save_day(Session, day, incomplete, [])
    complete = complete_meta(ns)
    complete['counts']['oversold'] = 1
    assert ns.save_day(Session, day, complete, [{'code': '600001.XSHG', 'methods': ['oversold']}])
    with Session() as session:
        assert session.get(ns.ShortTermRun, day).payload['counts']['oversold'] == 1
        assert session.query(ns.ShortTermStock).count() == 1
    engine.dispose()


def test_same_second_detail_repair_changes_snapshot_revision(ns, mod, monkeypatch):
    stamp = dt.datetime(2026, 10, 22, 17)
    monkeypatch.setattr(mod, 'china_now', lambda: stamp)
    engine = create_engine('sqlite://')
    ns.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    day, meta = stamp.date(), complete_meta(ns, 1)
    records = [{'code': '600001.XSHG', 'methods': ['bukou']}]
    ns.save_day(Session, day, meta, records)
    with Session.begin() as session:
        previous = dict(session.get(ns.ShortTermRun, day).payload)
        session.query(ns.ShortTermStock).delete()
    assert ns.save_day(Session, day, meta, records)
    with Session() as session:
        current = session.get(ns.ShortTermRun, day)
        assert current.updated_at == stamp
        assert current.payload['counts'] == previous['counts']
        assert current.payload['snapshot_revision'] != previous['snapshot_revision']
        assert ns.complete_day(session, current)
    assert 'snapshot_revision' not in meta
    assert not ns.save_day(Session, day, meta, records)
    engine.dispose()
