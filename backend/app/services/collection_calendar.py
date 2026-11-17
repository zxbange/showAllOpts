"""Shared automatic-run policy, not a claim about a source's publication dates.

Manual collection remains available on closed/unknown dates. Its failed work
must not be enrolled in automatic retries; normal later schedules are separate.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.holdings_calendar import trading_day

CHINA = ZoneInfo('Asia/Shanghai')


def collection_day(moment: datetime | date) -> date:
    return moment.astimezone(CHINA).date() if isinstance(moment, datetime) else moment


def automatic_collection_allowed(moment: datetime | date) -> bool:
    return trading_day(collection_day(moment)) is True


def manual_failure_isolated(moment: datetime | date) -> bool:
    return not automatic_collection_allowed(moment)


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
