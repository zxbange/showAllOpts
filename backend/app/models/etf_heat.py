from sqlalchemy import Column, Date, DateTime, Integer, JSON, Numeric, String, Index
from app.models.base import Base

class Heat(Base):
    __tablename__ = 'etf_momentum_heat'
    trade_date = Column(Date, primary_key=True)
    code = Column(String(16), primary_key=True)
    name = Column(String(128), nullable=False)
    score = Column(Numeric(28, 10))
    close = Column(Numeric(24, 8))
    annual_return = Column(Numeric(28, 10))
    r2 = Column(Numeric(24, 10))
    reference_pass = Column(Integer)
    details = Column(JSON, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    __table_args__ = (Index('ix_etf_heat_code_date', 'code', 'trade_date'),)

class Pool(Base):
    __tablename__ = 'etf_momentum_pool'
    id = Column(Integer, primary_key=True)
    as_of_date = Column(Date, nullable=False)
    rule_version = Column(String(48), nullable=False)
    members = Column(JSON, nullable=False)
    updated_at = Column(DateTime, nullable=False)
