from app.schemas.dividend import DividendGroup, DividendItem, DividendOverview, DividendRun


class DividendService:
    def __init__(self, repository):
        self.repository = repository

    def latest(self):
        groups = [DividendGroup(id='value', name='高股息低估值（非银行业）', ranking_field='dividend_yield_static'),
                  DividendGroup(id='bank', name='高股息低估值（银行业）', ranking_field='dividend_yield_static')]
        rows = self.repository.latest()
        if not rows:
            return DividendOverview(groups=groups)
        head = rows[0][0]
        by_id = {group.id: group for group in groups}
        legacy = head.rule_version != 'dividend_value_pe12_v6'
        for _, stock in rows:
            if stock is not None and stock.method in by_id and not legacy:
                by_id[stock.method].items.append(DividendItem.model_validate(stock))
        return DividendOverview(snapshot=DividendRun.model_validate(head), groups=groups, requires_refresh=legacy)
