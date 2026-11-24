"""Transient publication failures recover without losing transaction evidence."""
from datetime import date, datetime

from pymysql.err import OperationalError as DriverError
import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from app.services import collection_notifications as notifications
from app.services import database_retry, dividend_collection as module
from app.services.collection_outcomes import latest_outcomes, outcome_history
from test_dividend_collection import CHINA, DividendCollection, publication_service, read, result, write_result


MOMENT = datetime(2026, 11, 16, 23, tzinfo=CHINA)


def transport_timeout():
    driver = DriverError(2013, 'PRIVATE-DRIVER-CONNECTION')
    driver.__cause__ = TimeoutError('PRIVATE-SOCKET-TIMEOUT')
    return OperationalError('PRIVATE-SQL', {}, driver, connection_invalidated=True)


@pytest.fixture
def publication(tmp_path, monkeypatch):
    monkeypatch.setattr(database_retry, 'sleep', lambda _: None)
    monkeypatch.setattr(notifications, 'enabled', lambda *_: True)
    monkeypatch.setattr(notifications, 'start_notifications', lambda *_: None)
    service, remote, local, cache, calls = publication_service(tmp_path, monkeypatch)
    cache.fail = False
    yield service, remote, local, cache, calls
    remote.dispose()
    local.dispose()


def messages(service):
    with notifications._database(service.settings) as db:
        return [row['message'] for row in db.execute('SELECT message FROM outbox ORDER BY rowid')]


