"""Latest selection snapshots, written only by the JoinQuant collector."""
from sqlalchemy import Column, Date, DateTime, Index, Integer, Numeric, String
from app.models.base import Base


class DividendSnapshot(Base):
    __tablename__ = 'dividend_selection_snapshot'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
    id = Column(Integer, primary_key=True)
    trade_date = Column(Date, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    rule_version = Column(String(32), nullable=False)
    universe_count = Column(Integer, nullable=False)
    eligible_count = Column(Integer, nullable=False)
    classic_candidates = Column(Integer, nullable=False)
    value_candidates = Column(Integer, nullable=False)
    classic_count = Column(Integer, nullable=False)
    value_count = Column(Integer, nullable=False)


class DividendStock(Base):
    __tablename__ = 'dividend_selection_stock'
    __table_args__ = (Index('ux_dividend_method_rank', 'method', 'rank', unique=True),
                      {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'})
    method = Column(String(16), primary_key=True)
    code = Column(String(16), primary_key=True)
    trade_date = Column(Date, nullable=False)
    rank = Column(Integer, nullable=False)
    name = Column(String(128), nullable=False)
    industry_code = Column(String(32), nullable=True)
    industry_name = Column(String(128), nullable=True)
    industry_level = Column(String(16), nullable=True)
    dividend_yield_1y = Column(Numeric(24, 6), nullable=False)
    dividend_ratio_3y = Column(Numeric(24, 6), nullable=False)
    dividend_cash_1y = Column(Numeric(28, 6), nullable=False)
    dividend_cash_3y = Column(Numeric(28, 6), nullable=False)
    pe_ttm = Column(Numeric(24, 6), nullable=False)
    pb = Column(Numeric(24, 6), nullable=True)
    market_cap = Column(Numeric(24, 6), nullable=False)
    close = Column(Numeric(24, 6), nullable=False)
    revenue_growth = Column(Numeric(24, 6), nullable=True)
    profit_growth = Column(Numeric(24, 6), nullable=True)
    adjusted_roe = Column(Numeric(24, 6), nullable=True)
    peg = Column(Numeric(24, 6), nullable=True)
    report_period = Column(Date, nullable=True)
    publication_date = Column(Date, nullable=True)
