"""Bounded retries for explicitly idempotent database publication operations.

The operation must include its complete transaction and reconcile an identical
snapshot before writing again. Never wrap arbitrary SQL or an individual COMMIT:
a lost acknowledgement does not establish whether the transaction committed.
Callers retain their durable payload and report uncertainty until reconciliation.
"""
from collections import deque
import errno
import sqlite3
from time import sleep

from pymysql.err import MySQLError
from sqlalchemy.exc import DBAPIError


_TRANSIENT_MYSQL = frozenset((1205, 1213, 2002, 2003, 2006, 2013, 2055))
_TRANSIENT_OS = frozenset((errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNREFUSED,
                           errno.ECONNABORTED, errno.EPIPE, errno.ENETUNREACH,
                           errno.EHOSTUNREACH))


def transient_database_error(error):
    """Classify driver errors by type/code, without parsing private messages."""
    pending, seen, transient = deque([error]), set(), False
    while pending and len(seen) < 8:
        current = pending.popleft()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, sqlite3.Error):
            # A local disk/transaction error is not a MySQL transport failure.
            return False
        if isinstance(current, MySQLError):
            code = current.args[0] if current.args else None
            if type(code) is not int or code not in _TRANSIENT_MYSQL:
                return False
            transient = True
        elif isinstance(current, DBAPIError):
            transient = transient or current.connection_invalidated
        elif isinstance(current, (TimeoutError, ConnectionError)):
            transient = True
        elif isinstance(current, OSError) and current.errno in _TRANSIENT_OS:
            transient = True
        for child in (getattr(current, 'orig', None), getattr(current, 'original_exception', None),
                      current.__cause__, current.__context__):
            if isinstance(child, BaseException):
                pending.append(child)
    return transient


def retry_database(operation, *, reset, check, on_retry=None, attempts=3):
    """Replay a complete idempotent operation using a fresh pool after failure.

    The writer has already unwound its transaction when reset() is called.
    Cancellation/configuration checks run outside the retry exception handler,
    so an interrupted or retargeted operation cannot be replayed accidentally.
    """
    if type(attempts) is not int or not 1 <= attempts <= 5:
        raise ValueError('Invalid database retry limit')
    for attempt in range(1, attempts + 1):
        check()
        try:
            return operation()
        except Exception as exc:
            if attempt == attempts or not transient_database_error(exc):
                raise
            check()
            reset()
            if on_retry is not None:
                on_retry(attempt + 1, exc)
            check()
            sleep(min(attempt, 2))
            check()
