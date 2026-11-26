"""Bind a cache-only recovery to its original, fully acquired ETF run.

The original partial row and its safe pending error remain durable. The
recovery receipt lives in that same detail payload, so existing history
retention and restoration preserve both facts without a second run identity.
"""

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.core.etf_publication import ETFKey
from app.repositories.etf_publication_state import publication_requirements
from app.services.collection_errors import sanitize_error
from app.services.collection_outcomes import publication_recovery_stamp

_CODE = re.compile(r'[0-9]{6}\.(?:XSHG|XSHE)')


@dataclass(frozen=True)
class PendingPublication:
    run_id: str
    trigger: str
    finished_at: str
    keys: frozenset[ETFKey]
    payload: dict
    raw: str


def _day(value):
    day = date.fromisoformat(value)
    if day.isoformat() != value:
        raise ValueError('Noncanonical ETF date')
    return day


def _time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError('Unknown ETF completion clock')
    return parsed


def _payload(raw):
    try:
        content = json.loads(raw) if raw else None
        return content if isinstance(content, dict) else {}
    except (ValueError, TypeError):
        return {}


def _is_pending(row, payload):
    if (
        row['status'] != 'partial'
        or row['error']
        or row['time_basis'] != 'completed_at'
        or row['trigger'] not in ('manual', 'scheduled')
        or payload.get('code') != 'publication_pending'
        or payload.get('stage') != 'cache_publish'
    ):
        return False
    try:
        _time(row['finished_at'])
        return True
    except (TypeError, ValueError):
        return False


def _archived(db, run_id):
    return bool(
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_history_archive'"
        ).fetchone()
        and db.execute('SELECT 1 FROM run_history_archive WHERE id=?', (run_id,)).fetchone()
    )


def _complete_observations(db, funds, days):
    required = {
        (kind, code, day) for kind in ('shares', 'prices') for code in funds for day in days
    }
    complete = set()
    for day in days:
        complete.update(
            (kind, code, day)
            for kind, code in db.execute(
                "SELECT kind,code FROM observations WHERE day=? AND status='complete'",
                (day.isoformat(),),
            )
        )
    return required if required and required <= complete else None


def bind_requirement(db: sqlite3.Connection, source, target, funds, *, legacy_manual=False):
    """Capture exact confirmed keys before a later collection can replace coverage_end."""
    if not funds or any(not isinstance(code, str) or not _CODE.fullmatch(code) for code in funds):
        return None
    try:
        meta = dict(
            db.execute(
                'SELECT key,value FROM metadata WHERE key IN '
                "('system_database_target','coverage_start','coverage_end','coverage_target_sessions')"
            )
        )
        if meta.get('system_database_target') != source or _day(meta['coverage_end']) != target:
            return None
        days = {target}
        if legacy_manual:
            start = _day(meta['coverage_start'])
            days = {
                _day(row[0])
                for row in db.execute(
                    'SELECT DISTINCT day FROM observations WHERE day BETWEEN ? AND ?',
                    (start.isoformat(), target.isoformat()),
                )
            }
            if (
                not days
                or min(days) != start
                or max(days) != target
                or len(days) != int(meta['coverage_target_sessions'])
                or db.execute(
                    "SELECT 1 FROM metadata WHERE (key='manual_gap_floor' OR key GLOB 'manual_gap_floor_deferred:*') AND value<=? LIMIT 1",
                    (target.isoformat(),),
                ).fetchone()
            ):
                return None
        required = _complete_observations(db, funds, days)
        if required is None:
            return None
        _, pending = publication_requirements(db)
        keys = required | pending
        return {
            'database_target': source,
            'trade_date': target.isoformat(),
            'keys': [[kind, code, day.isoformat()] for kind, code, day in sorted(keys)],
        }
    except (KeyError, TypeError, ValueError):
        return None


def _legacy_acquisition_complete(db, row):
    if row['trigger'] == 'scheduled' and row['slot'] and row['attempt'] is not None:
        saved = db.execute(
            'SELECT status,attempts,attempted_at,error FROM slots WHERE slot=?', (row['slot'],)
        ).fetchone()
        return bool(
            saved
            and saved['status'] == 'complete'
            and not saved['error']
            and saved['attempts'] == row['attempt']
            and saved['attempted_at'] == row['requested_at']
        )
    if row['trigger'] == 'manual' and row['id'].startswith('manual:'):
        saved = db.execute(
            'SELECT status,requested_at,error FROM manual_runs WHERE id=?',
            (row['id'].removeprefix('manual:'),),
        ).fetchone()
        return bool(
            saved
            and saved['status'] == 'complete'
            and not saved['error']
            and saved['requested_at'] == row['requested_at']
        )
    return False


