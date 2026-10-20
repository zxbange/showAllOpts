"""Latest high-growth screening and its twelve financial report observations."""
from sqlalchemy import Column, Date, DateTime, Index, Integer, JSON, Numeric, String
from app.models.base import Base


class GrowthSnapshot(Base):
    __tablename__ = 'high_growth_snapshot'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
    id = Column(Integer, primary_key=True)
    as_of_date = Column(Date, nullable=False)
    valuation_date = Column(Date, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    rule_version = Column(String(48), nullable=False)
    universe_count = Column(Integer, nullable=False)
    evaluated_count = Column(Integer, nullable=False)
    matched_count = Column(Integer, nullable=False)


class GrowthStock(Base):
    __tablename__ = 'high_growth_stock'
    __table_args__ = (Index('ux_high_growth_rank', 'rank', unique=True),
                      {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'})
    code = Column(String(16), primary_key=True)
    as_of_date = Column(Date, nullable=False)
    rank = Column(Integer, nullable=False)
    name = Column(String(128), nullable=False)
    industry_code = Column(String(32), nullable=True)
    industry_name = Column(String(128), nullable=True)
    industry_level = Column(String(16), nullable=True)
    pe_ttm = Column(Numeric(24, 6), nullable=True)
    pb = Column(Numeric(24, 6), nullable=True)
    market_cap = Column(Numeric(24, 6), nullable=True)
    annual_profit_growth = Column(Numeric(28, 8), nullable=False)
    reports = Column(JSON, nullable=False)
