"""Explicit, opt-in pruning of display history, never scheduling or business data."""
import sqlite3
import json


def prune_native_history(settings, *, apply=False, confirmed=False):
    if apply and not confirmed:
        raise ValueError('Explicit history deletion confirmation required')
    path = settings.log_path / 'market-data' / 'etf-collection' / 'state.sqlite'
    if not path.exists():
        return {'module': 'etf-shares', 'candidates': 0, 'deleted': 0}
    db = sqlite3.connect(path.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True)
    db.row_factory = sqlite3.Row
    try:
        if apply:
            db.execute('BEGIN IMMEDIATE')
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_history'").fetchone()
        if not exists:
            return {'module': 'etf-shares', 'candidates': 0, 'deleted': 0}
        # Match status ordering; unknown legacy completion times remain unknown.
        ids = [row[0] for row in db.execute("SELECT id FROM run_history WHERE status NOT IN ('running','publishing') "
              "ORDER BY (finished_at IS NOT NULL) DESC,julianday(COALESCE(finished_at,requested_at)) DESC,rowid DESC LIMIT -1 OFFSET 5")]
        if apply:
            db.execute('CREATE TABLE IF NOT EXISTS run_history_archive '
                       '(id TEXT PRIMARY KEY, original_rowid INTEGER NOT NULL, payload TEXT NOT NULL, details TEXT)')
            has_details = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_history_errors'").fetchone()
            for key in ids:
                row = dict(db.execute('SELECT rowid AS original_rowid,* FROM run_history WHERE id=?', (key,)).fetchone())
                original_rowid = row.pop('original_rowid')
                payload = json.dumps(row, sort_keys=True)
                detail = db.execute('SELECT details FROM run_history_errors WHERE id=?', (key,)).fetchone() if has_details else None
                details = detail[0] if detail else None
                db.execute('INSERT OR IGNORE INTO run_history_archive VALUES (?,?,?,?)', (key, original_rowid, payload, details))
                saved = db.execute('SELECT payload,details FROM run_history_archive WHERE id=?', (key,)).fetchone()
                if tuple(saved) != (payload, details):
                    raise ValueError('Archived history verification failed; active history unchanged')
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_history_errors'").fetchone():
                db.executemany('DELETE FROM run_history_errors WHERE id=?', [(key,) for key in ids])
            db.executemany('DELETE FROM run_history WHERE id=?', [(key,) for key in ids])
            db.commit()
        return {'module': 'etf-shares', 'candidates': len(ids), 'deleted': len(ids) if apply else 0}
    finally:
        db.close()


def restore_native_history(settings, *, apply=False, confirmed=False):
    """Restore archived display rows; archive and all operational ledgers remain intact."""
    if apply and not confirmed:
        raise ValueError('Explicit history restore confirmation required')
    if apply and getattr(settings, 'collection_history_retention_enabled', False):
        raise ValueError('Disable automatic history retention before restoring')
    path = settings.log_path / 'market-data' / 'etf-collection' / 'state.sqlite'
    if not path.exists():
        return {'module': 'etf-shares', 'candidates': 0, 'restored': 0}
    db = sqlite3.connect(path.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True)
    try:
        with db:
            if apply:
                db.execute('BEGIN IMMEDIATE')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_history_archive'").fetchone():
                return {'module': 'etf-shares', 'candidates': 0, 'restored': 0}
            count = 0
            for key, original_rowid, payload, details in db.execute('SELECT * FROM run_history_archive ORDER BY original_rowid').fetchall():
                if db.execute('SELECT 1 FROM run_history WHERE id=?', (key,)).fetchone():
                    continue
                if not apply:
                    count += 1
                    continue
                row = json.loads(payload)
                columns = ('id', 'trigger', 'status', 'error', 'requested_at', 'finished_at', 'slot', 'attempt', 'time_basis')
                # Preserve original tie-break order without changing business timestamps.
                if db.execute('SELECT 1 FROM run_history WHERE rowid=?', (original_rowid,)).fetchone():
                    raise ValueError('History row order conflict; restore aborted')
                db.execute('INSERT INTO run_history(rowid,' + ','.join(columns) + ') VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (original_rowid, *(row[column] for column in columns)))
                if details is not None:
                    db.execute('CREATE TABLE IF NOT EXISTS run_history_errors (id TEXT PRIMARY KEY, details TEXT NOT NULL)')
                    db.execute('INSERT OR REPLACE INTO run_history_errors VALUES (?,?)', (key, details))
                count += 1
            return {'module': 'etf-shares', 'candidates': count, 'restored': count if apply else 0}
    finally:
        db.close()


def purge_native_history(settings, *, apply=False, confirmed=False):
    """One-shot permanent purge. Never called by automatic retention.

    Rank the union of active and archived evidence, retaining the newest five
    terminal runs and all live run IDs. Operational ledgers are never touched.
    """
    if apply and not confirmed:
        raise ValueError('Explicit permanent history deletion confirmation required')
    path = settings.log_path / 'market-data' / 'etf-collection' / 'state.sqlite'
    result = {'module': 'etf-shares', 'candidates': 0, 'deleted': 0}
    if not path.exists():
        return result
    db = sqlite3.connect(path.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True)
    db.row_factory = sqlite3.Row
    try:
        if apply:
            db.execute('BEGIN IMMEDIATE')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        records, live = {}, set()
        if 'run_history_archive' in tables:
            for row in db.execute('SELECT * FROM run_history_archive'):
                record = json.loads(row['payload'])
                # Fail closed for corrupted archives rather than risk wrong retention.
                if record.get('id') != row['id']:
                    raise ValueError('Invalid archived history identity')
                record['_rowid'] = row['original_rowid']
                records[row['id']] = record
        if 'run_history' in tables:
            for row in db.execute('SELECT rowid AS _rowid,* FROM run_history'):
                records[row['id']] = dict(row)
        terminal_states = {'complete', 'failed', 'partial', 'cancelled', 'interrupted'}
        for key, row in records.items():
            stamp = row.get('finished_at') or row.get('requested_at')
            sortable = db.execute('SELECT julianday(?)', (stamp,)).fetchone()[0]
            # Permanent deletion fails safe: unknown states and unorderable
            # records are retained in addition to the five known terminal runs.
            # Legacy rows with no finish use their valid request time, matching
            # the display order without inventing a completion timestamp.
            if row.get('status') not in terminal_states or sortable is None:
                live.add(key)
        def order(row):
            stamp = row['finished_at'] or row['requested_at']
            # Match SQLite status ordering, including UTC offsets and legacy time.
            julian = db.execute('SELECT julianday(?)', (stamp,)).fetchone()[0]
            return (row['finished_at'] is not None, julian if julian is not None else float('-inf'), row['_rowid'])
        terminal = sorted((row for key, row in records.items() if key not in live), key=order, reverse=True)
        kept = live | {row['id'] for row in terminal[:5]}
        candidates = sorted(set(records) - kept)
        result['candidates'] = len(candidates)
        if apply:
            for table in ('run_history_errors', 'run_history_archive', 'run_history'):
                if table in tables:
                    db.executemany('DELETE FROM ' + table + ' WHERE id=?', [(key,) for key in candidates])
            db.commit()
            result['deleted'] = len(candidates)
        return result
    finally:
        db.close()
