"""Latest complete JoinQuant cash-cow result; three windows publish together."""
from sqlalchemy import Column, Date, DateTime, Integer, JSON
from app.models.base import Base


class CashCowSnapshot(Base):
    __tablename__ = 'cash_cow_snapshot'
    id = Column(Integer, primary_key=True)
    as_of_date = Column(Date, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    payload = Column(JSON, nullable=False)
