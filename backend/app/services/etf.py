from datetime import date, timedelta
from decimal import Decimal, localcontext

from app.core.etf_catalog import ETF_GROUPS, GROUP_NAMES
from app.repositories.etf import ETFRepository
from app.services.holdings_calendar import trading_day
from app.services.momentum import china_today
from app.schemas.etf import ETFFund, ETFGroupOption, ETFHistory, ETFMarketValue, ETFPoint


def percent(value: int, base: int) -> float | None:
    return float((Decimal(value) / Decimal(base) - 1) * 100) if base > 0 else None


def possible_unit_changes(series, prices, prior_prices, previous_days, previous_shares, market_dates):
    """Conservative warning screen, not a corporate-action adjustment engine.

    Large genuine creations/redemptions may also be suppressed. Never correct a
    price/quantity ratio speculatively. Adjacent sessions cover end-of-day units
    changing one session before the trading price changes.
    """
    excluded = {}
    ordered = sorted(market_dates)
    following = dict(zip(ordered, ordered[1:]))

    def flag(code, day, reason):
        if day is not None:
            excluded.setdefault((code, day), set()).add(reason)

    for code, values in series.items():
        for day, shares in values.items():
            previous = previous_shares.get((code, day))
            if previous is not None and previous > 0 and (Decimal(shares) * 2 >= Decimal(previous) * 3 or Decimal(shares) * 3 <= Decimal(previous) * 2):
                flag(code, day, 'possible_share_unit_change')
                flag(code, following.get(day), 'possible_share_unit_change')
            price, prior = prices.get((code, day)), prior_prices.get((code, day))
            if price is not None and prior is not None and prior.is_finite() and price.is_finite() and prior > 0 and price > 0:
                if price * 4 >= prior * 5 or price * 5 <= prior * 4:
                    flag(code, day, 'possible_price_unit_change')
                    flag(code, previous_days.get((code, day)), 'possible_price_unit_change')
    return excluded


class ETFService:
    def __init__(self, repository: ETFRepository):
        self.repository = repository

    def history(self, group: str, days: int, end: date) -> ETFHistory:
        catalog = ETF_GROUPS[group]
        codes = [code for code, _ in catalog]
        options = [ETFGroupOption(id=key, name=name) for key, name in GROUP_NAMES.items()]
        snapshots = {row.fund_code: (row, previous) for row, previous in self.repository.latest(codes, end)}
        if not snapshots:
            return ETFHistory(group=group, group_name=GROUP_NAMES[group], group_options=options, funds=[
                ETFFund(code=code, name=name, points=[]) for code, name in catalog])
        anchor = max(row.trade_date for row, _ in snapshots.values())
        start = anchor - timedelta(days=days - 1) if days else date.min
        history = self.repository.history(codes, start, anchor, known_end=china_today())
        series = {code: {} for code in codes}
        context_series = {code: {} for code in codes}
        previous_days, previous_shares = {}, {}
        for code, day, shares, prior_day, prior_shares in history:
            context_series[code][day] = shares
            if start <= day <= anchor:
                series[code][day] = shares
            previous_days[code, day] = prior_day
            previous_shares[code, day] = prior_shares
        available = [values for values in series.values() if values]
        positive_dates = [{day for day, shares in values.items() if shares > 0} for values in available]
        common = set.intersection(*positive_dates) if positive_dates else set()
        baseline = min(common) if common else None
        all_dates = {day for values in available for day in values}
        context_dates = {day for values in context_series.values() for day in values}
        context_start, context_end = min(context_dates), max(context_dates)
        price_rows = self.repository.prices(codes, context_start, context_end)
        prices = {(code, day): price for code, day, price, _ in price_rows}
        prior_prices = {(code, day): prior for code, day, _, prior in price_rows}
        context_dates.update(day for _, day in prices)
        context_dates.update(self.repository.trading_days(context_start, context_end))
        day = context_start
        while day <= context_end:
            # Legacy snapshots without persisted sessions retain the verified 2026 fallback.
            if trading_day(day) is True:
                context_dates.add(day)
            day += timedelta(days=1)
        market_dates = {day for day in context_dates if start <= day <= anchor}
        excluded = possible_unit_changes(context_series, prices, prior_prices, previous_days, previous_shares, context_dates)
        market_values = []
        for day in sorted(market_dates):
            total = Decimal(0)
            present = 0
            flagged = [code for code in codes if (code, day) in excluded]
            reasons = sorted({reason for code in flagged for reason in excluded[code, day]})
            for code in codes:
                if (code, day) in excluded:
                    continue
                shares, price = series[code].get(day), prices.get((code, day))
                # No forward fill, NAV substitution, partial sums or share-only weights.
                if shares is not None and shares >= 0 and price is not None and price.is_finite() and price > 0:
                    with localcontext() as context:
                        context.prec = 48  # BIGINT shares x NUMERIC(20,8), plus all members.
                        total += Decimal(shares) * price
                    present += 1
            market_values.append(ETFMarketValue(date=day,
                market_value=format(total, 'f') if present == len(codes) else None,
                available_funds=present, expected_funds=len(codes),
                excluded_funds=flagged, quality_flags=reasons))
        funds = []
        for code, name in catalog:
            values = series[code]
            points = [ETFPoint(date=day, shares=str(shares), change_pct=(
                percent(shares, values[baseline]) if baseline and day >= baseline else None
            )) for day, shares in values.items()]
            fund = ETFFund(code=code, name=name, points=points)
            if code in snapshots:
                row, previous = snapshots[code]
                fund.date, fund.shares = row.trade_date, str(row.shares)
                fund.previous_date, fund.updated_at = row.previous_trade_date, row.updated_at
                fund.lagging = row.trade_date < anchor
                if previous is not None:
                    fund.shares_change = str(row.shares - previous)
                    fund.daily_change_pct = percent(row.shares, previous)
            funds.append(fund)
        return ETFHistory(
            group=group, group_name=GROUP_NAMES[group], start_date=start if days else min(all_dates),
            end_date=anchor, baseline_date=baseline, funds=funds, observed_days=len(all_dates),
            available_funds=len(available), market_values=market_values, group_options=options,
        )
