from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.crowding import MarketCrowding


class CrowdingRepository:
    def __init__(self, session: Session):
        self.session = session

    def history(self, end: date) -> list[MarketCrowding]:
        # One indexed round trip even when the database is reached over the Internet.
        statement = select(MarketCrowding).where(
            MarketCrowding.top_pct == Decimal('0.05'),
            MarketCrowding.trade_date >= date(2020, 1, 1),
            MarketCrowding.trade_date <= end,
        ).order_by(MarketCrowding.trade_date)
        return list(self.session.scalars(statement))
