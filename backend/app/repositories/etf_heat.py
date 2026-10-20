from datetime import timedelta
from sqlalchemy import select, func
from app.models.etf_heat import Heat, Pool


class ETFHeatRepository:
    def __init__(self, session):
        self.session = session

    def head(self):
        return self.session.get(Pool, 1)

    def day(self, day, codes):
        return self.session.scalars(select(Heat).where(Heat.trade_date == day, Heat.code.in_(codes))).all()

    def previous_day(self, day):
        return self.session.scalar(select(func.max(Heat.trade_date)).where(Heat.trade_date < day))

    def history(self, code, end, days):
        return self.session.scalars(select(Heat).where(Heat.code == code, Heat.trade_date <= end,
            Heat.trade_date >= end-timedelta(days=days-1)).order_by(Heat.trade_date)).all()

    def calendar(self, start, end):
        return self.session.scalars(select(Heat.trade_date).where(Heat.trade_date >= start,
            Heat.trade_date <= end).distinct().order_by(Heat.trade_date)).all()
