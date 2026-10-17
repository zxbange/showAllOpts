from sqlalchemy import func, select
from sqlalchemy.orm import load_only
from app.models.earnings import EarningsRun, EarningsStock

SUMMARY_COLUMNS = [c for c in EarningsStock.__table__.columns.keys() if c not in ('events', 'prices', 'analysis', 'analysis_basis')]


class EarningsRepository:
    def __init__(self, session):
        self.session = session

    def periods(self):
        return list(self.session.scalars(select(EarningsRun.report_period).order_by(EarningsRun.report_period.desc())))

    def run(self, period):
        return self.session.get(EarningsRun, period)

    def industries(self, period):
        rows = self.session.execute(select(EarningsStock.industry_code, EarningsStock.industry_name).where(
            EarningsStock.report_period == period, EarningsStock.industry_code.is_not(None)
        ).distinct().order_by(EarningsStock.industry_code)).all()
        return [dict(code=code, name=name or code) for code, name in rows]

    def listing(self, period, search, industry, source, sort, direction, page, page_size):
        filters = [EarningsStock.report_period == period]
        if search:
            filters.append(EarningsStock.code.contains(search, autoescape=True) |
                           EarningsStock.name.contains(search, autoescape=True))
        if industry:
            filters.append(EarningsStock.industry_code == industry)
        if source:
            filters.append(EarningsStock.signal_source == source)
        count = self.session.scalar(select(func.count()).select_from(EarningsStock).where(*filters))
        column = getattr(EarningsStock, sort)  # Caller validates against a fixed allowlist.
        order = column.asc() if direction == 'asc' else column.desc()
        rows = self.session.scalars(select(EarningsStock).options(
            load_only(*[getattr(EarningsStock, c) for c in SUMMARY_COLUMNS])
        ).where(*filters).order_by(column.is_(None), order, EarningsStock.code)
            .offset((page-1)*page_size).limit(page_size)).all()
        return count, rows

    def detail(self, period, code):
        return self.session.get(EarningsStock, (period, code))
