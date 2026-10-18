from datetime import date as DateValue, datetime
from pydantic import BaseModel, Field


class BreadthPeriod(BaseModel):
    period: int
    high_count: int
    low_count: int
    high_ratio: float
    low_ratio: float
    spread_pp: float
    dominant_days: int
    observed_days: int
    dominance_pct: float


class BreadthPoint(BaseModel):
    date: DateValue
    sample_count: int
    periods: list[BreadthPeriod]
    strength: float
    sentiment: float
    change_6d: float | None = None
    change_12d: float | None = None
    market_state: str


class BreadthLatest(BreadthPoint):
    updated_at: datetime


class BreadthHistory(BaseModel):
    series_id: str = 'micro400_hl_v1'
    start_date: DateValue | None = None
    end_date: DateValue | None = None
    latest: BreadthLatest | None = None
    observed_days: int = 0
    points: list[BreadthPoint] = Field(default_factory=list)
