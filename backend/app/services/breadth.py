import math
from datetime import date
from app.models.breadth import PERIODS
from app.repositories.breadth import BreadthRepository
from app.schemas.breadth import BreadthHistory, BreadthLatest, BreadthPeriod, BreadthPoint


def market_state(strength: float) -> str:
    for boundary, label in ((10, '极度强势'), (5, '强势'), (0, '偏强'), (-5, '中性'), (-10, '偏弱')):
        if strength > boundary:
            return label
    return '极度弱势'


class BreadthService:
    def __init__(self, repository: BreadthRepository):
        self.repository = repository

    def history(self, days: int, end: date) -> BreadthHistory:
        # Extra 14 rows keep rolling statistics invariant when display range changes.
        rows = self.repository.history(end, days + 14)
        points = []
        for index, row in enumerate(rows):
            recent = rows[max(0, index - 14):index + 1]
            metrics = []
            for period in PERIODS:
                high, low = getattr(row, f'high_{period}'), getattr(row, f'low_{period}')
                dominant = sum(getattr(r, f'high_{period}') > getattr(r, f'low_{period}') for r in recent)
                metrics.append(BreadthPeriod(
                    period=period, high_count=high, low_count=low,
                    high_ratio=high / row.sample_count * 100, low_ratio=low / row.sample_count * 100,
                    spread_pp=(high-low) / row.sample_count * 100,
                    dominant_days=dominant, observed_days=len(recent), dominance_pct=dominant / len(recent) * 100,
                ))
            strength = sum(metric.spread_pp * weight for metric, weight in zip(metrics, (.48, .24, .16, .12)))
            points.append(BreadthPoint(
                date=row.trade_date, sample_count=row.sample_count, periods=metrics, strength=strength,
                sentiment=sum(metric.spread_pp * math.exp(-metric.period/20) for metric in metrics),
                change_6d=strength-points[index-6].strength if index >= 6 else None,
                change_12d=strength-points[index-12].strength if index >= 12 else None,
                market_state=market_state(strength),
            ))
        visible = points[-days:]
        if not visible:
            return BreadthHistory()
        return BreadthHistory(start_date=visible[0].date, end_date=visible[-1].date, observed_days=len(visible),
                              latest=BreadthLatest(**visible[-1].model_dump(), updated_at=rows[-1].updated_at),
                              points=visible)
