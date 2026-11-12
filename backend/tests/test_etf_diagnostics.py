"""Offline diagnostic regressions: keep useful causes without secrets/SQL."""
from contextlib import nullcontext
import errno
import logging
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pymysql
import pytest
from sqlalchemy.exc import OperationalError

from app.services.etf_configuration import ConfigurationFailure, target_key
from app.services.etf_diagnostics import database_diagnostic, log_database_failure
from test_etf_collection import worker, EVENING
from test_etf_page_configuration import form


@pytest.mark.parametrize('code,reason', [
    (1044, 'database_access_denied'), (1045, 'authentication_failed'),
    (1049, 'database_missing'), (1142, 'table_permission_denied'),
    (1146, 'table_missing'), (2003, 'connection_failed'),
    (3159, 'secure_transport_required'), (9999, 'database_operation_failed'),
])
def test_mysql_diagnostics_never_format_raw_errors_or_sql(code, reason, caplog):
    class UnsafeError(pymysql.err.OperationalError):
        def __str__(self):
            raise AssertionError('Driver message must never be formatted')
    raw = UnsafeError(code, 'secret password private-db private-user')
    error = OperationalError('sensitive SQL text', {'private_parameter': 'private-value'}, raw)
    with caplog.at_level(logging.ERROR):
        log_database_failure(error, operation='collection-test', stage='connect', target='test-target')
    output = caplog.text
    assert f'mysql_code={code}' in output and f'reason={reason}' in output
    assert 'error_type=sqlalchemy.exc.OperationalError' in output
    assert 'driver_error_type=test_etf_diagnostics.UnsafeError' in output
    assert all(secret not in output for secret in
               ('password', 'private-db', 'private-user', 'sensitive SQL', 'private_parameter', 'private-value'))
    assert all(record.exc_info is None for record in caplog.records)


def test_socket_error_and_unknown_error_have_safe_details():
    denied = ConnectionRefusedError(errno.ECONNREFUSED, 'private hostname')
    diagnostic = database_diagnostic(denied, 'socket_connect')
    assert diagnostic.os_errno == errno.ECONNREFUSED
    assert diagnostic.reason == 'connection_refused' and diagnostic.mysql_code is None
    error = pymysql.err.OperationalError(2003, 'private hostname')
    error.original_exception = denied
    assert database_diagnostic(error, 'connect').os_errno == errno.ECONNREFUSED
    assert database_diagnostic(TimeoutError('secret'), 'socket_connect').reason == 'connection_timeout'
    assert database_diagnostic(RuntimeError('secret'), 'connect').reason == 'database_operation_failed'


@pytest.mark.parametrize('failed_table', ['etf_share_daily', 'etf_price_daily', 'etf_trading_day'])
def test_probe_identifies_failed_table_and_disposes_engine(tmp_path, caplog, failed_table):
    w, _, _ = worker(tmp_path)
    w.configuration.validate=lambda body:w.configuration.load()
    calls, disposed = [], Mock()
    class Connection:
        def execute(self, statement):
            calls.append(str(statement))
            if failed_table in str(statement):
                raise OperationalError('private SQL', {'secret': 'parameter-secret'},
                    pymysql.err.ProgrammingError(1146, 'missing private_database.' + failed_table))
    w.configuration.engine = lambda config: SimpleNamespace(
        connect=lambda: nullcontext(Connection()), dispose=disposed)
    with caplog.at_level(logging.ERROR), pytest.raises(ConfigurationFailure) as failure:
        w.test_configuration(form())
    output = caplog.text
    assert len(caplog.records) == 1 and 'operation=collection-test stage=table_check' in output
    assert f'table={failed_table}' in output and 'mysql_code=1146' in output
    assert 'reason=table_missing' in output
    assert f'target={target_key(w.configuration.validate(form()))[:12]}' in output
    assert 'private_database' not in output and 'parameter-secret' not in output and 'private SQL' not in output
    assert '具体原因已记录到服务端错误日志' in str(failure.value)
    assert '1146' not in str(failure.value) and 'private' not in str(failure.value)
    assert not w.configuration.record()  # Probe did not save the configuration.
    disposed.assert_called_once_with()


@pytest.mark.parametrize('stage', ['resolve', 'validate_address', 'socket_connect', 'mysql_connect'])
def test_custom_target_preserves_original_diagnostic_and_logs_once(tmp_path, monkeypatch, caplog, stage):
    from app.services import database_sources
    w, _, _ = worker(tmp_path)
    body = form() | {'host': 'private-host.example', 'port': 3307, 'database': 'private_database'}
    w.configuration.validate=lambda request:w.configuration.load() | {k:body[k] for k in ('host','port','database')}
    disposed = Mock()
    w.engine_factory = lambda url, **options: SimpleNamespace(
        connect=lambda: nullcontext(options['creator']()), dispose=disposed)
    def resolve(*args):
        if stage == 'resolve':
            raise database_sources.SourceError('private DNS failure')
        address = '169.254.169.254' if stage == 'validate_address' else '10.1.2.3'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 3307))]
    monkeypatch.setattr(database_sources, 'resolve_addresses', resolve)
    connection, sock = Mock(), Mock()
    # Cleanup failures must not overwrite the actual authentication error.
    connection.close.side_effect = RuntimeError('private close secret')
    sock.close.side_effect = RuntimeError('private socket secret')
    connection.connect.side_effect = pymysql.err.OperationalError(1045, 'private-test-password private_database')
    monkeypatch.setattr(pymysql, 'connect', lambda **kwargs: connection)
    def create_connection(*args, **kwargs):
        if stage == 'socket_connect':
            raise ConnectionRefusedError(errno.ECONNREFUSED, 'private-host.example')
        return sock
    monkeypatch.setattr(socket, 'create_connection', create_connection)
    with caplog.at_level(logging.ERROR), pytest.raises(ConfigurationFailure):
        w.test_configuration(body)
    assert len(caplog.records) == 1
    output = caplog.text
    assert f'operation=collection-test stage={stage}' in output
    if stage == 'mysql_connect':
        assert 'mysql_code=1045' in output and 'reason=authentication_failed' in output
        assert 'driver_error_type=pymysql.err.OperationalError' in output
    elif stage == 'socket_connect':
        assert f'os_errno={errno.ECONNREFUSED}' in output and 'reason=connection_refused' in output
    elif stage == 'resolve':
        assert 'reason=dns_resolution_failed' in output
    else:
        assert 'reason=target_address_rejected' in output
    assert all(value not in output for value in
               ('private', '10.1.2.3', '169.254.169.254', 'etf_writer', '3307'))
    assert not w.configuration.record()
    disposed.assert_called_once_with()


def test_collection_write_failure_records_safe_database_details(tmp_path, caplog):
    w, collector, _ = worker(tmp_path)
    collector.write_records = Mock(side_effect=OperationalError('secret SQL', {'secret': 'private'},
        pymysql.err.OperationalError(1142, 'private account has no INSERT privilege')))
    with caplog.at_level(logging.ERROR):
        assert not w.tick(EVENING)
    output = caplog.text
    assert 'operation=collection-write stage=write_records' in output
    assert 'mysql_code=1142' in output and 'reason=table_permission_denied' in output
    assert 'secret SQL' not in output and 'private' not in output
    assert w.status()['disabled_reason'] == 'writer_permission_denied'
