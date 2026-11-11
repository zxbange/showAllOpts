import errno
import sqlite3

import httpx
import pytest
from pymysql.err import OperationalError as MySQLOperationalError
from sqlalchemy.exc import OperationalError

from app.core.diagnostics import diagnostic_details

SECRET = 'credential-canary-password'


def test_sqlalchemy_driver_and_nested_os_cause():
    socket_error = OSError(errno.ECONNREFUSED, SECRET, '/private/' + SECRET)
    driver = MySQLOperationalError(2003, SECRET)
    driver.original_exception = socket_error
    error = OperationalError('SELECT ' + SECRET, {'password': SECRET}, driver)
    output = diagnostic_details(error)
    assert 'error_type=OperationalError' in output
    assert 'driver_type=OperationalError mysql_errno=2003' in output
    assert f'os_errno={errno.ECONNREFUSED}' in output
    assert SECRET not in output
    assert 'SELECT' not in output
    assert '/private/' not in output


def test_http_status_never_logs_url_headers_or_body():
    request = httpx.Request('GET', 'https://example.com/' + SECRET,
                            headers={'authorization': SECRET})
    response = httpx.Response(403, request=request, text=SECRET)
    output = diagnostic_details(httpx.HTTPStatusError(SECRET, request=request, response=response))
    assert 'http_status=403' in output
    assert SECRET not in output
    assert 'https' not in output
    assert 'authorization' not in output


@pytest.mark.parametrize('kind', [httpx.ConnectTimeout, httpx.ReadTimeout,
                                  httpx.WriteTimeout, httpx.PoolTimeout])
def test_timeout_classification(kind):
    output = diagnostic_details(kind(SECRET))
    assert f'error_type={kind.__name__}' in output
    assert 'reason=http_timeout' in output
    assert SECRET not in output


def test_sqlite_code_and_allowlisted_name():
    with sqlite3.connect(':memory:') as connection:
        try:
            connection.execute('SELECT ' + SECRET)
        except sqlite3.Error as error:
            output = diagnostic_details(error)
    assert 'sqlite_errno=1' in output
    assert 'sqlite_name=SQLITE_ERROR' in output
    assert SECRET not in output


def test_no_arbitrary_code_name_or_exception_formatting():
    class UnsafeError(Exception):
        code = SECRET

        def __str__(self):
            raise AssertionError('Do not format')

        __repr__ = __str__

    error = UnsafeError(SECRET)
    error.__class__.__name__ = SECRET
    assert diagnostic_details(error) == 'error_type=Exception frames=-'
    sqlite_error = sqlite3.OperationalError(SECRET)
    sqlite_error.sqlite_errorname = SECRET
    sqlite_error.sqlite_errorcode = SECRET
    assert SECRET not in diagnostic_details(sqlite_error)
    assert 'mysql_errno' not in diagnostic_details(MySQLOperationalError(SECRET, SECRET))


def test_causes_cycles_and_bounded_chain():
    first = RuntimeError(SECRET)
    second = ValueError(SECRET)
    first.__cause__ = second
    second.__cause__ = first
    output = diagnostic_details(first)
    assert 'cause_type=ValueError' in output
    assert 'chain_cycle=true' in output
    assert SECRET not in output
    for _ in range(20):
        outer = RuntimeError(SECRET)
        outer.__cause__ = first
        first = outer
    output = diagnostic_details(first)
    assert output.count('frames=') == 6
    assert 'chain_truncated=true' in output


def test_trace_frames_are_bounded_without_source_paths_or_locals():
    def recurse(depth):
        password = SECRET
        if depth:
            recurse(depth - 1)
        else:
            raise ValueError(password)

    try:
        recurse(20)
    except ValueError as error:
        output = diagnostic_details(error)
    assert 'frames=truncated,' in output
    assert output.count('test_diagnostics.py:') == 5
    assert ':recurse' in output
    assert SECRET not in output
    assert '/workspace/' not in output
    assert 'raise ValueError' not in output


def test_context_and_suppressed_context():
    context = RuntimeError(SECRET)
    error = ValueError(SECRET)
    error.__context__ = context
    assert 'context_type=RuntimeError' in diagnostic_details(error)
    error.__suppress_context__ = True
    assert 'context_type' not in diagnostic_details(error)


@pytest.mark.parametrize('number', [True, '1045', 2**100])
def test_numeric_codes_reject_non_integers_and_unbounded_values(number):
    error = MySQLOperationalError(number, SECRET)
    assert 'mysql_errno=' not in diagnostic_details(error)


def test_driver_also_present_as_cause_is_not_duplicated():
    driver = MySQLOperationalError(1045, SECRET)
    error = OperationalError(SECRET, {}, driver)
    error.__cause__ = driver
    output = diagnostic_details(error)
    assert output.count('mysql_errno=1045') == 1
    assert 'chain_cycle=' not in output


def test_os_timeout_is_classified_without_message():
    output = diagnostic_details(TimeoutError(SECRET))
    assert 'error_type=TimeoutError reason=os_timeout' in output
    assert SECRET not in output


def test_nested_exception_groups_preserve_safe_leaf_diagnostics():
    driver = MySQLOperationalError(1045, SECRET)
    nested = ExceptionGroup(SECRET, [httpx.ReadTimeout(SECRET), driver])
    output = diagnostic_details(ExceptionGroup(SECRET, [nested, ValueError(SECRET)]))
    assert 'error_type=ExceptionGroup' in output
    assert 'group_type=ReadTimeout reason=http_timeout' in output
    assert 'group_type=OperationalError mysql_errno=1045' in output
    assert SECRET not in output


def test_wide_and_deep_exception_groups_share_node_budget():
    error = ExceptionGroup(SECRET, [ValueError(SECRET) for _ in range(100)])
    output = diagnostic_details(error)
    assert output.count('frames=') == 6
    assert output.count('chain_truncated=true') == 1
    for _ in range(20):
        error = ExceptionGroup(SECRET, [error])
    output = diagnostic_details(error)
    assert output.count('frames=') == 6
    assert 'chain_truncated=true' in output
    assert SECRET not in output


def test_base_exception_group_never_formats_members():
    class UnsafeError(BaseException):
        def __str__(self):
            raise AssertionError('Do not format')

        __repr__ = __str__

    error = BaseExceptionGroup(SECRET, [UnsafeError(SECRET), KeyboardInterrupt(SECRET)])
    output = diagnostic_details(error)
    assert 'group_type=BaseException' in output
    assert 'group_type=KeyboardInterrupt' in output
    assert SECRET not in output
