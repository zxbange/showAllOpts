"""Cache failures remain actionable without exposing database details or SQL values."""
import errno
import json
import sqlite3

import pytest
from pymysql.err import OperationalError as MysqlError
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout

from app.core.cache_errors import failure_details
from test_cache import cached


@pytest.mark.parametrize('error,code,explanation', [
    (OperationalError('SELECT private_sql', {'password':'private_secret'},
                      MysqlError(2013, 'private_host private_secret')), 'MYSQL_2013', '中断或超时'),
    (PermissionError(errno.EACCES, 'private_secret', '/private/path'), 'OS_13', '权限不足'),
    (OSError(errno.ENOSPC, 'private_secret'), 'OS_28', '空间不足'),
    (PoolTimeout('private_secret'), 'POOL_TIMEOUT', '连接超时'),
    (RuntimeError('private_host private_secret'), 'UNKNOWN', '错误日志'),
    (RuntimeError(1234), 'UNKNOWN', '错误日志'),
])
def test_failure_codes_explain_cause_without_raw_messages(error, code, explanation):
    try:
        raise error
    except Exception as exc:
        actual, hint, frames = failure_details(exc)
    assert actual == code and explanation in hint
    assert 'test_failure_codes_explain_cause_without_raw_messages' in frames
    assert all(value not in str((actual, hint, frames))
               for value in ('private_host', 'private_secret', 'private_sql', '/private/path'))


def test_real_sqlite_write_error_is_distinguished_from_source_failure(tmp_path):
    path = tmp_path/'readonly.sqlite'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE records (id INTEGER PRIMARY KEY)')
    connection = sqlite3.connect('file:'+path.as_posix()+'?mode=ro', uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError) as failure:
            connection.execute('INSERT INTO records VALUES (1)')
        code, hint, frames = failure_details(failure.value)
        assert code == 'SQLITE_READONLY' and '不可写' in hint
    finally:
        connection.close()


def test_cache_reports_safe_details_and_retains_old_snapshot(cached, monkeypatch, caplog):
    _, cache = cached
    cache.refresh('industry-momentum', background=False)
    previous = cache.read_state()['modules']['industry-momentum'].copy()
    def fail(module):
        raise OperationalError('SELECT private_sql', {'password':'private_secret'},
                               MysqlError(2013, 'private_host private_secret'))
    monkeypatch.setattr(cache, 'copy_module', fail)
    cache.refresh('industry-momentum', background=False)
    after = cache.read_state()['modules']['industry-momentum']
    assert after['file'] == previous['file'] and after['updated_at'] == previous['updated_at']
    assert '[MYSQL_2013]' in after['error'] and '中断或超时' in after['error']
    assert 'code=MYSQL_2013' in caplog.text and 'frames=' in caplog.text
    for secret in ('private_host', 'private_secret', 'private_sql'):
        assert secret not in caplog.text and secret not in json.dumps(cache.status())
