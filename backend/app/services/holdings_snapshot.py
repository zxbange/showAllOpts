"""Exact, observed position changes. Never interpret a partial read as liquidation."""
from decimal import Decimal, InvalidOperation, localcontext


class HoldingsError(ValueError):
    """Fixed, non-sensitive message safe for the authenticated management UI."""


MAX_ROWS = 100000
MAX_QUANTITY = Decimal('999999999999999999.999999')


def quantity(value):
    if isinstance(value, bool) or value is None:
        raise HoldingsError('持仓数量必须是非负数，不能为NULL')
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result > MAX_QUANTITY:
            raise ValueError()
        if result != result.quantize(Decimal('0.000001')):
            raise ValueError()
        return result
    except (ValueError, InvalidOperation):
        raise HoldingsError('持仓数量无效：须为非负数，最多6位小数且不能超出支持范围') from None


def number(value):
    return format(value.normalize(), 'f') if value else '0'


def label(value, maximum, field):
    # Do not cast integer codes: lost leading zeroes cannot be recovered safely.
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise HoldingsError(f'{field}必须是非空文本且不能超长；请检查源字段类型')
    return value.strip()


def normalize(rows):
    result = {}
    with localcontext() as context:
        context.prec = 40
        for index, row in enumerate(rows):
            if index >= MAX_ROWS:
                raise HoldingsError('持仓行数超过上限，未更新快照；请缩小源表范围')
            strategy = label(row['strategy2'], 200, '策略名称')
            code = label(row['code'], 64, '股票代码')
            name = label(row['name'], 200, '股票名称')
            amt = quantity(row['amt'])
            key = (strategy, code)
            if key in result:
                if result[key]['name'] != name:
                    raise HoldingsError('同一策略和代码存在不同股票名称，未更新快照')
                amt += Decimal(result[key]['amt'])
                if amt > MAX_QUANTITY:
                    raise HoldingsError('同一策略和代码的合计股数超过上限，未更新快照')
            result[key] = dict(strategy2=strategy, code=code, name=name, amt=number(amt))
    return [result[key] for key in sorted(result) if Decimal(result[key]['amt']) > 0]


def changes(previous, current):
    before = {(row['strategy2'], row['code']): row for row in previous}
    after = {(row['strategy2'], row['code']): row for row in current}
    events = []
    with localcontext() as context:
        context.prec = 40
        for key in sorted(before.keys() | after.keys()):
            old, new = before.get(key), after.get(key)
            old_amt = Decimal(old['amt']) if old else Decimal(0)
            new_amt = Decimal(new['amt']) if new else Decimal(0)
            delta = new_amt - old_amt
            if not delta:
                continue
            kind = 'buy' if old is None else 'clear' if new is None else 'add' if delta > 0 else 'reduce'
            row = new or old
            events.append(dict(strategy2=key[0], code=key[1], name=row['name'],
                               kind=kind, delta=number(delta), before=number(old_amt), after=number(new_amt)))
    return events
