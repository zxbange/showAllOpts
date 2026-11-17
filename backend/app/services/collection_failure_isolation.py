"""Retain closed-day manual publication failures without automatic replay.

Callers hold their existing execution lock. Quarantined payloads are never deleted
by a successful automatic run; a later manual attempt may recover them only while
no newer success has been published.
"""
import json

TABLES = {'public_publication', 'pending'}


def _archive(db):
    db.execute('CREATE TABLE IF NOT EXISTS manual_publication_failures ('
               'id INTEGER PRIMARY KEY, source_table TEXT NOT NULL, target TEXT NOT NULL, '
               'payload TEXT NOT NULL, success_marker TEXT NOT NULL)')


def quarantine_publication(service, table, state):
    if table not in TABLES:
        raise ValueError('Unsupported publication table')
    with service.db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return False
        _archive(db)
        row = db.execute(f'SELECT target,payload FROM {table} WHERE id=1').fetchone()
        if row is None:
            return False
        db.execute('INSERT INTO manual_publication_failures(source_table,target,payload,success_marker) VALUES(?,?,?,?)',
                   (table, row[0], row[1], json.dumps(state.get('last_success_at'))))
        db.execute(f'DELETE FROM {table} WHERE id=1')
        return True


def restore_manual_publication(service, table, state):
    if table not in TABLES:
        raise ValueError('Unsupported publication table')
    with service.db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return False
        _archive(db)
        if db.execute(f'SELECT 1 FROM {table} WHERE id=1').fetchone():
            return False
        row = db.execute('SELECT id,target,payload FROM manual_publication_failures '
                         'WHERE source_table=? AND success_marker=? ORDER BY id DESC LIMIT 1',
                         (table, json.dumps(state.get('last_success_at')))).fetchone()
        if row is None:
            return False
        db.execute(f'INSERT INTO {table}(id,target,payload) VALUES(1,?,?)', (row[1], row[2]))
        db.execute('DELETE FROM manual_publication_failures WHERE id=?', (row[0],))
        return True
