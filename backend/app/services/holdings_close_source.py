"""Bulk, date-only Tushare unadjusted A-share daily closes.

The provider documents a 6,000-row ceiling and a 15:00–16:00 release window,
but no pagination contract for `daily`. Reject a cap-sized response rather
than guessing offsets or asking for any of the user's security identifiers.
Empty or partial market publication at 15:05 is possible; this adapter cannot
prove completeness below the cap. Callers must check every required symbol.
"""
from datetime import date
from decimal import Decimal
import json
import re

import httpx


_URL = 'https://api.tushare.pro'
_FIELDS = ('ts_code', 'trade_date', 'close')
_ROW_CAP = 6000
_RESPONSE_CAP = 2 * 1024 * 1024
_CODE = re.compile(r'[0-9]{6}\.(?:SH|SZ|BJ)')
_PRICE = re.compile(r'[0-9]{1,8}(?:\.[0-9]{1,8})?')
_MAX_PRICE = Decimal('99999999.99999999')
_MIN_PRICE = Decimal('0.00000001')


class HoldingsCloseUnavailable(ValueError):
    """A safe outward diagnostic, with no raw provider messages or credentials."""

    def __init__(self) -> None:
        super().__init__('当日未复权收盘价暂不可用。')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate response key')
        result[key] = value
    return result


def _exact_price(value):
    if type(value) is str and _PRICE.fullmatch(value):
        value = Decimal(value)
    elif type(value) is int:
        value = Decimal(value)
    if type(value) is not Decimal or not value.is_finite():
        return None
    if not _MIN_PRICE <= value <= _MAX_PRICE:
        return None
    # Check scale without rounding and without expanding tiny exponents into
    # unbounded strings. Trailing zeroes do not count against precision.
    _, digits, scale = value.as_tuple()
    count = len(digits)
    while count and digits[count - 1] == 0:
        count -= 1
        scale += 1
    if scale < -8:
        return None
    whole, sep, fraction = format(value, 'f').partition('.')
    fraction = fraction.rstrip('0')
    return whole + sep + fraction if fraction else whole


def _decode(body: bytes | bytearray, target: str) -> dict[str, str]:
    payload = json.loads(body, parse_float=Decimal, object_pairs_hook=_unique_object)
    if not isinstance(payload, dict) or type(payload.get('code')) is not int or payload['code'] != 0:
        raise HoldingsCloseUnavailable()
    data = payload.get('data')
    if not isinstance(data, dict):
        raise HoldingsCloseUnavailable()
    fields, rows = data.get('fields'), data.get('items')
    if (not isinstance(fields, list) or len(fields) != len(_FIELDS)
            or not all(fields.count(field) == 1 for field in _FIELDS)
            or not isinstance(rows, list) or not 0 < len(rows) < _ROW_CAP
            or data.get('has_more', False) is not False
            or payload.get('has_more', False) is not False):
        raise HoldingsCloseUnavailable()
    code_col, day_col, close_col = (fields.index(f) for f in _FIELDS)
    closes = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != len(_FIELDS):
            raise HoldingsCloseUnavailable()
        symbol = row[code_col]
        if (not isinstance(symbol, str) or not _CODE.fullmatch(symbol)
                or symbol in closes or row[day_col] != target):
            raise HoldingsCloseUnavailable()
        value = _exact_price(row[close_col])
        if value is None:
            raise HoldingsCloseUnavailable()
        closes[symbol] = value
    return closes


def fetch_daily_closes(day: date, token: str) -> dict[str, str]:
    """Fetch market daily closes for exactly one session, not securities held."""
    if type(day) is not date or not isinstance(token, str) or not token.strip():
        raise HoldingsCloseUnavailable()
    target = day.isoformat().replace('-', '')
    payload = dict(api_name='daily', token=token, params={'trade_date': target}, fields=','.join(_FIELDS))
    try:
        # Redirects stay disabled so credentials cannot move to another endpoint.
        with httpx.Client(timeout=httpx.Timeout(20, connect=8), follow_redirects=False) as client:
            with client.stream('POST', _URL, json=payload) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > _RESPONSE_CAP:
                        raise HoldingsCloseUnavailable()
                    body.extend(chunk)
        return _decode(body, target)
    except (httpx.HTTPError, ValueError, ArithmeticError, RecursionError):
        pass
    # The safe exception has no raw provider/request exception as cause/context.
    raise HoldingsCloseUnavailable()
