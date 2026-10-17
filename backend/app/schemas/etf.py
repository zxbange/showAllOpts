from datetime import date as DateValue, datetime

from pydantic import BaseModel


class ETFPoint(BaseModel):
    date: DateValue
    shares: str  # Preserve BIGINT precision in JSON.
    change_pct: float | None = None


class ETFFund(BaseModel):
    code: str
    name: str
    date: DateValue | None = None
    shares: str | None = None
    previous_date: DateValue | None = None
    shares_change: str | None = None
    daily_change_pct: float | None = None
    updated_at: datetime | None = None
    lagging: bool = False
    points: list[ETFPoint]


class ETFHistory(BaseModel):
    group: str
    group_name: str
    start_date: DateValue | None = None
    end_date: DateValue | None = None
    baseline_date: DateValue | None = None
    observed_days: int = 0
    available_funds: int = 0
    funds: list[ETFFund]
