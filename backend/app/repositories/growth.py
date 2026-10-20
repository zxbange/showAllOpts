from sqlalchemy import select
from app.models.growth import GrowthSnapshot, GrowthStock


class GrowthRepository:
    def __init__(self, session):
        self.session = session

    def latest(self):
        return self.session.execute(select(GrowthSnapshot, GrowthStock).outerjoin(
            GrowthStock, GrowthStock.as_of_date == GrowthSnapshot.as_of_date
        ).where(GrowthSnapshot.id == 1).order_by(GrowthStock.rank, GrowthStock.code)).all()
