"""Scoped socket budgets and safe publication-stage diagnostics."""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event

from app.core import database
from app.core.config import Settings
from app.services.dividend_collection import DividendCollection, write_result
from test_cache_network import capture_driver_options
from test_dividend_collection import result


def test_dividend_and_other_connections_follow_sixty_second_floor(monkeypatch, tmp_path):
    options = capture_driver_options(monkeypatch)
    socket_timeouts = []
    monkeypatch.setattr(database.socket, 'create_connection',
                        lambda address, timeout: socket_timeouts.append(timeout) or SimpleNamespace(close=lambda: None))
    settings = Settings(_env_file=None, log_dir=tmp_path, cache_db_read_timeout=150)
    service = DividendCollection(settings)
    service.engine_factory(settings)
    database.build_engine(settings)
    database.build_engine(settings, for_cache=True)
    assert [(o['connect_timeout'], o['read_timeout'], o['write_timeout']) for o in options] == [
        (60, 60, 60), (60, 60, 60), (60, 150, 60)]
    assert socket_timeouts == [60, 60, 60]


@pytest.mark.parametrize('field', ['connect_timeout_seconds', 'read_timeout_seconds', 'write_timeout_seconds'])
@pytest.mark.parametrize('value', [0, True, -1, 3601, '60'])
def test_invalid_socket_budget_is_rejected_before_any_connection(monkeypatch, field, value):
    options = capture_driver_options(monkeypatch)
    with pytest.raises(ValueError, match='socket timeout'):
        database.build_engine(Settings(_env_file=None), **{field: value})
    assert options == []


def test_publication_progress_tracks_transaction_and_does_not_log_payload(caplog):
    caplog.set_level('INFO')
    engine = create_engine('sqlite://')
    meta, records = result()
    records[0]['name'] = 'PRIVATE_PAYLOAD_SENTINEL'
    stages = []
    try:
        write_result(engine, meta, records, progress=stages.append)
        assert stages == ['检查数据库表结构', '检查股票字段', '数据库快照事务', '锁定数据库快照',
                          '读取现有股票记录', '写入两组股票快照', '提交数据库事务']
        assert 'phase complete stage=数据库快照事务 seconds=' in caplog.text
        assert 'PRIVATE_PAYLOAD_SENTINEL' not in caplog.text
    finally:
        engine.dispose()


def test_statement_timeout_reports_phase_without_sql_or_exception_message(caplog):
    engine = create_engine('sqlite://')
    def fail(conn, cursor, statement, *args):
        if statement.startswith('INSERT INTO dividend_selection_stock'):
            raise TimeoutError('mysql://user:SECRET@host/db private SQL')
    event.listen(engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(TimeoutError):
            write_result(engine, *result(), progress=lambda stage: None)
        assert 'phase failed stage=写入两组股票快照 seconds=' in caplog.text
        assert 'reason=os_timeout' in caplog.text
        assert 'SECRET' not in caplog.text and 'INSERT INTO' not in caplog.text
        assert 'phase complete stage=数据库快照事务' not in caplog.text
    finally:
        engine.dispose()


@pytest.mark.parametrize('seconds,expected', [(5, 60), (20, 60), (60, 60), (120, 120), (600, 600)])
def test_shared_explicit_timeouts_preserve_larger_values(monkeypatch, seconds, expected):
    options = capture_driver_options(monkeypatch)
    database.build_engine(Settings(_env_file=None), connect_timeout_seconds=seconds,
                          read_timeout_seconds=seconds, write_timeout_seconds=seconds)
    assert [options[0][key] for key in ('connect_timeout', 'read_timeout', 'write_timeout')] == [expected] * 3


def test_legacy_small_cache_read_timeout_is_raised_at_connection(monkeypatch):
    options = capture_driver_options(monkeypatch)
    database.build_engine(Settings(_env_file=None, cache_db_read_timeout=5), for_cache=True)
    assert options[0]['read_timeout'] == 60


def test_validation_source_also_uses_sixty_second_socket_budget(monkeypatch):
    from app.services import database_sources
    source = object.__new__(database_sources.DatabaseSources)
    monkeypatch.setattr(source, 'allowed', lambda config: '127.0.0.1')
    monkeypatch.setattr(database_sources, 'create_engine', lambda *args, **kw: kw['creator']())
    options, waits = [], []
    monkeypatch.setattr(database_sources.pymysql, 'connect', lambda **kw:
                        options.append(kw) or SimpleNamespace(connect=lambda **kw: None))
    monkeypatch.setattr(database_sources.socket, 'create_connection', lambda address, timeout: waits.append(timeout))
    source.build_engine(dict(host='example.test', port=3306, username='test', password='fake', database='test'))
    assert waits == [60]
    assert [options[0][key] for key in ('connect_timeout', 'read_timeout', 'write_timeout')] == [60, 60, 60]
