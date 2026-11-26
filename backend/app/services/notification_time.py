"""Presentation-only timestamps for human-readable notification messages."""

from datetime import datetime
from zoneinfo import ZoneInfo

CHINA = ZoneInfo('Asia/Shanghai')


def notification_time(value: str | datetime | None, *, fallback: str = '未知') -> str:
    try:
        moment = datetime.fromisoformat(value) if isinstance(value, str) else value
        if moment is None or moment.utcoffset() is None:
            return fallback
        return moment.astimezone(CHINA).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError, AttributeError, OverflowError):
        return fallback
