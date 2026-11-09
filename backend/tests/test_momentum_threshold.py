"""Exact >20% boundaries and both offline collector entry points; no live services."""
import datetime as dt
import math
import sys
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from types import ModuleType

import numpy as np
import pandas as pd
import pytest

from test_collector_batch import CONFIG, load
from test_momentum_collector import module as standalone

ROOT = Path(__file__).resolve().parents[2]
LONG_START = '123456789012345678901234567890.12345678901234567890'
LONG_EQUAL = '148148146814814814681481481468.148148146814814814680'
LONG_ABOVE = '148148146814814814681481481468.148148146814814814681'
LONG_BELOW = '148148146814814814681481481468.148148146814814814679'


@pytest.fixture
def batch_momentum(monkeypatch):
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    result = load(ROOT / 'collectors/策略批量采集_数据库版.py', 'exact_momentum_batch')
    monkeypatch.setattr(result, 'create_engine', lambda *a, **kw: pytest.fail('no database access'))
    return result


@pytest.fixture(params=['rule', 'standalone', 'batch'])
def threshold(request, batch_momentum):
    if request.param == 'rule':
        return load(ROOT / 'collectors/rules/momentum_threshold.py', 'momentum_rule')._momentum_front_status
    if request.param == 'standalone':
        return standalone._momentum_front_status
    return batch_momentum._momentum_front_status


@pytest.mark.parametrize('start,end,expected', [
    (10, 12, False), (10, 12.01, True), (10, 11.99, False),
    (0.7, 0.84, False), (3.4, 4.08, False), (4.35, 5.22, False),
    (42.1, 50.52, False), (np.float64(3.4), np.float64(4.08), False),
    (np.float32(3.4), np.float32(4.08), False),
    (0.7, math.nextafter(0.84, math.inf), True),
    (0.7, math.nextafter(0.84, -math.inf), False),
    ('1', '1.2000000000000000000000000001', True),
    ('1', '1.1999999999999999999999999999', False),
    (LONG_START, LONG_EQUAL, False), (LONG_START, LONG_ABOVE, True),
    (LONG_START, LONG_BELOW, False),
    (Decimal(LONG_START), Decimal(LONG_ABOVE), True),
    ('1e-1000', '1.2e-1000', False), ('1e-1000', '1.2000000001e-1000', True),
    ('1e1000', '1.2e1000', False), ('1e1000', '1.2000000001e1000', True),
    (10, 10, False), (10, 1, False),
    (0, 1, None), (1, 0, None), (-1, 2, None), (1, -2, None),
    (None, 1, None), (1, None, None), (pd.NA, 1, None),
    (float('nan'), 1, None), (1, float('nan'), None),
    (float('inf'), 1, None), (1, float('-inf'), None),
    (Decimal('sNaN'), 1, None), ('bad', 1, None), (1, '', None),
])
def test_exact_threshold_and_invalid_prices(threshold, start, end, expected):
    assert threshold(start, end) is expected


def test_threshold_independent_of_decimal_context(threshold):
    with localcontext() as context:
        context.prec = 2
        for signal in context.traps:
            context.traps[signal] = True
        assert threshold(LONG_START, LONG_EQUAL) is False
        assert threshold(LONG_START, LONG_ABOVE) is True
        assert threshold(LONG_START, LONG_BELOW) is False


def test_many_price_scales_against_independent_fraction_oracle(threshold):
    # 3,000 exact decimal pairs spanning several price scales. No epsilon or
    # cents-rounding may silently exclude a genuinely above-threshold close.
    for scale in (1, 100, 10000, 1000000):
        for ticks in range(1, 251):
            start = Decimal(ticks) / scale
            boundary = start * Decimal('1.2')
            step = Decimal(1) / (scale * 100)
            for end in (boundary - step, boundary, boundary + step):
                expected = Fraction(str(end)) / Fraction(str(start)) > Fraction(6, 5)
                assert threshold(start, end) is expected, (start, end)


