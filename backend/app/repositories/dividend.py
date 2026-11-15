from sqlalchemy import inspect, select
from sqlalchemy.orm import defer
from app.models.dividend import DividendSnapshot, DividendStock


class DividendRepository:
    def __init__(self, session):
        self.session = session

    def latest(self):
        # One SQL statement prevents mixing metadata and stocks during replacement.
        query = select(DividendSnapshot, DividendStock).outerjoin(
            DividendStock, DividendStock.trade_date == DividendSnapshot.trade_date
        ).where(DividendSnapshot.id == 1).order_by(DividendStock.method, DividendStock.rank)
        columns = {c['name'] for c in inspect(self.session.connection()).get_columns(DividendStock.__tablename__)}
        if 'dividend_yield_history' not in columns:
            # 旧快照只提示更新，不访问尚不存在的字段。
            query = query.options(defer(DividendStock.dividend_yield_history, raiseload=True))
        return self.session.execute(query).all()
