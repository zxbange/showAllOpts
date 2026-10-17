from datetime import date, datetime

from pydantic import BaseModel


class MicrocapPoint(BaseModel):
    date: date
    micro_median: float
    all_median: float
    relative_ratio: float
    crowding_ratio: float
    amount_crowding_ratio: float
    pe_median: float
    pb_median: float
    avg_np_growth: float | None
    cap_pct: float
    relative_pct: float
    crowding_pct: float
    amount_crowding_pct: float
    score: float
    micro_index: float
    period_return: float


class MicrocapLatest(MicrocapPoint):
    micro_num: int
    sample_step: int
    sample_count: int
    universe_count: int
    profit_growth_count: int
    updated_at: datetime


class MicrocapSummary(BaseModel):
    observed_samples: int = 0
    average_score: float | None = None
    peak_score: float | None = None
    low_score: float | None = None
    score_change: float | None = None
    index_change_pct: float | None = None


class MicrocapHistory(BaseModel):
    start_date: date | None = None
    end_date: date | None = None
    latest: MicrocapLatest | None = None
    summary: MicrocapSummary
    points: list[MicrocapPoint]
