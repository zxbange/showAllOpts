import ast
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'collectors' / '短线选股胜率_研究版.py'


@pytest.fixture
def mod(monkeypatch):
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    spec = importlib.util.spec_from_file_location('short_winrate', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'china_now', lambda: dt.datetime(2026, 10, 24, 18))
    return module


def record(mod, day, codes=('600001.XSHG',), methods=('xuli',)):
    return dict(version=mod.CACHE_VERSION, rule_version=mod.RULE_VERSION, day=day.isoformat(),
                universe_count=len(codes), evaluated_count=len(codes), outcomes={},
                selected=[dict(code=c, name='测试股票', market_cap=30, methods=list(methods),
                               selection_close=999, end_date=None) for c in codes])


def setup_apis(mod, monkeypatch, tmp_path, end='2026-07-21', methods=('xuli',)):
    days = [d.date() for d in pd.bdate_range('2026-07-01', end)]
    calls = {'selected': [], 'prices': []}
    def select(apis, selector, day, size):
        calls['selected'].append(day)
        return record(mod, day, methods=methods)
    monkeypatch.setattr(mod, 'collect_selections', select)
    def calendar(start_date=None, end_date=None, **kwargs):
        return [d for d in days if (start_date is None or d >= start_date) and d <= end_date]
    def prices(codes, **kwargs):
        calls['prices'].append((list(codes), kwargs))
        first = kwargs['start_date']
        return pd.DataFrame([dict(code=code, time=day, close=100 + days.index(day)-days.index(first), paused=0)
                             for code in codes for day in days if first <= day <= kwargs['end_date']])
    apis = {name: object() for name in mod.API_NAMES}
    apis.update(get_trade_days=calendar, get_price=prices)
    def run(end_date, as_of_date):
        return mod.run(start_date=days[0], end_date=end_date, as_of_date=as_of_date,
                       cache_dir=str(tmp_path), api_namespace=apis, show=False)
    return days, calls, apis, run


