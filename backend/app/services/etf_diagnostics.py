"""Allowlisted ETF database diagnostics, never raw driver messages or SQL."""
from dataclasses import dataclass
import logging
import socket

from pymysql.err import MySQLError


LOG = logging.getLogger(__name__)
MYSQL_REASONS = {
    1040: 'too_many_connections',
    1044: 'database_access_denied',
    1045: 'authentication_failed',
    1049: 'database_missing',
    1054: 'column_missing',
    1062: 'duplicate_key',
    1064: 'sql_syntax_error',
    1129: 'host_blocked',
    1130: 'host_not_allowed',
    1142: 'table_permission_denied',
    1143: 'column_permission_denied',
    1146: 'table_missing',
    1153: 'packet_too_large',
    1205: 'lock_wait_timeout',
    1213: 'deadlock',
    1227: 'privilege_denied',
    1251: 'authentication_protocol_unsupported',
    1290: 'server_read_only_or_restricted',
    1370: 'routine_permission_denied',
    2002: 'connection_failed',
    2003: 'connection_failed',
    2005: 'dns_resolution_failed',
    2006: 'connection_lost',
    2013: 'connection_lost',
    2026: 'tls_connection_failed',
    2055: 'connection_lost',
    2059: 'authentication_plugin_unavailable',
    3159: 'secure_transport_required',
}


@dataclass(frozen=True)
class DatabaseDiagnostic:
    driver_type: str
    mysql_code: int | None
    os_errno: int | None
    reason: str
    stage: str


def error_type(error):
    return f'{type(error).__module__}.{type(error).__name__}'


def database_diagnostic(error, stage):
    original = getattr(error, 'orig', error)
    saved = getattr(original, 'database_diagnostic', None)
    if isinstance(saved, DatabaseDiagnostic):
        return saved
    args = getattr(original, 'args', ())
    number = (args[0] if isinstance(original, MySQLError) and args and type(args[0]) is int else None)
    # PyMySQL retains the underlying socket error here on connection failure.
    socket_error = getattr(original, 'original_exception', original)
    os_errno = getattr(socket_error, 'errno', None) if isinstance(socket_error, OSError) else None
    if type(os_errno) is not int:
        os_errno = None
    reason = MYSQL_REASONS.get(number)
    if reason is None:
        if stage == 'resolve' or isinstance(original, socket.gaierror):
            reason = 'dns_resolution_failed'
        elif stage == 'validate_address':
            reason = 'target_address_rejected'
        elif isinstance(socket_error, TimeoutError):
            reason = 'connection_timeout'
        elif isinstance(socket_error, ConnectionRefusedError):
            reason = 'connection_refused'
        elif isinstance(socket_error, OSError):
            reason = 'network_or_os_error'
        else:
            reason = 'database_operation_failed'
    return DatabaseDiagnostic(error_type(original), number, os_errno, reason, stage)


def log_database_failure(error, *, operation, stage, target, table=None):
    diagnostic = database_diagnostic(error, stage)
    # Do not use str/repr(error), exc_info, SQL, parameters or the config/URL.
    # These structured values remain safe even for unsaved page credentials,
    # which the global log formatter does not know how to redact.
    LOG.error(
        'ETF database failure operation=%s stage=%s table=%s target=%s '
        'error_type=%s driver_error_type=%s mysql_code=%s os_errno=%s reason=%s',
        operation, diagnostic.stage, table or '-', target, error_type(error),
        diagnostic.driver_type, diagnostic.mysql_code if diagnostic.mysql_code is not None else '-',
        diagnostic.os_errno if diagnostic.os_errno is not None else '-', diagnostic.reason,
    )
