from sqlalchemy import Column, Date, DateTime, JSON, String
from app.models.base import Base

class OwnershipStock(Base):
    __tablename__ = 'ownership_stock'
    code = Column(String(16), primary_key=True)
    name = Column(String(128), nullable=False, index=True)
    industry = Column(String(128))
    updated_at = Column(DateTime, nullable=False)

class OwnershipReport(Base):
    __tablename__ = 'ownership_report'
    code = Column(String(16), primary_key=True)
    report_date = Column(Date, primary_key=True, index=True)
    payload = Column(JSON, nullable=False)
    updated_at = Column(DateTime, nullable=False)

class OwnershipHolders(Base):
    __tablename__ = 'ownership_holders'
    code = Column(String(16), primary_key=True)
    end_date = Column(Date, primary_key=True)
    pub_date = Column(Date)
    payload = Column(JSON, nullable=False)
    updated_at = Column(DateTime, nullable=False)
