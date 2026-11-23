"""Pure A-share ticker normalization and position valuation arithmetic."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re

from app.services.holdings_snapshot import HoldingsError, number, quantity

_SUFFIXES = {'SH': 'SH', 'XSHG': 'SH', 'SZ': 'SZ', 'XSHE': 'SZ', 'BJ': 'BJ'}
_SYMBOL = re.compile(r'(?:(SH|SZ|BJ)([0-9]{6})|([0-9]{6})(?:\.(SH|XSHG|SZ|XSHE|BJ))?)')
_CENT = Decimal('0.01')
_PRICE_LIMIT = Decimal('1000000000000')


def quote_symbol(code: str) -> str | None:
    """Return Tushare six-digit .SH/.SZ/.BJ identity, or None if unsupported.

    Bare codes are interpreted in the mainland A-share namespaces only. The
    Beijing 920xxx range is accepted bare; older OTC-overlapping numbers
    need an explicit Beijing exchange. Even then only a matching returned
    same-date close can value that identity; never translate to a new code.
    """
    if not isinstance(code, str) or len(code) > 64 or not code.isascii():
        return None
    match = _SYMBOL.fullmatch(code.strip().upper())
    if match is None:
        return None
    prefix, prefixed, suffixed, suffix = match.groups()
    digits = prefixed or suffixed
    explicit = prefix if prefix else _SUFFIXES.get(suffix)
    if digits.startswith(('600', '601', '603', '605', '688', '689')):
        expected = 'SH'
    elif digits.startswith(('000', '001', '002', '003', '300', '301')):
        expected = 'SZ'
    elif digits.startswith('920') or (explicit == 'BJ' and digits.startswith(('43', '83', '87'))):
        expected = 'BJ'
    else:
        return None
    return f'{digits}.{expected}' if explicit in (None, expected) else None


def _decimal(raw):
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (ValueError, InvalidOperation):
        return None
    if not value.is_finite() or value < 0:
        return None
    return value


def _close(raw):
    value = _decimal(raw)
    # Bound untrusted quote magnitudes and precision before arithmetic. Market
    # closes have far fewer digits; no rounding or truncation repairs bad inputs.
    if value is None or not 0 < value < _PRICE_LIMIT:
        return None
    scaled = value.quantize(Decimal('0.000000000001'))
    return scaled if value == scaled else None


def value_positions(rows, closes) -> tuple[dict[str, str], dict[str, str | None]]:
    """Value normalized snapshot rows using closes keyed by quote_symbol().

    Publish known row prices keyed by the original code. Missing or malformed
    inputs poison only that strategy's total; never publish a partial total.
    Round the exact sum, not individual rows, to cents (half up).
    """
    prices, totals, parsed = {}, {}, {}
    with localcontext() as ctx:
        # 24 quantity digits + 24 quote digits + 5 summation digits; the
        # snapshot bounds rows at 100,000.
        ctx.prec = 64
        for row in rows:
            strategy, code = row['strategy2'], row['code']
            total = totals.setdefault(strategy, Decimal(0))
            symbol = quote_symbol(code)
            if symbol not in parsed:
                parsed[symbol] = _close(closes.get(symbol)) if symbol else None
            price = parsed[symbol]
            if price is not None:
                prices[code] = number(price)
            try:
                amt = quantity(row['amt'])
            except HoldingsError:
                amt = None
            totals[strategy] = (
                total + amt * price
                if total is not None and amt is not None and price is not None else None
            )
        return prices, {
            strategy: format(total.quantize(_CENT, rounding=ROUND_HALF_UP), 'f')
            if total is not None else None for strategy, total in totals.items()
        }
