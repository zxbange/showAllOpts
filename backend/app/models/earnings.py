"""Collector-owned quarterly snapshots. The web application only reads."""
from sqlalchemy import Column, Date, DateTime, Index, Integer, JSON, Numeric, String, Text
from app.models.base import Base


class EarningsRun(Base):
    __tablename__ = 'earnings_gap_run'
    report_period = Column(Date, primary_key=True)
    as_of_date = Column(Date, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    universe_count = Column(Integer, nullable=False)
    matched_count = Column(Integer, nullable=False)
    rule_version = Column(String(32), nullable=False)


class EarningsStock(Base):
    __tablename__ = 'earnings_gap_stock'
    __table_args__ = (
        Index('ix_earnings_period_industry', 'report_period', 'industry_code'),
        Index('ix_earnings_period_signal', 'report_period', 'signal_date'),
    )
    report_period = Column(Date, primary_key=True)
    code = Column(String(16), primary_key=True)
    name = Column(String(128), nullable=False)
    industry_code = Column(String(32), nullable=True)
    industry_name = Column(String(128), nullable=True)
    industry_level = Column(String(16), nullable=True)
    signal_date = Column(Date, nullable=False)
    announcement_date = Column(Date, nullable=False)
    signal_source = Column(String(16), nullable=False)
    profit_basis = Column(String(32), nullable=False)
    profit_yoy = Column(Numeric(24, 6), nullable=False)
    net_profit = Column(Numeric(28, 4), nullable=True)
    open_gap_pct = Column(Numeric(20, 6), nullable=False)
    intraday_pct = Column(Numeric(20, 6), nullable=False)
    market_cap = Column(Numeric(24, 6), nullable=True)
    circulating_market_cap = Column(Numeric(24, 6), nullable=True)
    pe_ttm = Column(Numeric(24, 6), nullable=True)
    pb = Column(Numeric(24, 6), nullable=True)
    valuation_date = Column(Date, nullable=False)
    analysis = Column(Text, nullable=False)
    analysis_basis = Column(String(32), nullable=False)
    events = Column(JSON, nullable=False)
    prices = Column(JSON, nullable=False)
