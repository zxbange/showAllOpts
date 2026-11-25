"""Compatibility tests for durable SQLite module state (not disposable cache)."""

import sqlite3

import pytest

from app.repositories.etf_state_schema import initialize_etf_state, migrate_etf_run_history
from app.repositories.market_state_schema import initialize_market_state
from app.repositories.valuation_state_schema import initialize_valuation_state


def test_etf_history_backfill_rolls_back_and_replays(tmp_path):
    db = sqlite3.connect(tmp_path / 'state.sqlite')
    db.row_factory = sqlite3.Row
    with db:
        initialize_etf_state(db)
        db.execute("INSERT INTO slots VALUES ('slot','failed',3,'2026-11-01','reason')")
        db.execute(
            "INSERT INTO manual_runs(id,status,requested_at) VALUES ('queued','queued','2026-11-02')"
        )
        db.execute(
            "INSERT INTO manual_runs(id,status,requested_at) VALUES ('done','complete','2026-11-03')"
        )
        db.execute("INSERT INTO configuration VALUES (1,2,X'1234')")
        db.execute("INSERT INTO manual_run_scope VALUES ('queued','08:00',1)")
        db.execute("INSERT INTO observations VALUES ('share','fund','2026-11-01','pending',2)")
        db.execute(
            'CREATE TRIGGER reject_marker BEFORE INSERT ON metadata '
            "WHEN NEW.key='run_history_migrated' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError), db:
        migrate_etf_run_history(db)
    assert list(db.execute('SELECT * FROM run_history')) == []
    assert list(db.execute('SELECT * FROM metadata')) == []
    with db:
        db.execute('DROP TRIGGER reject_marker')
    for _ in range(2):
        initialize_etf_state(db)
        with db:
            migrate_etf_run_history(db)
    assert [
        tuple(row)
        for row in db.execute(
            'SELECT id,status,finished_at,time_basis FROM run_history ORDER BY id'
        )
    ] == [
        ('legacy-manual:done', 'complete', None, 'legacy_request_time'),
        ('legacy-slot:slot', 'failed', None, 'legacy_request_time'),
    ]
    assert tuple(db.execute('SELECT revision,ciphertext FROM configuration').fetchone()) == (
        2,
        b'\x12\x34',
    )
    assert tuple(db.execute('SELECT * FROM manual_run_scope').fetchone()) == ('queued', '08:00', 1)
    assert db.execute('SELECT attempts FROM observations').fetchone()[0] == 2
    db.close()


def test_market_reopen_additive_tables_preserves_existing_state(tmp_path):
    path = tmp_path / 'state.sqlite'
    db = sqlite3.connect(path)
    with db:
        initialize_market_state(db)
        db.execute("INSERT INTO configuration VALUES(1,5,X'1234')")
        db.execute("INSERT INTO metadata VALUES('methodology','current')")
        db.execute(
            "INSERT INTO days(trade_date,status,snapshot_hash,attempts,cache_pending,created_at,updated_at) VALUES('2026-11-01','pending','hash',4,1,'a','b')"
        )
        db.execute(
            "INSERT INTO observations(day,symbol,updated_at,attempts) VALUES('2026-11-01','stock','a',3)"
        )
        db.execute(
            "INSERT INTO manual_runs(id,status,requested_at,updated_at,cancel_requested) VALUES('run','running','a','b',1)"
        )
        db.execute('DROP TABLE manual_run_clocks')
        db.execute('DROP TABLE hq_checkpoints')
    db.close()
    db = sqlite3.connect(path)
    for _ in range(2):
        with db:
            initialize_market_state(db)
    assert db.execute('SELECT revision,ciphertext FROM configuration').fetchone() == (
        5,
        b'\x12\x34',
    )
    assert db.execute(
        'SELECT status,snapshot_hash,attempts,cache_pending FROM days'
    ).fetchone() == ('pending', 'hash', 4, 1)
    assert db.execute('SELECT attempts FROM observations').fetchone() == (3,)
    assert db.execute('SELECT status,cancel_requested FROM manual_runs').fetchone() == (
        'running',
        1,
    )
    assert list(db.execute('SELECT * FROM manual_run_clocks')) == []
    assert list(db.execute('SELECT * FROM hq_checkpoints')) == []
    assert db.execute("SELECT value FROM metadata WHERE key='methodology'").fetchone() == (
        'current',
    )
    db.close()


def test_valuation_existing_state_and_null_member_survive_reopen(tmp_path):
    path = tmp_path / 'history.sqlite3'
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    with db:
        initialize_valuation_state(db)
        db.execute('UPDATE state SET payload=\'{"run_id":"previous"}\'')
        db.execute("INSERT INTO members VALUES('2026-11-01','stock',NULL)")
        db.execute("INSERT INTO snapshots VALUES('2026-11-01','members','proof',X'1234')")
        db.execute("INSERT INTO slots VALUES('slot','failed')")
    db.close()
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    with db:
        initialize_valuation_state(db)
    assert tuple(db.execute('SELECT * FROM members').fetchone()) == ('2026-11-01', 'stock', None)
    assert db.execute('SELECT payload FROM state').fetchone()[0] == '{"run_id":"previous"}'
    assert db.execute('SELECT workbook FROM snapshots').fetchone()[0] == b'\x12\x34'
    assert db.execute('SELECT outcome FROM slots').fetchone()[0] == 'failed'
    db.close()
