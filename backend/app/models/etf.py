from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, String
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