def test_selectors_match_live_rules_and_python36():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'), feature_version=(3, 6))
    original = ast.parse((ROOT / 'collectors' / '短线选股_数据库版.py').read_text(encoding='utf-8'))
    source_factory = next(n for n in original.body if isinstance(n, ast.FunctionDef) and n.name == '_short_term_strategy')
    target_factory = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'make_selector')
    class NormalizeBuiltins(ast.NodeTransformer):
        def visit_Attribute(self, node):
            if isinstance(node.value, ast.Name) and node.value.id == 'builtins':
                return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
            return self.generic_visit(node)
    originals = {n.name: ast.dump(n) for n in source_factory.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    for node in target_factory.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            # Explicit builtin qualification is equivalent; all strategy expressions must still match.
            assert ast.dump(NormalizeBuiltins().visit(node)) == originals[node.name], node.name
    text = SOURCE.read_text(encoding='utf-8')
    assert '.to_numpy(' not in text and '.to_csv(' not in text
    assert 'create_engine' not in text and '__file__' not in text


@pytest.mark.parametrize('after,expected', [
    ('100', (False, False, False, False, False, False, True)),
    ('105', (True, False, False, False, False, False, False)),
    ('110', (True, True, False, False, False, False, False)),
    ('112', (True, True, True, False, False, False, False)),
    ('95', (False, False, False, True, False, False, False)),
    ('90', (False, False, False, True, True, False, False)),
    ('88', (False, False, False, True, True, True, False)),
])
def test_strict_threshold_boundaries(mod, after, expected):
    pct, flags = mod.measure_return('100', after)
    assert tuple(flags[k] for k in ('up_0', 'up_5', 'up_10', 'down_0', 'down_5', 'down_10', 'flat')) == expected
    assert pct == pytest.approx(float(after)-100)


def test_winrate_denominator_flat_missing_pending_and_fifty(mod):
    rows = []
    for i, after in enumerate((110, 100, 90, 101, None, None)):
        row = {column: None for column in mod.DETAIL_COLUMNS}
        row.update({'策略': '补票龙', '观察期限（交易日）': 2,
                    '状态': '有效' if after is not None else ('待观察' if i == 4 else '数据缺失')})
        if after is not None:
            pct, flags = mod.measure_return(100, after)
            row.update({'涨跌幅（%）': pct, '持平': flags['flat'], '入选日停牌': False, '观察日停牌': i == 1})
            row.update({label: flags[key] for label, key in mod.THRESHOLDS})
        rows.append(row)
    summary, distribution = mod.summarize(pd.DataFrame(rows, columns=mod.DETAIL_COLUMNS))
    row = summary.iloc[0]
    assert row['有效样本'] == 4 and row['上涨次数'] == 2 and row['持平次数'] == 1
    assert row['胜率（%）'] == 50 and row['超过50%'] == '否'
    assert row['总入选次数'] == 6 and row['待观察'] == row['数据缺失'] == 1
    assert row['停牌估值样本'] == 1 and len(summary) == 9
    assert distribution.iloc[0]['涨幅>10%'] == 0
    assert pd.isna(summary.iloc[-1]['胜率（%）'])


def test_trade_day_offsets_repeat_signals_cross_strategy_and_paired_prices(mod, monkeypatch, tmp_path):
    days, calls, apis, run = setup_apis(mod, monkeypatch, tmp_path, methods=('bukou', 'xuli'))
    result = run(days[1], days[10])
    assert len(calls['selected']) == 2 and len(result.details) == 12
    first = result.details[(result.details['策略'] == '蓄力龙') & (result.details['入选日'] == str(days[0]))]
    assert list(first['观察日']) == [str(days[2]), str(days[4]), str(days[10])]
    assert list(first['涨跌幅（%）']) == [2, 4, 10]
    assert list(first['入选日收盘价（前复权）']) == [100, 100, 100]  # cached selection_close=999 is never the denominator.
    xuli = result.summary[result.summary['策略'] == '蓄力龙']
    assert list(xuli['总入选次数']) == [2, 2, 2]
    assert list(xuli['有效样本']) == [2, 2, 1]
    assert list(xuli['待观察']) == [0, 0, 1]
    assert list(xuli['胜率（%）']) == [100, 100, 100]
    assert days[4] - days[0] == dt.timedelta(days=6)  # Weekend does not count.
    assert not list(tmp_path.rglob('*.csv'))


def test_resume_skips_completed_selections_and_only_matures_new_horizons(mod, monkeypatch, tmp_path):
    days, calls, apis, run = setup_apis(mod, monkeypatch, tmp_path)
    first = run(days[0], days[2])
    assert len(calls['prices']) == 1
    again = run(days[0], days[2])
    pd.testing.assert_frame_equal(first.summary, again.summary)
    assert len(calls['selected']) == len(calls['prices']) == 1
    later = run(days[0], days[10])
    assert len(calls['selected']) == 1 and len(calls['prices']) == 2
    assert list(later.summary[later.summary['策略'] == '蓄力龙']['有效样本']) == [1, 1, 1]
    earlier = run(days[0], days[2])  # Future cached observations must not leak into an earlier as-of.
    pd.testing.assert_frame_equal(first.summary, earlier.summary)
    assert len(calls['prices']) == 2


def test_failure_after_selection_resumes_without_scanning_again(mod, monkeypatch, tmp_path):
    days, calls, apis, run = setup_apis(mod, monkeypatch, tmp_path)
    fetch = apis['get_price']
    apis['get_price'] = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('network'))
    with pytest.raises(RuntimeError, match='network'):
        run(days[0], days[10])
    assert len(list(tmp_path.rglob('*.json'))) == 1
    apis['get_price'] = fetch
    result = run(days[0], days[10])
    assert len(calls['selected']) == 1 and result.summary['有效样本'].sum() == 3


def test_missing_prices_are_not_zero_returns_and_retry_next_run(mod, monkeypatch, tmp_path):
    days, calls, apis, run = setup_apis(mod, monkeypatch, tmp_path)
    fetch = apis['get_price']
    def missing(*a, **kw):
        frame = fetch(*a, **kw)
        return frame[frame.time != days[2]]
    apis['get_price'] = missing
    first = run(days[0], days[10])
    row = first.summary[first.summary['策略'] == '蓄力龙'].iloc[0]
    assert row['数据缺失'] == 1 and row['有效样本'] == 0 and pd.isna(row['胜率（%）'])
    apis['get_price'] = fetch
    second = run(days[0], days[10])
    assert second.summary['数据缺失'].sum() == 0
    assert len(calls['selected']) == 1


