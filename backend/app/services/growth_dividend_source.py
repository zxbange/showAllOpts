"""Public index closes and an independent exchange-session calendar.

Tencent serves at most 640 recent rows ending at ``end`` and may ignore ``start``.
All callers therefore request bounded windows and every response is filtered
locally. Only unadjusted price indices are used; no ETF or total-return proxy.
"""
from dataclasses import dataclass
from datetime import date, timedelta
import math
import re
import time
import logging

from app.core.diagnostics import diagnostic_details

LOG = logging.getLogger(__name__)

import httpx

START = date(2018, 1, 1)
CALENDAR_URL = 'https://finance.sina.com.cn/realstock/company/klc_td_sh.txt'
TENCENT_URL = 'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get'
EASTMONEY_URL = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'


class IndexSourceError(ValueError):
    """Provider response is unavailable or cannot safely represent index closes."""


@dataclass(frozen=True)
class IndexDefinition:
    key: str
    code: str
    name: str
    symbol: str
    secid: str
    first_date: date


INDEXES = (
    IndexDefinition('chinext', '399006', '创业板指', 'sz399006', '0.399006', START),
    IndexDefinition('dividend', '000922', '中证红利', 'sh000922', '1.000922', START),
    # The official base date is 2019-12-31. Public historical daily observations
    # start on 2020-01-02; the base value is not invented as a trading-day quote.
    IndexDefinition('star50', '000688', '科创50', 'sh000688', '1.000688', date(2020, 1, 2)),
)


@dataclass(frozen=True)
class IndexQuote:
    day: date
    close: float
    source: str
    source_url: str


