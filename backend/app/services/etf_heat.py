from datetime import timedelta


def item(row):
    return dict(date=row.trade_date, code=row.code, name=row.name,
                score=float(row.score) if row.score is not None else None,
                close=float(row.close) if row.close is not None else None,
                annual_return=float(row.annual_return) if row.annual_return is not None else None,
                r2=float(row.r2) if row.r2 is not None else None,
                reference_pass=bool(row.reference_pass) if row.reference_pass is not None else None,
                details=row.details)


def ranked(rows):
    values = sorted(rows, key=lambda r: (r.score is None, -(r.score or 0), r.code))
    rank = 0
    result = []
    for row in values:
        value = item(row)
        if row.score is not None:
            rank += 1
        value['rank'] = rank if row.score is not None else None
        result.append(value)
    return result


class ETFHeatService:
    def __init__(self, repository):
        self.repository = repository

    def latest(self):
        head = self.repository.head()
        if head is None:
            return dict(trade_date=None, updated_at=None, items=[], previous_date=None)
        members = {r['code']: r for r in head.members}
        current = ranked(self.repository.day(head.as_of_date, list(members)))
        previous_date = self.repository.previous_day(head.as_of_date)
        previous = {r['code']: r for r in ranked(self.repository.day(previous_date, list(members)))} if previous_date else {}
        for row in current:
            old = previous.get(row['code'], {})
            row['rank_change'] = old['rank']-row['rank'] if old.get('rank') is not None and row['rank'] is not None else None
            row['fund'] = members[row['code']]
        return dict(trade_date=head.as_of_date, updated_at=head.updated_at, previous_date=previous_date, items=current)

    def history(self, code, days):
        head = self.repository.head()
        if head is None:
            return None
        records = self.repository.history(code, head.as_of_date, days)
        if not records:
            return None
        by_day = {r.trade_date: r for r in records}
        dates = self.repository.calendar(head.as_of_date-timedelta(days=days-1), head.as_of_date)
        points = [item(by_day[day]) if day in by_day else dict(date=day, code=code, name=records[-1].name,
                   score=None, close=None, annual_return=None, r2=None, reference_pass=None,
                   details={'status': 'missing', 'reason': '该基金当日无记录'}) for day in dates]
        return dict(code=code, name=records[-1].name, end_date=head.as_of_date,
                    fund=next((m for m in head.members if m['code']==code), None), points=points)
