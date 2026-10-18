from sqlalchemy import select
from app.models.dividend import DividendSnapshot, DividendStock


class DividendRepository:
    def __init__(self, session):
        self.session = session

    def latest(self):
        # One SQL statement prevents mixing metadata and stocks during replacement.
        return self.session.execute(select(DividendSnapshot, DividendStock).outerjoin(
            DividendStock, DividendStock.trade_date == DividendSnapshot.trade_date
        ).where(DividendSnapshot.id == 1).order_by(DividendStock.method, DividendStock.rank)).all()
