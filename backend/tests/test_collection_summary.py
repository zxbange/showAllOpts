from datetime import date
from decimal import Decimal

from app.services.collection_summary import summarize_dividend, summarize_market, summarize_published

D1 = '2026-11-19'
D2 = '2026-11-20'
D3 = '2026-11-23'


def body(lines):
    assert len(lines) <= 12 and all(len(line) <= 400 for line in lines)
    return '\n'.join(lines)


def test_margin_reports_previous_published_sample_and_preserves_zero_change():
    values = {D1: '256.50', D2: '256.50', '2026-11-24': 'invalid-URL-token'}
    output = body(summarize_published('margin', values))
    assert '256.5%' in output and D2 in output and f'对比日 {D1}' in output
    assert '变动 0个百分点' in output and 'token' not in output
    assert '未取得已发布样本' in body(summarize_published('margin', {}))


def test_growth_pairs_same_date_no_mixed_ratio_even_with_later_partial_close():
    values = {(D1, '399006'): 100, (D1, '000922'): 200, (D1, '000688'): 75,
              (D2, '399006'): 200, (D2, '000922'): 250, (D2, '000688'): 100,
              (D3, '399006'): 300}
    text = summarize_published('growth_dividend', values)
    assert D3 in text[0] and '300点' in text[0]
    assert f'0.8（{D2}；对比日 {D1}；变动 +0.3' in text[3]
    assert f'0.4（{D2}' in text[4]


def test_commodity_deltas_are_same_field_and_ignore_forged_upstream_change():
    values = {D1: {'low': 100, 'high': 200, 'mean': 150},
              D2: {'low': 100, 'high': None, 'mean': 155},
              D3: {'low': 99, 'high': 209, 'mean': 155, 'change': 1000}}
    output = body(summarize_published('commodities', values))
    assert f'最低：99(-1)（对比日 {D2}）' in output
    assert f'最高：209(+9)（对比日 {D1}）' in output
    assert f'均价：155(0)（对比日 {D2}）' in output and '1000' not in output


def test_coal_has_grade_specific_dates_and_no_fake_values():
    values = {(D1, 'CCI5500'): {'price': 802},
              (D2, 'CCI5500'): {'price': 797},
              (D3, 'CCI4500'): {'price': 480}}
    output = body(summarize_published('cci', values))
    assert f'CCI5500：797(-5) 元/吨（{D2}；对比日 {D1}）' in output
    assert 'CCI5000：—（未取得已发布报价）' in output
    assert f'CCI4500：480(—) 元/吨（{D3}；对比日 —）' in output


def test_market_metrics_have_independent_dates_counts_and_correct_percentage_points():
    samples = {'heat': {date.fromisoformat(D1): {'ratio': Decimal('0.4123')},
                        D2: {'ratio': '0.4444', 'above_count': 4444, 'eligible_count': 10000},
                        D3: {'ratio': 'nan', 'above_count': 20231122}},
               'crowding': {D1: {'ratio': '0.385', 'top_count': 5, 'stock_count': 100,
                                 'top_amount': '3850', 'total_amount': '10000'}}}
    output = body(summarize_market(samples))
    assert f'市场热度：44.44%（{D2}；对比日 {D1}；变动 +3.21个百分点）' in output
    assert '站上 MA20：4444 / 10000 只' in output and '20231122' not in output
    assert f'抱团拥挤度：38.5%（{D1}；对比日 —；变动 —）' in output
    assert '成交额 3850 / 10000 元' in output


def test_valuation_uses_comparable_dates_for_each_index_and_leaves_missing_metric_unknown():
    values = {(D1, '000300.XSHG'): {'index_close': 4200, 'mean_pe_ttm': 15.2, 'median_pe_ttm': 20},
              (D3, '000300.XSHG'): {'index_close': 4180, 'mean_pe_ttm': 15, 'mean_pe_lyr': 14},
              (D2, '000016.XSHG'): {'index_close': 3000, 'mean_pe_ttm': 11.2},
              (D1, 'arbitrary-secret-index'): {'index_close': 1}}
    output = body(summarize_published('valuation_ruler', values))
    assert f'沪深300：{D3}；对比日 {D1}' in output
    assert '指数 4180(-20)点' in output and '滚动等权PE 15(-0.2)倍' in output
    assert '滚动中位PE —' in output and '静态等权PE 14(—)倍' in output
    assert f'上证50：{D2}；对比日 —' in output and '中证1000：—' in output
    assert 'secret' not in output


def test_dividend_and_cash_counts_distinguish_verified_zero_from_unavailable():
    sample = {'trade_date': D3, 'groups': {'value': {'count': 0, 'items': []},
              'bank': {'count': 1, 'items': [{'dividend_yield_1y': '9.1', 'name': 'hidden-token'}]}}}
    earlier = {'trade_date': D2, 'nonbank_count': 4, 'bank_count': 1}
    output = body(summarize_dividend(sample, earlier))
    assert f'{D3}；对比日 {D2}' in output and '非银行：入选 0 只（变动 -4）' in output
    assert '银行：入选 1 只（变动 0）；最高静态股息率 9.1%' in output and 'token' not in output
    assert '对比日 —' in body(summarize_dividend(sample, sample))
    assert '最新已发布选股：—' in body(summarize_dividend({'date': 'fake-date'}))
    current = {3: {'snapshot': {'as_of': D3, 'matched_count': 0}}}
    previous = {3: {'snapshot': {'as_of': D2, 'matched_count': 7}}}
    output = body(summarize_published('cash_cow', current, previous=previous))
    assert f'近3年：入选 0 只（筛选日 {D3}；对比日 {D2}；变动 -7）' in output
    assert '近5年：入选 — 只（筛选日 —；对比日 —；变动 —）' in output


def test_invalid_or_extreme_values_do_not_become_numbers_or_raw_text():
    bad = {D1: '1E+80000000', D2: True, D3: 'secret\nother recipient'}
    assert body(summarize_published('margin', bad)) == '两融担保比例：—（未取得已发布样本）'
