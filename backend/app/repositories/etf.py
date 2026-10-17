from datetime import date

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, aliased

from app.models.etf import ETFShare


class ETFRepository:
    def __init__(self, session: Session):
        self.session = session

    def latest(self, codes: list[str], end: date):
        latest = select(ETFShare.fund_code, func.max(ETFShare.trade_date).label('day')).where(
            ETFShare.fund_code.in_(codes), ETFShare.trade_date <= end,
        ).group_by(ETFShare.fund_code).subquery()
        previous = aliased(ETFShare)
        return self.session.execute(select(ETFShare, previous.shares).join(latest, and_(
            ETFShare.fund_code == latest.c.fund_code, ETFShare.trade_date == latest.c.day,
        )).outerjoin(previous, and_(
            previous.fund_code == ETFShare.fund_code,
            previous.trade_date == ETFShare.previous_trade_date,
        ))).all()

    def history(self, codes: list[str], start: date, end: date):
        # Only transfer the two plotting fields and code, not audit metadata.
        return self.session.execute(select(ETFShare.fund_code, ETFShare.trade_date, ETFShare.shares).where(
            ETFShare.fund_code.in_(codes), ETFShare.trade_date >= start, ETFShare.trade_date <= end,
        ).order_by(ETFShare.fund_code, ETFShare.trade_date)).all()
