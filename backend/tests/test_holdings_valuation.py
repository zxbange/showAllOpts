from decimal import Decimal, localcontext

import pytest

from app.services.holdings_valuation import quote_symbol, value_positions


@pytest.mark.parametrize(('raw', 'expected'), [
    ('600000.XSHG', '600000.SH'), ('600001.SS', '600001.SH'),
    ('605001.ss', '605001.SH'), ('000001.SS', None), ('510300.SS', None),
    ('sh.600000', '600000.SH'), ('sz.000001', '000001.SZ'),
    ('SHSE.600000', '600000.SH'), ('SZSE.000001', '000001.SZ'),
    ('600000.SSE', '600000.SH'), ('600000.SHSE', '600000.SH'),
    ('000001.SZSE', '000001.SZ'), ('920001.BSE', '920001.BJ'),
    ('BSE.920001', '920001.BJ'), ('bj.830001', '830001.BJ'),
    ('XSHG.600000', '600000.SH'), ('XSHE.000001', '000001.SZ'),
    ('SHSE.000001', None), ('SZSE.600000', None), ('BSE.600000', None),
    ('sh.510300', None), ('sz.159919', None), ('600000.HK', None),
    ('SH.600000.SS', None), ('600000..SH', None), ('1', None), ('000001.XSHE', '000001.SZ'),
    (' 001001.sz ', '001001.SZ'), ('SH688001', '688001.SH'),
    ('920001.BJ', '920001.BJ'), ('920001', '920001.BJ'),
    ('301001', '301001.SZ'), ('605001.SH', '605001.SH'),
    ('000001.SH', None), ('600000.XSHE', None), ('bj600000', None),
    ('830001.BJ', '830001.BJ'), ('bj430047', '430047.BJ'),
    ('870001.BJ', '870001.BJ'), ('830001', None), ('830001.SH', None), ('510300.SH', None), ('159919.SZ', None),
    ('110001.SH', None), ('900901.SH', None), ('200001.SZ', None),
    ('600000.HK', None), ('00700.HK', None), ('T600000.SH', None),
    ('600000.0', None), ('０００００１', None), (1, None),
])
def test_quote_symbol(raw, expected):
    assert quote_symbol(raw) == expected


def row(strategy, code, amt):
    return dict(strategy2=strategy, code=code, amt=amt)


def test_exact_rounding_and_missing_price_is_per_strategy():
    rows = [
        row('committed', '600000.XSHG', '.003'), row('committed', 'sh600000', '.003'),
        row('unknown', '600000.XSHG', '100'), row('unknown', '510300.SH', '50'),
        row('invalid', '301001.SZ', '1'),
    ]
    with localcontext() as ctx:
        ctx.prec = 3
        prices, totals = value_positions(rows, {'600000.SH': Decimal('1.00'), '301001.SZ': 'NaN'})
    assert prices == {'600000.XSHG': '1', 'sh600000': '1'}
    assert totals == {'committed': '0.01', 'unknown': None, 'invalid': None}


@pytest.mark.parametrize('price', ['0', '-1', True, 'inf', '0.0000000000001'])
def test_bad_quote_never_publishes_partial_value(price):
    assert value_positions([row('x', '000001', '1')], {'000001.SZ': price}) == ({}, {'x': None})


def test_large_quantity_exact():
    _, totals = value_positions([row('x', '000001', '999999999999999999.999999')],
                                 {'000001.SZ': '99.123456789012'})
    assert totals == {'x': '99123456789012000000.00'}


def test_beijing_legacy_requires_explicit_identity_and_matching_close():
    positions = [row('explicit', '830001.BJ', '2'), row('bare', '830001', '3')]
    assert value_positions(positions, {'830001.BJ': '3.42'}) == (
        {'830001.BJ': '3.42'}, {'explicit': '6.84', 'bare': None}
    )
    assert value_positions(positions, {'920001.BJ': '3.42'}) == (
        {}, {'explicit': None, 'bare': None}
    )


def test_shanghai_ss_alias_uses_existing_same_date_market_quotes():
    positions = [row('x', '600001.SS', '2'), row('x', '605001.ss', '3')]
    prices, totals = value_positions(positions, {'600001.SH': '12.50', '605001.SH': '4.20'})
    assert prices == {'600001.SS': '12.5', '605001.ss': '4.2'}
    assert totals == {'x': '37.60'}
