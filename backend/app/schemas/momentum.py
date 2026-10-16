from datetime import date, datetime

from pydantic import BaseModel


class IndustryItem(BaseModel):
    code: str
    name: str
    momentum: float
    rank: int
    front_stock_count: int
    industry_stock_count: int
    breadth: float
    previous_momentum: float | None
    change: float | None


class SnapshotSummary(BaseModel):
    industry_count: int
    active_count: int
    average_momentum: float
    max_momentum: float


class Snapshot(BaseModel):
    industry_type: str
    trade_date: date | None
    previous_trade_date: date | None
    updated_at: datetime | None
    summary: SnapshotSummary
    industries: list[IndustryItem]


class HistoryPoint(BaseModel):
    date: date
    momentum: float | None
    rank: int | None = None
    front_stock_count: int | None = None
    industry_stock_count: int | None = None


class HistorySummary(BaseModel):
    latest_momentum: float | None
    latest_date: date | None
    peak_momentum: float | None
    peak_date: date | None
    average_momentum: float | None
    change: float | None
    observed_days: int
    missing_days: int


class IndustryHistory(BaseModel):
    code: str
    name: str
    industry_type: str
    start_date: date
    end_date: date
    summary: HistorySummary
    points: list[HistoryPoint]
