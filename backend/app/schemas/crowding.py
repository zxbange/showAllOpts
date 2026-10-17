from datetime import date, datetime

from pydantic import BaseModel


class CrowdingPoint(BaseModel):
    date: date
    ratio: float
    smoothed_ratio: float


class CrowdingLatest(BaseModel):
    date: date
    ratio: float
    stock_count: int
    top_stock_count: int
    top_money: float
    total_money: float
    updated_at: datetime


class CrowdingSummary(BaseModel):
    observed_days: int = 0
    average_ratio: float | None = None
    peak_ratio: float | None = None
    low_ratio: float | None = None
    change_pp: float | None = None


class CrowdingHistory(BaseModel):
    top_pct: float = 0.05
    start_date: date | None = None
    end_date: date | None = None
    latest: CrowdingLatest | None = None
    summary: CrowdingSummary
    points: list[CrowdingPoint]
