"""Bounded Telegram facts from caller-supplied, already-published snapshots.

Never performs I/O or copies source names, URLs, exception strings or free text.
A comparison uses a verified earlier sample, not an assumed previous calendar day.
"""
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext

MISSING = '—'
_INDEX_NAMES = {'000300.XSHG': '沪深300', '000016.XSHG': '上证50', '000852.XSHG': '中证1000'}
_ISO_MAX_LENGTH = 10


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _date(value):
    if type(value) is date:
        return value.isoformat()
    if isinstance(value, str) and len(value) == _ISO_MAX_LENGTH:
        try:
            parsed = date.fromisoformat(value).isoformat()
            return parsed if value == parsed else None
        except ValueError:
            pass
    return None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    # Bound formatting and calculation even if a persisted payload is malformed.
    try:
        raw = str(value)
        if len(raw) > 80:
            return None
        number = Decimal(raw)
    except (ValueError, InvalidOperation):
        return None
    return number if number.is_finite() and number.copy_abs() <= Decimal('1e20') else None


def _text(value, places=4):
    if value is None:
        return MISSING
    with localcontext() as ctx:
        ctx.prec = 40
        rounded = value.quantize(Decimal(1).scaleb(-places))
        if not rounded:
            return '0'
        result = format(rounded, 'f')
        return result.rstrip('0').rstrip('.') if '.' in result else result


def _count(value):
    return str(value) if type(value) is int and 0 <= value <= 10**9 else MISSING


def _diff(current, previous):
    if current is None or previous is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 40
        return current - previous


def _change(current, previous, places=4):
    diff = _diff(current, previous)
    text = _text(diff, places)
    return '+' + text if text not in (MISSING, '0') and diff > 0 else text


def _quote(current, previous):
    return MISSING if current is None else f'{_text(current)}({_change(current, previous)})'


def _series(values, *, subkey=None):
    for key, item in _mapping(values).items():
        if subkey is not None:
            if not isinstance(key, tuple) or len(key) != 2 or key[1] != subkey:
                continue
            key = key[0]
        day = _date(key)
        if day is not None:
            yield day, item


def _latest(samples, get, *, before=None):
    result_day, result = None, None
    for day, item in samples:
        if (before is not None and day >= before) or (result_day is not None and day <= result_day):
            continue
        value = get(item)
        if value is not None:
            result_day, result = day, value
    return result_day, result


def _previous(samples, get, day):
    return _latest(samples, get, before=day) if day else (None, None)


def _basis(previous_date):
    return '对比日 ' + previous_date if previous_date else '对比日 —'


def _series_line(label, samples, get=_number, *, scale=1, unit='', change_unit='', places=4):
    current_day, current = _latest(samples, get)
    if current_day is None:
        return f'{label}：{MISSING}（未取得已发布样本）'
    previous_day, previous = _previous(samples, get, current_day)
    current = current * scale
    previous = previous * scale if previous is not None else None
    delta = _change(current, previous, places)
    return (f'{label}：{_text(current, places)}{unit}（{current_day}；{_basis(previous_day)}；'
            f'变动 {delta}{change_unit if delta != MISSING else ""}）')


def _commodities(values):
    samples = list(_series(values))
    day, row = _latest(samples, lambda item: item if isinstance(item, Mapping) else None)
    if not day:
        return [f'最新已发布报价：{MISSING}']
    lines = [f'最新已发布报价：{day}（元/吨）']
    for key, label in (('low', '最低'), ('high', '最高'), ('mean', '均价')):
        get = lambda item: _number(_mapping(item).get(key))
        previous, old = _previous(samples, get, day)
        lines.append(f'{label}：{_quote(get(row), old)}（{_basis(previous)}）')
    return lines


def _cci(values):
    lines = []
    for grade in ('CCI5500', 'CCI5000', 'CCI4500'):
        samples = list(_series(values, subkey=grade))
        get = lambda item: _number(_mapping(item).get('price'))
        day, value = _latest(samples, get)
        previous, old = _previous(samples, get, day)
        lines.append(f'{grade}：{_quote(value, old)} 元/吨（{day or MISSING}；{_basis(previous)}）'
                     if day else f'{grade}：{MISSING}（未取得已发布报价）')
    return lines


def _growth_dividend(values):
    codes = (('399006', '创业板指'), ('000688', '科创50'), ('000922', '中证红利'))
    series = {code: {day: _number(value) for day, value in _series(values, subkey=code)}
              for code, _ in codes}
    lines = [_series_line(label, list(series[code].items()), unit='点', change_unit='点')
             for code, label in codes]
    base = series['000922']
    for code, label in (('399006', '创红比'), ('000688', '科创红利比')):
        ratios = []
        with localcontext() as ctx:
            ctx.prec = 40
            for day, close in series[code].items():
                if close is not None and close > 0 and base.get(day) is not None and base[day] > 0:
                    ratios.append((day, close / base[day]))
        lines.append(_series_line(label, ratios, places=6))
    return lines


