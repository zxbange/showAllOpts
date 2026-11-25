"""Existing market state schema compatibility boundary; no new schema."""

import sqlite3


def initialize_market_state(connection: sqlite3.Connection) -> None:
    connection.execute('PRAGMA busy_timeout=15000')
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS configuration (
          id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
          ciphertext BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS calendar (day TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS days (
          trade_date TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
          error TEXT, snapshot_json TEXT, snapshot_hash TEXT,
          total INTEGER NOT NULL DEFAULT 0, heat_status TEXT NOT NULL DEFAULT 'pending',
          crowding_status TEXT NOT NULL DEFAULT 'pending', heat_json TEXT, crowding_json TEXT,
          heat_hash TEXT, crowding_hash TEXT, attempts INTEGER NOT NULL DEFAULT 0,
          attempt_date TEXT, attempts_today INTEGER NOT NULL DEFAULT 0,
          next_attempt_at TEXT, cache_pending INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS observations (
          day TEXT NOT NULL, symbol TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, error TEXT,
          quote_json TEXT, valid INTEGER NOT NULL DEFAULT 0,
          above INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
          PRIMARY KEY(day,symbol));
        CREATE TABLE IF NOT EXISTS manual_runs (
          id TEXT PRIMARY KEY, status TEXT NOT NULL, day TEXT,
          trigger TEXT NOT NULL DEFAULT 'manual', phase TEXT NOT NULL DEFAULT 'queued',
          requested_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT,
          cancel_requested INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS manual_run_clocks (
          run_id TEXT PRIMARY KEY, hhmm TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS market_observations_pending ON observations(day,status);
        CREATE TABLE IF NOT EXISTS methodology_archive (
          kind TEXT NOT NULL, day TEXT NOT NULL, symbol TEXT NOT NULL DEFAULT '',
          methodology TEXT NOT NULL, payload_hash TEXT NOT NULL, payload TEXT NOT NULL,
          archived_at TEXT NOT NULL, PRIMARY KEY(kind,day,symbol,methodology,payload_hash));
        CREATE TABLE IF NOT EXISTS hq_checkpoints (
          day TEXT NOT NULL, node_snapshot_hash TEXT NOT NULL, symbol TEXT NOT NULL,
          row_json TEXT NOT NULL, updated_at TEXT NOT NULL,
          PRIMARY KEY(day,node_snapshot_hash,symbol));
    """)
