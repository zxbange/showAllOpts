from datetime import date, datetime
from pydantic import BaseModel, ConfigDict, Field


class DividendRun(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    trade_date: date
    updated_at: datetime
    rule_version: str
    universe_count: int
    eligible_count: int
    classic_candidates: int
    value_candidates: int
    classic_count: int
    value_count: int


class DividendItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    method: str
    code: str
    trade_date: date
    rank: int
    name: str
    industry_code: str | None
    industry_name: str | None
    industry_level: str | None
    dividend_yield_1y: float
    dividend_ratio_3y: float
    dividend_cash_1y: float
    dividend_cash_3y: float
    pe_ttm: float
    pb: float | None
    market_cap: float
    close: float
    revenue_growth: float | None
    profit_growth: float | None
    adjusted_roe: float | None
    peg: float | None
    report_period: date | None
    publication_date: date | None


class DividendGroup(BaseModel):
    id: str
    name: str
    ranking_field: str
    candidates: int = 0
    items: list[DividendItem] = Field(default_factory=list)


class DividendOverview(BaseModel):
    snapshot: DividendRun | None = None
    groups: list[DividendGroup]
    overlap_codes: list[str] = Field(default_factory=list)
