"""Recover a complete cash-cow snapshot without guessing a COMMIT result."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3

from pymysql.err import OperationalError
import pytest
from sqlalchemy import event, select

from app.core.config import Settings
from app.models.cash_cow import CashCowSnapshot
from app.services import cash_cow as module
from app.services import public_publication as publication_module
from app.services.cash_cow_import import write_public
from app.services.public_publication import PublicPublication
from test_cash_cow import Source, run


@pytest.fixture
def cash(tmp_path, monkeypatch, public_result_database):
    from app.services import database_retry
    monkeypatch.setattr(database_retry, 'sleep', lambda _: None)
    source_calls, outcomes, writes = [], [], []

    class Counted(Source):
        def universe(self):
            source_calls.append(1)
            return super().universe()

    service = module.CashCowScreen(Settings(_env_file=None, log_dir=tmp_path / 'logs'),
                                  source_factory=Counted, client_factory=lambda: nullcontext(None))
    record = module.record_task

    def captured(*args, **kwargs):
        outcomes.append(kwargs)
        return record(*args, **kwargs)

    def statements(conn, cursor, statement, *args):
        if statement.startswith(('INSERT INTO cash_cow_snapshot', 'UPDATE cash_cow_snapshot')):
            writes.append(statement.split()[0])

    monkeypatch.setattr(module, 'record_task', captured)
    event.listen(public_result_database, 'before_cursor_execute', statements)
    return service, source_calls, outcomes, writes


def pending(service):
    with service.db() as db:
        row = db.execute('SELECT payload FROM public_publication WHERE id=1').fetchone()
    return json.loads(row[0]) if row else None


def test_lost_commit_acknowledgement_reconciles_without_second_write(cash, monkeypatch):
    service, source_calls, outcomes, writes = cash
    attempts = []

    def lost_ack(engine, payload, check, **kwargs):
        attempts.append(1)
        result = write_public(engine, payload, check, **kwargs)
        if len(attempts) == 1:
            raise OperationalError(2013, 'PRIVATE MYSQL SQL AND PASSWORD')
        return result

    monkeypatch.setattr(module, 'write_public', lost_ack)
    assert run(service)
    assert attempts == [1, 1] and source_calls == [1]
    assert writes == ['INSERT']
    assert outcomes[-1]['outcome'] == 'success'
    assert pending(service) is None
    assert service.latest()['snapshot']['as_of'] == '2026-11-10'
    assert not service.read_state()['database_commit_uncertain']


def test_precommit_retries_are_bounded_and_saved_payload_does_not_refetch(cash, monkeypatch):
    service, source_calls, outcomes, writes = cash
    attempts = []

    def unavailable(engine, payload, check, *, progress=None):
        attempts.append(1)
        progress('schema')
        raise OperationalError(2013, 'PRIVATE MYSQL SQL AND PASSWORD')

    monkeypatch.setattr(module, 'write_public', unavailable)
    run(service)
    assert len(attempts) == 3 and source_calls == [1] and writes == []
    assert outcomes[-1]['outcome'] == 'failed'
    assert outcomes[-1]['error_details']['operation'] == 'schema'
    assert outcomes[-1]['error_details']['trade_date'] == '2026-11-10'
    assert 'PRIVATE' not in str(outcomes[-1])
    assert pending(service)['3']['snapshot']['as_of'] == '2026-11-10'
    assert not service.read_state()['changed_count_unconfirmed']
    monkeypatch.setattr(module, 'write_public', write_public)
    run(service)
    assert source_calls == [1] and writes == ['INSERT']
    assert outcomes[-1]['outcome'] == 'success' and pending(service) is None


def test_commit_uncertainty_survives_restart_and_later_precommit_errors(cash, monkeypatch):
    service, source_calls, outcomes, writes = cash
    attempts = []

    def lost_ack_then_outage(engine, payload, check, *, progress=None):
        attempts.append(1)
        if len(attempts) == 1:
            write_public(engine, payload, check, progress=progress)
        else:
            progress('schema')
        raise OperationalError(2013, 'private')

    monkeypatch.setattr(module, 'write_public', lost_ack_then_outage)
    run(service)
    assert outcomes[-1]['outcome'] == 'publication_pending'
    assert service.read_state()['database_commit_uncertain']
    assert service.read_state()['changed_count_unconfirmed']
    assert writes == ['INSERT']
    restarted = module.CashCowScreen(service.settings, source_factory=service.source_factory,
                                    client_factory=lambda: nullcontext(None))
    restarted.refresh(background=False, moment=datetime(2026, 11, 11, tzinfo=timezone.utc))
    assert len(attempts) == 6 and source_calls == [1]
    assert restarted.read_state()['target_date'] == '2026-11-10'
    assert restarted.read_state()['due_day'] == '2026-11-11'
    assert restarted.read_state()['database_commit_uncertain']
    assert restarted.read_state()['changed_count_unconfirmed']
    assert outcomes[-1]['outcome'] == 'publication_pending'
    monkeypatch.setattr(module, 'write_public', write_public)
    run(restarted)
    assert pending(restarted) is None and source_calls == [1] and writes == ['INSERT']
    assert not restarted.read_state()['database_commit_uncertain']


def test_confirmed_remote_commit_local_failure_is_partial_and_replays_without_update(cash, monkeypatch):
    service, source_calls, outcomes, writes = cash
    database = service.db

    @contextmanager
    def unavailable_local():
        with database() as db:
            class Connection:
                def execute(self, sql, params=()):
                    if sql.startswith('INSERT OR REPLACE INTO snapshots') and params[0] == 5:
                        raise sqlite3.OperationalError('synthetic local write failure')
                    return db.execute(sql, params)
            yield Connection()

    monkeypatch.setattr(service, 'db', unavailable_local)
    run(service)
    state = service.read_state()
    assert state['database_committed'] and not state['database_commit_uncertain']
    assert state['changed_count_unconfirmed'] and state['pending_run_id']
    assert not state.get('last_success_at')
    assert outcomes[-1]['outcome'] == 'publication_pending'
    assert outcomes[-1]['error_details']['stage'] == 'cache_publish'
    assert outcomes[-1]['error_details']['operation'] == 'local_publish'
    assert pending(service) and service.latest()['snapshot'] is None
    monkeypatch.setattr(service, 'db', database)
    run(service)
    assert source_calls == [1] and writes == ['INSERT']
    assert outcomes[-1]['outcome'] == 'success' and pending(service) is None


def test_cancel_after_lost_ack_retains_unknown_count_and_pending_result(cash, monkeypatch):
    service, source_calls, outcomes, writes = cash
    attempts = []

    def lost_ack(engine, payload, check, **kwargs):
        attempts.append(1)
        write_public(engine, payload, check, **kwargs)
        assert service.cancel()
        raise OperationalError(2013, 'private')

    monkeypatch.setattr(module, 'write_public', lost_ack)
    run(service)
    assert attempts == [1] and source_calls == [1] and writes == ['INSERT']
    state = service.read_state()
    assert outcomes[-1]['outcome'] == 'cancelled'
    assert state['database_commit_uncertain'] and state['changed_count_unconfirmed']
    assert state['publication_pending'] and pending(service)
    assert service.latest()['snapshot'] is None
    monkeypatch.setattr(module, 'write_public', write_public)
    run(service)
    assert source_calls == [1] and writes == ['INSERT'] and pending(service) is None


def test_cancel_at_local_commit_preserves_pending_identity_and_success_history(cash, monkeypatch):
    service, source_calls, outcomes, writes = cash
    database = service.db

    @contextmanager
    def cancel_before_commit():
        with database() as db:
            class Connection:
                def execute(self, sql, params=()):
                    result = db.execute(sql, params)
                    if sql.startswith('UPDATE state SET payload='):
                        state = json.loads(params[0])
                        if state.get('stage') == '3/5/10年筛选完成':
                            service.cancel_path(state).touch()
                    return result
            yield Connection()

    monkeypatch.setattr(service, 'db', cancel_before_commit)
    run(service)
    state = service.read_state()
    assert outcomes[-1]['outcome'] == 'cancelled'
    assert state['publication_pending'] and state['database_committed']
    assert state['changed_count_unconfirmed'] and state['pending_run_id']
    assert not state.get('last_success_at')
    assert service.latest()['snapshot'] is None and pending(service)
    monkeypatch.setattr(service, 'db', database)
    run(service)
    assert source_calls == [1] and writes == ['INSERT'] and pending(service) is None


def test_generic_publication_does_not_retry_without_explicit_opt_in(cash):
    service, _, _, _ = cash
    publication = PublicPublication(service)
    calls = []

    def writer(*_):
        calls.append(1)
        raise OperationalError(2013, 'private')

    try:
        publication.stage({'synthetic': True})
        with pytest.raises(OperationalError):
            publication.resume(writer, lambda *_: pytest.fail('must not publish'), lambda: None)
        assert calls == [1] and pending(service)
    finally:
        publication.close()


def test_database_configuration_change_blocks_retry(cash, monkeypatch):
    service, _, _, _ = cash
    publication = PublicPublication(service)
    calls = []

    def writer(*_):
        calls.append(1)
        raise OperationalError(2013, 'private')

    def changed(*_):
        monkeypatch.setattr(publication_module, 'database_config',
                            lambda _: dict(publication.config, revision=publication.config['revision'] + 1))

    try:
        publication.stage({'synthetic': True})
        with pytest.raises(RuntimeError, match='配置已变化'):
            publication.resume(writer, lambda *_: pytest.fail('must not publish'), lambda: None,
                               database_retry=True, on_retry=changed)
        assert calls == [1] and pending(service)
    finally:
        publication.close()


def test_identical_payload_replay_ignores_mysql_scalar_timestamp_precision(cash, monkeypatch, public_result_database):
    service, _, _, writes = cash
    precise = datetime(2026, 11, 10, 12, 0, 0, 987654, tzinfo=timezone.utc)
    monkeypatch.setattr(module, 'now', lambda: precise)
    run(service)
    table = CashCowSnapshot.__table__
    with public_result_database.begin() as conn:
        payload = conn.execute(select(table.c.payload)).scalar_one()
        conn.execute(table.update().values(updated_at=precise.replace(tzinfo=None, microsecond=0)))
    writes.clear()
    assert write_public(public_result_database, payload, lambda: None) == payload
    assert writes == []


def test_same_second_newer_snapshot_is_preserved_using_json_timestamp(cash, monkeypatch, public_result_database):
    service, _, _, writes = cash
    precise = datetime(2026, 11, 10, 12, 0, 0, 900000, tzinfo=timezone.utc)
    monkeypatch.setattr(module, 'now', lambda: precise)
    run(service)
    table = CashCowSnapshot.__table__
    with public_result_database.begin() as conn:
        current = conn.execute(select(table.c.payload)).scalar_one()
        conn.execute(table.update().values(updated_at=precise.replace(tzinfo=None, microsecond=0)))
    older = deepcopy(current)
    for part in older.values():
        part['snapshot']['run_id'] = 'older-same-second-publication'
        part['snapshot']['updated_at'] = precise.replace(microsecond=100000).isoformat()
    writes.clear()
    assert write_public(public_result_database, older, lambda: None) == current
    assert writes == []
    with public_result_database.connect() as conn:
        assert conn.execute(select(table.c.payload)).scalar_one() == current