def test_paused_and_delisted_are_explicit(mod):
    day = dt.date(2026, 7, 1)
    targets = {2: dt.date(2026, 7, 3), 4: None, 10: None}
    rec = record(mod, day, codes=('600001.XSHG', '600002.XSHG'))
    rec['selected'][1]['end_date'] = day.isoformat()
    raw = pd.DataFrame([dict(code=c, time=d, close=100, paused=d != day)
                        for c in ('600001.XSHG', '600002.XSHG') for d in (day, targets[2])])
    mod.collect_outcomes({'get_price': lambda *a, **kw: raw}, rec, targets, None, 50)
    assert rec['outcomes']['600001.XSHG|2']['target_paused'] is True
    assert rec['outcomes']['600001.XSHG|2']['flags']['flat'] is True
    assert rec['outcomes']['600002.XSHG|2']['status'] == '数据缺失'
    assert '退市' in rec['outcomes']['600002.XSHG|2']['reason']


def test_zero_selection_days_are_cached_and_summary_still_has_nine_rows(mod, monkeypatch, tmp_path):
    days, calls, apis, run = setup_apis(mod, monkeypatch, tmp_path, methods=())
    count = []
    def empty(*args):
        count.append(args[2])
        return record(mod, args[2], codes=())
    monkeypatch.setattr(mod, 'collect_selections', empty)
    result = run(days[1], days[10])
    run(days[1], days[10])
    assert len(count) == 2 and not calls['prices']
    assert len(result.summary) == 9 and result.summary['胜率（%）'].isna().all()
    assert result.details.empty
    mod.show_results(result)


def test_incomplete_selection_does_not_mark_day_complete(mod, monkeypatch, tmp_path):
    days, calls, apis, run = setup_apis(mod, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, 'collect_selections', lambda *a: (_ for _ in ()).throw(ValueError('ST missing')))
    with pytest.raises(ValueError, match='ST missing'):
        run(days[0], days[10])
    assert not list(tmp_path.rglob('*.json'))


def test_future_and_intraday_end_rejected(mod, monkeypatch, tmp_path):
    days, calls, apis, run = setup_apis(mod, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, 'china_now', lambda: dt.datetime(2026, 7, 3, 14))
    with pytest.raises(ValueError, match='日期'):
        run(days[0], dt.date(2026, 7, 3))
    result = mod.run(start_date=days[0], cache_dir=str(tmp_path), api_namespace=apis, show=False)
    assert result.as_of_date == dt.date(2026, 7, 2)
    assert all(kw['end_date'] <= result.as_of_date for _, kw in calls['prices'])


def test_invalid_calendar_and_checkpoint_rejected(mod, tmp_path):
    day = dt.date(2026, 7, 1)
    with pytest.raises(ValueError):
        mod.checked_calendar([day, day])
    rec = record(mod, day)
    rec['version'] = 'older'
    mod.save_checkpoint(tmp_path, rec)
    with pytest.raises(ValueError, match='版本'):
        mod.read_checkpoint(tmp_path, day)


