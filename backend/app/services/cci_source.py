"""Public third-party CCI observations, not an official licensed index feed."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
from math import isfinite

SOURCE_URL = 'https://coalindex.com/market'
ABOUT_URL = 'https://coalindex.com/about'
START = '2026-01-01'
GRADES = ('CCI5500', 'CCI5000', 'CCI4500')


class CCIError(ValueError):
    pass


def validate_rows(rows, *, today=None):
    """Validate the entire response before a transaction; absent changes stay absent."""
    today = today or date.today()
    if not isinstance(rows, list) or len(rows) > 10000:
        raise CCIError('报价响应格式或数量不符合预期')
    result = {}
    for item in rows:
        if not isinstance(item, dict):
            raise CCIError('报价记录格式不符合预期')
        day = item.get('quoteDate')
        try:
            parsed = date.fromisoformat(day)
        except (TypeError, ValueError):
            raise CCIError('报价日期无效') from None
        if parsed.isoformat() != day or not START <= day <= today.isoformat():
            raise CCIError('报价日期超出范围')
        grade = item.get('grade')
        if grade not in GRADES:
            raise CCIError('未知CCI档位')
        price = item.get('price')
        change = item.get('change')
        for value in (price, change):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value)):
                raise CCIError('报价数值无效')
        if price is None or not 0 < price < 100000:
            raise CCIError('报价价格无效')
        row = dict(quoteDate=day, grade=grade, price=price, change=change,
                   sourceUrl=SOURCE_URL, source='coalindex.com', evidence='public_query',
                   official=False, currency='CNY', unit='元/吨')
        key = (day, grade)
        if key in result and result[key] != row:
            raise CCIError('同日同档位报价冲突，保留原记录')
        result[key] = row
    return sorted(result.values(), key=lambda r: (r['quoteDate'], r['grade']))


def number(value, *, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or value is None:
        raise CCIError('报价数值无效')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise CCIError('报价数值无效') from None
    if not result.is_finite():
        raise CCIError('报价数值无效')
    return float(result)


def parse_response(payload, grade, start, end):
    if not isinstance(payload, dict) or not isinstance(payload.get('rows'), list):
        raise CCIError('公开报价响应格式无效')
    rows = payload['rows']
    total = payload.get('total')
    if isinstance(total, bool) or not isinstance(total, int) or total != len(rows) or len(rows) >= 1000:
        raise CCIError('公开报价可能被截断，保留历史')
    result = []
    for item in rows:
        if not isinstance(item, dict) or item.get('kind') != 'index' or not isinstance(item.get('data'), dict):
            raise CCIError('公开报价记录类型无效')
        data = item['data']
        if data.get('index_type') != '日指数' or data.get('index_name') != grade or data.get('unit') != '元/吨':
            raise CCIError('公开报价档位或单位不匹配')
        day = data.get('date')
        if not isinstance(day, str) or not start <= day <= end:
            raise CCIError('返回报价超出请求日期')
        result.append(dict(quoteDate=day, grade=grade, price=number(data.get('price')),
                           change=number(data.get('change'), nullable=True)))
    normalized = validate_rows(result, today=date.fromisoformat(end))
    if len(normalized) != len(rows):
        raise CCIError('公开报价存在重复记录')
    return normalized


def fetch_rows(client, start, end, *, should_stop=lambda: False):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last or start < START or (last - first).days > 3660:
        raise CCIError('请求日期范围无效')
    result = []
    day = first
    # One series and at most 31 dates per request: never rely on the source's
    # unfiltered endpoint fitting under its 1000-row response cap.
    while day <= last:
        finish = min(day + timedelta(days=30), last)
        for grade in GRADES:
            if should_stop():
                raise CCIError('采集已停止')
            params = dict(tab='steamcoal-index', date_from=day.isoformat(),
                          date_to=finish.isoformat(), index_name=grade)
            with client.stream('GET', 'https://coalindex.com/api/market', params=params) as response:
                response.raise_for_status()
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 2_000_000:
                        raise CCIError('公开报价响应过大')
                    chunks.append(chunk)
                payload = json.loads(b''.join(chunks))
            result.extend(parse_response(payload, grade, day.isoformat(), finish.isoformat()))
        day = finish + timedelta(days=1)
    return validate_rows(result, today=last)
