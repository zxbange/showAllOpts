from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.momentum import IndustryMomentum as Row


class MomentumRepository:
    def __init__(self, session: Session):
        self.session = session

    def latest_date(self, industry_type: str, on_or_before: date) -> date | None:
        return self.session.scalar(select(func.max(Row.trade_date)).where(
            Row.industry_type == industry_type, Row.trade_date <= on_or_before,
        ))

    def snapshot(self, industry_type: str, day: date) -> list[Row]:
        return list(self.session.scalars(select(Row).where(
            Row.industry_type == industry_type, Row.trade_date == day,
        ).order_by(Row.momentum.desc(), Row.industry_code.desc())))

    def identity(self, industry_type: str, code: str, end: date) -> Row | None:
        return self.session.scalar(select(Row).where(
            Row.industry_type == industry_type, Row.industry_code == code,
            Row.trade_date <= end,
        ).order_by(Row.trade_date.desc()).limit(1))

    def history(self, industry_type: str, code: str, start: date, end: date) -> list[Row]:
        return list(self.session.scalars(select(Row).where(
            Row.industry_type == industry_type, Row.industry_code == code,
            Row.trade_date.between(start, end),
        ).order_by(Row.trade_date)))

    def observed_dates(self, industry_type: str, start: date, end: date) -> list[date]:
        # This is the dates present in the source, not an invented trading calendar.
        return list(self.session.scalars(select(Row.trade_date).where(
            Row.industry_type == industry_type, Row.trade_date.between(start, end),
        ).distinct().order_by(Row.trade_date)))
