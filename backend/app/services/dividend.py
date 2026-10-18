from app.schemas.dividend import DividendGroup, DividendItem, DividendOverview, DividendRun


class DividendService:
    def __init__(self, repository):
        self.repository = repository

    def latest(self):
        groups = [DividendGroup(id='classic', name='纯高股息', ranking_field='dividend_ratio_3y'),
                  DividendGroup(id='value', name='高股息低估值', ranking_field='dividend_yield_1y')]
        rows = self.repository.latest()
        if not rows:
            return DividendOverview(groups=groups)
        head = rows[0][0]
        by_id = {g.id: g for g in groups}
        for group in groups:
            group.candidates = getattr(head, group.id + '_candidates')
        for _, stock in rows:
            if stock is not None and stock.method in by_id:
                by_id[stock.method].items.append(DividendItem.model_validate(stock))
        overlap = sorted({s.code for s in groups[0].items} & {s.code for s in groups[1].items})
        return DividendOverview(snapshot=DividendRun.model_validate(head), groups=groups, overlap_codes=overlap)
