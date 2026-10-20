from datetime import timedelta
from sqlalchemy import select, func
from app.models.etf_heat import Bar, Heat, Pool
from app.services.etf_calculation import RAW_VERSION, calculate_rows
from types import SimpleNamespace


class ETFHeatRepository:
    def __init__(self, session):
        self.session = session
        self._computed = None

    def head(self):
        return self.session.get(Pool, 1)

    def computed(self):
        # Cache-disabled installations remain usable; production uses prepared SQLite scores.
        if 'cache_updated_at' in self.session.info:
            return None
        head = self.head()
        if not head or head.rule_version != RAW_VERSION:
            return None
        if self._computed is None:
            self._computed = [SimpleNamespace(**r) for r in calculate_rows(self.session.scalars(select(Bar)).all())]
        return self._computed

    def day(self, day, codes):
        rows = self.computed()
        if rows is not None:
            return [r for r in rows if r.trade_date == day and r.code in codes]
        return self.session.scalars(select(Heat).where(Heat.trade_date == day, Heat.code.in_(codes))).all()

    def previous_day(self, day):
        rows = self.computed()
        if rows is not None:
            return max((r.trade_date for r in rows if r.trade_date < day), default=None)
        return self.session.scalar(select(func.max(Heat.trade_date)).where(Heat.trade_date < day))

    def history(self, code, end, days):
        rows = self.computed()
        if rows is not None:
            return sorted((r for r in rows if r.code == code and end-timedelta(days=days-1) <= r.trade_date <= end), key=lambda r:r.trade_date)
        return self.session.scalars(select(Heat).where(Heat.code == code, Heat.trade_date <= end,
            Heat.trade_date >= end-timedelta(days=days-1)).order_by(Heat.trade_date)).all()

    def calendar(self, start, end):
        rows = self.computed()
        if rows is not None:
            return sorted({r.trade_date for r in rows if start <= r.trade_date <= end})
        return self.session.scalars(select(Heat.trade_date).where(Heat.trade_date >= start,
            Heat.trade_date <= end).distinct().order_by(Heat.trade_date)).all()
