from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

DEFAULT_SERIES = 'micro1000_5d_20210101_v1'


class MicrocapTemperature(Base):
    """The JoinQuant collector owns this table; the web app only reads metrics."""

    __tablename__ = 'microcap_temperature'

    series_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    micro_num: Mapped[int] = mapped_column(Integer)
    sample_step: Mapped[int] = mapped_column(Integer)
    sample_count: Mapped[int] = mapped_column(Integer)
    universe_count: Mapped[int] = mapped_column(Integer)
    profit_growth_count: Mapped[int] = mapped_column(Integer)
    micro_median: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    all_median: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    relative_ratio: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    crowding_ratio: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    amount_crowding_ratio: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    pe_median: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    pb_median: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    avg_np_growth: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    cap_pct: Mapped[Decimal] = mapped_column(Numeric(18, 10))
    relative_pct: Mapped[Decimal] = mapped_column(Numeric(18, 10))
    crowding_pct: Mapped[Decimal] = mapped_column(Numeric(18, 10))
    amount_crowding_pct: Mapped[Decimal] = mapped_column(Numeric(18, 10))
    score: Mapped[Decimal] = mapped_column(Numeric(18, 10))
    micro_index: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    period_return: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    # The dashboard must not transfer the 1,000 constituent codes per sample.
    member_codes: Mapped[str] = mapped_column(Text, deferred=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
