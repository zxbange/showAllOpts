"""Bounded exception diagnostics without messages, request data, SQL or locals."""
import builtins
from collections import deque
import re
import sqlite3

import httpx
from pymysql import err as mysql_errors
from sqlalchemy import exc as sqlalchemy_errors

_MAX_ERRORS = 6
_MAX_FRAMES = 5
_MAX_FRAME_SCAN = 64
# Custom exception names may themselves contain data. Use a known base type.
_TYPES = {
    value: value.__name__
    for module in (builtins, httpx, mysql_errors, sqlalchemy_errors, sqlite3)
    for value in vars(module).values()
    if isinstance(value, type) and issubclass(value, BaseException)
}
_SQLITE_NAMES = {
    name for name, value in vars(sqlite3).items()
    if name.startswith('SQLITE_') and type(value) is int
}
_IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z_0-9.<>-]{0,79}\Z')


def _error_type(error):
    return next((_TYPES[cls] for cls in type(error).__mro__ if cls in _TYPES), 'Exception')


def _number(value):
    return type(value) is int and -(2**31) <= value < 2**31


def _frames(error):
    frames = deque(maxlen=_MAX_FRAMES)
    tb = error.__traceback__
    count = 0
    while tb is not None and count < _MAX_FRAME_SCAN:
        code = tb.tb_frame.f_code
        filename = code.co_filename.replace('\\', '/').rsplit('/', 1)[-1]
        function = code.co_name
        filename = filename if _IDENTIFIER.fullmatch(filename) else 'unknown'
        function = function if _IDENTIFIER.fullmatch(function) else 'unknown'
        frames.append(f'{filename}:{tb.tb_lineno}:{function}')
        tb = tb.tb_next
        count += 1
    prefix = 'truncated,' if count > _MAX_FRAMES or tb is not None else ''
    return prefix + ','.join(frames) if frames else '-'


def _codes(error):
    fields = []
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if type(status) is int and 100 <= status <= 599:
            fields.append(f'http_status={status}')
    if isinstance(error, httpx.TimeoutException):
        fields.append('reason=http_timeout')
    elif isinstance(error, TimeoutError):
        fields.append('reason=os_timeout')
    if isinstance(error, mysql_errors.MySQLError) and error.args and _number(error.args[0]):
        fields.append(f'mysql_errno={error.args[0]}')
    if isinstance(error, sqlite3.Error):
        number = getattr(error, 'sqlite_errorcode', None)
        if _number(number):
            fields.append(f'sqlite_errno={number}')
        name = getattr(error, 'sqlite_errorname', None)
        if type(name) is str and name in _SQLITE_NAMES:
            fields.append(f'sqlite_name={name}')
    if isinstance(error, OSError) and _number(error.errno):
        fields.append(f'os_errno={error.errno}')
    return fields


def diagnostic_details(error: BaseException) -> str:
    """Return safe types, numeric codes and basename:line:function frames only.

    Follow at most six cause/context/driver/group nodes, with at most five
    frames per node. Never format exceptions, SQL, URLs, headers, locals or an
    arbitrary ``code`` attribute. Unknown custom types use a known base type.
    """
    pending = deque([('error', error)])
    seen = set()
    parts = []
    groups_truncated = False
    while pending and len(seen) < _MAX_ERRORS:
        relation, current = pending.popleft()
        if id(current) in seen:
            parts.append('chain_cycle=true')
            continue
        seen.add(id(current))
        fields = [f'{relation}_type={_error_type(current)}', *_codes(current),
                  f'frames={_frames(current)}']
        parts.append(' '.join(fields))
        linked = []
        if isinstance(current, BaseExceptionGroup):
            # Bound both traversal and queued work, even for very wide groups.
            linked.extend(('group', child) for child in current.exceptions[:_MAX_ERRORS])
            groups_truncated = groups_truncated or len(current.exceptions) > _MAX_ERRORS
        if isinstance(current, sqlalchemy_errors.StatementError):
            linked.append(('driver', current.orig))
        if isinstance(current, mysql_errors.MySQLError):
            linked.append(('driver', getattr(current, 'original_exception', None)))
        if current.__cause__ is not None:
            linked.append(('cause', current.__cause__))
        elif not current.__suppress_context__:
            linked.append(('context', current.__context__))
        added = set()
        for kind, child in linked:
            if isinstance(child, BaseException) and id(child) not in added:
                pending.append((kind, child))
                added.add(id(child))
    if pending or groups_truncated:
        parts.append('chain_truncated=true')
    return ' | '.join(parts)
