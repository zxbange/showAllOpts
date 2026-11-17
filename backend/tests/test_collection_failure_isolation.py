from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace

import pytest
from app.services.collection_failure_isolation import quarantine_publication, restore_manual_publication


@pytest.mark.parametrize('table', ['pending', 'public_publication'])
def test_manual_failure_archived_without_automatic_replay(tmp_path, table):
    @contextmanager
    def db():
        with sqlite3.connect(tmp_path / 'state.db') as conn:
            yield conn
    service = SimpleNamespace(db=db)
    state = {'last_success_at': 'old'}
    with db() as conn:
        conn.execute(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY,target TEXT,payload TEXT)')
        conn.execute(f'INSERT INTO {table} VALUES(1,?,?)', ('database', '{"saved":true}'))
    assert quarantine_publication(service, table, state)
    assert not quarantine_publication(service, table, state)
    with db() as conn:
        assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
        assert conn.execute('SELECT payload FROM manual_publication_failures').fetchone()[0] == '{"saved":true}'
    # A newer scheduled publication makes old manual payload unsafe to replay.
    assert not restore_manual_publication(service, table, {'last_success_at': 'new'})
    assert restore_manual_publication(service, table, state)
    assert not restore_manual_publication(service, table, state)
    with db() as conn:
        assert conn.execute(f'SELECT payload FROM {table}').fetchone()[0] == '{"saved":true}'


def test_next_automatic_collection_unknown_and_bounded(monkeypatch):
    from datetime import datetime, timezone
    from app.services import collection_calendar as calendar
    moment = datetime(2026, 11, 14, tzinfo=timezone.utc)
    calls = []
    def closed(day):
        calls.append(day)
        return False
    monkeypatch.setattr(calendar, 'trading_day', closed)
    assert calendar.next_automatic_collection(moment) is None
    assert len(calls) == 370
    monkeypatch.setattr(calendar, 'trading_day', lambda day: None)
    assert calendar.next_automatic_collection(moment) is None
