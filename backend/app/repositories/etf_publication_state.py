"""Durable ETF cache-only intent in the existing collector metadata table."""

import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import date

from app.core.etf_publication import ETFKey

KEY_PREFIX = 'cache_expected_day:'
_CODE = re.compile(r'[0-9]{6}\.(?:XSHG|XSHE)')


def _codes(value: object) -> set[str]:
    if not isinstance(value, list) or any(
        not isinstance(code, str) or not _CODE.fullmatch(code) for code in value
    ):
        raise ValueError('Invalid ETF cache requirement')
    return set(value)


def mark_committed(
    db: sqlite3.Connection,
    day: date,
    earliest: date,
    keys: Iterable[ETFKey],
) -> None:
    """Only called after an acknowledged business COMMIT."""
    db.execute(
        "INSERT INTO metadata VALUES ('cache_dirty_start',?) "
        'ON CONFLICT(key) DO UPDATE SET value=min(value,excluded.value)',
        (earliest.isoformat(),),
    )
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('cache_pending','1')")
    key = KEY_PREFIX + day.isoformat()
    row = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
    try:
        previous = json.loads(row[0]) if row else {'shares': [], 'prices': []}
        current = {kind: _codes(previous[kind]) for kind in ('shares', 'prices')}
    except (ValueError, TypeError, KeyError):
        # Don't discard damaged existing expectations or lose the dirty intent.
        # The publisher will report an invalid requirement, retaining the old snapshot.
        return
    for kind, code, target in keys:
        if target == day:
            current[kind].add(code)
    db.execute(
        'INSERT OR REPLACE INTO metadata VALUES (?,?)',
        (key, json.dumps({kind: sorted(codes) for kind, codes in current.items()})),
    )


def _legacy_pending(db: sqlite3.Connection) -> tuple[date, set[ETFKey]] | None:
    # Old versions did not record exact publication keys. Retain the known
    # committed ledger for their last target, but do not invent any missing keys.
    pending = db.execute("SELECT value FROM metadata WHERE key='cache_pending'").fetchone()
    target = db.execute("SELECT value FROM metadata WHERE key='coverage_end'").fetchone()
    if not pending or pending[0] != '1' or not target:
        return None
    day = date.fromisoformat(target[0])
    if db.execute('SELECT 1 FROM metadata WHERE key=?', (KEY_PREFIX + day.isoformat(),)).fetchone():
        return None
    keys: set[ETFKey] = {
        (kind, code, day)
        for kind, code in db.execute(
            "SELECT kind,code FROM observations WHERE day=? AND status='complete'",
            (day.isoformat(),),
        )
        if kind in ('shares', 'prices') and isinstance(code, str) and _CODE.fullmatch(code)
    }
    return day, keys


def preserve_legacy_pending(db: sqlite3.Connection) -> None:
    """Before a new run can replace the old target, retain its known coverage."""
    try:
        legacy = _legacy_pending(db)
    except (TypeError, ValueError):
        # The publisher reports malformed requirements; do not guess a date.
        return
    if legacy is not None:
        day, keys = legacy
        mark_committed(db, day, day, keys)


def publication_requirements(db: sqlite3.Connection) -> tuple[date | None, frozenset[ETFKey]]:
    anchors = []
    expected: set[ETFKey] = set()
    for row in db.execute(
        "SELECT key,value FROM metadata WHERE key IN ('cache_repair_start','cache_dirty_start') OR key LIKE ?",
        (KEY_PREFIX + '%',),
    ):
        key, value = row
        if key in ('cache_repair_start', 'cache_dirty_start'):
            anchors.append(date.fromisoformat(value))
            continue
        day = date.fromisoformat(key.removeprefix(KEY_PREFIX))
        anchors.append(day)
        payload = json.loads(value)
        for kind in ('shares', 'prices'):
            expected.update((kind, code, day) for code in _codes(payload[kind]))
    legacy = _legacy_pending(db)
    if legacy is not None:
        day, keys = legacy
        anchors.append(day)
        expected.update(keys)
    return min(anchors, default=None), frozenset(expected)


def clear_publication_requirements(db: sqlite3.Connection) -> None:
    db.execute(
        "DELETE FROM metadata WHERE key IN ('cache_repair_start','cache_dirty_start','cache_full_rebuild') OR key LIKE ?",
        (KEY_PREFIX + '%',),
    )
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('cache_pending','0')")
