from datetime import date, datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class RunInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    report_period: date
    as_of_date: date
    updated_at: datetime
    universe_count: int
    matched_count: int
    rule_version: str


class StockSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    report_period: date
    code: str
    name: str
    industry_code: str | None
    industry_name: str | None
    industry_level: str | None
    signal_date: date
    announcement_date: date
    signal_source: str
    profit_basis: str
    profit_yoy: float
    net_profit: float | None
    open_gap_pct: float
    intraday_pct: float
    market_cap: float | None
    circulating_market_cap: float | None
    pe_ttm: float | None
    pb: float | None
    valuation_date: date
    analysis: str
    analysis_basis: str


class StockDetail(StockSummary):
    events: list[dict[str, Any]]
    prices: list[dict[str, Any]]


class EarningsOverview(BaseModel):
    target_period: date
    periods: list[date] = Field(default_factory=list)
    run: RunInfo | None = None
    industries: list[dict[str, str]] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 30
    items: list[StockSummary] = Field(default_factory=list)
