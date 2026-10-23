from fastapi import HTTPException

METHODS = {'bukou': '补票龙', 'redhover': '红悬停龙', 'xuli': '蓄力龙'}


class ShortTermService:
    def __init__(self, repository):
        self.repository = repository

    def overview(self, day, method, offset, limit):
        runs = self.repository.runs()
        dates = [r.trade_date for r in runs]
        chosen = day or (dates[0] if dates else None)
        if day and day not in dates:
            raise HTTPException(404, '该交易日没有保留的选股快照，请切换日期。')
        meta = next((r.payload for r in runs if r.trade_date == chosen), {})
        items = [dict(r.payload, code=r.code) for r in self.repository.stocks(chosen)
                 if method in r.payload.get('methods', [])] if chosen else []
        return dict(trade_date=chosen, dates=dates, method=method,
                    counts=meta.get('counts', {m: 0 for m in METHODS}), meta=meta,
                    total=len(items), offset=offset, limit=limit, items=items[offset:offset+limit])
