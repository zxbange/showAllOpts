from fastapi import HTTPException

METHODS = {'bukou': '补票龙', 'oversold': '超跌后止跌反弹'}


class ShortTermService:
    def __init__(self, repository):
        self.repository = repository

    def overview(self, day, method, offset, limit):
        runs = self.repository.runs()
        dates = [r.trade_date for r in runs]
        chosen = day or (dates[0] if dates else None)
        if day and day not in dates:
            raise HTTPException(404, '该交易日没有保留的选股快照，请切换日期。')
        meta = dict(next((r.payload for r in runs if r.trade_date == chosen), {}))
        meta['counts'] = {m: meta['counts'][m] for m in METHODS if m in meta.get('counts', {})}
        total, stocks = self.repository.stocks(chosen, method, offset, limit) if chosen else (0, [])
        items = [dict(r.payload, code=r.code, methods=[m for m in r.payload.get('methods', []) if m in METHODS]) for r in stocks]
        return dict(trade_date=chosen, dates=dates, method=method,
                    counts=meta.get('counts', {m: 0 for m in METHODS}), meta=meta,
                    total=total, offset=offset, limit=limit, items=items)
