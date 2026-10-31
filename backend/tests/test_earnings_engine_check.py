"""MySQL-only checks with compiled MySQL SQL and real SQLAlchemy Rows, offline."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql

ROOT = Path(__file__).resolve().parents[2]
NAMES = ('earnings_gap_run', 'earnings_gap_stock', 'earnings_sync_head', 'earnings_sync_migration')
QUERY = ("SELECT TABLE_NAME, TABLE_TYPE, ENGINE FROM information_schema.TABLES "
         "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN "
         "('earnings_gap_run','earnings_gap_stock','earnings_sync_head','earnings_sync_migration')")

@pytest.fixture(params=['collector_rules/earnings_version.py', 'collectors/净利润断层_数据库版.py', 'collectors/策略批量采集_数据库版.py'])
def protocol(request):
    source = (ROOT / request.param).read_text()
    ns = {}
    exec(source[source.index('# BEGIN SHARED EARNINGS VERSION'):source.index('# END SHARED EARNINGS VERSION')], ns)
    return ns

class MysqlMetadataEngine:
    # _prepare executes its actual mysql branch. SQLite only makes result Rows;
    # this does not simulate server privileges, networking or MySQL transactions.
    dialect = mysql.dialect()
    def __init__(self, rows, selected=1, fail=False):
        self.rows, self.selected, self.fail = rows, selected, fail
        self.calls = []
        self.sql = create_engine('sqlite://')
    @contextmanager
    def connect(self):
        with self.sql.connect() as backing:
            engine = self
            class Connection:
                def execute(self, statement):
                    compiled = str(statement.compile(dialect=engine.dialect))
                    engine.calls.append(compiled)
                    if compiled == 'SELECT DATABASE() IS NOT NULL':
                        return backing.exec_driver_sql('SELECT ?', (engine.selected,))
                    if compiled == QUERY:
                        if engine.fail:
                            raise RuntimeError('password=do-not-leak host=private.invalid')
                        if not engine.rows:
                            return backing.exec_driver_sql('SELECT 1, 2, 3 WHERE 0')
                        width = len(engine.rows[0])
                        sql = ' UNION ALL '.join('SELECT ' + ','.join('?' for _ in range(width)) for _ in engine.rows)
                        return backing.exec_driver_sql(sql, tuple(v for row in engine.rows for v in row))
                    return backing.exec_driver_sql('SELECT 1 WHERE 0')
            yield Connection()
    begin = connect
    def run(self, protocol):
        session = SimpleNamespace(kw={'bind': self})
        try:
            protocol['_prepare_earnings_version'](session)
        finally:
            self.sql.dispose()
        assert session._earnings_version_ready

def good():
    return [(name, 'BASE TABLE', 'InnoDB') for name in NAMES]

@pytest.mark.parametrize('value', ['InnoDB', 'INNODB', ' innodb \t', b'InnoDB', b' InnoDB\n'])
def test_mysql_accepts_normalized_innodb(protocol, value):
    engine = MysqlMetadataEngine([(name.encode(), b' BASE TABLE ', value) for name in NAMES])
    engine.run(protocol)
    assert engine.calls[2:4] == ['SELECT DATABASE() IS NOT NULL', QUERY]
    assert all('ENGINE=InnoDB' in q and 'IF NOT EXISTS' in q for q in engine.calls[:2])

@pytest.mark.parametrize('value,reason', [(None, '无法识别'), (123, '无法识别'), (b'\xff', '无法识别'), ('', '无法识别'),
    ('password=do-not-leak', '无法识别'), ('MyISAM', '实际引擎非InnoDB'), ('MEMORY', '实际引擎非InnoDB'), (b'MyISAM', '实际引擎非InnoDB')])
def test_mysql_rejects_unsafe_engines_and_redacts(protocol, value, reason):
    rows = good(); rows[2] = (NAMES[2], 'BASE TABLE', value)
    with pytest.raises(ValueError, match=reason) as error:
        MysqlMetadataEngine(rows).run(protocol)
    assert 'do-not-leak' not in str(error.value)
    assert all(name in str(error.value) for name in NAMES)

@pytest.mark.parametrize('case,reason', [('missing', '缺失'), ('extra', '非目标表'), ('duplicate', '重复'),
    ('view', '普通表'), ('nulltype', '普通表'), ('wrongcase', '缺失'), ('malformed', '结构异常'), ('no_database', '未确认已选择数据库')])
def test_mysql_metadata_fails_closed(protocol, case, reason):
    rows, selected = good(), 1
    if case == 'missing': rows.pop()
    elif case == 'extra': rows.append(('private-do-not-leak', 'BASE TABLE', 'InnoDB'))
    elif case == 'duplicate': rows.append(rows[0])
    elif case == 'view': rows[0] = (NAMES[0], 'VIEW', None)
    elif case == 'nulltype': rows[0] = (NAMES[0], None, 'InnoDB')
    elif case == 'wrongcase': rows[0] = (NAMES[0].upper(), 'BASE TABLE', 'InnoDB')
    elif case == 'malformed': rows = [('unknown', 'InnoDB')]
    elif case == 'no_database': selected = 0
    with pytest.raises(ValueError, match=reason) as error:
        MysqlMetadataEngine(rows, selected).run(protocol)
    assert 'do-not-leak' not in str(error.value)

def test_mysql_query_failure_is_redacted(protocol):
    with pytest.raises(ValueError, match='无法读取') as error:
        MysqlMetadataEngine(good(), fail=True).run(protocol)
    assert 'password' not in str(error.value) and 'private.invalid' not in str(error.value)
    assert error.value.__suppress_context__
