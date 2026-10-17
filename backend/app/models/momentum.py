from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class IndustryMomentum(Base):
    """Mapping of the collector's existing table; never created by this app."""

    __tablename__ = 'industry_momentum'

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    industry_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    industry_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    industry_name: Mapped[str] = mapped_column(String(128))
    momentum: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    momentum_rank: Mapped[int] = mapped_column(Integer)
    front_stock_count: Mapped[int] = mapped_column(Integer)
    industry_stock_count: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
