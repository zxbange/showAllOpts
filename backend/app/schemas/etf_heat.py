from datetime import date, datetime
from typing import Any
from pydantic import BaseModel


class HeatPoint(BaseModel):
    date: date
    code: str
    name: str
    score: float | None
    close: float | None
    annual_return: float | None
    r2: float | None
    reference_pass: bool | None
    details: dict[str, Any]


class HeatItem(HeatPoint):
    rank: int | None
    rank_change: int | None
    fund: dict[str, Any]


class HeatSnapshot(BaseModel):
    trade_date: date | None
    updated_at: datetime | None
    previous_date: date | None
    items: list[HeatItem]


class HeatHistory(BaseModel):
    code: str
    name: str
    end_date: date
    fund: dict[str, Any] | None
    points: list[HeatPoint]
