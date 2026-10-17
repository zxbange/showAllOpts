from datetime import date, timedelta
from fastapi import HTTPException
from app.schemas.earnings import EarningsOverview, StockDetail, StockSummary
from app.services.momentum import china_today


def latest_period():
    today = china_today()
    return date(today.year, ((today.month-1)//3)*3+1, 1)-timedelta(days=1)


def validate_period(period):
    if period > latest_period() or (period.month, period.day) not in ((3, 31), (6, 30), (9, 30), (12, 31)):
        raise HTTPException(422, '报告期必须为已结束季度的最后一天。')
    return period


class EarningsService:
    def __init__(self, repository):
        self.repository = repository

    def overview(self, period, search, industry, source, sort, direction, page, page_size):
        target = validate_period(period or latest_period())
        run = self.repository.run(target)
        result = EarningsOverview(target_period=target, periods=self.repository.periods(),
                                  run=run, page=page, page_size=page_size)
        if run is None:
            return result  # Do not silently fall back to last quarter.
        result.industries = self.repository.industries(target)
        total, rows = self.repository.listing(target, search, industry, source, sort, direction, page, page_size)
        result.total = total
        result.items = [StockSummary.model_validate(row) for row in rows]
        return result

    def detail(self, period, code):
        validate_period(period)
        row = self.repository.detail(period, code)
        if row is None:
            raise HTTPException(404, '该季度没有这只股票的入选记录。')
        return StockDetail.model_validate(row)
