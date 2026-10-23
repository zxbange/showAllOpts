from sqlalchemy import select
from app.models.short_term import ShortTermRun, ShortTermStock


class ShortTermRepository:
    def __init__(self, session):
        self.session = session

    def runs(self):
        return self.session.scalars(select(ShortTermRun).order_by(ShortTermRun.trade_date.desc()).limit(3)).all()

    def stocks(self, day):
        return self.session.scalars(select(ShortTermStock).where(ShortTermStock.trade_date == day).order_by(ShortTermStock.code)).all()