def test_real_historical_selection_st_cap_filter_and_warmup(mod):
    day = dt.date(2026, 7, 1)
    dates = [d.date() for d in pd.bdate_range(end=day, periods=90)]
    codes = ['600001.XSHG', '600002.XSHG', '600003.XSHG', '600004.XSHG']
    requests = []
    class Field:
        def in_(self, values): return values
    class Query:
        def filter(self, codes): return codes
    def prices(batch, **kwargs):
        requests.append((batch, kwargs))
        close = np.linspace(10, 20, 90)
        close[-4:] = [20, 21, 20.5, 21.5]
        frame = pd.DataFrame(dict(time=dates, close=close, open=close-.1,
                                 low=close-.2, high=close+.2, volume=100.))
        frame.loc[87, 'open'], frame.loc[88, 'open'] = 20, 21
        frame.loc[87:, 'volume'] = [130, 80, 130]
        return pd.concat([frame.assign(code=code) for code in batch], ignore_index=True)
    def securities(**kwargs):
        assert kwargs == {'types': ['stock'], 'date': day}
        return pd.DataFrame([dict(display_name=c, start_date=dates[0], end_date=dt.date(2200, 1, 1)) for c in codes], index=codes)
    def fundamentals(selected, **kwargs):
        assert kwargs['date'] == day
        return pd.DataFrame([{'code': c, 'market_cap': cap} for c, cap in zip(codes, (31, 40, 29.99, 30)) if c in selected])
    def extras(info, batch, **kwargs):
        assert kwargs['end_date'] == day
        return pd.DataFrame({c: [c == codes[1]] for c in batch}, index=[day])
    apis = dict(get_trade_days=lambda **kw: dates, get_all_securities=securities, get_price=prices,
                get_fundamentals=fundamentals, get_extras=extras, query=lambda *a: Query(),
                valuation=SimpleNamespace(code=Field(), market_cap=Field()))
    rec = mod.collect_selections(apis, mod.make_selector(apis), day, 50)
    assert rec['evaluated_count'] == 2
    assert [s['code'] for s in rec['selected']] == [codes[0], codes[3]]
    assert all('xuli' in s['methods'] for s in rec['selected'])
    assert requests[0][0] == [codes[0], codes[3]]
    assert requests[0][1]['start_date'] == dates[0] and requests[0][1]['end_date'] == day
    assert requests[0][1]['fq'] == 'pre'
    apis['get_extras'] = lambda *a, **kw: pd.DataFrame() if kw.get('df') else {}
    with pytest.raises(ValueError, match='ST'):
        mod.collect_selections(apis, mod.make_selector(apis), day, 50)


def test_fresh_research_environment_uses_builtin_market_apis(mod, monkeypatch, tmp_path):
    # Mirror the reported research environment: jqdata exposes calendar/query only;
    # the remaining five APIs live in builtins, with no prior Notebook imports.
    import builtins
    days, calls, apis, _ = setup_apis(mod, monkeypatch, tmp_path)
    for name, value in apis.items():
        monkeypatch.delattr(mod, name, raising=False)
        if name in ('get_trade_days', 'query'):
            monkeypatch.setattr(mod.jq, name, value, raising=False)
            monkeypatch.delattr(builtins, name, raising=False)
        else:
            monkeypatch.delattr(mod.jq, name, raising=False)
            monkeypatch.setattr(builtins, name, value, raising=False)
    assert mod.resolve_apis() == apis
    result = mod.run(start_date=days[0], end_date=days[0], as_of_date=days[10],
                     cache_dir=str(tmp_path), show=False)
    assert calls['selected'] == [days[0]]
    assert result.summary['有效样本'].sum() == 3
    again = mod.run(start_date=days[0], end_date=days[0], as_of_date=days[10],
                    cache_dir=str(tmp_path), show=False)
    pd.testing.assert_frame_equal(result.summary, again.summary)
    assert len(calls['selected']) == len(calls['prices']) == 1


def test_api_resolution_precedence_and_no_truth_testing_table_objects(mod, monkeypatch):
    import builtins
    class Table:
        def __bool__(self):
            raise AssertionError('Do not truth-test provider table objects.')
    default = {name: object() for name in mod.API_NAMES}
    for name, value in default.items():
        monkeypatch.setattr(builtins, name, value, raising=False)
        monkeypatch.delattr(mod.jq, name, raising=False)
        monkeypatch.delattr(mod, name, raising=False)
    jq_price, global_price, explicit_price = object(), object(), object()
    monkeypatch.setattr(mod.jq, 'get_price', jq_price, raising=False)
    monkeypatch.setattr(mod, 'get_price', global_price, raising=False)
    assert mod.resolve_apis()['get_price'] is global_price
    table = Table()
    resolved = mod.resolve_apis({'get_price': explicit_price, 'valuation': table})
    assert resolved['get_price'] is explicit_price
    assert resolved['valuation'] is table
    assert mod.resolve_apis({'get_price': None})['get_price'] is jq_price
    monkeypatch.setattr(mod.jq, 'get_price', None)
    assert mod.resolve_apis({})['get_price'] is default['get_price']


def test_missing_api_reports_only_unavailable_names(mod, monkeypatch):
    import builtins
    namespace = {name: object() for name in mod.API_NAMES}
    namespace['get_price'] = None
    monkeypatch.delattr(mod.jq, 'get_price', raising=False)
    monkeypatch.delattr(builtins, 'get_price', raising=False)
    with pytest.raises(RuntimeError, match='未找到聚宽研究接口：get_price；'):
        mod.resolve_apis(namespace)


