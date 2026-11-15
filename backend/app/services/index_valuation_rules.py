"""Shared index PE calculation; embedded into standalone JoinQuant collectors by the local tool."""
import builtins as _builtins
import datetime as dt
from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_EVEN
import hashlib
import json
import re

class HistoryError(ValueError):
    pass

INDEX = '000300.XSHG'
INDEXES = {'000300.XSHG': ('沪深300', 300, '2010-01-01'),
           '000016.XSHG': ('上证50', 50, '2010-01-01'),
           '000852.XSHG': ('中证1000', 1000, '2014-10-17')}
METHOD = 'index_pe_lyr_ttm_cap300_v2'
SOURCE = 'JQ_PLATFORM.VALUATION.V2'
PUBLIC_SOURCE = 'EASTMONEY.VALUATION.V2'

def china_now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=8)

def number(value, precision, scale, field='数值', round_source=False):
    if _builtins.isinstance(value, _builtins.bool) or value is None:
        raise HistoryError('{}：数值缺失或类型错误。'.format(field))
    try:
        result = Decimal(_builtins.str(value))
    except (InvalidOperation, ValueError):
        raise HistoryError('{}：数值无效。'.format(field)) from None
    with localcontext() as ctx:
        ctx.prec = 80
        if not result.is_finite():
            raise HistoryError('{}：数值非有限（NaN/Infinity）。'.format(field))
        bound = Decimal(10) ** (precision-scale)
        if result.copy_abs() >= bound:
            raise HistoryError('{}：数值超出 DECIMAL({},{}) 范围。'.format(field, precision, scale))
        rounded = result.quantize(Decimal(10) ** -scale, rounding=ROUND_HALF_EVEN)
        if result != rounded:
            if not round_source:
                raise HistoryError('{}：小数精度超过 {} 位；已有数据及汇总不自动舍入。'.format(field, scale))
            if rounded.copy_abs() >= bound or rounded == 0:
                raise HistoryError('{}：按 {} 位小数舍入后越界或归零。'.format(field, scale))
            result = rounded
    return result


def derived(value, scale):
    with localcontext() as ctx:
        ctx.prec = 80
        return value.quantize(Decimal(10) ** -scale, rounding=ROUND_HALF_EVEN)


def valid_code(code):
    return (_builtins.isinstance(code, _builtins.str)
            and re.match(r'^[036]\d{5}\.(?:XSHG|XSHE)\Z', code) is not None
            and ((code.startswith('6') and code.endswith('.XSHG'))
                 or (code.startswith(('0', '3')) and code.endswith('.XSHE'))))


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def source_number(value, field):
    if value is None:
        return None
    try:
        if Decimal(_builtins.str(value)).is_nan():
            return None
    except (InvalidOperation, ValueError):
        pass
    return number(value, 30, 12, field, round_source=True)


def aggregate(members, field):
    """Separate samples for each PE basis; never apply the 300 cap to overall PE."""
    active = [row for row in members if row['suspended_listing'] is not True]
    values = [Decimal(row[field]) for row in active if row[field] is not None]
    selected = _builtins.sorted(value for value in values if 0 < value <= 300)
    valid = [row for row in active if row[field] is not None and row['market_cap'] is not None]
    counts = dict(eligible=_builtins.len(selected), loss=_builtins.sum(v < 0 for v in values),
                  over_limit=_builtins.sum(v > 300 for v in values), missing_pe=_builtins.len(active)-_builtins.len(values),
                  suspended_listing=_builtins.len(members)-_builtins.len(active),
                  listing_status_unverified=_builtins.sum(row['suspended_listing'] is None for row in members),
                  overall_valid=_builtins.len(valid), overall_missing=_builtins.len(active)-_builtins.len(valid))
    with localcontext() as ctx:
        ctx.prec = 80
        mean = derived(_builtins.sum(selected, Decimal(0))/_builtins.len(selected), 12) if selected else None
        middle = _builtins.len(selected)//2
        median = (selected[middle] if _builtins.len(selected)%2 else
                  derived((selected[middle-1]+selected[middle])/2, 12)) if selected else None
        cap = _builtins.sum((Decimal(r['market_cap']) for r in valid), Decimal(0))
        profit = _builtins.sum((Decimal(r['market_cap'])/Decimal(r[field]) for r in valid), Decimal(0))
        # Missing/zero-profit PE must not produce a misleading whole-index ratio.
        overall = derived(cap/profit, 12) if valid and _builtins.len(valid) == _builtins.len(active) and profit > 0 else None
        counts.update(market_cap_yi=format(cap, 'f'), implied_profit_yi=format(derived(profit, 12), 'f'))
    return overall, mean, median, counts


def summarize(day, close, members, index_code=INDEX, source=SOURCE):
    if source not in (SOURCE, PUBLIC_SOURCE):
        raise HistoryError('估值来源无效。')
    if index_code not in INDEXES:
        raise HistoryError('指数不在支持范围。')
    name, expected, _ = INDEXES[index_code]
    codes = [row['code'] for row in members]
    if (_builtins.len(codes) != expected or _builtins.len(_builtins.set(codes)) != expected
            or not _builtins.all(valid_code(code) for code in codes)):
        raise HistoryError('{}须有{}个唯一当日成分。'.format(name, expected))
    close = number(close, 24, 8, index_code+' close')
    if close <= 0:
        raise HistoryError('指数收盘价必须为正。')
    checked = []
    for row in _builtins.sorted(members, key=lambda r: r['code']):
        status = row.get('suspended_listing')
        if status is not None and _builtins.type(status) is not _builtins.bool:
            raise HistoryError('暂停上市标记必须为布尔值或未独立核验的NULL。')
        item = dict(code=row['code'], suspended_listing=status)
        for field in ('pe_ttm', 'pe_lyr', 'market_cap'):
            value = row[field]
            if value is not None:
                value = number(value, 30, 12, row['code']+' '+field)
                if value == 0 or (field == 'market_cap' and value < 0):
                    raise HistoryError('{} {}：零值或负市值无效。'.format(row['code'], field))
            item[field] = None if value is None else format(value, '.12f')
        checked.append(item)
    key = dict(trade_date=day, index_code=index_code, method_version=METHOD, source=source, updated_at=china_now())
    daily = dict(key, index_close=close, member_count=expected)
    stats = dict(listing_status_basis=('historical_index_members' if source == SOURCE else 'eastmoney_current_members'), profit_basis='market_cap_divided_by_pe',
                 market_cap_unit='亿元', ordinary_trading_suspension_excluded=False)
    for basis in ('ttm', 'lyr'):
        overall, mean, median, stats[basis] = aggregate(checked, 'pe_'+basis)
        for metric, value in (('overall', overall), ('mean', mean), ('median', median)):
            daily[metric+'_pe_'+basis] = None if value is None else number(value, 30, 12)
    payload = json_text(checked)
    daily.update(statistics=json_text(stats), members_sha256=hashlib.sha256(payload.encode('utf-8')).hexdigest())
    return daily, dict(key, payload=payload)
