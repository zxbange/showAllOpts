from datetime import date, timedelta
from decimal import Decimal

from app.core.etf_catalog import ETF_GROUPS, GROUP_NAMES
from app.repositories.etf import ETFRepository
from app.schemas.etf import ETFFund, ETFHistory, ETFPoint


def percent(value: int, base: int) -> float | None:
    return float((Decimal(value) / Decimal(base) - 1) * 100) if base > 0 else None


class ETFService:
    def __init__(self, repository: ETFRepository):
        self.repository = repository

    def history(self, group: str, days: int, end: date) -> ETFHistory:
        catalog = ETF_GROUPS[group]
        codes = [code for code, _ in catalog]
        snapshots = {row.fund_code: (row, previous) for row, previous in self.repository.latest(codes, end)}
        if not snapshots:
            return ETFHistory(group=group, group_name=GROUP_NAMES[group], funds=[
                ETFFund(code=code, name=name, points=[]) for code, name in catalog])
        anchor = max(row.trade_date for row, _ in snapshots.values())
        start = anchor - timedelta(days=days - 1) if days else date.min
        history = self.repository.history(codes, start, anchor)
        series = {code: {} for code in codes}
        for code, day, shares in history:
            series[code][day] = shares
        available = [values for values in series.values() if values]
        positive_dates = [{day for day, shares in values.items() if shares > 0} for values in available]
        common = set.intersection(*positive_dates) if positive_dates else set()
        baseline = min(common) if common else None
        all_dates = {day for values in available for day in values}
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
            available_funds=len(available),
        )