def _valuation_ruler(values):
    lines = []
    specs = (('index_close', '指数', '点'), ('mean_pe_ttm', '滚动等权PE', '倍'),
             ('median_pe_ttm', '滚动中位PE', '倍'), ('overall_pe_lyr', '静态整体PE', '倍'),
             ('mean_pe_lyr', '静态等权PE', '倍'), ('median_pe_lyr', '静态中位PE', '倍'))
    legacy = not any(isinstance(key, tuple) for key in values)
    for code, label in _INDEX_NAMES.items():
        if legacy and code != '000300.XSHG':
            lines.append(f'{label}：{MISSING}（未取得已发布估值）')
            continue
        samples = list(_series(values, subkey=None if legacy else code))
        get_row = lambda x: x if isinstance(x, Mapping) else None
        day, current = _latest(samples, get_row)
        if not day:
            lines.append(f'{label}：{MISSING}（未取得已发布估值）')
            continue
        previous, old = _previous(samples, get_row, day)
        parts = []
        for key, name, unit in specs:
            value, earlier = _number(current.get(key)), _number(_mapping(old).get(key))
            parts.append(f'{name} {_quote(value, earlier)}{unit if value is not None else ""}')
        lines.append(f'{label}：{day}；{_basis(previous)}')
        lines.append('；'.join(parts[:3]))
        lines.append('；'.join(parts[3:]))
    return lines


def _cash_snapshot(value):
    if isinstance(value, list):  # Legacy projection has a count but no as-of date.
        return None, len(value)
    payload = _mapping(value)
    snapshot = _mapping(payload.get('snapshot'))
    count = snapshot.get('matched_count')
    if _count(count) == MISSING and isinstance(payload.get('items'), list):
        count = len(payload['items'])
    return _date(snapshot.get('as_of')), count


def _cash_cow(values, previous):
    lines = []
    for period in (3, 5, 10):
        day, count = _cash_snapshot(values.get(period))
        previous_day, old_count = _cash_snapshot(previous.get(period))
        earlier = previous_day and day and previous_day < day
        difference = _change(_number(count), _number(old_count)) if earlier else MISSING
        lines.append(f'近{period}年：入选 {_count(count)} 只（筛选日 {day or MISSING}；'
                     f'{_basis(previous_day if earlier else None)}；变动 {difference}）')
    return lines


def summarize_published(task, values, *, previous=None):
    """Render latest verified business metrics, independent of this run's changes."""
    values = _mapping(values)
    if task == 'margin':
        return [_series_line('两融担保比例', list(_series(values)), unit='%', change_unit='个百分点')]
    if task == 'commodities':
        return _commodities(values)
    if task == 'cci':
        return _cci(values)
    if task == 'growth_dividend':
        return _growth_dividend(values)
    if task == 'valuation_ruler':
        return _valuation_ruler(values)
    if task == 'cash_cow':
        return _cash_cow(values, _mapping(previous))
    return [f'已发布业务指标：{MISSING}']


def summarize_market(samples):
    """Independent metrics; caller supplies only cache-published, trusted payloads."""
    samples = _mapping(samples)
    lines = []
    for key, label in (('heat', '市场热度'), ('crowding', '抱团拥挤度')):
        series = list(_series(samples.get(key)))
        def get(item):
            ratio = _number(_mapping(item).get('ratio'))
            return ratio if ratio is not None and 0 <= ratio <= 1 else None
        lines.append(_series_line(label, series, get, scale=100, unit='%', change_unit='个百分点'))
        day, _ = _latest(series, get)
        if day is None:
            continue
        row = _mapping(dict(series)[day])
        if key == 'heat':
            above, eligible = _count(row.get('above_count')), _count(row.get('eligible_count', row.get('valid_count')))
            lines.append(f'站上 MA20：{above} / {eligible} 只')
        else:
            top = _count(row.get('top_stock_count', row.get('top_count')))
            count = _count(row.get('stock_count', row.get('universe_count')))
            amounts = [_number(row.get(field, row.get(alias)))
                       for field, alias in (('top_money', 'top_amount'), ('total_money', 'total_amount'))]
            lines.append(f'前5%样本：{top} / {count} 只；成交额 {_text(amounts[0], 2)} / {_text(amounts[1], 2)} 元')
    return lines


def _dividend_group(snapshot, method):
    group = _mapping(_mapping(snapshot.get('groups')).get(method))
    value = group.get('count', snapshot.get('bank_count' if method == 'bank' else 'nonbank_count'))
    items = group.get('items')
    yields = []
    if isinstance(items, list):
        for item in items[:50]:
            rate = _number(_mapping(item).get('dividend_yield_1y'))
            if rate is not None and rate > 0:
                yields.append(rate)
    return value, max(yields, default=None)


def summarize_dividend(current, previous=None):
    """Both groups are published atomically; accept grouped or compact count facts."""
    current, previous = _mapping(current), _mapping(previous)
    day, previous_day = (_date(x.get('trade_date') or x.get('date')) for x in (current, previous))
    if not day:
        return [f'最新已发布选股：{MISSING}']
    if previous_day is None or previous_day >= day:
        previous, previous_day = {}, None
    lines = [f'最新已发布选股：{day}；{_basis(previous_day)}']
    if _count(current.get('universe_count')) != MISSING:
        lines.append(f'股票池 {_count(current.get("universe_count"))} 只；符合资格 {_count(current.get("eligible_count"))} 只')
    for method, label in (('value', '非银行'), ('bank', '银行')):
        count, high = _dividend_group(current, method)
        old, old_high = _dividend_group(previous, method)
        delta = _change(_number(count) if _count(count) != MISSING else None,
                        _number(old) if _count(old) != MISSING else None)
        yield_delta = _change(high, old_high)
        lines.append(f'{label}：入选 {_count(count)} 只（变动 {delta}）；最高静态股息率 '
                     f'{_text(high)}{"%" if high is not None else ""}（变动 '
                     f'{yield_delta}{"个百分点" if yield_delta != MISSING else ""}）')
    return lines