def decode_sina_trade_calendar(payload: str) -> list[date]:
    """Decode only Sina's type-139 calendar stream, without executing JavaScript.

    The bit format is documented by the calendar (R) branch of AKShare's
    ``hk_js_decode`` in https://github.com/akfamily/akshare/blob/main/akshare/stock/cons.py.
    This small Python decoder deliberately rejects unsupported formats.
    """
    match = re.search(r'=\s*[\"\']([A-Za-z0-9+/]+)[\"\']', payload)
    token = match.group(1) if match else payload.strip()
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
    if not token or len(token) > 200000 or any(char not in alphabet for char in token):
        raise IndexSourceError('交易日历编码无效。')
    values = [alphabet.index(char) for char in token]
    position = 0

    def bits(width):
        nonlocal position
        if width < 0 or width > 30 or position + width > len(values) * 6:
            raise IndexSourceError('交易日历响应不完整。')
        result = 0
        for offset in range(width):
            result |= ((values[position // 6] >> (position % 6)) & 1) << offset
            position += 1
        return result

    def length_delta():
        sign = 1 if bits(1) else -1
        magnitude = 1
        while bits(1):
            magnitude += 1
            if magnitude > 30:
                raise IndexSourceError('交易日历游程长度无效。')
        return sign * magnitude

    codec, version = bits(12), 63 ^ bits(6)
    if codec != 139 or version > 1:
        raise IndexSourceError('交易日历编码版本不受支持。')
    width, remaining = 0, -1
    offset, final = bits(18) - 1, bits(18)
    origin = date(1970, 1, 1) + timedelta(days=7657)
    if final < offset or final - offset > 60000:
        raise IndexSourceError('交易日历日期范围无效。')
    days = []
    while offset < final:
        offset += 1
        weekday = offset % 7
        if weekday in (3, 4):
            offset += 5 - weekday
        day = origin + timedelta(days=offset)
        if remaining <= 0:
            if bits(1):
                width += length_delta()
            remaining = bits(3 * width) + 1
            if not days:
                days.append(day)
                remaining -= 1
        else:
            days.append(day)
        remaining -= 1
    return days


def fetch_calendar(client: httpx.Client) -> list[date]:
    response = client.get(CALENDAR_URL)
    response.raise_for_status()
    days = decode_sina_trade_calendar(response.text)
    if (not days or days[0] > START or len(days) < 5000 or
            days != sorted(set(days)) or any(day.weekday() >= 5 for day in days if day >= START)):
        raise IndexSourceError('交易日历范围或内容不完整。')
    return days


def _close(raw):
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        raise IndexSourceError('指数收盘点位不是有效数值。') from None
    if not math.isfinite(value) or not 0 < value < 10000000 or isinstance(raw, bool):
        raise IndexSourceError('指数收盘点位不在有效范围。')
    return value


def _rows(raw, start, end, source, url, *, separated=False):
    if not isinstance(raw, list):
        raise IndexSourceError('指数行情结构无效。')
    quotes = {}
    for entry in raw:
        entry = entry.split(',') if separated and isinstance(entry, str) else entry
        if not isinstance(entry, list) or len(entry) < 3:
            raise IndexSourceError('指数日线记录不完整。')
        try:
            day = date.fromisoformat(entry[0])
        except (TypeError, ValueError):
            raise IndexSourceError('指数交易日期无效。') from None
        # Tencent intentionally returns rows older than the requested start.
        if not start <= day <= end:
            continue
        close = _close(entry[2])
        if day in quotes and quotes[day].close != close:
            raise IndexSourceError('同一交易日返回冲突点位。')
        quotes[day] = IndexQuote(day, close, source, url)
    return quotes


def fetch_tencent(client, index, start, end):
    count = min(640, max(5, (end - start).days + 5))
    response = client.get(TENCENT_URL, params={
        'param': f'{index.symbol},day,{start},{end},{count},',
    })
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get('code') not in (0, '0'):
        raise IndexSourceError('腾讯指数行情请求未成功。')
    data = payload.get('data')
    item = data.get(index.symbol) if isinstance(data, dict) else None
    if not isinstance(item, dict) or 'day' not in item:
        raise IndexSourceError('腾讯未返回指定指数的日线。')
    return _rows(item['day'], start, end, '腾讯财经', str(response.url))


def fetch_eastmoney(client, index, start, end):
    response = client.get(EASTMONEY_URL, params={
        'secid': index.secid, 'klt': '101', 'fqt': '0',
        'beg': start.strftime('%Y%m%d'), 'end': end.strftime('%Y%m%d'),
        'lmt': '640', 'fields1': 'f1,f2,f3,f4,f5,f6',
        'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
    })
    response.raise_for_status()
    payload = response.json()
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, dict) or str(data.get('code')) != index.code:
        raise IndexSourceError('东方财富未返回指定指数的日线。')
    return _rows(data.get('klines'), start, end, '东方财富', str(response.url), separated=True)


def fetch_prices(client, index, start, end, *, expected_dates, check_cancel=lambda: None):
    """Keep valid partial results; a missing requested date always stays pending."""
    if start > end or (end - start).days > 366:
        raise IndexSourceError('指数行情必须按不超过一年的窗口读取。')
    expected = set(expected_dates)
    quotes = {}
    failures = []
    for source, fetcher in (('tencent', fetch_tencent), ('eastmoney', fetch_eastmoney)):
        # Tencent occasionally resets a connection despite healthy adjacent
        # annual windows. Retry one transient failure before the fallback;
        # malformed data and empty/stale responses are never retried in a loop.
        attempts = 2 if fetcher is fetch_tencent else 1
        for attempt in range(attempts):
            check_cancel()
            try:
                received = fetcher(client, index, start, end)
                for day, quote in received.items():
                    if day in expected and day not in quotes:
                        quotes[day] = quote
                break
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                LOG.warning('Index source request failed source=%s index=%s start=%s end=%s attempt=%s/%s %s',
                            source, index.code, start, end, attempt + 1, attempts, diagnostic_details(exc))
                transient = isinstance(exc, httpx.RequestError) or (
                    isinstance(exc, httpx.HTTPStatusError) and
                    (exc.response.status_code == 429 or exc.response.status_code >= 500))
                if transient and attempt + 1 < attempts:
                    check_cancel()
                    time.sleep(.25)
                    check_cancel()
                    continue
                failures.append(type(exc).__name__)
                break
        if expected.issubset(quotes):
            break
    check_cancel()
    if not quotes and failures:
        raise IndexSourceError('公开行情接口暂不可用，未完成日期会保留待补。')
    return quotes
