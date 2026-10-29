import importlib.util
from pathlib import Path
import pandas as pd
import pytest
from test_short_term_collector import mod, ns, history
from test_ultra_short_statistics import m, prices, snapshots

ROOT = Path(__file__).resolve().parents[2]

def sample():
    return prices([100.] * 65 + [97., 94., 91., 93.])

def test_embedded_rules():
    spec = importlib.util.spec_from_file_location('sync_rules', ROOT/'scripts/sync_oversold_rules.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.sync(check=True)
    assert {k: m.PARAMS[k] for k in m.OVERSOLD_PARAMS} == m.OVERSOLD_PARAMS

def test_parity_and_causality(ns):
    frame = sample()
    for end in range(60, len(frame)+1):
        past = frame.iloc[:end]
        expected = (m.pattern_candidates(past, m.PARAMS)[m.STRATEGIES[1]] & m.signal_eligibility(past, snapshots(past.index).set_index('trade_date'), m.PARAMS)).iloc[-1]
        assert ns.rebound_selected(past, '2020-01-01', 100.) == expected
    assert ns.rebound_selected(frame, '2020-01-01', 100.)
    future = prices([1., 10000., .1], calendar=pd.bdate_range(frame.index[-1]+pd.Timedelta(days=1), periods=3))
    full = pd.concat([frame, future])
    pd.testing.assert_series_equal(m.oversold_pattern(frame, m.PARAMS), m.oversold_pattern(full, m.PARAMS).loc[frame.index])
    frame['d1_win'] = 0
    assert ns.rebound_selected(frame, '2020-01-01', 100.)

@pytest.mark.parametrize('field,value,expected', [('adj_close', 91.05, False), ('adj_open', 93., False), ('volume', 99.99, False), ('volume', 100., True), ('money', 49999999., False), ('money', 50000000., True), ('paused', 1, False), ('paused', 0, True)])
def test_boundaries(ns, field, value, expected):
    frame = sample()
    frame.loc[frame.index[-1], field] = value
    assert ns.rebound_selected(frame, '2020-01-01', 30.) == expected

def test_eligibility(ns):
    frame = sample()
    assert not ns.rebound_selected(frame, '2020-01-01', 29.99)
    day = frame.index[-1]
    assert ns.rebound_selected(frame, day-pd.Timedelta(days=365), 30.)
    assert not ns.rebound_selected(frame, day-pd.Timedelta(days=364), 30.)
    assert not ns.rebound_selected(frame.tail(59), '2020-01-01', 30.)
    frame.loc[frame.index[-2], 'adj_close'] = 92.01
    assert not ns.rebound_selected(frame, '2020-01-01', 30.)

@pytest.mark.parametrize('key,value', [('oversold_3', 0), ('oversold_3', -1), ('oversold_3', float('nan')), ('rebound_volume', 0), ('rebound_volume', float('inf'))])
def test_invalid_parameters(key, value):
    with pytest.raises(ValueError):
        m.oversold_pattern(sample(), dict(m.PARAMS, **{key: value}))

def test_price_sources(ns):
    raw = history().rename(columns={'date': 'time'})
    raw['code'], raw['money'], raw['paused'] = '600001.XSHG', 1e8, 0
    post = raw.copy()
    post[['open', 'high', 'low', 'close']] *= 2
    post['volume'] *= 10
    days = [d.date() for d in raw.time]
    frame = ns.rebound_history(raw, post, '600001.XSHG', days, days[0])
    assert frame.volume.iloc[-1] == raw.volume.iloc[-1]
    assert frame.adj_close.iloc[-1] == raw.close.iloc[-1]*2
    with pytest.raises(ValueError, match='money'):
        ns.rebound_history(raw.drop(columns='money'), post, '600001.XSHG', days, days[0])
    with pytest.raises(ValueError, match='日期不完整'):
        ns.rebound_history(raw, post.iloc[:-1], '600001.XSHG', days, days[0])

@pytest.mark.parametrize('code,included', [('600001.XSHG', True), ('300001.XSHE', True), ('688001.XSHG', True), ('900001.XSHG', False), ('830001.XBJ', False)])
def test_collection_uses_signal_day_raw_volume_and_post_prices(mod, code, included):
    from types import SimpleNamespace
    frame = sample()
    raw = frame.rename_axis('time').reset_index()
    raw['code'] = code
    day = frame.index[-1].date()
    calls = []
    class Field:
        def in_(self, values): return self
    def get_price(codes, **kw):
        calls.append(kw)
        assert kw['end_date'] == day
        assert kw['skip_paused'] is False
        result = raw.copy()
        if kw['fq'] == 'post':
            result[['open', 'high', 'low', 'close']] *= 10
            # This deliberately must not influence the raw-volume signal.
            result.loc[result.index[-1], 'volume'] = 1
        return result
    apis = dict(get_trade_days=lambda **kw: [d.date() for d in frame.index],
        get_all_securities=lambda **kw: pd.DataFrame([dict(display_name='样本', start_date=pd.Timestamp('2020-01-01').date())], index=[code]),
        get_price=get_price,
        get_extras=lambda *args, **kw: pd.DataFrame({code: [False]}, index=[day]),
        get_fundamentals=lambda *args, **kw: pd.DataFrame([dict(code=code, market_cap=30., pe_ratio=10., pb_ratio=1.)]),
        get_industry=lambda *args, **kw: {},
        query=lambda *args: SimpleNamespace(filter=lambda *args: None),
        valuation=SimpleNamespace(**{k: Field() for k in ('code', 'market_cap', 'pe_ratio', 'pb_ratio')}))
    ns = mod._short_term_strategy(apis, mod.DB_CONFIG)
    meta, stocks = ns.collect(day)
    assert meta['counts']['oversold'] == int(included)
    assert [c['fq'] for c in calls] == (['pre', None, 'post'] if included else ['pre'])
    if included:
        assert 'oversold' in stocks[0]['methods']
        assert stocks[0]['bars'][-1]['close'] == 93.
        assert meta['oversold_params'] == m.OVERSOLD_PARAMS

@pytest.mark.parametrize('key,value', [('min_market_cap', -1), ('min_money', float('nan')), ('min_listing_days', float('inf'))])
def test_invalid_eligibility_parameters(key, value):
    frame = sample()
    with pytest.raises(ValueError):
        m.signal_eligibility(frame, snapshots(frame.index).set_index('trade_date'), dict(m.PARAMS, **{key: value}))


def test_missing_historical_close_and_st_are_ineligible():
    frame = sample()
    universe = snapshots(frame.index).set_index('trade_date')
    universe.loc[frame.index[-1], 'is_st'] = 1
    assert not m.signal_eligibility(frame, universe, m.PARAMS).iloc[-1]
    universe.loc[frame.index[-1], 'is_st'] = 0
    frame.loc[frame.index[-20], 'adj_close'] = float('nan')
    assert not m.signal_eligibility(frame, universe, m.PARAMS).iloc[-1]
