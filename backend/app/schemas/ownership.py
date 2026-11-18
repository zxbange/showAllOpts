from datetime import date, datetime
from typing import Any
from pydantic import BaseModel

class OwnershipStockItem(BaseModel):
    code: str
    name: str
    industry: str | None = None

class OwnershipSearch(BaseModel):
    stocks: list[OwnershipStockItem]

class OwnershipPoint(BaseModel):
    date: date
    payload: dict[str, Any]

class HolderPoint(OwnershipPoint):
    pub_date: date | None

class OwnershipDetail(BaseModel):
    updated_at: datetime | None = None
    stock: OwnershipStockItem
    reports: list[OwnershipPoint]
    holders: list[HolderPoint]
    holder_change: int | None
    holder_change_pct: float | None

class SocialEntries(BaseModel):
    updated_at: datetime | None = None
    period: date
    periods: list[date]
    items: list[dict[str, Any]]
    comparable_count: int
    unknown_count: int
    collected_count: int
