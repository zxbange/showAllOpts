from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ETFShare(Base):
    """Collector-owned table. Shares are integer units, never currency or volume."""
    __tablename__ = 'etf_share_daily'

    fund_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    fund_name: Mapped[str] = mapped_column(String(128))
    shares: Mapped[int] = mapped_column(BigInteger)
    previous_trade_date: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(48))
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class ETFPrice(Base):
    """Exchange daily unadjusted close, separate from legacy share history."""
    __tablename__ = 'etf_price_daily'

    fund_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    close_price: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    source: Mapped[str] = mapped_column(String(48))
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class ETFTradingDay(Base):
    """Observed exchange sessions, populated by the official index calendar."""
    __tablename__ = 'etf_trading_day'

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    previous_trade_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(48))
    updated_at: Mapped[datetime] = mapped_column(DateTime)
