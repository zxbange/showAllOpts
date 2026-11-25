"""Pure source-MAX gap planning and durable metric-state transitions."""

from collections.abc import Collection, Mapping, Sequence, Set
from dataclasses import dataclass
from typing import Literal, cast

from app.services.market_observation_rules import SourceFailure

PUBLISHED = ('written', 'existing_preserved')
METRIC_DONE = (*PUBLISHED, 'outside_window')
GapOperation = Literal['clear', 'remember']


@dataclass(frozen=True)
class GapPlan:
    target: str | None
    latest: dict[str, str | None]
    needed: dict[str, set[str]]
    planned: set[str]
    verified: set[str]


def plan_market_gaps(
    calendar: Sequence[str],
    existing: Mapping[str, Set[str]],
    saved_gaps: Mapping[str, Collection[str]],
    manual_only: Set[str],
    *,
    cutoff: str,
    manual: bool,
) -> GapPlan:
    """Plan relative to source MAX, never infer historical holes from MAX alone.

    The ordered calendar is the caller's verified coverage through cutoff.
    Previously manual-only gaps stay excluded from normal automatic planning.
    """
    latest = {kind: max(values, default=None) for kind, values in existing.items()}
    target = calendar[-1] if calendar else None
    needed = {
        kind: {day for day in calendar if day > after} if after else ({target} if target else set())
        for kind, after in latest.items()
    }
    for kind in needed:
        needed[kind].update(day for day in saved_gaps[kind] if manual or day not in manual_only)
    if calendar and any(value and value < calendar[0] for value in latest.values()):
        raise SourceFailure('calendar_incomplete')
    planned = {day for dates in needed.values() for day in dates if day <= cutoff}
    planned |= {value for value in latest.values() if value and value <= cutoff}
    verified = {kind for kind, values in existing.items() if target in values}
    return GapPlan(target, latest, needed, planned, verified)


@dataclass(frozen=True)
class DayTransition:
    heat_status: str
    crowding_status: str
    status: str
    error: str | None
    next_attempt_at: str | None
    updated_at: str
    gap_operations: dict[str, GapOperation]


def transition_market_day(
    day: str,
    row: Mapping[str, str | None],
    existing: Mapping[str, Set[str]],
    needed: Mapping[str, Set[str]],
    stamp: str,
) -> DayTransition:
    states: dict[str, str] = {}
    gap_operations: dict[str, GapOperation] = {}
    for kind, values in existing.items():
        old = row[kind + '_status']
        if day in values:
            value = old if old in PUBLISHED else 'existing_preserved'
            gap_operations[kind] = 'clear'
        elif day in needed[kind]:
            value = old if old in ('pending', 'blocked') else 'pending'
            gap_operations[kind] = 'remember'
        elif old in PUBLISHED:
            # MAX-only source reads do not enumerate prior successes.
            value = old
            gap_operations[kind] = 'clear'
        else:
            value = 'outside_window'
        states[kind + '_status'] = value
    complete = all(value in METRIC_DONE for value in states.values())
    excluded = all(value == 'outside_window' for value in states.values())
    reset = row['status'] in ('complete', 'superseded') and not complete
    return DayTransition(
        states['heat_status'],
        states['crowding_status'],
        'superseded'
        if excluded
        else 'complete'
        if complete
        else 'pending'
        if reset
        else cast(str, row['status']),
        None if complete or reset else row['error'],
        None if complete or reset else row['next_attempt_at'],
        # Both retained fields are NOT NULL in the existing days schema.
        stamp if complete or reset else cast(str, row['updated_at']),
        gap_operations,
    )