def test_shared_threshold_is_embedded_and_python36_compatible():
    import ast
    sync = load(ROOT / 'collectors/tools/sync_momentum_threshold.py', 'sync_momentum')
    sync.sync(check=True)
    for name in ['collectors/rules/momentum_threshold.py'] + sync.TARGETS:
        ast.parse((ROOT / name).read_text(encoding='utf-8'), feature_version=(3, 6))


def price_apis(pairs):
    day = dt.date(2026, 3, 31)
    days = [d.date() for d in pd.bdate_range(end=day, periods=21)]
    securities = pd.DataFrame({
        'start_date': [dt.date(2020, 1, 1)] * len(pairs),
        'end_date': [dt.date(2200, 1, 1)] * len(pairs),
    }, index=list(pairs))
    members = {'A': list(pairs), 'B': [c for c in pairs if c.startswith('equal')],
               'C': [c for c in pairs if c.startswith('above')]}
    # Object dtype must preserve provider Decimal/string digits instead of
    # silently converting the fixture itself to binary floats.
    prices = pd.DataFrame([
        {'code': code, 'time': d, 'close': value}
        for code, values in pairs.items() for d, value in zip((days[0], day), values)
    ], dtype=object).iloc[::-1]
    return day, {
        'get_industries': lambda **kw: pd.DataFrame({'name': ['甲', '乙', '丙']}, index=['A', 'B', 'C']),
        'get_industry_stocks': lambda code, **kw: members[code],
        'get_all_securities': lambda **kw: securities,
        'get_trade_days': lambda **kw: days,
        'get_price': lambda *a, **kw: prices,
    }


@pytest.fixture(params=['standalone', 'batch'])
def calculate(request, monkeypatch, batch_momentum):
    def calculate_pairs(pairs):
        day, apis = price_apis(pairs)
        if request.param == 'standalone':
            for name, value in apis.items():
                monkeypatch.setattr(standalone, name, value)
            monkeypatch.setattr(standalone, 'create_engine', lambda *a, **kw: pytest.fail('no database access'))
            return standalone.calculate_momentum(day)
        return batch_momentum.STRATEGIES['行业动量'](apis, CONFIG).calculate_momentum(day)
    return calculate_pairs


def test_collectors_keep_counts_scores_and_ranks_at_decimal_boundaries(calculate):
    pairs = {
        'equal_float': (3.4, 4.08), 'equal_other': (0.7, 0.84),
        'equal_long': (Decimal(LONG_START), Decimal(LONG_EQUAL)),
        'above_tiny': (Decimal('1'), Decimal('1.2000000000000000000000000001')),
        'above_float': (10, 12.01),
        'below_tiny': (Decimal('1'), Decimal('1.1999999999999999999999999999')),
        'invalid_nan': (float('nan'), 3), 'invalid_inf': (2, float('inf')),
        'invalid_zero': (0, 2), 'invalid_none': (1, None),
    }
    records = calculate(pairs)
    assert [r['industry_code'] for r in records] == ['C', 'A', 'B']
    assert [r['front_stock_count'] for r in records] == [2, 2, 0]
    assert [r['industry_stock_count'] for r in records] == [2, 10, 3]
    assert [r['momentum'] for r in records] == [Decimal('2.00'), Decimal('0.40'), Decimal('0.00')]
    assert [r['momentum_rank'] for r in records] == [1, 2, 3]


def test_valid_exact_threshold_is_a_zero_score_not_missing_data(calculate):
    records = calculate({'equal': (3.4, 4.08)})
    assert all(r['front_stock_count'] == 0 and r['momentum'] == 0 for r in records)


def test_all_invalid_price_pairs_stop_before_writing(calculate):
    with pytest.raises(ValueError, match='没有可计算涨幅'):
        calculate({'nan': (float('nan'), 3), 'zero': (0, 1), 'inf': (1, float('inf'))})
