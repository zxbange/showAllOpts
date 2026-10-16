from datetime import date, datetime, timedelta, timezone

from app.repositories.momentum import MomentumRepository
from app.schemas.momentum import (
    HistoryPoint, HistorySummary, IndustryHistory, IndustryItem, Snapshot, SnapshotSummary,
)


def china_today() -> date:
    return datetime.now(timezone(timedelta(hours=8))).date()


class MomentumService:
    def __init__(self, repository: MomentumRepository):
        self.repository = repository

    def latest(self, industry_type: str) -> Snapshot:
        day = self.repository.latest_date(industry_type, china_today())
        if day is None:
            return Snapshot(
                industry_type=industry_type, trade_date=None, previous_trade_date=None,
                updated_at=None, industries=[],
                summary=SnapshotSummary(industry_count=0, active_count=0,
                                        average_momentum=0, max_momentum=0),
            )
        rows = self.repository.snapshot(industry_type, day)
        previous = self.repository.latest_date(industry_type, day - timedelta(days=1))
        previous_values = {
            r.industry_code: float(r.momentum)
            for r in self.repository.snapshot(industry_type, previous)
        } if previous else {}
        items = []
        for rank, row in enumerate(rows, 1):
            score = float(row.momentum)
            prior = previous_values.get(row.industry_code)
            items.append(IndustryItem(
                code=row.industry_code, name=row.industry_name, momentum=score, rank=rank,
                front_stock_count=row.front_stock_count, industry_stock_count=row.industry_stock_count,
                breadth=round(row.front_stock_count / row.industry_stock_count, 6)
                if row.industry_stock_count else 0,
                previous_momentum=prior, change=round(score - prior, 6) if prior is not None else None,
            ))
        scores = [i.momentum for i in items]
        return Snapshot(
            industry_type=industry_type, trade_date=day, previous_trade_date=previous,
            updated_at=max((r.updated_at for r in rows if r.updated_at), default=None),
            summary=SnapshotSummary(
                industry_count=len(items), active_count=sum(s > 0 for s in scores),
                average_momentum=round(sum(scores) / len(scores), 6) if scores else 0,
                max_momentum=max(scores, default=0),
            ), industries=items,
        )

    def history(self, industry_type: str, code: str, days: int, end: date | None) -> IndustryHistory | None:
        end = end or self.repository.latest_date(industry_type, china_today())
        if end is None:
            return None
        identity = self.repository.identity(industry_type, code, end)
        if identity is None:
            return None
        start = end - timedelta(days=days - 1)
        rows = self.repository.history(industry_type, code, start, end)
        calendar = self.repository.observed_dates(industry_type, start, end)
        by_date = {r.trade_date: r for r in rows}
        points = []
        for day in calendar:
            row = by_date.get(day)
            points.append(HistoryPoint(
                date=day, momentum=float(row.momentum) if row else None,
                rank=row.momentum_rank if row else None,
                front_stock_count=row.front_stock_count if row else None,
                industry_stock_count=row.industry_stock_count if row else None,
            ))
        peak = max(rows, key=lambda r: r.momentum) if rows else None
        summary = HistorySummary(
            latest_momentum=float(rows[-1].momentum) if rows else None,
            latest_date=rows[-1].trade_date if rows else None,
            peak_momentum=float(peak.momentum) if peak else None,
            peak_date=peak.trade_date if peak else None,
            average_momentum=round(sum(float(r.momentum) for r in rows) / len(rows), 6) if rows else None,
            change=round(float(rows[-1].momentum - rows[0].momentum), 6) if rows else None,
            observed_days=len(rows), missing_days=len(calendar) - len(rows),
        )
        return IndustryHistory(code=code, name=identity.industry_name, industry_type=industry_type,
                               start_date=start, end_date=end, summary=summary, points=points)
