"""ETF-only observation scope and routing; no database or exchange access."""

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Literal, Protocol

ObservationKey = tuple[str, str, date]
ManualPlan = tuple[tuple[date, ...], set[ObservationKey], set[ObservationKey]]


class ObservationRow(Protocol):
    def __getitem__(self, key: str, /) -> str: ...


class PlanningCalendar(Protocol):
    def range(self, start: date, end: date, /) -> Iterable[date]: ...


def plan_manual_observations(
    rows: Iterable[ObservationRow],
    saved_floor: str | None,
    calendar: PlanningCalendar,
    end: date,
    funds: Iterable[str],
) -> ManualPlan:
    """Plan from observations and a saved gap floor already bounded by end."""
    expected = {(kind, code) for kind in ('shares', 'prices') for code in funds}
    observed: dict[date, dict[tuple[str, str], str]] = {}
    for row in rows:
        key = row['kind'], row['code']
        if key in expected:
            try:
                day = date.fromisoformat(row['day'])
            except (TypeError, ValueError):
                continue
            observed.setdefault(day, {})[key] = row['status']
    completed = sorted(
        day
        for day, coverage in observed.items()
        if all(coverage.get(key) == 'complete' for key in expected)
    )
    candidates = [completed[-1] if completed else end]
    if completed:
        # A later fully covered scheduled date does not erase a known
        # earlier gap, but old blocked history alone does not open a range.
        candidates.extend(
            day
            for day, coverage in observed.items()
            if completed[0] < day
            and any(
                value in ('pending', 'manual_only', 'retry_exhausted')
                for value in coverage.values()
            )
        )
    if saved_floor is not None:
        candidates.append(date.fromisoformat(saved_floor))
    available = getattr(calendar, 'days', ())
    if not completed and saved_floor is None:
        # Bootstrap only the last three verified sessions. The first known
        # date has no proven predecessor and cannot be a repair target.
        recent = [day for day in available[1:] if day <= end][-3:]
        if recent:
            candidates.append(recent[0])
    start = min(candidates)
    if available:
        start = max(start, available[0])
    scope = (end,) if start >= end else tuple(dict.fromkeys(calendar.range(start, end)))
    required = {
        (kind, code, day)
        for day in scope
        if day < end and (day != scope[0] or day not in completed)
        for kind, code in expected
    }
    unchecked = {
        (kind, code, day)
        for kind, code, day in required
        if observed.get(day, {}).get((kind, code)) != 'complete'
    }
    return scope, required, unchecked


class ScopedSourceGuard:
    """One source's release window and rate limit, confined to this run."""

    def __init__(self) -> None:
        self.rate_limited = False
        self.available_from: date | None = None

    def skip_reason(self, day: date) -> Literal['rate_limit', 'archive'] | None:
        if self.rate_limited:
            return 'rate_limit'
        if self.available_from is not None and day < self.available_from:
            return 'archive'
        return None

    def observe(self, error_details: Mapping[str, object], available_from: date | None) -> None:
        if error_details.get('http_status') == 429:
            self.rate_limited = True
        if available_from is not None and (
            self.available_from is None or available_from > self.available_from
        ):
            self.available_from = available_from


def observation_source(
    kind: str, code: str, day: date, today: date, guards: Mapping[str, ScopedSourceGuard]
) -> tuple[str, dict[str, bool], ScopedSourceGuard | None]:
    if kind == 'shares' and code.endswith('.XSHG'):
        return 'sse_scoped_shares', {'scoped_sse': True}, guards['sse_scoped_shares']
    if kind == 'prices' and code.endswith('.XSHE') and day < today:
        return 'szse_dated_close', {'dated_szse_prices': True}, guards['szse_dated_close']
    return 'exchanges', {}, None
