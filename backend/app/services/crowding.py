from datetime import date, timedelta

from app.repositories.crowding import CrowdingRepository
from app.schemas.crowding import (
    CrowdingHistory, CrowdingLatest, CrowdingPoint, CrowdingSummary,
)


class CrowdingService:
    def __init__(self, repository: CrowdingRepository):
        self.repository = repository

    def history(self, days: int, end: date) -> CrowdingHistory:
        rows = self.repository.history(end)
        if not rows:
            return CrowdingHistory(summary=CrowdingSummary(), points=[])
        latest = rows[-1]
        start = latest.trade_date - timedelta(days=days - 1) if days else rows[0].trade_date
        points = []
        numerator = denominator = 0.0
        # pandas ewm(span=10, adjust=True), calculated BEFORE slicing the display window.
        decay = 1 - 2 / 11
        for row in rows:
            ratio = float(row.ratio)
            numerator = ratio + decay * numerator
            denominator = 1 + decay * denominator
            if row.trade_date >= start:
                points.append(CrowdingPoint(
                    date=row.trade_date, ratio=ratio,
                    smoothed_ratio=numerator / denominator,
                ))
        values = [point.ratio for point in points]
        return CrowdingHistory(
            start_date=max(start, date(2020, 1, 1)), end_date=latest.trade_date,
            latest=CrowdingLatest(
                date=latest.trade_date, ratio=float(latest.ratio),
                stock_count=latest.stock_count, top_stock_count=latest.top_stock_count,
                top_money=float(latest.top_money), total_money=float(latest.total_money),
                updated_at=latest.updated_at,
            ),
            summary=CrowdingSummary(
                observed_days=len(values), average_ratio=sum(values) / len(values),
                peak_ratio=max(values), low_ratio=min(values),
                change_pp=(values[-1] - values[0]) * 100,
            ),
            points=points,
        )