def test_transient_write_reopens_pool_and_recovers_without_refetch(publication, monkeypatch):
    service, remote, local, cache, calls = publication
    original, dispose = module.write_result, remote.dispose
    attempts, resets, commit_markers = [], [], []
    def observe_commit(conn):
        if service.read_state().get('stage') == '提交数据库事务':
            commit_markers.append(service.pending()[1]['commit_unconfirmed'])
    event.listen(remote, 'commit', observe_commit)
    def reset():
        resets.append(1)
        dispose()
    monkeypatch.setattr(remote, 'dispose', reset)
    def write(*args, progress=None, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            progress('写入两组股票快照')
            raise transport_timeout()
        return original(*args, progress=progress, **kwargs)
    monkeypatch.setattr(module, 'write_result', write)
    assert service.refresh(background=False, moment=MOMENT, slot='2026-11-16')
    state = service.status()
    assert len(attempts) == 2 and len(resets) == 2  # Retry reset, then final disposal.
    assert calls == [1] and commit_markers == [True]
    assert state['stage'] == 'complete' and state['database_attempt'] == 2
    assert state['written'] == 2 and not state['commit_unconfirmed']
    assert service.pending() is None
    assert len(read(remote)[1]) == len(read(local)[1]) == 2
    assert len(messages(service)) == 1 and '采集结果：成功' in messages(service)[0]
    assert latest_outcomes(service.settings)['items']['dividend-selection']['outcome'] == 'success'


@pytest.mark.parametrize('phase,operation', [
    ('检查数据库表结构', 'schema'), ('锁定数据库快照', 'lock'), ('写入两组股票快照', 'write'),
])
def test_exhausted_precommit_failure_retains_payload_and_reports_actual_phase(
        publication, monkeypatch, phase, operation):
    service, remote, local, cache, calls = publication
    old = result(date(2026, 11, 13))
    write_result(remote, *old)
    write_result(local, *old)
    before = read(remote), read(local)
    service.save_state({'written': 99})
    attempts = []
    def unavailable(*args, progress=None, **kwargs):
        attempts.append(1)
        progress(phase)
        raise transport_timeout()
    monkeypatch.setattr(module, 'write_result', unavailable)
    assert service.refresh(background=False, moment=MOMENT, slot='2026-11-16')
    state = service.status()
    assert len(attempts) == 3 and calls == [1]
    assert state['stage'] == 'failed' and state['publication_pending']
    assert not state['database_committed'] and not state['commit_unconfirmed']
    assert state['failed_stage'] == phase and state['written'] == 0
    assert service.pending()[1]['meta']['trade_date'] == MOMENT.date()
    assert (read(remote), read(local)) == before
    entry = outcome_history(service.settings, 'dividend-selection')['items'][0]
    assert entry['outcome'] == 'failed'
    assert entry['error_details'] == {
        'code': 'timeout', 'message': '操作超时，请稍后重试。', 'stage': 'database_write',
        'operation': operation, 'trade_date': '2026-11-16', 'mysql_errno': 2013,
    }
    message = messages(service)[0]
    assert '采集结果：失败' in message and '更新记录：0' in message
    assert '数据库写入' in message and 'MySQL 2013' in message
    assert '最新已发布选股：2026-11-13' in message
    assert '非银行：入选 1 只' in message and '银行：入选 1 只' in message
    assert 'PRIVATE-' not in message
    assert latest_outcomes(service.settings)['items']['dividend-selection']['outcome'] == 'failed'


def test_cancelled_transient_failure_does_not_retry(publication, monkeypatch):
    service, remote, local, cache, calls = publication
    attempts = []
    def cancelled(*args, progress=None, **kwargs):
        attempts.append(1)
        progress('锁定数据库快照')
        assert service.cancel()
        raise transport_timeout()
    monkeypatch.setattr(module, 'write_result', cancelled)
    assert service.refresh(background=False, moment=MOMENT)
    assert attempts == [1] and calls == [1]
    assert service.pending() is not None
    assert latest_outcomes(service.settings)['items']['dividend-selection']['outcome'] == 'cancelled'
    assert '采集结果：已取消' in messages(service)[0]


def test_lost_commit_acknowledgement_reconciles_without_false_zero(publication, monkeypatch):
    service, remote, local, cache, calls = publication
    original, attempts, committed = module.write_result, [], []
    def lose_ack(*args, progress=None, **kwargs):
        attempts.append(1)
        acknowledged = original(*args, progress=progress, **kwargs)
        if len(attempts) == 1:
            assert service.pending()[1]['commit_unconfirmed']
            committed.append(read(remote))
            raise transport_timeout()
        return acknowledged
    monkeypatch.setattr(module, 'write_result', lose_ack)
    assert service.refresh(background=False, moment=MOMENT)
    assert len(attempts) == 2 and calls == [1]
    assert read(remote) == committed[0]
    assert len(read(local)[1]) == 2
    state = service.status()
    assert state['stage'] == 'complete' and not state['commit_unconfirmed']
    assert state['database_committed'] and state['written'] is None
    assert service.pending() is None
    message = messages(service)[0]
    assert '采集结果：成功' in message and '更新记录：未核实' in message
    assert '无新增或变更' not in message


def test_commit_uncertainty_survives_later_connection_failures_and_restart(publication, monkeypatch):
    service, remote, local, cache, calls = publication
    original, attempts = module.write_result, []
    def unavailable_after_commit(*args, progress=None, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            original(*args, progress=progress, **kwargs)
        else:
            progress('检查数据库表结构')
        raise transport_timeout()
    monkeypatch.setattr(module, 'write_result', unavailable_after_commit)
    assert service.refresh(background=False, moment=MOMENT)
    state = service.status()
    assert len(attempts) == 3 and calls == [1]
    assert state['stage'] == 'publication_pending' and state['commit_unconfirmed']
    assert state['written'] is None and state['failed_stage'] == '检查数据库表结构'
    pending = service.pending()[1]
    assert pending['commit_unconfirmed'] and pending['changed_count_unknown']
    assert pending['changed_count'] is None
    assert '部分完成（数据发布待确认）' in messages(service)[0]
    assert '更新记录：未核实' in messages(service)[0]
    monkeypatch.setattr(module, 'write_result', original)
    restarted = DividendCollection(service.settings, cache=cache, source_factory=service.source_factory,
        engine_factory=lambda *_, **__: remote, client_factory=service.client_factory)
    assert restarted.refresh(background=False, moment=MOMENT)
    state = restarted.status()
    assert state['stage'] == 'complete' and not state['commit_unconfirmed']
    assert state['written'] is None and restarted.pending() is None
    assert calls == [1] and len(read(local)[1]) == 2


def test_restart_commit_marker_remains_unknown_when_database_cannot_be_reached(publication, monkeypatch):
    service, remote, local, cache, calls = publication
    from app.core.database import database_identity
    config = module.database_config(service.settings)
    meta, rows = result()
    service.stage(database_identity(config), meta, rows)
    # Simulate the durable marker that is written before COMMIT, without any
    # in-memory exception handler or confirmed affected-row count surviving.
    import json
    with service.db() as db:
        payload = json.loads(db.execute('SELECT payload FROM pending WHERE id=1').fetchone()[0])
        payload.update(commit_unconfirmed=True, database_committed=False,
                       changed_count=0, changed_count_unknown=False)
        db.execute('UPDATE pending SET payload=? WHERE id=1', (json.dumps(payload),))
    def unavailable(*args, progress=None, **kwargs):
        progress('检查数据库表结构')
        raise transport_timeout()
    monkeypatch.setattr(module, 'write_result', unavailable)
    assert service.refresh(background=False, moment=MOMENT)
    assert calls == [] and service.status()['commit_unconfirmed']
    assert service.status()['stage'] == 'publication_pending'
    assert service.status()['written'] is None
    assert '更新记录：未核实' in messages(service)[0]


def test_cache_failure_is_reported_as_cache_publication(publication):
    service, remote, local, cache, calls = publication
    cache.fail = True
    assert service.refresh(background=False, moment=MOMENT)
    state = service.status()
    assert state['stage'] == 'publication_pending' and state['database_committed']
    assert not state['commit_unconfirmed'] and state['written'] == 2
    entry = outcome_history(service.settings, 'dividend-selection')['items'][0]
    assert entry['error_details']['stage'] == 'cache_publish'
    assert entry['error_details']['operation'] == 'cache_refresh'
    assert '缓存发布' in messages(service)[0]
