"""Existing etf state schema compatibility boundary; no new schema."""

import sqlite3


def initialize_etf_state(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS configuration (id INTEGER PRIMARY KEY CHECK(id=1),
          revision INTEGER NOT NULL, ciphertext BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS manual_runs (id TEXT PRIMARY KEY, status TEXT NOT NULL,
          requested_at TEXT NOT NULL, error TEXT, phase TEXT, completed INTEGER DEFAULT 0,
          total INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS manual_run_scope (
          id TEXT PRIMARY KEY, schedule TEXT NOT NULL, retry_exhausted INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS slots (slot TEXT PRIMARY KEY, status TEXT NOT NULL,
          attempts INTEGER NOT NULL, attempted_at TEXT NOT NULL, error TEXT);
        CREATE TABLE IF NOT EXISTS run_history (
          id TEXT PRIMARY KEY, trigger TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
          requested_at TEXT NOT NULL, finished_at TEXT, slot TEXT, attempt INTEGER,
          time_basis TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS run_history_errors (id TEXT PRIMARY KEY, details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS observations (kind TEXT, code TEXT, day TEXT, status TEXT,
          attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind,code,day));
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
    """)


def migrate_etf_run_history(db: sqlite3.Connection) -> None:
    # Legacy state records only the most recent attempt for each automatic
    # slot, and no completion timestamp. Preserve what exists without
    # inventing previous attempts or their finish times.
    if db.execute("SELECT 1 FROM metadata WHERE key='run_history_migrated'").fetchone():
        return
    # etf-001-run-history: serialize the one-time backfill and marker.
    db.execute('BEGIN IMMEDIATE')
    if db.execute("SELECT 1 FROM metadata WHERE key='run_history_migrated'").fetchone():
        return
    records: list[tuple[str, str, str, str | None, str, None, str | None, int | None, str]] = []
    terminal = {'complete', 'partial', 'failed', 'interrupted', 'cancelled', 'abandoned'}
    for row in db.execute('SELECT * FROM slots'):
        if row['status'] in terminal:
            records.append(
                (
                    'legacy-slot:' + row['slot'],
                    'scheduled',
                    row['status'],
                    row['error'],
                    row['attempted_at'],
                    None,
                    row['slot'],
                    row['attempts'],
                    'legacy_request_time',
                )
            )
    for row in db.execute('SELECT * FROM manual_runs'):
        if row['status'] in terminal:
            records.append(
                (
                    'legacy-manual:' + row['id'],
                    'manual',
                    row['status'],
                    row['error'],
                    row['requested_at'],
                    None,
                    None,
                    None,
                    'legacy_request_time',
                )
            )
    records.sort(key=lambda row: row[4])
    db.executemany(
        'INSERT OR IGNORE INTO run_history VALUES (?,?,?,?,?,?,?,?,?)',
        records,
    )
    db.execute("INSERT OR IGNORE INTO metadata VALUES ('run_history_migrated','1')")
