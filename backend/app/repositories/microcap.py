from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.microcap import DEFAULT_SERIES, MicrocapTemperature


class MicrocapRepository:
    def __init__(self, session: Session):
        self.session = session

    def history(self, end: date) -> list[MicrocapTemperature]:
        statement = select(MicrocapTemperature).where(
            MicrocapTemperature.series_id == DEFAULT_SERIES,
            MicrocapTemperature.trade_date <= end,
        ).order_by(MicrocapTemperature.trade_date)
        return list(self.session.scalars(statement))
