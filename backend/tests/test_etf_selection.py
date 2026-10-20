"""ETF research selection invariants; no network or JoinQuant dependency."""
import ast
import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest


@pytest.fixture
def script():
    path = Path(__file__).resolve().parents[2] / 'collectors' / 'ETF分类去重_研究版.py'
    tree = ast.parse(path.read_text(encoding='utf-8'), feature_version=(3, 6))
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.Import) and node.names[0].name in ('pandas', 'jqdata')
        or isinstance(node, ast.ImportFrom) and node.module == 'jqdata')]
    env = {'__name__': 'offline_test'}
    exec(compile(tree, str(path), 'exec'), env)
    return env


def row(code, group='index:300', category='宽基ETF'):
    return dict(code=code, group=group, category=category, name=code,
                target_name='沪深300', classification_reason='test')


def test_rank_by_assets_not_shares_and_same_day(script):
    d1, d2 = dt.date(2026, 10, 15), dt.date(2026, 10, 16)
    sizes = {'a': {d1: (Decimal(200), 200, 1), d2: (Decimal(900), 900, 1)},
             'b': {d1: (Decimal(300), 100, 3)}}
    selected, audit, candidates = script['select_largest']([row('a'), row('b')], sizes, [d1, d2])
    assert selected[0]['code'] == 'b'
    assert selected[0]['size_date'] == str(d1)
    assert selected[0]['group_count'] == 2
    assert not audit and len(candidates) == 2


def test_missing_competitor_blocks_whole_group(script):
    day = dt.date(2026, 10, 16)
    result, audit, _ = script['select_largest']([row('a'), row('b')], {'a': {day: (100, 100, 1)}}, [day])
    assert not result and len(audit) == 1


def test_stale_day_blocks_group(script):
    days = [dt.date(2026, 10, i) for i in range(12, 17)]
    result, audit, _ = script['select_largest']([row('a')], {'a': {days[0]: (100, 100, 1)}}, days)
    assert not result and '过旧' in audit[0]['reason']


def test_ties_deterministic_different_indices_retained(script):
    day = dt.date(2026, 10, 16)
    result, _, _ = script['select_largest']([row('b'), row('a'), row('c', 'other')],
                                           {c: {day: (100, 100, 1)} for c in 'abc'}, [day])
    assert [item['code'] for item in result] == ['a', 'c']


def test_category_conflict_does_not_duplicate_index(script):
    result, audit, _ = script['select_largest']([row('a'), row('b', category='主题ETF')], {}, [])
    assert not result and len(audit) == 1


@pytest.mark.parametrize('name,expected', [
    ('沪深300指数', '宽基ETF'), ('沪深300(000300)', '宽基ETF'),
    ('中证500医药指数', '行业ETF'), ('中证银行指数', '行业ETF'),
    ('中证新能源汽车指数', '主题ETF'), ('中证红利低波指数', '策略/风格ETF'),
    ('恒生科技指数', '跨境ETF'), ('上海黄金交易所黄金现货合约', '商品ETF'),
    ('中债国债指数', '债券ETF'), ('中证货币基金指数', '排除：货币ETF'),
    ('中证黄金产业股票指数', '待核对'), ('未识别标的', '待核对'),
])
def test_classification(script, name, expected):
    assert script['classify'](name, {})[0] == expected


def test_manual_category(script):
    assert script['classify']('未识别', {'category': '行业ETF'}) == ('行业ETF', '人工指定')
    with pytest.raises(ValueError):
        script['classify']('未识别', {'category': 'invalid'})


@pytest.mark.parametrize('value', [0, -1, 'NaN', 'Infinity', None, 'bad'])
def test_invalid_sizes(script, value):
    assert script['positive'](value) is None
