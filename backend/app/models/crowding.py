from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MarketCrowding(Base):
    """Read-only mapping; the JoinQuant collector creates this table."""

    __tablename__ = 'market_crowding'

    top_pct: Mapped[Decimal] = mapped_column(Numeric(8, 6), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    ratio: Mapped[Decimal] = mapped_column(Numeric(18, 10))
    top_money: Mapped[Decimal] = mapped_column(Numeric(24, 2))
    total_money: Mapped[Decimal] = mapped_column(Numeric(24, 2))
    stock_count: Mapped[int] = mapped_column(Integer)
    top_stock_count: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
