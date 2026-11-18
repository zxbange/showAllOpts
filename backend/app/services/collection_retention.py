"""Reversible active-history retention. Archives are never purged automatically."""
import sqlite3


def retain_history(settings, *, apply=False, confirmed=False):
    if apply and not confirmed:
        raise ValueError('Explicit confirmation required')
    path = settings.log_path / 'collection-outcomes' / 'outcomes.sqlite3'
    if not path.exists():
        return {'candidates': 0, 'archived': 0, 'dry_run': not apply}
    db = sqlite3.connect(path.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True, timeout=5)
    try:
        if apply:
            db.execute('BEGIN IMMEDIATE')
        rows = db.execute('SELECT rowid,module,run_key,outcome,finished_at,trigger FROM results '
                          'ORDER BY module,finished_at DESC,rowid DESC').fetchall()
        counts, candidates = {}, []
        for row in rows:
            counts[row[1]] = counts.get(row[1], 0) + 1
            if counts[row[1]] > 5:
                candidates.append(row)
        if not apply:
            return {'candidates': len(candidates), 'archived': 0, 'dry_run': True}
        db.execute('CREATE TABLE IF NOT EXISTS results_archive '
                   '(original_rowid INTEGER NOT NULL,module TEXT NOT NULL,run_key TEXT NOT NULL,'
                   'outcome TEXT NOT NULL,finished_at TEXT NOT NULL,trigger TEXT NOT NULL,'
                   'details TEXT,PRIMARY KEY(module,run_key))')
        has_details = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='result_details'").fetchone()
        for row in candidates:
            detail = db.execute('SELECT payload FROM result_details WHERE module=? AND run_key=?', row[1:3]).fetchone() if has_details else None
            payload = (*row, detail[0] if detail else None)
            db.execute('INSERT OR IGNORE INTO results_archive VALUES(?,?,?,?,?,?,?)', payload)
            saved = db.execute('SELECT * FROM results_archive WHERE module=? AND run_key=?', row[1:3]).fetchone()
            # Original rowid may differ after restoration. All content must match.
            if saved[1:] != payload[1:]:
                raise ValueError('Archive verification failed; active history retained')
            db.execute('DELETE FROM results WHERE module=? AND run_key=?', row[1:3])
            if has_details:
                db.execute('DELETE FROM result_details WHERE module=? AND run_key=?', row[1:3])
        db.commit()
        return {'candidates': len(candidates), 'archived': len(candidates), 'dry_run': False}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def restore_history(settings, *, apply=False, confirmed=False):
    if apply and (not confirmed or getattr(settings, 'collection_history_retention_enabled', False)):
        raise ValueError('Disable retention and explicitly confirm restoration first')
    path = settings.log_path / 'collection-outcomes' / 'outcomes.sqlite3'
    if not path.exists():
        return {'restored': 0, 'candidates': 0, 'dry_run': not apply}
    db = sqlite3.connect(path.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True, timeout=5)
    try:
        if apply:
            db.execute('BEGIN IMMEDIATE')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='results_archive'").fetchone():
            return {'restored': 0, 'candidates': 0, 'dry_run': not apply}
        rows = db.execute('SELECT * FROM results_archive a WHERE NOT EXISTS '
                          '(SELECT 1 FROM results r WHERE r.module=a.module AND r.run_key=a.run_key) '
                          'ORDER BY original_rowid').fetchall()
        restored = 0
        if apply:
            db.execute('CREATE TABLE IF NOT EXISTS result_details (module TEXT NOT NULL,run_key TEXT NOT NULL,'
                       'payload TEXT NOT NULL,PRIMARY KEY(module,run_key))')
            for original, module, key, outcome, stamp, trigger, details in rows:
                # Restored older tied completions remain before active/newer rows.
                restored += db.execute('INSERT INTO results(rowid,module,run_key,outcome,finished_at,trigger) '
                                       'VALUES(?,?,?,?,?,?)', (-10**15 + original, module, key, outcome, stamp, trigger)).rowcount
                if details is not None:
                    db.execute('INSERT INTO result_details VALUES(?,?,?)', (module, key, details))
            db.commit()
        return {'restored': restored, 'candidates': len(rows), 'dry_run': not apply}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def purge_history(settings, *, apply=False, confirmed=False):
    """One-time irreversible removal; never called by automatic retention."""
    if apply and not confirmed:
        raise ValueError('Explicit permanent deletion confirmation required')
    path = settings.log_path / 'collection-outcomes' / 'outcomes.sqlite3'
    empty = dict(active_candidates=0, archive_candidates=0, details_candidates=0,
                 deleted_active=0, deleted_archive=0, deleted_details=0,
                 irreversible=True, dry_run=not apply)
    if not path.exists():
        return empty
    db = sqlite3.connect(path.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True, timeout=5)
    try:
        if apply:
            db.execute('BEGIN IMMEDIATE')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        active = db.execute('SELECT rowid,module,run_key,finished_at,outcome FROM results').fetchall() if 'results' in tables else []
        archived = db.execute('SELECT original_rowid,module,run_key,finished_at,outcome FROM results_archive').fetchall() if 'results_archive' in tables else []
        from app.services.collection_outcomes import MODULES, TERMINAL
        from datetime import datetime, timezone
        records, protected = {}, set()
        for order, module, key, stamp, outcome in [*archived, *active]:
            identity = (module, key)
            if module not in MODULES or outcome not in TERMINAL:
                protected.add(identity)
                continue
            try:
                moment = datetime.fromisoformat(stamp)
                if moment.tzinfo is None:
                    raise ValueError('Unknown timestamp basis')
            except (TypeError, ValueError):
                protected.add(identity)  # Cannot safely rank malformed legacy data.
                continue
            candidate = (moment.astimezone(timezone.utc), order, module, key)
            if identity not in records or candidate > records[identity]:
                records[identity] = candidate
        counts = {}
        for _, _, module, key in sorted(records.values(), reverse=True):
            counts[module] = counts.get(module, 0) + 1
            if counts[module] <= 5:
                protected.add((module, key))
        active_keys = {(r[1], r[2]) for r in active} - protected
        archive_keys = {(r[1], r[2]) for r in archived} - protected
        keys = active_keys | archive_keys
        details = {(r[0], r[1]) for r in db.execute('SELECT module,run_key FROM result_details')} & keys if 'result_details' in tables else set()
        result = empty | dict(active_candidates=len(active_keys), archive_candidates=len(archive_keys), details_candidates=len(details))
        if apply:
            # Retain only operational identity keys to prevent replay resurrecting
            # deleted history. No outcome, timestamp, error or payload survives here.
            db.execute('CREATE TABLE IF NOT EXISTS result_tombstones '
                       '(module TEXT NOT NULL,run_key TEXT NOT NULL,PRIMARY KEY(module,run_key))')
            db.executemany('INSERT OR IGNORE INTO result_tombstones VALUES(?,?)', sorted(keys))
            db.executemany('DELETE FROM results WHERE module=? AND run_key=?', sorted(active_keys))
            if 'results_archive' in tables:
                db.executemany('DELETE FROM results_archive WHERE module=? AND run_key=?', sorted(archive_keys))
            if 'result_details' in tables:
                db.executemany('DELETE FROM result_details WHERE module=? AND run_key=?', sorted(details))
            db.commit()
            result.update(deleted_active=len(active_keys), deleted_archive=len(archive_keys), deleted_details=len(details))
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
