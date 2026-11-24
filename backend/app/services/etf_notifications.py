"""Explain the committed ETF target using the same fixed funds on both dates."""
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select

from app.core.etf_catalog import ETF_GROUPS, GROUP_NAMES
from app.models.etf import ETFShare


_GROUP_CODES = {
    key: tuple(code for code, _ in members) for key, members in ETF_GROUPS.items()
}


def _quantities(rows):
    """Retain only valid integral share units; never replace a missing key by 0."""
    values, seen = {}, set()
    for code, units in rows:
        if code in seen:
            values.pop(code, None)
        elif type(units) is int and 0 <= units <= 9_223_372_036_854_775_807:
            values[code] = units
        seen.add(code)
    return values


def _wan_shares(units, *, signed=False):
    """Render the exact count in 万份 without losing a single share to rounding."""
    sign = '-' if units < 0 else '+' if units > 0 and signed else ''
    whole, fraction = divmod(abs(units), 10_000)
    fractional = f'.{fraction:04d}'.rstrip('0') if fraction else ''
    return f'{sign}{whole:,}{fractional}'


@dataclass(slots=True)
class ETFShareComparison:
    target: date
    previous: date | None
    current: dict | None = None
    baseline: dict | None = None

    def capture_committed(self, connection, shares):
        """One optional post-commit, primary-key scoped read on the writer checkout."""
        self.current = _quantities((row['fund_code'], row['shares']) for row in shares
                                   if row['trade_date'] == self.target)
        # There can be no comparison without a complete current group. Avoid
        # remote read work for an unpublished day or only incomplete groups.
        completed = tuple(codes for codes in _GROUP_CODES.values() if all(code in self.current for code in codes))
        wanted = sorted({code for codes in completed for code in codes})
        if not self.previous or not wanted:
            return
        table = ETFShare.__table__
        query = select(table.c.fund_code, table.c.shares).where(
            table.c.fund_code.in_(wanted), table.c.trade_date == self.previous)
        self.baseline = _quantities(connection.execute(query))

    def lines(self):
        if self.previous is None:
            lines = ['份额对比：前一交易日未核验（单位：万份）']
        else:
            lines = [f'份额对比：{self.previous.isoformat()} → {self.target.isoformat()}（单位：万份）']
        for key, codes in _GROUP_CODES.items():
            lines.append(f'{GROUP_NAMES[key]}：{self._group_line(codes)}')
        return lines

    def _group_line(self, codes):
        if self.current is None:
            return '当日已入库份额未核实，无法计算增减'
        missing = [code[:6] for code in codes if code not in self.current]
        if missing:
            return f'当日缺 {len(missing)}/{len(codes)} 只（{"、".join(missing)}），无法计算增减'
        total = sum(self.current[code] for code in codes)
        if self.baseline is None:
            return f'当日 {_wan_shares(total)}；前交易日未核验，无法计算增减'
        missing = [code[:6] for code in codes if code not in self.baseline]
        if missing:
            return (f'当日 {_wan_shares(total)}；前交易日缺 {len(missing)}/{len(codes)} 只'
                    f'（{"、".join(missing)}），无法计算增减')
        prior = sum(self.baseline[code] for code in codes)
        delta = total - prior
        return f'当日 {_wan_shares(total)}；前交易日 {_wan_shares(prior)}；增减 {_wan_shares(delta, signed=True)}'


@dataclass(slots=True)
class ETFRunSummary:
    enabled: bool
    target: date | None = None
    comparison: ETFShareComparison | None = None
    coverage: tuple[int, int, int] | None = None
    historical_gaps: set | None = None
    committed_changes: int = 0
    commit_unconfirmed: bool = False

    def set_target(self, target, calendar):
        self.target = target
        if not self.enabled:
            return
        try:
            previous = calendar.previous(target)
            if type(previous) is not date or previous >= target:
                previous = None
        except Exception:
            # The comparison is optional; the write maintains its own strict
            # calendar checks even if a prior session cannot be displayed.
            previous = None
        self.comparison = ETFShareComparison(target, previous)

    def lines(self, known_history=None):
        if self.coverage is None:
            lines = ['目标日份额和收盘价未核验']
        else:
            shares, prices, total = self.coverage
            lines = [f'目标日来源核验：份额 {shares}/{total} 只，收盘价 {prices}/{total} 只']
        if self.historical_gaps is None:
            suffix = f'；最早已知待补 {known_history.isoformat()}' if known_history is not None else ''
            lines.append('历史：本轮未核验完整度' + suffix)
        elif not self.historical_gaps:
            lines.append('历史：本轮补查范围内无缺口')
        else:
            days = {day for _, _, day in self.historical_gaps}
            shares = sum(kind == 'shares' for kind, _, _ in self.historical_gaps)
            prices = len(self.historical_gaps) - shares
            lines.append(f'历史待补：{len(days)} 个交易日，份额 {shares} 项，收盘价 {prices} 项，最早 {min(days).isoformat()}')
        if self.commit_unconfirmed and self.committed_changes:
            lines.append(f'此前已确认变更 {self.committed_changes} 条；另有提交结果待确认')
        if self.comparison is not None:
            lines.extend(self.comparison.lines())
        return lines
