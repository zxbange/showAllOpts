import math
import logging
from datetime import date

LOG = logging.getLogger(__name__)

def listed_three_years(listed, as_of):
    """The third anniversary is Feb 28 for a Feb 29 listing."""
    listed = date.fromisoformat(listed)
    as_of = date.fromisoformat(as_of)
    try:
        anniversary = listed.replace(year=listed.year + 3)
    except ValueError:
        anniversary = listed.replace(year=listed.year + 3, day=28)
    return as_of >= anniversary


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def evaluate_year(year, dividend, financial):
    """A known failing predicate is a rejection, null data are never zero."""
    dividend = dividend or {}
    financial = financial or {}
    cash = dividend.get('cash_dividend')
    adjusted = financial.get('adjusted_profit')
    profit = financial.get('net_profit')
    operating = financial.get('operating_cashflow')
    if any(numeric(value) and value <= 0 for value in (cash, adjusted, profit)):
        return 'excluded', None
    if numeric(cash) and numeric(operating) and operating < cash:
        return 'excluded', None
    payout = 100 * cash / profit if numeric(cash) and numeric(profit) and profit > 0 else None
    if payout is not None and not 30 <= payout <= 100:
        return 'excluded', None
    if not all(numeric(value) for value in (cash, adjusted, profit, operating)):
        return 'insufficient', None
    return 'eligible', dict(year=year, cash_dividend=cash, adjusted_profit=adjusted,
                           net_profit=profit, operating_cashflow=operating,
                           payout_ratio=payout, cashflow_coverage=operating / cash)


def finish_stock(stock, years, dividends, prices, period=10):
    """Dividend per share and unadjusted close must share a valid share basis."""
    window = [row['year'] for row in years]
    if len(window) != period or sorted(window) != list(range(min(window), min(window) + period)):
        return 'insufficient', None
    result = []
    for annual in years:
        year = annual['year']
        dps = dividends.get(year)
        price = prices.get(year) or {}
        close = price.get('year_average_close')
        day = price.get('price_date')
        # New source uses the mean of returned actual trading days, not a
        # mandatory December close. Legacy adapters retain their existing check.
        if (not numeric(dps) or dps <= 0 or not numeric(close) or close <= 0
                or not isinstance(day, str) or not day.startswith(f'{year}-')
                or (price.get('price_basis') != 'returned_unadjusted_trading_days'
                    and not day.startswith(f'{year}-12-'))):
            return 'insufficient', None
        result.append(dict(annual, dividend_per_share=dps, year_average_close=close,
                           price_date=day, dividend_yield=100 * dps / close,
                           price_basis=price.get('price_basis'), trading_days=price.get('trading_days'),
                           missing_open_days=price.get('missing_open_days'),
                           suspension_verified=price.get('suspension_verified')))
    average = sum(row['dividend_yield'] for row in result) / period
    if average <= 4:
        return 'excluded', None
    return 'matched', dict(code=stock['code'], name=stock['name'], exchange=stock['exchange'],
                           average_yield=average,
                           payout_min=min(row['payout_ratio'] for row in result),
                           payout_max=max(row['payout_ratio'] for row in result),
                           coverage_min=min(row['cashflow_coverage'] for row in result),
                           years=result)


PERIODS = (3, 5, 10)

def screen_cash_cows(source, end_year, progress, check_cancel=lambda: None):
    stocks = source.universe()
    if not stocks:
        raise ValueError('股票池为空，未替换已有结果。')
    universe = {stock['code']: stock for stock in stocks}
    if len(universe) != len(stocks):
        raise ValueError('股票池代码重复，未替换已有结果。')
    pending = dict(universe)
    annual = {code: [] for code in universe}
    candidates = {period: set() for period in PERIODS}
    outcomes = {period: {} for period in PERIODS}

    # The production source has already reconciled the full, unfiltered pool.
    # Preserve excluded records in totals without fetching any financial/price
    # data for them. Legacy test/import adapters without dates retain behavior.
    for code, stock in list(pending.items()):
        if 'list_date' in stock and not listed_three_years(stock['list_date'], source.as_of):
            pending.pop(code)
            for period in PERIODS:
                outcomes[period][code] = 'excluded'
    LOG.info('Cash cow listing age filter total=%s eligible=%s excluded=%s',
             len(stocks), len(pending), len(stocks)-len(pending))

    def reject(code, outcome, depth):
        pending.pop(code)
        # An older failure belongs only to windows containing that
        # year. Shorter windows' survivors are already frozen.
        for period in PERIODS:
            if period >= depth:
                outcomes[period][code] = outcome

    # Cheap, newest-first annual predicates are evaluated once. At
    # each boundary freeze the eligible set, then prune only longer
    # windows. Never prune by a shorter window's average yield.
    for depth in range(1, max(PERIODS) + 1):
        if not pending:
            break
        year = end_year - depth + 1
        # A partial listing year cannot count as a full fiscal year, even if
        # the issuer publishes pre-IPO financials. Freeze shorter windows first.
        for code in list(pending):
            listed = universe[code].get('list_date')
            if listed and listed > f'{year}-01-01':
                reject(code, 'insufficient', depth)
        if not pending:
            break
        progress(f'初筛{year}年分红与财报（3/5/10年共用）',
                 len(stocks) - len(pending), len(stocks))
        dividends = source.dividends(year, codes=list(pending))
        for code in list(pending):
            cash = (dividends.get(code) or {}).get('cash_dividend')
            if numeric(cash) and cash <= 0:
                reject(code, 'excluded', depth)
        financials = source.financials(year, codes=list(pending)) if pending else {}
        for code in list(pending):
            outcome, row = evaluate_year(year, dividends.get(code), financials.get(code))
            if outcome == 'eligible':
                annual[code].append(row)
            else:
                reject(code, outcome, depth)
        if depth in candidates:
            candidates[depth] = set(pending)

    items = {period: [] for period in PERIODS}
    survivors = set().union(*candidates.values())
    for index, code in enumerate(sorted(survivors)):
        check_cancel()
        stock = universe[code]
        periods = [period for period in PERIODS if code in candidates[period]]
        progress(f'核对候选{stock["name"]}的股息率（共用分红与行情）',
                 len(stocks) - len(survivors) + index, len(stocks))
        dps = source.per_share_dividends_by_period(code, end_year, periods)
        usable = []
        for period in periods:
            window = range(end_year - period + 1, end_year + 1)
            if all(numeric(dps[period].get(year)) and dps[period][year] > 0 for year in window):
                usable.append(period)
            else:
                outcomes[period][code] = 'insufficient'
        # Invalid share basis needs no expensive historical prices.
        if not usable:
            continue
        prices = source.prices(code, end_year - max(usable) + 1, end_year)
        matched = []
        for period in usable:
            rows = sorted((row for row in annual[code] if row['year'] > end_year - period),
                          key=lambda row: row['year'])
            outcome, item = finish_stock(stock, rows, dps[period], prices, period)
            outcomes[period][code] = outcome
            if outcome == 'matched':
                matched.append(item)
                items[period].append(item)
        if matched:
            current = source.current_dividend_yield(code)
            for item in matched:
                item.update(current)
                # The latest fiscal year remains the current static yield.
                # Older entries reuse the exact annual-detail result, not a
                # fresh DPS/current-price calculation or rounded UI values.
                annual_yields = {row['year']: row['dividend_yield'] for row in item['years']}
                item['dividend_yield_history'] = [
                    dict(fiscal_year=year, yield_pct=(current.get('current_yield')
                         if year == end_year else annual_yields.get(year)))
                    for year in range(end_year, end_year - 3, -1)
                ]
    return items, outcomes, stocks
