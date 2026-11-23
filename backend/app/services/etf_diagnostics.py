"""Allowlisted ETF database diagnostics, never raw driver messages or SQL."""
import builtins
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import logging
import re
import socket
from time import monotonic

from pymysql import err as mysql_errors
from sqlalchemy import exc as sqlalchemy_errors

MySQLError = mysql_errors.MySQLError


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


_WRITE_PHASES = frozenset(('schema_check', 'connect', 'load', 'load_share_keys',
                           'load_price_keys', 'load_calendar', 'flush', 'commit'))
_WRITE_RUN = ContextVar('etf_database_write_run', default='-')
_STAGES = _WRITE_PHASES | {
    'engine_create', 'write_records', 'connect', 'connection_close', 'table_check',
    'resolve', 'validate_address', 'driver_init', 'socket_connect', 'mysql_connect',
}
_TABLES = frozenset(('etf_share_daily', 'etf_price_daily', 'etf_trading_day'))
_TYPE_MAP = {
    value: f'{value.__module__}.{value.__name__}'
    for module in (builtins, socket, mysql_errors, sqlalchemy_errors)
    for value in vars(module).values()
    if isinstance(value, type) and issubclass(value, BaseException)
    and not value.__module__.startswith('_')
}


@dataclass(frozen=True)
class DatabaseDiagnostic:
    driver_type: str
    mysql_code: int | None
    os_errno: int | None
    reason: str
    stage: str
    table: str | None = None
    socket_timeout: bool = False
    seconds: float | None = None


@contextmanager
def database_write_run(run):
    """Correlate database diagnostics without accepting arbitrary IDs into logs."""
    token = _WRITE_RUN.set(run if isinstance(run, str) and re.fullmatch(r'[a-f0-9]{12}', run) else '-')
    try:
        yield
    finally:
        _WRITE_RUN.reset(token)


@contextmanager
def database_phase(stage, table=None, *, rows=None):
    """Tag the original exception with its innermost, allowlisted write phase."""
    started = monotonic()
    known = isinstance(stage, str) and stage in _WRITE_PHASES
    table = table if isinstance(table, str) and table in _TABLES else None
    try:
        yield
    except Exception as error:
        try:
            if known and not hasattr(error, '_etf_database_phase'):
                error._etf_database_phase = (stage, table, monotonic() - started)
        except (AttributeError, TypeError):
            pass  # A diagnostic tag must not mask an immutable exception.
        raise
    else:
        if known:
            LOG.info('ETF database phase complete run=%s stage=%s table=%s rows=%s seconds=%.3f',
                     _WRITE_RUN.get(), stage, table or '-', rows if _number(rows) else '-', monotonic() - started)


def error_type(error):
    return next((_TYPE_MAP[cls] for cls in type(error).__mro__ if cls in _TYPE_MAP),
                'builtins.Exception')


def _number(value):
    return type(value) is int and 0 <= value <= 2**31 - 1


def _error_chain(error):
    """Visit SQLAlchemy driver, PyMySQL socket, and Python exception cause links."""
    pending, visited = deque([error]), set()
    while pending and len(visited) < 8:
        current = pending.popleft()
        if not isinstance(current, BaseException) or id(current) in visited:
            continue
        visited.add(id(current))
        yield current
        if isinstance(current, sqlalchemy_errors.StatementError):
            pending.append(current.orig)
        if isinstance(current, MySQLError):
            pending.append(getattr(current, 'original_exception', None))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif not current.__suppress_context__:
            pending.append(current.__context__)


def database_diagnostic(error, stage):
    chain = tuple(_error_chain(error))
    stage = stage if stage in _STAGES else 'unknown'
    phase = next((phase for item in chain
                  if isinstance((phase := getattr(item, '_etf_database_phase', None)), tuple)
                  and len(phase) == 3 and phase[0] in _WRITE_PHASES), None)
    original = error.orig if isinstance(error, sqlalchemy_errors.StatementError) else error
    saved = next((saved for item in chain
                  if isinstance((saved := getattr(item, 'database_diagnostic', None)),
                                DatabaseDiagnostic)), None)
    if saved is not None:
        # A concrete socket/handshake stage survives a surrounding writer phase.
        return replace(saved, table=phase[1], seconds=phase[2]) if phase else saved
    table, seconds = None, None
    if phase:
        stage, table, seconds = phase
    mysql_error = next((item for item in chain if isinstance(item, MySQLError)), None)
    number = (mysql_error.args[0] if mysql_error is not None and mysql_error.args
              and _number(mysql_error.args[0]) else None)
    socket_errors = tuple(item for item in chain if isinstance(item, OSError))
    os_errno = next((item.errno for item in socket_errors if _number(item.errno)), None)
    timeout = any(isinstance(item, TimeoutError) for item in socket_errors)
    reason = MYSQL_REASONS.get(number)
    if reason is None:
        if stage == 'resolve' or any(isinstance(item, socket.gaierror) for item in socket_errors):
            reason = 'dns_resolution_failed'
        elif stage == 'validate_address':
            reason = 'target_address_rejected'
        elif timeout:
            reason = ('socket_timeout' if stage in _WRITE_PHASES and stage != 'connect'
                      else 'connection_timeout')
        elif any(isinstance(item, ConnectionRefusedError) for item in socket_errors):
            reason = 'connection_refused'
        elif socket_errors:
            reason = 'network_or_os_error'
        else:
            reason = 'database_operation_failed'
    return DatabaseDiagnostic(error_type(mysql_error if mysql_error is not None else original),
                              number, os_errno, reason, stage, table, timeout, seconds)


def log_database_failure(error, *, operation, stage, target, table=None):
    diagnostic = database_diagnostic(error, stage)
    # Never format the error, SQL, parameters, credentials, raw URL or traceback.
    LOG.error(
        'ETF database failure operation=%s stage=%s table=%s target=%s '
        'error_type=%s driver_error_type=%s mysql_code=%s os_errno=%s reason=%s '
        'socket_timeout=%s stage_seconds=%s run=%s',
        operation if operation in ('collection-write', 'collection-test') else 'unknown',
        diagnostic.stage, diagnostic.table or (table if table in _TABLES else '-'),
        target if isinstance(target, str) and re.fullmatch(r'[a-f0-9]{12}', target) else '-',
        error_type(error), diagnostic.driver_type,
        diagnostic.mysql_code if diagnostic.mysql_code is not None else '-',
        diagnostic.os_errno if diagnostic.os_errno is not None else '-', diagnostic.reason,
        'true' if diagnostic.socket_timeout else 'unconfirmed',
        f'{diagnostic.seconds:.3f}' if diagnostic.seconds is not None else '-', _WRITE_RUN.get(),
    )
