from datetime import date
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.breadth import DEFAULT_SERIES, MicrocapBreadth


class BreadthRepository:
    def __init__(self, session: Session):
        self.session = session

    def history(self, end: date, limit: int) -> list[MicrocapBreadth]:
        # Composite primary key serves both latest-date and bounded history reads.
        rows = self.session.scalars(select(MicrocapBreadth).where(
            MicrocapBreadth.series_id == DEFAULT_SERIES,
            MicrocapBreadth.trade_date <= end,
        ).order_by(MicrocapBreadth.trade_date.desc()).limit(limit)).all()
        return list(reversed(rows))
