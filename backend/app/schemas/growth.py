from datetime import date, datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class GrowthRun(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    as_of_date: date
    valuation_date: date
    updated_at: datetime
    rule_version: str
    universe_count: int
    evaluated_count: int
    matched_count: int


class GrowthReport(BaseModel):
    report_period: date
    publication_date: date
    net_profit: float
    eps: float
    profit_growth: float | None
    eps_growth: float | None


class GrowthItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    as_of_date: date
    rank: int
    name: str
    industry_code: str | None
    industry_name: str | None
    industry_level: str | None
    pe_ttm: float | None
    pb: float | None
    market_cap: float | None
    annual_profit_growth: float
    reports: dict[Literal['annual', 'q1', 'q2', 'q3'], list[GrowthReport]]


class GrowthOverview(BaseModel):
    snapshot: GrowthRun | None = None
    items: list[GrowthItem] = Field(default_factory=list)
