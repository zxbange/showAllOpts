"""Existing valuation state schema compatibility boundary; no new schema."""

import sqlite3


def initialize_valuation_state(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS calendar (trade_date TEXT PRIMARY KEY, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS points (trade_date TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS members (trade_date TEXT NOT NULL, code TEXT NOT NULL,
            pe_ttm TEXT, PRIMARY KEY(trade_date,code));
        CREATE TABLE IF NOT EXISTS proofs (trade_date TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots (trade_date TEXT PRIMARY KEY, members TEXT NOT NULL,
            proof TEXT NOT NULL, workbook BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS failures (trade_date TEXT PRIMARY KEY, code TEXT NOT NULL,
            reason TEXT NOT NULL, attempts INTEGER NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS comparisons (id INTEGER PRIMARY KEY, trade_date TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS comparisons_date ON comparisons(trade_date);
        CREATE TABLE IF NOT EXISTS slots (slot TEXT PRIMARY KEY, outcome TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
        INSERT OR IGNORE INTO state VALUES(1,'{}');
    """)
    # valuation-001-members-nullable: preserve legacy rows in one transaction.
    db.execute('BEGIN IMMEDIATE')
    if any(
        row['name'] == 'pe_ttm' and row['notnull']
        for row in db.execute('PRAGMA table_info(members)')
    ):
        db.execute(
            'CREATE TABLE members_nullable (trade_date TEXT NOT NULL, code TEXT NOT NULL, '
            'pe_ttm TEXT, PRIMARY KEY(trade_date,code))'
        )
        db.execute('INSERT INTO members_nullable SELECT trade_date,code,pe_ttm FROM members')
        db.execute('DROP TABLE members')
        db.execute('ALTER TABLE members_nullable RENAME TO members')
