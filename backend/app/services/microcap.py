from datetime import date, timedelta

from app.repositories.microcap import MicrocapRepository
from app.schemas.microcap import MicrocapHistory, MicrocapLatest, MicrocapPoint, MicrocapSummary

METRICS = (
    'micro_median', 'all_median', 'relative_ratio', 'crowding_ratio', 'amount_crowding_ratio',
    'pe_median', 'pb_median', 'avg_np_growth', 'cap_pct', 'relative_pct',
    'crowding_pct', 'amount_crowding_pct', 'score', 'micro_index', 'period_return',
)


class MicrocapService:
    def __init__(self, repository: MicrocapRepository):
        self.repository = repository

    def history(self, days: int, end: date) -> MicrocapHistory:
        rows = self.repository.history(end)
        if not rows:
            return MicrocapHistory(summary=MicrocapSummary(), points=[])
        latest = rows[-1]
        start = latest.trade_date - timedelta(days=days - 1) if days else rows[0].trade_date
        def point(row):
            return MicrocapPoint(date=row.trade_date, **{
                field: None if getattr(row, field) is None else float(getattr(row, field))
                for field in METRICS
            })
        points = [point(row) for row in rows if row.trade_date >= start]
        scores = [item.score for item in points]
        first_index = points[0].micro_index
        return MicrocapHistory(
            start_date=max(start, date(2021, 1, 1)), end_date=latest.trade_date,
            latest=MicrocapLatest(
                **point(latest).model_dump(), micro_num=latest.micro_num,
                sample_step=latest.sample_step, sample_count=latest.sample_count,
                universe_count=latest.universe_count, profit_growth_count=latest.profit_growth_count,
                updated_at=latest.updated_at,
            ),
            summary=MicrocapSummary(
                observed_samples=len(points), average_score=sum(scores) / len(scores),
                peak_score=max(scores), low_score=min(scores),
                score_change=(scores[-1] - scores[0]) * 100,
                index_change_pct=(float(latest.micro_index) / first_index - 1) * 100 if first_index > 0 else None,
            ),
            points=points,
        )
