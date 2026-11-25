"""Shared automatic-run policy, not a claim about a source's publication dates.

Manual collection remains available on closed/unknown dates. Its failed work
must not be enrolled in automatic retries; normal later schedules are separate.
"""

import re
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.holdings_calendar import trading_day

CHINA = ZoneInfo('Asia/Shanghai')
_SCHEDULE_TIME = re.compile(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]')


def collection_day(moment: datetime | date) -> date:
    return moment.astimezone(CHINA).date() if isinstance(moment, datetime) else moment


def automatic_collection_allowed(moment: datetime | date) -> bool:
    return trading_day(collection_day(moment)) is True


def manual_failure_isolated(moment: datetime | date) -> bool:
    return not automatic_collection_allowed(moment)


def latest_due_slot(
    moment: datetime, times: Sequence[str], *, is_open: Callable[[date], bool | None] | None = None
) -> datetime | None:
    """Latest configured slot at/before a frozen request, in Beijing time.

    With no calendar predicate, schedule days are civil dates. Otherwise only
    True qualifies, False skips a known closed day, and None (or any unknown
    value) stops the search rather than guessing an older eligible slot.
    This is a data cutoff, not a trigger: it ignores an automatic enable
    switch, a worker's restart floor, missed runs, and past outcomes.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError('Timezone-aware schedule time required')
    if (
        not isinstance(times, (list, tuple))
        or not times
        or any(not isinstance(value, str) or not _SCHEDULE_TIME.fullmatch(value) for value in times)
    ):
        raise ValueError('Configured collection times must use HH:MM')
    clocks = sorted({(int(value[:2]), int(value[3:])) for value in times}, reverse=True)
    local = moment.astimezone(CHINA)
    day = local
    for _ in range(370):
        for hour, minute in clocks:
            slot = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if slot <= local:
                eligible = True if is_open is None else is_open(slot.date())
                if eligible is True:
                    return slot
                if eligible is not False:
                    return None
                break  # All remaining slots are on the same closed day.
        if day.date() == date.min:
            break
        day -= timedelta(days=1)
    return None


def next_automatic_collection(moment: datetime) -> datetime | None:
    """First verified open date at/after a proposed slot, retaining local time.

    Stop at unknown coverage rather than inventing a future schedule. The bound
    also protects callers if a future calendar provider returns closed forever.
    """
    if moment.tzinfo is None:
        raise ValueError('Timezone-aware schedule time required')
    candidate = moment.astimezone(CHINA)
    for _ in range(370):
        status = trading_day(candidate.date())
        if status is None:
            return None
        if status:
            return candidate
        candidate += timedelta(days=1)
    return None
