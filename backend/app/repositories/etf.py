from datetime import date

from sqlalchemy import and_, func, inspect, or_, select
from sqlalchemy.orm import Session, aliased

from app.models.etf import ETFPrice, ETFShare, ETFTradingDay


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

    def history(self, codes: list[str], start: date, end: date, known_end: date | None = None):
        # Expand one stored observation on each side for risk-screen consistency.
        # Each observation still joins its actual previous session, never a gap predecessor.
        before = select(ETFShare.fund_code, func.max(ETFShare.trade_date).label('day')).where(
            ETFShare.fund_code.in_(codes), ETFShare.trade_date < start,
        ).group_by(ETFShare.fund_code).subquery()
        after = select(ETFShare.fund_code, func.min(ETFShare.trade_date).label('day')).where(
            ETFShare.fund_code.in_(codes), ETFShare.trade_date > end,
            ETFShare.trade_date <= (known_end or end),
        ).group_by(ETFShare.fund_code).subquery()
        previous = aliased(ETFShare)
        return self.session.execute(select(ETFShare.fund_code, ETFShare.trade_date, ETFShare.shares,
            ETFShare.previous_trade_date, previous.shares).outerjoin(previous, and_(
                previous.fund_code == ETFShare.fund_code, previous.trade_date == ETFShare.previous_trade_date,
            )).outerjoin(before, before.c.fund_code == ETFShare.fund_code
            ).outerjoin(after, after.c.fund_code == ETFShare.fund_code).where(
            ETFShare.fund_code.in_(codes), or_(
                and_(ETFShare.trade_date >= start, ETFShare.trade_date <= end),
                ETFShare.trade_date == before.c.day, ETFShare.trade_date == after.c.day,
            ),
        ).order_by(ETFShare.fund_code, ETFShare.trade_date)).all()

    def prices(self, codes: list[str], start: date, end: date):
        # Old source databases and immutable snapshots may not have prices yet.
        # Missing table is an explicit empty series; query/permission errors still surface.
        if not inspect(self.session.connection()).has_table(ETFPrice.__tablename__):
            return []
        previous = aliased(ETFPrice)
        return self.session.execute(select(ETFPrice.fund_code, ETFPrice.trade_date, ETFPrice.close_price,
            previous.close_price).outerjoin(ETFShare, and_(ETFShare.fund_code == ETFPrice.fund_code,
            ETFShare.trade_date == ETFPrice.trade_date)).outerjoin(previous, and_(
            previous.fund_code == ETFPrice.fund_code, previous.trade_date == ETFShare.previous_trade_date)).where(
            ETFPrice.fund_code.in_(codes), ETFPrice.trade_date >= start, ETFPrice.trade_date <= end,
        ).order_by(ETFPrice.fund_code, ETFPrice.trade_date)).all()

    def trading_days(self, start: date, end: date):
        if not inspect(self.session.connection()).has_table(ETFTradingDay.__tablename__):
            return []
        return self.session.scalars(select(ETFTradingDay.trade_date).where(
            ETFTradingDay.trade_date >= start, ETFTradingDay.trade_date <= end,
        ).order_by(ETFTradingDay.trade_date)).all()