def test_notebook_numpy_any_does_not_reject_valid_calendar(mod, monkeypatch):
    monkeypatch.setattr(mod, 'any', np.any, raising=False)
    start, end = dt.date(2026, 7, 1), dt.date(2026, 7, 3)
    days = [start, dt.date(2026, 7, 2), end]
    assert mod.checked_calendar(days, start, end) == days
    for invalid in ([start, start], [end, start], [start-dt.timedelta(days=1)], [end+dt.timedelta(days=1)]):
        with pytest.raises(ValueError):
            mod.checked_calendar(invalid, start, end)


def test_pasted_notebook_with_numpy_names_runs_real_selection_and_resumes(mod, monkeypatch, tmp_path):
    import builtins
    # Reproduce a Notebook populated by numerical imports before this script is pasted.
    namespace = {'__name__': 'notebook_cell', 'any': np.any, 'all': np.all,
                 'min': np.min, 'max': np.max, 'sum': np.sum, 'round': np.round,
                 'bool': np.bool_, 'int': np.int64, 'float': np.float64}
    exec(compile(SOURCE.read_text(encoding='utf-8'), '<ipython-input-test>', 'exec'), namespace)
    day = dt.date(2026, 7, 1)
    past = [d.date() for d in pd.bdate_range(end=day, periods=90)]
    future = [d.date() for d in pd.bdate_range(start=day, periods=11)]
    code = '600001.XSHG'
    calls = {'stock_pool': 0, 'signal_prices': 0, 'forward_prices': 0}
    def calendar(start_date=None, end_date=None, count=None):
        return past if count else [d for d in future if start_date <= d <= end_date]
    def stocks(**kwargs):
        calls['stock_pool'] += 1
        assert kwargs['date'] == day
        return pd.DataFrame([dict(display_name='测试股票', start_date=past[0])], index=[code])
    def prices(codes, **kwargs):
        assert codes == [code]
        if 'open' in kwargs['fields']:
            calls['signal_prices'] += 1
            assert kwargs['end_date'] == day
            close = np.linspace(10, 20, 90)
            close[-4:] = [20, 21, 20.5, 21.5]
            frame = pd.DataFrame(dict(time=past, code=code, close=close, open=close-.1,
                                     low=close-.2, high=close+.2, volume=100.))
            frame.loc[87, 'open'], frame.loc[88, 'open'] = 20, 21
            frame.loc[87:, 'volume'] = [130, 80, 130]
            return frame
        calls['forward_prices'] += 1
        return pd.DataFrame([dict(code=code, time=d, close=100+i, paused=0)
                             for i, d in enumerate(future) if day <= d <= kwargs['end_date']])
    class Field:
        def in_(self, values): return values
    class Query:
        def filter(self, values): return values
    apis = dict(get_trade_days=calendar, get_all_securities=stocks, get_price=prices,
                get_fundamentals=lambda *a, **kw: pd.DataFrame([dict(code=code, market_cap=30)]),
                get_extras=lambda *a, **kw: pd.DataFrame({code: [False]}, index=[day]),
                query=lambda *a: Query(), valuation=SimpleNamespace(code=Field(), market_cap=Field()))
    for name, value in apis.items():
        if name in ('get_trade_days', 'query'):
            monkeypatch.setattr(namespace['jq'], name, value, raising=False)
        else:
            monkeypatch.setattr(builtins, name, value, raising=False)
    namespace['china_now'] = lambda: dt.datetime(2026, 10, 24, 18)
    options = dict(start_date=day, end_date=day, as_of_date=future[-1], cache_dir=str(tmp_path), show=False)
    result = namespace['run'](**options)
    xuli = result.summary[result.summary['策略'] == '蓄力龙']
    assert list(xuli['胜率（%）']) == [100, 100, 100]
    assert list(xuli['超过50%']) == ['是', '是', '是']
    assert result.daily_counts.iloc[0]['蓄力龙'] == 1
    assert calls == {'stock_pool': 1, 'signal_prices': 1, 'forward_prices': 1}
    namespace['show_results'](result)
    again = namespace['run'](**options)
    pd.testing.assert_frame_equal(result.summary, again.summary)
    assert calls == {'stock_pool': 1, 'signal_prices': 1, 'forward_prices': 1}
