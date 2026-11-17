"""Current public screen publishes full independently ranked 50/20 groups."""
from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from app.services.dividend_collection import DividendCollectionError, validate_result, write_result
from app.services.dividend_public_source import rank_records


def expanded_result(nonbank=55, bank=25):
    stocks, valuations, dividends, industries = [], {}, {}, {}
    for method, count, start in [('value', nonbank, 600000), ('bank', bank, 601000)]:
        for i in range(count):
            symbol = f'{start + i}.SH'
            stocks.append(dict(symbol=symbol, name=symbol))
            valuations[symbol] = dict(pe_ttm=7, total_mv=1000000, close=10, pb=1)
            dividends[symbol] = dict(fiscal_year=2025, annual_dps={2025: Decimal('1') - Decimal(i) / 100,
                                                                               2024: Decimal('.4'), 2023: Decimal('.3')})
            industries[symbol] = dict(l1_code='801780.SI' if method == 'bank' else '801160.SI', l1_name=method)
    return rank_records(stocks[::-1], valuations, dividends, industries, date(2026, 11, 16), 5500)


def test_expanded_ranking_is_independent_and_preserves_thresholds():
    meta, rows = expanded_result()
    assert meta['value_count'] == 70 and meta['value_candidates'] == 80
    for method, count, start in [('value', 50, 600000), ('bank', 20, 601000)]:
        selected = [r for r in rows if r['method'] == method]
        assert [r['rank'] for r in selected] == list(range(1, count + 1))
        assert [r['code'] for r in selected] == [f'{start+i}.XSHG' for i in range(count)]
        assert all(r['dividend_yield_1y'] > 3 and 0 < r['pe_ttm'] < 12 for r in selected)
    validate_result(meta, rows)


def test_small_groups_are_not_padded():
    meta, rows = expanded_result(3, 2)
    assert meta['value_count'] == len(rows) == 5
    validate_result(meta, rows)


@pytest.mark.parametrize('method,limit', [('value', 50), ('bank', 20)])
def test_publication_rejects_each_group_over_limit(method, limit):
    meta, rows = expanded_result()
    extra = deepcopy(next(r for r in rows if r['method'] == method))
    extra.update(code='688888.XSHG', rank=limit + 1)
    rows.append(extra)
    meta['value_count'] += 1
    with pytest.raises(DividendCollectionError, match='ranks'):
        validate_result(meta, rows)


def test_all_seventy_records_survive_database_and_api(engine, client):
    meta, rows = expanded_result()
    assert write_result(engine, meta, rows) == 70
    response = client.get('/api/v1/dividend-selection/latest')
    assert response.status_code == 200
    payload = response.json()
    assert payload['snapshot']['value_count'] == 70
    assert {g['id']: len(g['items']) for g in payload['groups']} == {'value': 50, 'bank': 20}
    assert [g['items'][-1]['rank'] for g in payload['groups']] == [50, 20]
