"""Collector-owned ETF snapshot publication receipt and coverage checks."""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, cast

from sqlalchemy import Table, select, tuple_
from sqlalchemy.engine import Connection

from app.core.cache_errors import SnapshotDataError
from app.models.etf import ETFPrice, ETFShare

ETFKey = tuple[str, str, date]
TABLES = {'shares': cast(Table, ETFShare.__table__), 'prices': cast(Table, ETFPrice.__table__)}
PUBLICATION_STATES = frozenset(
    {'pending', 'publishing', 'waiting_lock', 'failed', 'published', 'direct_read'}
)
_SAFE_ERROR = re.compile(
    r'(?:MYSQL_[0-9]{1,5}|SQLITE_[A-Z0-9_]{1,48}|OS_[0-9]{1,6}|POOL_TIMEOUT|SOURCE_DATA_INCOMPLETE|UNKNOWN|ETF_SNAPSHOT_UNAVAILABLE|ETF_REQUIREMENTS_INVALID)'
)


@dataclass(frozen=True)
class ETFPublicationResult:
    status: Literal['waiting_lock', 'failed', 'published', 'direct_read']
    error_code: str | None = None
    latest_share_date: str | None = None
    latest_price_date: str | None = None
    published_at: str | None = None

    @property
    def successful(self) -> bool:
        return self.status in ('published', 'direct_read')

    def public(self) -> dict[str, str | None]:
        return {
            'status': self.status,
            'error_code': safe_publication_error(self.error_code),
            'latest_share_date': _safe_day(self.latest_share_date),
            'latest_price_date': _safe_day(self.latest_price_date),
            'published_at': safe_publication_time(self.published_at),
        }


def safe_publication_error(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_ERROR.fullmatch(value) else None


def safe_publication_time(value: object) -> str | None:
    try:
        moment = datetime.fromisoformat(value) if isinstance(value, str) else None
        return moment.isoformat() if moment is not None and moment.utcoffset() is not None else None
    except ValueError:
        return None


def _safe_day(value: object) -> str | None:
    try:
        day = date.fromisoformat(value) if isinstance(value, str) else None
        return day.isoformat() if day is not None and value == day.isoformat() else None
    except ValueError:
        return None


def public_cache_status(
    raw: str | None, pending: bool, attempted_at: str | None
) -> dict[str, str | None] | None:
    try:
        saved = json.loads(raw) if raw else {}
        if not isinstance(saved, dict):
            saved = {}
    except (ValueError, TypeError):
        saved = {}
    status = saved.get('status')
    if not isinstance(status, str) or status not in PUBLICATION_STATES:
        status = 'pending' if pending else None
    if pending and status in ('published', 'direct_read'):
        status = 'pending'
    if status is None:
        return None
    return {
        'status': status,
        'error_code': safe_publication_error(saved.get('error_code')),
        'latest_share_date': _safe_day(saved.get('latest_share_date')),
        'latest_price_date': _safe_day(saved.get('latest_price_date')),
        'published_at': safe_publication_time(saved.get('published_at')),
        'last_attempt_at': safe_publication_time(attempted_at),
        'required_through': _safe_day(saved.get('required_through')),
    }


def _missing_coverage(missing: set[ETFKey]) -> None:
    if missing:
        first = min(day for _, _, day in missing)
        raise SnapshotDataError(
            f'待发布的 ETF 入库记录未全部出现在指定来源或快照中（{len(missing)} 项，最早 {first.isoformat()}）。'
        )


def validate_source_coverage(
    remote: Mapping[str, Mapping[tuple[object, ...], tuple[object, ...]]],
    expected: Iterable[ETFKey],
) -> None:
    _missing_coverage(
        {
            (kind, code, day)
            for kind, code, day in expected
            if (code, day) not in remote.get(TABLES[kind].name, {})
        }
    )


def validate_local_coverage(connection: Connection, expected: Iterable[ETFKey]) -> None:
    # Validate the actual unpublished immutable file, not merely the returned
    # row count or MAX(date). This stays local and uses the existing PK.
    missing = set(expected)
    for kind, table in TABLES.items():
        keys = sorted((code, day) for series, code, day in missing if series == kind)
        for offset in range(0, len(keys), 250):
            batch = keys[offset : offset + 250]
            found = connection.execute(
                select(table.c.fund_code, table.c.trade_date).where(
                    tuple_(table.c.fund_code, table.c.trade_date).in_(batch)
                )
            )
            missing.difference_update((kind, code, day) for code, day in found)
    _missing_coverage(missing)
