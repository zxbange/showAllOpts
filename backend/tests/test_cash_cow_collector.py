"""Synthetic JoinQuant data, SQLite publication and page integration; no network."""
import ast
from copy import deepcopy
from datetime import date, datetime
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.cache import SnapshotCache
from app.core.cache_errors import SnapshotDataError
from app.core.config import Settings
from app.models.cash_cow import CashCowSnapshot
from app.services.cash_cow import CashCowScreen
from app.services.cash_cow_import import validate_snapshot
from test_cash_cow import Source
from test_dividend_collector import Frame

ROOT = Path(__file__).resolve().parents[2]
DAY = date(2026, 11, 13)


@pytest.fixture
def collector(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    pd = ModuleType('pandas')
    pd.DataFrame = Frame
    pd.isna = lambda v: v is None or str(v).lower() in ('nan', 'nat')
    monkeypatch.setitem(sys.modules, 'pandas', pd)
    spec = importlib.util.spec_from_file_location('offline_cow', ROOT/'collectors/奶牛股选股_数据库版.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, 'PROGRESS_ENABLED', False)
    monkeypatch.setattr(mod, 'china_now', lambda: datetime(2026, 11, 13, 19))
    engine = create_engine('sqlite:///' + str(tmp_path/'source.sqlite'))
    monkeypatch.setattr(mod, 'connect_database', lambda _: engine)
    ns = mod._cash_cow_strategy({'get_trade_days': lambda **kw: [DAY]}, mod.DB_CONFIG)
    ns.Base.metadata.create_all(engine)
    return mod, ns, engine


def payload(ns):
    return ns.calculate(DAY, Source(None, lambda: None))


def test_cash_cow_generated_entries_are_current():
    spec = importlib.util.spec_from_file_location('sync_cash_cow', ROOT/'collectors/tools/sync_cash_cow.py')
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tool.sync(check=True)


def test_same_factory_and_schema_in_standalone_batch_and_web(collector):
    _, ns, _ = collector
    nodes = []
    for name in ('奶牛股选股_数据库版.py', '策略批量采集_数据库版.py'):
        tree = ast.parse((ROOT/'collectors'/name).read_text(encoding='utf-8'), feature_version=(3, 6))
        nodes.append(ast.dump(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_cash_cow_strategy')))
    assert nodes[0] == nodes[1]
    signature = lambda t: [(c.name, str(c.type), c.primary_key, c.nullable) for c in t.columns]
    assert signature(ns.CashCowSnapshot.__table__) == signature(CashCowSnapshot.__table__)


def test_publish_three_windows_and_repeat_without_any_source_calls(collector, monkeypatch):
    mod, ns, engine = collector
    data = payload(ns)
    ns.save(sessionmaker(bind=engine), DAY, data)
    with engine.connect() as conn:
        validate_snapshot(conn)
        saved = conn.execute(select(CashCowSnapshot.__table__)).mappings().one()
    assert set(saved['payload']) == {'3', '5', '10'}
    assert all(r['snapshot']['matched_count'] == 1 for r in data.values())
    monkeypatch.setattr(ns.JoinQuantSource, 'universe', lambda _: pytest.fail('Same day must not call source'))
    assert len(ns.run()) == 3


def test_rejection_does_not_replace_newer_results_and_failed_write_rolls_back(collector):
    _, ns, engine = collector
    data = payload(ns)
    Session = sessionmaker(bind=engine)
    ns.save(Session, DAY, data)
    with pytest.raises(ValueError, match='历史日期'):
        ns.save(Session, date(2025, 1, 1), deepcopy(data))
    corrupt = deepcopy(data)
    corrupt['3']['items'][0]['current_yield'] = object()
    with pytest.raises(Exception):
        ns.save(Session, DAY, corrupt)
    with engine.connect() as conn:
        assert conn.execute(select(CashCowSnapshot.payload)).scalar_one() == data


@pytest.mark.parametrize('corruption', ['missing_period', 'counts', 'profit', 'average', 'run'])
def test_sync_rejects_incomplete_or_inconsistent_results(collector, corruption):
    _, ns, engine = collector
    data = payload(ns)
    if corruption == 'missing_period': del data['5']
    elif corruption == 'counts': data['3']['snapshot']['matched_count'] = 4
    elif corruption == 'profit': data['3']['items'][0]['years'][0]['adjusted_profit'] = -1
    elif corruption == 'average': data['3']['items'][0]['average_yield'] = 12
    else: data['3']['snapshot']['run_id'] = 'different'
    with engine.begin() as conn:
        conn.execute(CashCowSnapshot.__table__.insert(), dict(id=1, as_of_date=DAY, updated_at=datetime(2026,11,13), payload=data))
    with engine.connect() as conn, pytest.raises(SnapshotDataError):
        validate_snapshot(conn)


def test_sync_and_page_choose_newest_complete_source_without_erasing_public_result(collector, tmp_path):
    _, ns, engine = collector
    ns.save(sessionmaker(bind=engine), DAY, payload(ns))
    settings = Settings(_env_file=None, log_dir=tmp_path/'log', cache_dir=tmp_path/'cache')
    cache = SnapshotCache(settings, engine)
    cache.root.mkdir(parents=True, exist_ok=True)
    filename, count = cache.copy_module('cash-cow')
    assert count == 1
    cache.write_state({'modules': {'cash-cow': {'file': filename, 'updated_at': '2026-11-13T20:00:00+08:00'}}})
    screen = CashCowScreen(settings, years=3, cache=cache)
    assert screen.latest()['snapshot']['source'] == '聚宽'
    older = deepcopy(payload(ns)['3'])
    older['snapshot'].update(as_of='2026-11-12', source='东方财富公开数据接口')
    import json
    with screen.db() as db:
        db.execute('INSERT INTO snapshots VALUES (?,?)', (3, json.dumps(older)))
    assert screen.latest()['snapshot']['source'] == '聚宽'
    newer = deepcopy(older)
    newer['snapshot']['as_of'] = '2026-11-14'
    with screen.db() as db:
        db.execute('UPDATE snapshots SET payload=? WHERE years=3', (json.dumps(newer),))
    assert screen.latest()['snapshot']['source'] == '东方财富公开数据接口'


def source_adapter(ns):
    source = ns.JoinQuantSource.__new__(ns.JoinQuantSource)
    source.day, source.as_of = DAY, str(DAY)
    source.codes = {'600000': '600000.XSHG'}
    source.events, source.capital, source.daily = {}, {}, {}
    return source


def event(**changes):
    return dict(dict(code='600000.XSHG', report_date=date(2025,12,31), paid=date(2026,6,1),
        bonus_amount_rmb=5, bonus_ratio_rmb=5, dividend_ratio=0, transfer_ratio=0, implementation_bonusnote='10派5元'), **changes)


def test_cash_units_fiscal_years_duplicates_and_missing_values(collector):
    _, ns, _ = collector
    source = source_adapter(ns)
    source.dividend_rows = lambda *args: iter([event()])
    assert source.dividends(2025, ['600000'])['600000']['cash_dividend'] == 50000
    source.capital_stable = lambda *args: True
    assert source.per_share_dividends('600000', 2025, 2025) == {2025: .5}
    source.dividend_rows = lambda *args: iter([event(), event()])
    assert source.dividends(2025, ['600000'])['600000']['cash_dividend'] is None
    source.dividend_rows = lambda *args: iter([event(bonus_amount_rmb=None)])
    assert source.dividends(2025, ['600000'])['600000']['cash_dividend'] is None


def test_corporate_actions_only_invalidate_windows_containing_them(collector):
    _, ns, _ = collector
    source = source_adapter(ns)
    source.capital_stable = lambda *args: True
    source.events['600000'] = [event(), event(report_date=date(2020,12,31),
        paid=date(2021,6,1), transfer_ratio=10, implementation_bonusnote='10转10派5元')]
    assert source.per_share_dividends('600000', 2023, 2025)[2025] == .5
    assert all(v is None for v in source.per_share_dividends('600000', 2016, 2025).values())


def test_current_static_history_uses_one_current_price_and_withholds_stale_quote(collector):
    _, ns, _ = collector
    source = source_adapter(ns)
    source.per_share_dividends = lambda *args: {2025:.5, 2024:.4, 2023:.3}
    source.daily['600000'] = [(DAY, 10)]
    assert [r['yield_pct'] for r in source.current_dividend_yield('600000')['dividend_yield_history']] == [5,4,3]
    source.daily['600000'] = [(date(2026,10,1), 10)]
    assert source.current_dividend_yield('600000')['current_yield'] is None
