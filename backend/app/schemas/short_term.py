from datetime import date
from typing import Any
from pydantic import BaseModel


class ShortTermOverview(BaseModel):
    trade_date: date | None
    dates: list[date]
    method: str
    counts: dict[str, int]
    meta: dict[str, Any]
    total: int
    offset: int
    limit: int
    items: list[dict[str, Any]]
