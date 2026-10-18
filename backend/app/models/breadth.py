from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base

DEFAULT_SERIES = 'micro400_hl_v1'
PERIODS = (5, 10, 20, 30)


class MicrocapBreadth(Base):
    __tablename__ = 'microcap_breadth'
    series_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    sample_count: Mapped[int] = mapped_column(Integer)
    high_5: Mapped[int] = mapped_column(Integer)
    low_5: Mapped[int] = mapped_column(Integer)
    high_10: Mapped[int] = mapped_column(Integer)
    low_10: Mapped[int] = mapped_column(Integer)
    high_20: Mapped[int] = mapped_column(Integer)
    low_20: Mapped[int] = mapped_column(Integer)
    high_30: Mapped[int] = mapped_column(Integer)
    low_30: Mapped[int] = mapped_column(Integer)
    member_codes: Mapped[str] = mapped_column(Text, deferred=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