def bind_legacy_latest(db, source, funds):
    """Old releases stored only the last coverage target. Do not infer older runs."""
    row = db.execute(
        'SELECT h.*,e.details FROM run_history h LEFT JOIN run_history_errors e ON e.id=h.id '
        "WHERE h.finished_at IS NOT NULL AND h.status NOT IN ('running','publishing') "
        'ORDER BY julianday(h.finished_at) DESC,h.rowid DESC LIMIT 1'
    ).fetchone()
    if not row or _archived(db, row['id']):
        return
    payload = _payload(row['details'])
    if (
        not _is_pending(row, payload)
        or payload.get('publication_requirement') is not None
        or not _legacy_acquisition_complete(db, row)
    ):
        return
    # A newer attempt may have updated coverage_end despite an unavailable
    # history sink. Without its original target, the old partial cannot be
    # safely bound to that new ledger.
    if (
        db.execute(
            'SELECT 1 FROM slots WHERE julianday(attempted_at)>julianday(?) LIMIT 1',
            (row['requested_at'],),
        ).fetchone()
        or db.execute(
            "SELECT 1 FROM manual_runs WHERE status!='queued' AND julianday(requested_at)>julianday(?) LIMIT 1",
            (row['requested_at'],),
        ).fetchone()
    ):
        return
    # The existing reconciliation archives old target ledgers on a database
    # change. A later target cannot retroactively certify an old run.
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_target_archive'"
    ).fetchone():
        if db.execute(
            'SELECT 1 FROM system_target_archive WHERE julianday(archived_at) IS NULL OR julianday(archived_at)>=julianday(?) LIMIT 1',
            (row['finished_at'],),
        ).fetchone():
            return
    try:
        target = _day(
            db.execute("SELECT value FROM metadata WHERE key='coverage_end'").fetchone()[0]
        )
        reference = _time(row['slot'] or row['requested_at']).astimezone(ZoneInfo('Asia/Shanghai'))
        if target > reference.date() or (reference.hour < 15 and target == reference.date()):
            return
        if payload.get('trade_date') and _day(payload['trade_date']) != target:
            return
    except (TypeError, ValueError, IndexError):
        return
    requirement = bind_requirement(
        db, source, target, funds, legacy_manual=row['trigger'] == 'manual'
    )
    if requirement is not None:
        payload['publication_requirement'] = requirement
        db.execute(
            'UPDATE run_history_errors SET details=? WHERE id=? AND details=?',
            (json.dumps(payload, ensure_ascii=False), row['id'], row['details']),
        )


def pending_publications(db, source):
    for row in db.execute(
        'SELECT h.*,e.details FROM run_history h JOIN run_history_errors e ON e.id=h.id '
        "WHERE h.status='partial' AND e.details LIKE '%publication_requirement%'"
    ):
        payload = _payload(row['details'])
        if (
            not _is_pending(row, payload)
            or 'publication_recovered_at' in payload
            or _archived(db, row['id'])
        ):
            continue
        try:
            requirement = payload['publication_requirement']
            target = _day(requirement['trade_date'])
            if requirement['database_target'] != source:
                continue
            keys = frozenset((kind, code, _day(day)) for kind, code, day in requirement['keys'])
            if (
                not keys
                or any(
                    kind not in ('shares', 'prices') or not _CODE.fullmatch(code)
                    for kind, code, day in keys
                )
                or not any(day == target for _, _, day in keys)
            ):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        yield PendingPublication(
            row['id'], row['trigger'], row['finished_at'], keys, payload, row['details']
        )


def mark_recovered(db, candidate, stamp):
    payload = candidate.payload | {'publication_recovered_at': stamp}
    verified = publication_recovery_stamp(payload, candidate.finished_at)
    if verified is None:
        return False
    payload['publication_recovered_at'] = verified
    return bool(
        db.execute(
            'UPDATE run_history_errors SET details=? WHERE id=? AND details=?',
            (json.dumps(payload, ensure_ascii=False), candidate.run_id, candidate.raw),
        ).rowcount
    )


def unsynced_recoveries(db):
    latest = db.execute(
        "SELECT id FROM run_history WHERE finished_at IS NOT NULL AND status NOT IN ('running','publishing') "
        'ORDER BY julianday(finished_at) DESC,rowid DESC LIMIT 1'
    ).fetchone()
    rows = db.execute(
        'SELECT h.*,e.details FROM run_history h JOIN run_history_errors e ON e.id=h.id '
        "WHERE h.status='partial' AND e.details LIKE '%publication_recovered_at%'"
    ).fetchall()
    for row in rows:
        payload = _payload(row['details'])
        stamp = (
            publication_recovery_stamp(payload, row['finished_at'])
            if _is_pending(row, payload)
            else None
        )
        # Reconcile the latest even after a prior successful projection: a
        # restored shared ledger may contain its original partial again.
        if (
            stamp
            and (not payload.get('publication_synced') or (latest and latest['id'] == row['id']))
            and not _archived(db, row['id'])
        ):
            yield row, payload, stamp


def mark_synced(db, row, payload):
    db.execute(
        'UPDATE run_history_errors SET details=? WHERE id=? AND details=?',
        (
            json.dumps(payload | {'publication_synced': True}, ensure_ascii=False),
            row['id'],
            row['details'],
        ),
    )


def project_recovery(item, raw):
    payload = _payload(raw)
    stamp = (
        publication_recovery_stamp(payload, item['finished_at'])
        if _is_pending(item, payload)
        else None
    )
    item['error_details'] = sanitize_error(payload)
    if stamp:
        item.update(status='complete', error_details=None, publication_recovered_at=stamp)
