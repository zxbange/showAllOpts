"""Canonical rules; embedded into standalone scripts by sync_oversold_rules.py."""
import pandas as pd

# BEGIN SHARED OVERSOLD RULES
OVERSOLD_PARAMS = dict(min_market_cap=30., min_listing_days=365, min_money=50_000_000.,
                       oversold_3=-.08, rebound_volume=1.)


def oversold_pattern(frame, params):
    drop, multiplier = params['oversold_3'], params['rebound_volume']
    if not (-1 < drop < 0) or not (0 < multiplier < float('inf')):
        raise ValueError('超跌阈值须在(-1, 0)之间，量比须为有限正数。')
    c, o, h, v = [frame[n] for n in ('adj_close', 'adj_open', 'adj_high', 'volume')]
    return ((c.shift(1) / c.shift(4) - 1 <= drop)
            & (c > h.shift(1)) & (c > o)
            & (v >= v.shift(1).rolling(5).mean() * multiplier))


def signal_eligibility(bars, universe, params, return_price_field='adj_close'):
    for key in ('min_market_cap', 'min_listing_days', 'min_money'):
        if not (0 <= params[key] < float('inf')):
            raise ValueError(key + '须为有限非负数')
    c = bars['adj_close']
    listed = pd.to_datetime(universe['listed_date'], errors='coerce')
    age = (pd.Series(bars.index, index=bars.index) - listed).dt.days
    ready = c.where(c > 0).rolling(60, min_periods=60).count() == 60
    return ((universe['eligible'] == 1) & (universe['is_st'] == 0)
            & (universe['market_cap'] >= params['min_market_cap'])
            & (age >= params['min_listing_days']) & ready
            & (bars['paused'] == 0) & (bars['money'] >= params['min_money'])
            & (bars['volume'] > 0) & (c > 0) & (bars[return_price_field] > 0))
# END SHARED OVERSOLD RULES
