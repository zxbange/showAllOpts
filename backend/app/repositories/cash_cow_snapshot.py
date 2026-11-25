"""Typed persistence boundary for the existing cash-cow singleton publication.

No business predicate, source readiness, retry loop, local publication, or
notification belongs here. The caller validates the candidate before writing
and supplies its domain validator when an existing newer snapshot wins.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, TypedDict, cast

from sqlalchemy import Table, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.core.cache_errors import SnapshotDataError
from app.models.cash_cow import CashCowSnapshot

# The persisted JSON document contains a nested schema validated by the domain
# service. Keep its exact serialization and do not normalize numeric values.
CashCowPayload = dict[str, Any]
_TABLE = cast(Table, CashCowSnapshot.__table__)


class SnapshotRecord(TypedDict):
    id: int
    as_of_date: date
    updated_at: datetime
    payload: CashCowPayload


@dataclass(frozen=True)
class PublicationResult:
    payload: CashCowPayload
    written: bool


def publication_stamp(snapshot: Mapping[str, Any]) -> datetime:
    try:
        value = datetime.fromisoformat(snapshot['updated_at'])
        if value.tzinfo is None:
            raise ValueError('Missing timestamp timezone')
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotDataError('奶牛股快照发布时间无效，保留已有结果。') from exc


def candidate_record(payload: CashCowPayload) -> SnapshotRecord:
    meta = payload['3']['snapshot']
    return SnapshotRecord(
        id=1,
        as_of_date=date.fromisoformat(meta['as_of']),
        updated_at=publication_stamp(meta),
        payload=payload,
    )


def read_all(connection: Connection) -> list[SnapshotRecord]:
    table = _TABLE
    return [
        cast(SnapshotRecord, dict(row))
        for row in connection.execute(select(table)).mappings().all()
    ]


def read_period(session: Session, years: int) -> CashCowPayload | None:
    row = session.get(CashCowSnapshot, 1)
    if row is None:
        return None
    payload = cast(CashCowPayload, row.payload)
    result = payload.get(str(years))
    return cast(CashCowPayload | None, result)


def ensure_schema(engine: Engine) -> None:
    # Existing create-if-missing contract, with no new revision or DDL.
    _TABLE.create(engine, checkfirst=True)


def publish(
    engine: Engine,
    record: SnapshotRecord,
    *,
    check: Callable[[], None],
    phase: Callable[[str], None],
    source: str,
    validate_existing: Callable[[Mapping[str, Any]], None],
) -> PublicationResult:
    """Lock, reconcile and replace all three windows in one transaction.

    Reconciliation must use the JSON's precise publication clock, not the
    potentially rounded MySQL DATETIME column. The original equality and
    source precedence rules are retained. Returning a newer canonical
    publication still requires validation by the domain service.
    """
    table = _TABLE
    payload = record['payload']
    with engine.begin() as conn:
        old = (
            conn.execute(select(table).where(table.c.id == 1).with_for_update()).mappings().first()
        )
        if old:
            old_payload = cast(CashCowPayload, old['payload'])
            if old['as_of_date'] == record['as_of_date'] and old_payload == payload:
                return PublicationResult(old_payload, False)
            old_meta = old_payload.get('3', {}).get('snapshot', {})
            if old_meta.get('source') == source:
                old_stamp = publication_stamp(old_meta)
                if (old['as_of_date'], old_stamp) > (record['as_of_date'], record['updated_at']):
                    validate_existing(dict(old))
                    return PublicationResult(old_payload, False)
        check()
        phase('write')
        if old:
            conn.execute(table.update().where(table.c.id == 1).values(**record))
        else:
            conn.execute(table.insert().values(**record))
        check()
        phase('commit')
    return PublicationResult(payload, True)
