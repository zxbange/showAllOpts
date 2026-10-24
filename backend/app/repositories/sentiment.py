from datetime import date
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.sentiment import MarketSentiment


def three_years_ago(day: date) -> date:
    try:
        return day.replace(year=day.year - 3)
    except ValueError:
        return day.replace(year=day.year - 3, day=28)


class SentimentRepository:
    def __init__(self, session: Session):
        self.session = session

    def history(self, end: date) -> list[MarketSentiment]:
        return list(self.session.scalars(select(MarketSentiment).where(
            MarketSentiment.trade_date >= three_years_ago(end),
            MarketSentiment.trade_date <= end,
        ).order_by(MarketSentiment.trade_date)))
