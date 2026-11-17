import pytest

from app.services.dividend_history import enrich_history


def rows():
    return [dict(code='600900.XSHG', dividend_fiscal_year=2025, dividend_yield_1y=6,
                 dividend_yield_history=[dict(fiscal_year=y, yield_pct=6) for y in (2025, 2024, 2023)])]


class Source:
    def __init__(self, *args, **kwargs):
        self.symbols, self.listed_dates = {}, {}
    def per_share_dividends(self, code, start, end):
        assert (start, end) == (2023, 2024)
        return {2024: 1., 2023: .6}
    def prices(self, code, start, end):
        assert self.symbols[code] == '600900.SH'
        return {y: dict(year_average_close=p, price_date=f'{y}-12-29',
                        price_basis='returned_unadjusted_trading_days', trading_days=240)
                for y,p in [(2024, 20.), (2023, 10.)]}


def run(records, factory=Source, check=lambda: None):
    enrich_history(records, client=None, settings=None, as_of='2026-11-17', cache_dir=None,
                   listed_dates={'600900.SH': '20031118'}, check_cancel=check, source_factory=factory)


def test_same_cash_cow_annual_formula_preserves_current_yield():
    records = rows()
    run(records)
    assert [p['yield_pct'] for p in records[0]['dividend_yield_history']] == [6, 5, 6]
    assert records[0]['dividend_yield_1y'] == 6


def test_transport_failure_leaves_old_years_null_not_current_price_values(caplog):
    class Failure(Source):
        def prices(self, *args): raise RuntimeError('secret should not be logged')
    records = rows()
    run(records, Failure)
    assert [p['yield_pct'] for p in records[0]['dividend_yield_history']] == [6, None, None]
    assert 'stage=annual_raw_prices' in caplog.text
    assert 'secret' not in caplog.text


def test_unverified_share_basis_does_not_fetch_prices():
    class Missing(Source):
        def per_share_dividends(self, *args): return {2024: None, 2023: None}
        def prices(self, *args): pytest.fail('should skip prices')
    records = rows(); run(records, Missing)
    assert records[0]['dividend_yield_history'][1]['yield_pct'] is None


def test_only_valid_annual_inputs_publish():
    class Partial(Source):
        def per_share_dividends(self, *args): return {2024: float('nan'), 2023: .6}
    records = rows(); run(records, Partial)
    assert [p['yield_pct'] for p in records[0]['dividend_yield_history']] == [6, None, 6]


def test_cancellation_is_not_swallowed():
    def cancel(): raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError, match='cancelled'):
        run(rows(), check=cancel)
