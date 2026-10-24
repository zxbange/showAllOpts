from datetime import date, timedelta
from app.repositories.sentiment import SentimentRepository
from app.schemas.sentiment import SentimentHistory, SentimentLatest, SentimentPoint, SentimentSummary


def sentiment_state(above: int, eligible: int):
    # Integer comparison retains strict thresholds, including exactly 20% and 80%.
    return 'cold' if above * 5 < eligible else 'hot' if above * 5 > eligible * 4 else 'normal'


class SentimentService:
    def __init__(self, repository: SentimentRepository):
        self.repository = repository

    def history(self, days: int, end: date) -> SentimentHistory:
        rows = self.repository.history(end)
        if not rows:
            return SentimentHistory(summary=SentimentSummary(), points=[])
        if days:
            start = rows[-1].trade_date - timedelta(days=days - 1)
            rows = [row for row in rows if row.trade_date >= start]
        points = [SentimentPoint(date=row.trade_date, ratio=float(row.ratio), above_count=row.above_count,
                  eligible_count=row.eligible_count, universe_count=row.universe_count,
                  state=sentiment_state(row.above_count, row.eligible_count)) for row in rows]
        values = [point.ratio for point in points]
        return SentimentHistory(
            start_date=points[0].date, end_date=points[-1].date,
            latest=SentimentLatest(**points[-1].model_dump(), updated_at=rows[-1].updated_at),
            summary=SentimentSummary(observed_days=len(points), cold_days=sum(point.state == 'cold' for point in points),
                hot_days=sum(point.state == 'hot' for point in points), low_ratio=min(values), peak_ratio=max(values),
                average_ratio=sum(values) / len(values), change_pp=(values[-1] - values[0]) * 100),
            points=points,
        )
