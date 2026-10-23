from sqlalchemy import Column, Date, DateTime, JSON, String
from app.models.base import Base


class ShortTermRun(Base):
    __tablename__ = 'short_term_run'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
    trade_date = Column(Date, primary_key=True)
    payload = Column(JSON, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class ShortTermStock(Base):
    __tablename__ = 'short_term_stock'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
    trade_date = Column(Date, primary_key=True)
    code = Column(String(16), primary_key=True)
    payload = Column(JSON, nullable=False)
    updated_at = Column(DateTime, nullable=False)
