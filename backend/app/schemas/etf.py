from datetime import date as DateValue, datetime

from pydantic import BaseModel, Field


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


class ETFMarketValue(BaseModel):
    date: DateValue
    market_value: str | None = None  # CNY; null unless every tracked fund is present.
    available_funds: int
    expected_funds: int
    excluded_funds: list[str] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)


class ETFGroupOption(BaseModel):
    id: str
    name: str


class ETFHistory(BaseModel):
    group: str
    group_name: str
    start_date: DateValue | None = None
    end_date: DateValue | None = None
    baseline_date: DateValue | None = None
    observed_days: int = 0
    available_funds: int = 0
    funds: list[ETFFund]
    market_values: list[ETFMarketValue] = Field(default_factory=list)
    group_options: list[ETFGroupOption] = Field(default_factory=list)
