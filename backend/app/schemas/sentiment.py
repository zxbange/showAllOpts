from datetime import date, datetime
from typing import Literal
from pydantic import BaseModel


class SentimentPoint(BaseModel):
    date: date
    ratio: float
    above_count: int
    eligible_count: int
    universe_count: int
    state: Literal['cold', 'normal', 'hot']


class SentimentLatest(SentimentPoint):
    updated_at: datetime


class SentimentSummary(BaseModel):
    observed_days: int = 0
    cold_days: int = 0
    hot_days: int = 0
    low_ratio: float | None = None
    peak_ratio: float | None = None
    average_ratio: float | None = None
    change_pp: float | None = None


class SentimentHistory(BaseModel):
    start_date: date | None = None
    end_date: date | None = None
    latest: SentimentLatest | None = None
    summary: SentimentSummary
    points: list[SentimentPoint]
