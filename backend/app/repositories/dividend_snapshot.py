"""Atomic high-dividend storage; callers validate candidates and own retries/cache."""

import math
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast

from sqlalchemy import Table, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.dividend import DividendSnapshot, DividendStock


class DividendCollectionError(ValueError):
    pass


def records_match(
    actual: Sequence[Mapping[str, Any]], expected: Sequence[Mapping[str, Any]]
) -> bool:
    """Compare full snapshots using their database storage representations.

    MySQL DECIMAL rounds ties away from zero. JSON DOUBLE can round-trip
    to an adjacent IEEE-754 float: allow only one ULP for historical yields.
    All identities, years, nulls, ranks and other fields remain exact.
    """
    numeric = {
        c.name: c.type.scale
        for c in DividendStock.__table__.columns
        if hasattr(c.type, 'scale') and c.type.scale is not None
    }

    def normalize(value: Any, scale: int) -> Decimal:
        return Decimal(str(value)).quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)

    def history_match(a: Any, b: Any) -> bool:
        if a is None or b is None:
            return a is b
        if not isinstance(a, list) or not isinstance(b, list) or len(a) != len(b):
            return False
        for left, right in zip(a, b, strict=True):
            if (
                not isinstance(left, dict)
                or not isinstance(right, dict)
                or left.keys() != right.keys()
            ):
                return False
            if any(left[k] != right[k] for k in left if k != 'yield_pct'):
                return False
            x, y = left.get('yield_pct'), right.get('yield_pct')
            if x is None or y is None:
                if x is not y:
                    return False
                continue
            if type(x) not in (int, float) or type(y) not in (int, float):
                return False
            if not math.isfinite(x) or not math.isfinite(y):
                return False
            if x != y and (x == 0 or y == 0 or math.nextafter(x, y) != y):
                return False
        return True

    def by_identity(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        return {(r['method'], r['code']): dict(r) for r in rows}

    a, b = by_identity(actual), by_identity(expected)
    if len(a) != len(actual) or len(b) != len(expected) or a.keys() != b.keys():
        return False
    for key, row in a.items():
        other = b[key]
        if row.keys() != other.keys():
            return False
        for field, x in row.items():
            y = other[field]
            if field == 'dividend_yield_history':
                if not history_match(x, y):
                    return False
            elif field in numeric and x is not None and y is not None:
                if normalize(x, numeric[field]) != normalize(y, numeric[field]):
                    return False
            elif x != y:
                return False
    return True


def read_snapshot(session: Session) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Read the pinned cache snapshot used for post-publication verification."""
    head = (
        session.execute(select(DividendSnapshot.__table__).where(DividendSnapshot.id == 1))
        .mappings()
        .first()
    )
    rows = session.execute(select(DividendStock.__table__)).mappings().all()
    return (dict(head) if head is not None else None, [dict(row) for row in rows])


def publish_snapshot(
    engine: Engine,
    meta: Mapping[str, Any],
    records: list[dict[str, Any]],
    check: Callable[[], None],
    *,
    phase: Callable[[str, Callable[[str], None] | None], AbstractContextManager[Any]],
    progress: Callable[[str], None] | None,
    updated_at: Callable[[], datetime],
) -> int:
    """Replace both validated groups atomically, rejecting an older date."""
    head, stocks = cast(Table, DividendSnapshot.__table__), cast(Table, DividendStock.__table__)
    with phase('数据库快照事务', progress), engine.begin() as conn:
        with phase('锁定数据库快照', progress):
            previous = (
                conn.execute(select(head).where(head.c.id == 1).with_for_update())
                .mappings()
                .first()
            )
        if previous and previous['trade_date'] > meta['trade_date']:
            raise DividendCollectionError('A newer dividend snapshot already exists')
        check()
        with phase('读取现有股票记录', progress):
            existing = conn.execute(select(stocks)).mappings().all()
        if (
            previous
            and all(previous[key] == value for key, value in meta.items())
            and records_match([dict(row) for row in existing], records)
        ):
            return 0
        values = dict(meta, updated_at=updated_at())
        with phase('写入两组股票快照', progress):
            if previous:
                conn.execute(head.update().where(head.c.id == 1).values(**values))
            else:
                # Insert before deleting protects against concurrent first publication.
                conn.execute(head.insert().values(id=1, **values))
            conn.execute(delete(stocks))
            if records:
                conn.execute(stocks.insert(), records)
        check()
        if progress is not None:
            progress('提交数据库事务')
    return max(len(records), 1)
