from app.schemas.growth import GrowthItem, GrowthOverview, GrowthRun


class GrowthService:
    def __init__(self, repository):
        self.repository = repository

    def latest(self):
        rows = self.repository.latest()
        if not rows:
            return GrowthOverview()
        return GrowthOverview(snapshot=GrowthRun.model_validate(rows[0][0]),
                              items=[GrowthItem.model_validate(stock) for _, stock in rows if stock is not None])
