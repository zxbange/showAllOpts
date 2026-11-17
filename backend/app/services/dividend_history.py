"""Optional historical yields using the cash-cow annual pricing contract."""
import logging
import math

from app.services.cash_cow_combined import CombinedCashCowSource

LOG = logging.getLogger(__name__)


def enrich_history(records, *, client, settings, as_of, cache_dir, listed_dates, check_cancel,
                   source_factory=CombinedCashCowSource):
    # No historical value may retain the old current-price denominator.
    for record in records:
        for point in record['dividend_yield_history'][1:]:
            point['yield_pct'] = None
    if not records:
        return
    try:
        source = source_factory(client, settings=settings, as_of=as_of,
                                cache_dir=cache_dir, check_cancel=check_cancel)
    except Exception as exc:
        check_cancel()
        LOG.warning('Dividend annual history unavailable stage=configuration error_type=%s', type(exc).__name__)
        return
    for record in records:
        check_cancel()
        code, exchange = record['code'].split('.')
        symbol = code + { 'XSHG': '.SH', 'XSHE': '.SZ', 'XBSE': '.BJ' }[exchange]
        source.symbols[code] = symbol
        source.listed_dates[code] = listed_dates.get(symbol, '')
        year = record['dividend_fiscal_year']
        stage = 'implemented_dividends'
        try:
            dividends = source.per_share_dividends(code, year - 2, year - 1)
            # Unknown share basis or absent payouts must not become zero.
            if not any(_positive(dividends.get(y)) for y in (year - 1, year - 2)):
                LOG.warning('Dividend annual history unavailable symbol=%s years=%s,%s stage=dividends reason=missing_or_unverified_share_basis', symbol, year - 1, year - 2)
                continue
            stage = 'annual_raw_prices'
            prices = source.prices(code, year - 2, year - 1)
            for point in record['dividend_yield_history'][1:]:
                y = point['fiscal_year']
                dps, price = dividends.get(y), prices.get(y) or {}
                close = price.get('year_average_close')
                if (not _positive(dps) or not _positive(close)
                        or price.get('price_basis') != 'returned_unadjusted_trading_days'
                        or not str(price.get('price_date', '')).startswith(f'{y}-')):
                    LOG.warning('Dividend annual history unavailable symbol=%s year=%s stage=validation reason=missing_or_invalid_annual_input', symbol, y)
                    continue
                point['yield_pct'] = 100 * dps / close
                LOG.info('Dividend annual history calculated symbol=%s year=%s basis=implemented_dps_over_annual_raw_close_mean trading_days=%s missing_open_days=%s',
                         symbol, y, price.get('trading_days'), price.get('missing_open_days'))
        except Exception as exc:
            check_cancel()
            LOG.warning('Dividend annual history unavailable symbol=%s years=%s,%s stage=%s error_type=%s', symbol, year - 1, year - 2, stage, type(exc).__name__)


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
