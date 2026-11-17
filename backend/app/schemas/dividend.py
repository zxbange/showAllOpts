from datetime import date, datetime
from pydantic import BaseModel, ConfigDict, Field


class DividendRun(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    trade_date: date
    updated_at: datetime
    rule_version: str
    universe_count: int
    eligible_count: int
    value_candidates: int
    value_count: int


class DividendAnnualYield(BaseModel):
    fiscal_year: int
    yield_pct: float | None


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
    dividend_yield_static: float = Field(validation_alias='dividend_yield_1y')
    dividend_cash_annual: float | None = Field(validation_alias='dividend_cash_1y')
    dividend_yield_history: list[DividendAnnualYield] | None = None
    dividend_per_share: float | None = None
    dividend_fiscal_year: int | None = None
    pe_ttm: float
    pb: float | None
    market_cap: float
    close: float


class DividendGroup(BaseModel):
    id: str
    name: str
    ranking_field: str
    candidates: int | None = None
    items: list[DividendItem] = Field(default_factory=list)


class DividendOverview(BaseModel):
    snapshot: DividendRun | None = None
    groups: list[DividendGroup]
    requires_refresh: bool = False
