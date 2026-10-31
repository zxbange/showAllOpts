from sqlalchemy import select
from app.models.short_term import ShortTermRun, ShortTermStock


class ShortTermRepository:
    def __init__(self, session):
        self.session = session

    def runs(self):
        return self.session.scalars(select(ShortTermRun).order_by(ShortTermRun.trade_date.desc()).limit(6)).all()

    def stocks(self, day, method, offset, limit):
        # Filter the small strategy list first; only load candles for this page.
        index = self.session.execute(select(ShortTermStock.code, ShortTermStock.payload['methods'])
                                     .where(ShortTermStock.trade_date == day).order_by(ShortTermStock.code))
        codes = [code for code, methods in index if method in (methods or [])]
        page = codes[offset:offset + limit]
        if not page:
            return len(codes), []
        stocks = self.session.scalars(select(ShortTermStock)
                                      .where(ShortTermStock.trade_date == day, ShortTermStock.code.in_(page))
                                      .order_by(ShortTermStock.code)).all()
        return len(codes), stocks
