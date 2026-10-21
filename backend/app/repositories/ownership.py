from sqlalchemy import select, or_
from app.models.ownership import OwnershipStock, OwnershipReport, OwnershipHolders

class OwnershipRepository:
    def __init__(self, session):
        self.session = session

    def search(self, q):
        statement = select(OwnershipStock)
        if q:
            statement = statement.where(or_(OwnershipStock.code.startswith(q, autoescape=True),
                                            OwnershipStock.name.contains(q, autoescape=True)))
        return self.session.scalars(statement.order_by(OwnershipStock.code).limit(30)).all()

    def stock(self, code):
        if len(code) == 6:
            return self.session.scalars(select(OwnershipStock).where(
                OwnershipStock.code.startswith(code + '.', autoescape=True))).first()
        return self.session.get(OwnershipStock, code)

    def reports(self, code, today):
        return self.session.scalars(select(OwnershipReport).where(
            OwnershipReport.code == code, OwnershipReport.report_date <= today
        ).order_by(OwnershipReport.report_date)).all()

    def holders(self, code, today):
        return self.session.scalars(select(OwnershipHolders).where(
            OwnershipHolders.code == code, OwnershipHolders.end_date <= today,
            or_(OwnershipHolders.pub_date.is_(None), OwnershipHolders.pub_date <= today)
        ).order_by(OwnershipHolders.end_date)).all()

    def periods(self, today):
        return list(self.session.scalars(select(OwnershipReport.report_date).where(
            OwnershipReport.report_date <= today).distinct().order_by(OwnershipReport.report_date.desc())))

    def period_rows(self, period):
        return self.session.execute(select(OwnershipReport, OwnershipStock).join(
            OwnershipStock, OwnershipStock.code == OwnershipReport.code
        ).where(OwnershipReport.report_date == period)).all()
