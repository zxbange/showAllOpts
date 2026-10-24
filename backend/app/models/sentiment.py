from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import Date, DateTime, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base


class MarketSentiment(Base):
    """Daily aggregates computed by the JoinQuant collector; web reads only."""
    __tablename__ = 'market_sentiment'
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    ratio: Mapped[Decimal] = mapped_column(Numeric(18, 10))
    above_count: Mapped[int] = mapped_column(Integer)
    eligible_count: Mapped[int] = mapped_column(Integer)
    universe_count: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
