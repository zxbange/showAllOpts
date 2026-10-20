"""ETF/LOF scoring from provider quotes, without a runtime numpy/pandas dependency."""
from collections import defaultdict
from decimal import Decimal
import math
from types import SimpleNamespace

from sqlalchemy import select
from app.models.etf_heat import Bar, Heat, Pool
from app.core.cache_errors import SnapshotDataError

RAW_VERSION = 'etf_raw_market_v1'
CALCULATION_VERSION = 'weighted-momentum-26-v1'


def metrics(prices, paused=False):
    empty = dict(score=None, annual_return=None, r2=None, reference_pass=None,
                 details={'status': 'missing', 'reason': '有效价格不足26期或当日停牌'})
    if paused or len(prices) != 26 or any(p is None or not math.isfinite(float(p)) or p <= 0 for p in prices):
        return empty
    # A constant price scale does not affect regression slope or R².
    y = [math.log(float(p) / float(prices[0])) for p in prices]
    weights = [1 + i / 25 for i in range(26)]
    squared = [w*w for w in weights]
    total = sum(squared)
    mx = sum(w*i for i, w in enumerate(squared)) / total
    my = sum(w*v for w, v in zip(squared, y)) / total
    slope = sum(w*(i-mx)*(v-my) for i, (w,v) in enumerate(zip(squared,y))) / sum(w*(i-mx)**2 for i,w in enumerate(squared))
    intercept = my-slope*mx
    residual = sum(w*(v-(slope*i+intercept))**2 for i,(w,v) in enumerate(zip(weights,y)))
    average = sum(y)/26
    variance = sum(w*(v-average)**2 for w,v in zip(weights,y))
    r2 = 1-residual/variance if variance > 1e-25 else 0.0
    try:
        annual = math.expm1(slope*250)
        values = [annual*r2, annual, r2]
        if any(not math.isfinite(v) or abs(v) >= 1e18 for v in values):
            raise OverflowError
    except OverflowError:
        return dict(empty, details={'status': 'missing', 'reason': '异常价格导致计算越界'})
    score, annual, r2 = [Decimal(str(v)).quantize(Decimal('0.0000000001')) for v in values]
    return dict(score=score, annual_return=annual, r2=r2, reference_pass=None,
                details={'status': 'ok', 'calculation_version': CALCULATION_VERSION})


def calculate_rows(bars):
    groups = defaultdict(list)
    for bar in bars:
        groups[bar.code].append(bar)
    calendar = sorted({b.trade_date for b in bars})
    positions = {d:i for i,d in enumerate(calendar)}
    result = []
    for code, rows in groups.items():
        rows.sort(key=lambda r:r.trade_date)
        start = min(r.observation_start for r in rows)
        for i, row in enumerate(rows):
            if row.trade_date < start:
                continue
            window = rows[max(0,i-25):i+1]
            consecutive = len(window) == 26 and positions[row.trade_date]-positions[window[0].trade_date] == 25
            values = [r.adjusted_close for r in window] if consecutive else []
            result.append(dict(trade_date=row.trade_date, code=code, name=row.name, close=row.close,
                               updated_at=row.updated_at, **metrics(values, row.paused)))
    return result


def rebuild_snapshot(connection):
    head = connection.execute(select(Pool.__table__)).mappings().first()
    if head and head['rule_version'] != RAW_VERSION:
        raise SnapshotDataError('ETF热度需要先运行新版聚宽采集脚本，采集原始行情后再同步')
    bars = [SimpleNamespace(**dict(r)) for r in connection.execute(select(Bar.__table__)).mappings()]
    rows = calculate_rows(bars)
    if head:
        have = {r['code'] for r in rows if r['trade_date'] == head['as_of_date']}
        if {m['code'] for m in head['members']} - have:
            raise SnapshotDataError('ETF候选池对应的最新行情不完整，保留旧缓存')
    connection.execute(Heat.__table__.delete())
    for offset in range(0,len(rows),1000):
        connection.execute(Heat.__table__.insert(), rows[offset:offset+1000])
    return len(rows)
