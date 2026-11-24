"""Transport recovery preserves transaction, cancellation and failure boundaries."""
import sqlite3

import pytest
from pymysql.err import OperationalError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError

from app.services import database_retry as retry


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(retry, 'sleep', lambda seconds: None)


@pytest.mark.parametrize('code', [1205, 1213, 2002, 2003, 2006, 2013, 2055])
def test_wrapped_mysql_transport_failure_is_retryable(code):
    error = SQLAlchemyOperationalError('PRIVATE SQL', {'password': 'SECRET'}, OperationalError(code, 'PRIVATE'))
    assert retry.transient_database_error(error)


@pytest.mark.parametrize('error', [OperationalError(1045, 'PRIVATE password'),
                                   OperationalError(1146, 'PRIVATE table'),
                                   ValueError('invalid source'), sqlite3.OperationalError('database is locked')])
def test_permanent_or_local_failures_are_not_retried(error):
    calls = []
    def fail():
        calls.append(1)
        raise error
    with pytest.raises(type(error)) as raised:
        retry.retry_database(fail, reset=lambda: pytest.fail('no connection reset'), check=lambda: None)
    assert raised.value is error
    assert calls == [1]


def test_connection_wrapper_retains_numeric_cause_for_retry():
    try:
        raise OperationalError(2013, 'PRIVATE database address')
    except OperationalError:
        try:
            raise RuntimeError('safe database failure') from None
        except RuntimeError as error:
            assert retry.transient_database_error(error)


def test_lost_commit_acknowledgement_reconciles_without_duplicate_write(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'publication.sqlite'))
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE snapshot(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)'))
    attempts, writes, resets = [], [], []
    def publish():
        attempts.append(1)
        with engine.begin() as conn:
            existing = conn.execute(text('SELECT payload FROM snapshot WHERE id=1')).scalar()
            if existing == 'complete validated snapshot':
                return existing
            conn.execute(text('INSERT INTO snapshot VALUES(1,:payload)'), {'payload': 'complete validated snapshot'})
            writes.append(1)
        # The server committed but its acknowledgement never reached the client.
        raise OperationalError(2013, 'PRIVATE acknowledgement lost')
    def reset():
        resets.append(1)
        engine.dispose()
    try:
        assert retry.retry_database(publish, reset=reset, check=lambda: None) == 'complete validated snapshot'
        assert len(attempts) == 2 and writes == [1] and resets == [1]
        with engine.connect() as conn:
            assert conn.execute(text('SELECT count(*) FROM snapshot')).scalar() == 1
    finally:
        engine.dispose()


def test_exhaustion_leaves_original_failure_for_owner():
    attempts, resets, retry_numbers = [], [], []
    error = OperationalError(2013, 'PRIVATE network timeout')
    def fail():
        attempts.append(1)
        raise error
    with pytest.raises(OperationalError) as raised:
        retry.retry_database(fail, reset=lambda: resets.append(1), check=lambda: None,
                             on_retry=lambda number, exc: retry_numbers.append(number))
    assert raised.value is error
    assert len(attempts) == 3 and len(resets) == 2 and retry_numbers == [2, 3]


def test_cancellation_after_disconnect_prevents_any_replay():
    attempts, cancelled = [], []
    def fail():
        attempts.append(1)
        cancelled.append(True)
        raise OperationalError(2013, 'PRIVATE')
    def check():
        if cancelled:
            raise ValueError('cancelled')
    with pytest.raises(ValueError, match='cancelled'):
        retry.retry_database(fail, reset=lambda: pytest.fail('cancelled before reset'), check=check)
    assert attempts == [1]


def test_configuration_change_during_backoff_prevents_replay(monkeypatch):
    attempts, revision = [], [1]
    def fail():
        attempts.append(1)
        raise OperationalError(2013, 'PRIVATE')
    def check():
        if revision[0] != 1:
            raise ValueError('target changed')
    monkeypatch.setattr(retry, 'sleep', lambda _: revision.__setitem__(0, 2))
    with pytest.raises(ValueError, match='target changed'):
        retry.retry_database(fail, reset=lambda: None, check=check)
    assert attempts == [1]
