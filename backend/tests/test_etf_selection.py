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
    env['MIN_SIZE_YI'] = Decimal('0')  # Small fixtures isolate ranking from production threshold.
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


@pytest.mark.parametrize('value,passes', [('2999999999', False), ('3000000000', False), ('3000000001', True)])
def test_strict_30_yi_yuan_boundary(script, value, passes):
    script['MIN_SIZE_YI'] = Decimal('30')
    day = dt.date(2026, 10, 19)
    selected, audit, candidates = script['select_largest']([row('a')],
        {'a': {day: (Decimal(value), 1, 1)}}, [day])
    assert bool(selected) is passes
    assert bool(audit) is not passes
    assert len(candidates) == 1


@pytest.mark.parametrize('code,name,group', [
    ('518880.XSHG', '黄金ETF', 'commodity:gold'),
    ('159980.XSHE', '有色期货', 'commodity:nonferrous'),
    ('159985.XSHE', '豆粕ETF', 'commodity:soymeal'),
    ('501018.XSHG', '南方原油', 'commodity:oil'),
    ('other', '黄金ETF', 'commodity:gold'),
])
def test_commodity_direction_fallback(script, code, name, group):
    assert script['commodity_direction'](code, name)[1] == group


def test_gold_equity_not_commodity(script):
    assert script['commodity_direction']('other', '黄金股ETF') is None


def test_exact_cross_border_and_bond_allowlists(script):
    assert script['CROSS_BORDER_CODES'] == {
        '513100.XSHG', '159509.XSHE', '513030.XSHG', '513520.XSHG',
        '513690.XSHG', '513130.XSHG', '159550.XSHE'}
    for code in script['CROSS_BORDER_CODES']:
        assert script['permitted'](code, '跨境ETF') is None
    assert script['permitted']('513500.XSHG', '跨境ETF')
    assert script['permitted']('511010.XSHG', '债券ETF')
    assert script['permitted']('511220.XSHG', '债券ETF') is None

@pytest.mark.parametrize('net_assets,expected', [('1224223000', False), ('3100000000', True)])
def test_lof_disclosed_class_assets(script, net_assets, expected):
    from types import SimpleNamespace
    class Field:
        def __eq__(self, value): return ('code', value)
        def __le__(self, value): return ('before', value)
    class Frame:
        empty = False
        def to_dict(self, orient):
            return [dict(id=1, code='501018', period_end='2026-06-30', pub_date='2026-08-31',
                         total_tna=net_assets, nav='1.4937')]
    calls = []
    def read(table, filters):
        calls.append(filters)
        return Frame()
    script['finance'] = SimpleNamespace(FUND_FIN_INDICATOR=SimpleNamespace(
        code=Field(), pub_date=Field(), period_end=Field()))
    script['pd'] = SimpleNamespace(Timestamp=lambda value: SimpleNamespace(date=lambda: dt.date.fromisoformat(str(value))))
    script['read_pages'] = read
    script['MIN_SIZE_YI'] = Decimal('30')
    record = row('501018.XSHG', 'commodity:oil', '商品ETF')
    chosen, audit, candidates = script['load_lof_reports']([record], dt.date(2026, 10, 19))
    assert bool(chosen) is expected
    assert bool(audit) is not expected
    assert candidates[0]['size_yuan'] == Decimal(net_assets)
    assert candidates[0]['size_date'] == '2026-06-30'
    assert candidates[0]['size_pub_date'] == '2026-08-31'
    assert candidates[0]['shares'] is None
    assert calls[0][0] == ('code', '501018')
    # Old reports must not silently satisfy today's size threshold.
    chosen, audit, candidates = script['load_lof_reports']([record], dt.date(2027, 2, 1))
    assert not chosen and not candidates and '过旧' in audit[0]['reason']

def test_domestic_lof_scope_and_missing_master(script):
    import pandas as pd
    from types import SimpleNamespace
    class Field:
        def in_(self, values): return values
        def __le__(self, value): return value
    universe=pd.DataFrame({'display_name':['国内股票','国内混合','全球QDII','纯债','未知','缺失'],
                           'start_date':[dt.date(2020,1,1)]*6}, index=['160001.XSHE','160002.XSHE','160003.XSHE','160004.XSHE','160005.XSHE','160006.XSHE'])
    frame=pd.DataFrame([dict(main_code='16000'+str(i),name=name,underlying_asset_type=asset)
                       for i,name,asset in [(1,'股票基金','股票型'),(2,'混合基金','混合型'),(3,'海外QDII','股票型'),(4,'债券基金','债券型'),(5,'未知基金','其他')]])
    script['pd']=pd
    script['finance']=SimpleNamespace(FUND_MAIN_INFO=SimpleNamespace(main_code=Field(),start_date=Field()))
    script['read_pages']=lambda *args:frame
    rows,audit=script['lof_catalog'](universe,dt.date(2026,10,20))
    assert [r['code'] for r in rows]==['160001.XSHE','160002.XSHE']
    assert len(audit)==4
    assert rows[0]['group']!=rows[1]['group']
    assert rows[0]['listed_date']=='2020-01-01'
