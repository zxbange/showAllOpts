"""Production selection parity and four-strategy statistical contract."""
import ast
import datetime as dt
import importlib.util
import sys
import uuid
from pathlib import Path
from types import ModuleType

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / '脚本/本地统计/短线三策略胜率验证_研究版.py'
ORIGINAL = ROOT / '脚本/本地统计/超短线四策略胜率统计_本地版.py'


@pytest.fixture
def mod(monkeypatch):
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    spec = importlib.util.spec_from_file_location('three_validation', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'china_now', lambda: dt.datetime(2024, 1, 8, 18))
    return module


def functions(path):
    tree = ast.parse(path.read_text(), feature_version=(3, 6))
    return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}


def test_exact_statistical_and_selection_contract():
    new, stats = functions(SOURCE), functions(ORIGINAL)
    old = functions(ROOT / 'collectors/短线选股胜率_研究版.py')
    for name in ('forward_outcomes', 'summary_table', 'window_summary'):
        assert ast.dump(new[name]) == ast.dump(stats[name])
    for name in ('make_selector', 'collect_selections', 'read_checkpoint', 'save_checkpoint'):
        assert ast.dump(new[name]) == ast.dump(old[name])
    # Existing test_short_term_research also compares every nested selector with production AST.
    assert 'create_engine' not in SOURCE.read_text()
    assert '.to_numpy(' not in SOURCE.read_text()


def test_forward_calendar_paused_missing_flat_and_window(mod):
    days = pd.bdate_range('2024-01-02', periods=7)
    frame = pd.DataFrame(dict(close=[100, 110, 100, 90, np.nan, 100, 101],
                              adj_close=[100, 110, 100, 90, np.nan, 100, 101],
                              paused=[0, 0, 0, 0, 0, 1, 0]), index=days)
    out = mod.forward_outcomes(frame)
    assert out.iloc[0]['d1_return_pct'] == pytest.approx(10)
    assert out.iloc[0]['d2_return_pct'] == 0
    assert out.iloc[0]['d3_return_pct'] == pytest.approx(-10)
    assert out.iloc[3]['d1_status'] == '缺少行情'
    assert out.iloc[3]['d2_status'] == '停牌'
    assert out.iloc[-1]['d1_status'] == '待观察'
    out['strategy'], out['code'] = mod.STRATEGIES[0], '600001.XSHG'
    summary = mod.summary_table(out)
    row = summary.iloc[0]
    assert row['触发样本'] == 7
    assert row['有效样本'] == 4  # one invalid baseline additionally excluded by finite return
    assert row['上涨次数'] == 2
    assert row['停牌样本'] == 1 and row['行情缺失'] == 1 and row['待观察'] == 1
    window = mod.window_summary(out).iloc[0]
    assert window['完整三日样本'] == 1
    assert window['三日内任一收盘上涨率(%)'] == 100


def setup(mod, monkeypatch, tmp_path, empty=False):
    days = [d.date() for d in pd.bdate_range('2024-01-02', '2024-01-08')]
    calls = []
    def selections(apis, selector, day, size, progress=None):
        return dict(version=mod.CACHE_VERSION, rule_version=mod.RULE_VERSION, day=str(day),
                    universe_count=1, evaluated_count=1, outcomes={}, selected=[] if empty else [
                        dict(code='600001.XSHG', name='历史股票', market_cap=30, methods=list(mod.METHODS), end_date=None)])
    monkeypatch.setattr(mod, 'collect_selections', selections)
    def price(codes, **kw):
        calls.append(kw)
        factor = 2 if kw['fq'] == 'post' else 1
        return pd.DataFrame([dict(code=c, time=d, close=factor*(100+days.index(d)), paused=0)
                             for c in codes for d in days if kw['start_date'] <= d <= kw['end_date']])
    apis = {name: object() for name in mod.API_NAMES}
    apis.update(get_trade_days=lambda start_date, end_date: [d for d in days if start_date <= d <= end_date],
                get_price=price)
    def run(**kwargs):
        return mod.run(api_namespace=apis, cache_dir=str(tmp_path/'cache'), output_dir=str(tmp_path/('out-' + uuid.uuid4().hex)),
                       show=False, **kwargs)
    return days, calls, run


def test_default_holiday_maturity_cache_and_independent_methods(mod, monkeypatch, tmp_path):
    days, calls, run = setup(mod, monkeypatch, tmp_path)
    result = run()
    assert mod.START_DATE == '2024-01-01'
    assert len(result.details) == 15
    assert result.details.signal_date.min() == pd.Timestamp('2024-01-02')
    assert len(result.summary) == 9
    assert result.summary.iloc[:3]['有效样本'].tolist() == [4, 3, 2]
    assert result.summary.iloc[:3]['待观察'].tolist() == [1, 2, 3]
    assert set(c['fq'] for c in calls) == {None, 'post'}
    assert result.details.iloc[0].signal_close == 200
    count = len(calls)
    run()
    assert len(calls) == count
    earlier = run(end_date='2024-01-03', as_of_date='2024-01-03')
    assert earlier.summary.iloc[:3]['有效样本'].tolist() == [1, 0, 0]
    assert len(list(result.output_dir.glob('*.csv'))) == 5
    assert (result.output_dir/'运行参数.json').exists()


def test_empty_results(mod, monkeypatch, tmp_path):
    _, calls, run = setup(mod, monkeypatch, tmp_path, empty=True)
    result = run()
    assert result.details.empty and not calls
    assert result.summary['有效样本'].eq(0).all()
    assert result.summary['胜率(%)'].isna().all()


def test_before_close_excludes_today(mod, monkeypatch, tmp_path):
    _, _, run = setup(mod, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, 'china_now', lambda: dt.datetime(2024, 1, 8, 15, 59))
    result = run()
    assert result.details.signal_date.max() == pd.Timestamp('2024-01-05')


def test_collect_missing_paused_and_resume(mod, monkeypatch, tmp_path):
    days = [d.date() for d in pd.bdate_range('2024-01-02', periods=4)]
    record = dict(day=str(days[0]), outcomes={}, selected=[dict(code='A', methods=['xuli'])])
    calls = []
    missing = [True]
    def prices(codes, **kw):
        calls.append(kw)
        return pd.DataFrame([dict(code='A', time=d, close=100+i, paused=int(i == 2))
                             for i, d in enumerate(days) if not (missing[0] and i == 1)])
    with mod.ResearchProgress(1, enabled=False) as progress:
        mod.collect_forward({'get_price': prices}, record, days, None, 50, progress)
        row = record['outcomes']['A']['row']
        assert row['d1_status'] == '缺少行情'
        assert row['d2_status'] == '停牌' and row['d2_return_pct'] is None
        assert row['d3_status'] == '有效'
        missing[0] = False
        mod.collect_forward({'get_price': prices}, record, days, None, 50, progress)
        assert record['outcomes']['A']['row']['d1_status'] == '有效'
        assert len(calls) == 4
        mod.collect_forward({'get_price': prices}, record, days, None, 50, progress)
        assert len(calls) == 4


def test_corrupt_duplicate_checkpoint_rejected(mod, tmp_path):
    import json
    day = dt.date(2024, 1, 2)
    record = dict(version=mod.CACHE_VERSION, rule_version=mod.RULE_VERSION, day=str(day),
                  outcomes={}, selected=[dict(code='A', methods=['xuli', 'xuli'])])
    (tmp_path/(str(day)+'.json')).write_text(json.dumps(record))
    with pytest.raises(ValueError, match='重复'):
        mod.read_checkpoint(tmp_path, day)
