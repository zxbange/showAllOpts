from sqlalchemy import func, inspect, select
from sqlalchemy.orm import load_only
from app.models.earnings import EarningsRun, EarningsStock, EarningsPerformance

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

    def enrich(self, items):
        if not items:
            return items
        codes = {item.code for item in items}
        periods = {}
        for code, period in self.session.execute(select(EarningsStock.code, EarningsStock.report_period)
                .where(EarningsStock.code.in_(codes)).order_by(EarningsStock.report_period.desc())):
            periods.setdefault(code, []).append(period)
        performance = {}
        if inspect(self.session.connection()).has_table(EarningsPerformance.__tablename__):
            performance = {(row.report_period, row.code): row for row in self.session.scalars(
                select(EarningsPerformance).where(EarningsPerformance.code.in_(codes),
                    EarningsPerformance.report_period.in_({item.report_period for item in items})))}
        for item in items:
            item.close_change_pct = ((1 + item.open_gap_pct / 100) * (1 + item.intraday_pct / 100) - 1) * 100
            item.selected_periods = periods.get(item.code, [])
            current = performance.get((item.report_period, item.code))
            if current and current.signal_date == item.signal_date:
                item.since_signal_pct = float(current.since_signal_pct)
                item.latest_price_date = current.latest_date
        return items
