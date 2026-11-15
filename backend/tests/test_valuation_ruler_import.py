"""Bounded MySQL reads and compressed transport; no external database required."""
from datetime import date, datetime
from decimal import Decimal
import json
import struct
from types import SimpleNamespace
import zlib

import pytest
from sqlalchemy import Numeric, String
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BindParameter, BooleanClauseList, Grouping
from app.services import valuation_ruler_import as imp
from app.services.valuation_ruler_model import ValuationError, validate_day

DAY = date(2026, 11, 10)
STAMP = datetime(2026, 11, 10, 20)


class Result:
    def __init__(self, rows, owner):
        self.rows, self.owner, self.closed = iter(rows), owner, False

    def mappings(self):
        return self

    def fetchmany(self, size):
        self.owner.fetches += 1
        rows = []
        for _ in range(size):
            try:
                rows.append(next(self.rows))
            except StopIteration:
                break
        return rows

    def all(self):
        raise AssertionError('Unbounded reads are forbidden')

    def close(self):
        self.closed = True


class Inspector:
    def __init__(self):
        self.overrides, self.engine, self.pk = {}, 'InnoDB', None

    def get_table_names(self):
        return [t.name for t in (imp.DAILY, imp.MEMBER, imp.CALENDAR)]

    def get_columns(self, name):
        return [dict(name=c.name,type=self.overrides.get(c.name,c.type),nullable=False)
                for c in imp.metadata.tables[name].columns]

    def get_pk_constraint(self, name):
        return {'constrained_columns': self.pk if self.pk is not None else list(imp.metadata.tables[name].primary_key.columns.keys())}

    def get_table_options(self, name):
        return {'mysql_engine': self.engine}


class Engine:
    def __init__(self, rows, packed=False):
        self.dialect = SimpleNamespace(name='mysql', server_version_info=(8, 0, 36) if packed else (5, 7, 21))
        self.rows = rows
        self.sql, self.options, self.results, self.statements = [], [], [], []
        self.fetches, self.disposed = 0, False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def connect(self):
        return self

    def begin(self):
        return self

    def execution_options(self, **kwargs):
        self.options.append(kwargs)
        return self

    def exec_driver_sql(self, statement):
        self.sql.append(statement)

    def execute(self, statement):
        self.statements.append(statement)
        table = statement.get_final_froms()[0]
        rows = self.rows[table.name]
        if statement.whereclause is not None:
            rows = [r for r in rows if matches(statement.whereclause, r)]
        rows = sorted(rows, key=lambda r: tuple(r[c.name] for c in table.primary_key))
        if 'json_arrayagg' in str(statement):
            days = sorted({r['trade_date'] for r in rows})
            rows = [pack_day(day, [r for r in rows if r['trade_date'] == day]) for day in days]
        elif statement._distinct:
            rows = [dict(trade_date=day) for day in sorted({r['trade_date'] for r in rows})]
        result = Result(rows[:statement._limit_clause.value], self)
        self.results.append(result)
        return result

    def dispose(self):
        self.disposed = True


def matches(expr, row):
    if isinstance(expr, BindParameter):
        return expr.value
    if isinstance(expr, Grouping):
        return matches(expr.element, row)
    if isinstance(expr, BooleanClauseList):
        values = [matches(c, row) for c in expr.clauses]
        return all(values) if expr.operator is operators.and_ else any(values)
    if hasattr(expr, 'left'):
        left, right = matches(expr.left, row), matches(expr.right, row)
        return left in right if expr.operator is operators.in_op else expr.operator(left, right)
    return row[expr.name]


def pack_day(day, rows):
    # MySQL casts DECIMAL to text before JSON encoding, preserving null and scale.
    values = [[r['index_code'], r['method_version'], r['code'],
               None if r['pe_ttm'] is None else str(r['pe_ttm']), r['source'],
               None if r['updated_at'] is None else str(r['updated_at'])] for r in reversed(rows)]
    raw = json.dumps(values).encode()
    return dict(trade_date=day, members=len(rows), payload=struct.pack('<I', len(raw)) + zlib.compress(raw))


@pytest.fixture(params=[False, True], ids=['legacy-rows', 'mysql-packed'])
def source(monkeypatch, request):
    members = {f'{i:06d}.XSHE': Decimal('12.25') if i else Decimal('-3.1') for i in range(300)}
    daily = validate_day(DAY,'4000',members,imp.SOURCE)
    daily.update(trade_date=DAY,updated_at=STAMP)
    rows = {imp.DAILY.name:[daily], imp.CALENDAR.name:[dict(trade_date=DAY,source=imp.CALENDAR_SOURCE,updated_at=STAMP)],
            imp.MEMBER.name:[dict(trade_date=DAY,index_code=imp.INDEX,method_version=imp.METHOD,code=code,
                pe_ttm=pe,source=imp.SOURCE,updated_at=STAMP) for code,pe in members.items()]}
    inspector = Inspector()
    monkeypatch.setattr(imp,'inspect',lambda connection:inspector)
    return Engine(rows, packed=request.param), inspector


def test_buffered_readonly_staging_cleanup(source,tmp_path):
    engine,_ = source
    with imp.read_source(engine,DAY,lambda:None,stage_dir=tmp_path) as staged:
        points = list(staged.points())
        assert len(points) == 1
        assert points[0][0]['positive_count'] == 299
        assert len(points[0][1]) == 300
        assert staged.days() == [DAY]
        assert list(tmp_path.iterdir())
    assert not list(tmp_path.iterdir())
    assert engine.sql == ['SET TRANSACTION READ ONLY','START TRANSACTION WITH CONSISTENT SNAPSHOT']
    assert {'isolation_level':'REPEATABLE READ'} in engine.options
    assert all(r.closed for r in engine.results)
    assert all(o.get('stream_results') is not True for o in engine.options)


@pytest.mark.parametrize('phase', ['connect', 'schema', 'calendar', 'summary', 'members'])
def test_progress_identifies_disconnect_phase_without_exposing_sql(source, monkeypatch, caplog, phase):
    from pymysql.err import OperationalError as MysqlError
    from sqlalchemy.exc import OperationalError
    engine, _ = source
    progress = []
    def fail(*args, **kwargs):
        raise OperationalError('SELECT private_sql', {'password': 'private_password'},
                               MysqlError(2013, 'private_host'))
    if phase == 'connect':
        monkeypatch.setattr(engine, 'connect', fail)
    elif phase == 'schema':
        monkeypatch.setattr(imp, 'check_schema', fail)
    else:
        table = {'calendar': imp.CALENDAR.name, 'summary': imp.DAILY.name, 'members': imp.MEMBER.name}[phase]
        execute = engine.execute
        def disconnect(statement):
            return fail() if statement.get_final_froms()[0].name == table else execute(statement)
        monkeypatch.setattr(engine, 'execute', disconnect)
    with caplog.at_level('INFO'), pytest.raises(OperationalError):
        with imp.read_source(engine, DAY, lambda: None, progress=lambda message, percent: progress.append((message, percent))):
            pytest.fail('Disconnected snapshot exposed')
    expected = {'connect': '连接估值源数据库', 'schema': '检查估值表结构',
                'calendar': '读取估值交易日历', 'summary': '读取每日估值汇总', 'members': '估值历史：'}
    assert progress[-1][0].startswith(expected[phase])
    if phase not in ('connect', 'schema'):
        assert 'Valuation read start table=' + table in caplog.text
        assert 'Valuation read failed table=' + table in caplog.text
        assert 'code=MYSQL_2013' in caplog.text and 'frames=' in caplog.text
    assert all(secret not in caplog.text for secret in ('private_sql', 'private_password', 'private_host'))


@pytest.mark.parametrize('field,value', [('mean_pe_ttm','99'),('pe_sum','99'),('members_sha256','0'*64),
    ('positive_count',300),('loss_count',0),('member_count',299),('source','other'),('index_code','wrong'),
    ('method_version','wrong'),('updated_at',None)])
def test_bad_daily_never_exposed(source,tmp_path,field,value):
    engine,_ = source
    engine.rows[imp.DAILY.name][0][field] = value
    with pytest.raises(ValueError):
        with imp.read_source(engine,DAY,lambda:None,stage_dir=tmp_path):
            pytest.fail('Corrupt snapshot exposed')
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('value',[None,Decimal(0),Decimal('NaN'),Decimal('Infinity')])
def test_unknown_pe_rejected(source,value):
    engine,_ = source
    engine.rows[imp.MEMBER.name][0]['pe_ttm'] = value
    with pytest.raises(ValuationError):
        with imp.read_source(engine,DAY,lambda:None):
            pytest.fail('Invalid PE exposed')


@pytest.mark.parametrize('corruption',['duplicate','missing','all_missing','orphan','source','code','calendar',
                                       'index_code','method_version','updated_at'])
def test_invalid_members_calendar(source,corruption):
    engine,_ = source
    rows = engine.rows[imp.MEMBER.name]
    if corruption == 'duplicate':
        rows[-1]['code'] = rows[0]['code']
    elif corruption == 'missing':
        rows.pop()
    elif corruption == 'all_missing':
        rows.clear()
    elif corruption == 'orphan':
        engine.rows[imp.DAILY.name].clear()
    elif corruption == 'source':
        rows[0]['source'] = 'other'
    elif corruption == 'code':
        rows[0]['code'] = '600000.XSHE'
    elif corruption in ('index_code', 'method_version', 'updated_at'):
        rows[0][corruption] = None if corruption == 'updated_at' else 'wrong'
    else:
        engine.rows[imp.CALENDAR.name].clear()
    with pytest.raises(ValueError):
        with imp.read_source(engine,DAY,lambda:None):
            pytest.fail('Invalid snapshot exposed')


@pytest.mark.parametrize('kind',['engine','pk','pk_order','scale','string','unsigned','nullability','missing_table','missing_column','extra_column','wrong_type'])
def test_schema_fail_closed(source,monkeypatch,kind):
    engine,inspector = source
    if kind == 'engine':
        inspector.engine = 'MyISAM'
    elif kind == 'pk':
        inspector.pk = ['trade_date']
    elif kind == 'pk_order':
        inspector.pk = list(reversed(imp.DAILY.primary_key.columns.keys()))
    elif kind == 'scale':
        inspector.overrides['pe_ttm'] = Numeric(30,8)
    elif kind == 'string':
        inspector.overrides['source'] = String(49)
    elif kind == 'unsigned':
        from sqlalchemy.dialects.mysql import DECIMAL
        inspector.overrides['pe_ttm'] = DECIMAL(30,12,unsigned=True)
    elif kind == 'wrong_type':
        inspector.overrides['pe_ttm'] = String(30)
    elif kind == 'missing_table':
        monkeypatch.setattr(inspector,'get_table_names',lambda:[])
    else:
        original = inspector.get_columns
        def columns(name):
            result = original(name)
            if kind == 'missing_column':
                result.pop()
            elif kind == 'extra_column':
                result.append(dict(name='unexpected',type=String(10),nullable=True))
            else:
                result[0]['nullable'] = True
            return result
        monkeypatch.setattr(inspector,'get_columns',columns)
    with pytest.raises(imp.ImportError):
        imp.check_schema(engine)


def test_row_and_time_budget(source,monkeypatch):
    engine,_ = source
    monkeypatch.setattr(imp,'MAX_MEMBERS',299)
    with pytest.raises(imp.ImportError):
        with imp.read_source(engine,DAY,lambda:None):
            pytest.fail('Over-budget snapshot exposed')
    monkeypatch.setattr(imp,'MAX_SECONDS',-1)
    with pytest.raises(imp.ImportError):
        with imp.read_source(engine,DAY,lambda:None):
            pytest.fail('Over-time snapshot exposed')


def test_cancel_cleans_staging(source,tmp_path):
    engine,_ = source
    def cancel():
        raise RuntimeError('cancel')
    with pytest.raises(RuntimeError):
        with imp.read_source(engine,DAY,cancel,stage_dir=tmp_path):
            pytest.fail('Cancelled snapshot exposed')
    assert not list(tmp_path.iterdir())


def test_split_chunks_keep_day_boundaries(source,monkeypatch):
    engine,_ = source
    engine.dialect.server_version_info = (5, 7, 21)
    monkeypatch.setattr(imp,'CHUNK_SIZE',77)
    with imp.read_source(engine,DAY,lambda:None) as staged:
        assert len(list(staged.points())[0][1]) == 300
    assert engine.fetches == 6  # Calendar, summary and four primary-key pages.
    assert all(s._limit_clause.value <= 77 for s in engine.statements)


@pytest.mark.parametrize('reason', ['cancel', 'timeout'])
def test_stop_after_first_page_has_no_open_result_or_followup_read(source, monkeypatch, reason):
    engine, _ = source
    engine.dialect.server_version_info = (5, 7, 21)
    monkeypatch.setattr(imp, 'CHUNK_SIZE', 77)
    rows = imp._rows(engine, imp.MEMBER, imp.MAX_MEMBERS, lambda: None)
    next(rows)
    assert engine.results[-1].closed
    assert len(engine.statements) == 1
    rows.close()

    # Cancellation/deadline checks immediately after a bounded read must also
    # see an already closed cursor, without fetching a second page.
    engine.statements.clear()
    engine.results.clear()
    error = RuntimeError('cancel') if reason == 'cancel' else imp.ImportError('timeout')
    def stop():
        if engine.results:
            assert all(r.closed for r in engine.results)
            raise error
    with pytest.raises(type(error), match=str(error)):
        list(imp._rows(engine, imp.MEMBER, imp.MAX_MEMBERS, stop))
    assert len(engine.statements) == 1


def test_packed_pe_precision_and_null_preserved(source):
    engine, _ = source
    rows = engine.rows[imp.MEMBER.name]
    rows[0]['pe_ttm'] = None
    rows[1]['pe_ttm'] = Decimal('12345678901234567.123456789012')
    values = {r['code']: r['pe_ttm'] for r in rows}
    daily = validate_day(DAY, '4000', values, imp.SOURCE)
    daily.update(trade_date=DAY, updated_at=STAMP)
    engine.rows[imp.DAILY.name] = [daily]
    with imp.read_source(engine, DAY, lambda: None) as staged:
        point, members = list(staged.points())[0]
        assert members[rows[0]['code']] is None
        assert members[rows[1]['code']] == '12345678901234567.123456789012'
        assert point['missing_pe_count'] == 1


@pytest.mark.parametrize('kind', ['corrupt', 'null', 'count', 'shape', 'timestamp'])
def test_bad_packed_packet_fails_closed(source, monkeypatch, tmp_path, kind):
    engine, _ = source
    engine.dialect.server_version_info = (8, 0, 36)
    original = pack_day
    def damaged(day, rows):
        packet = original(day, rows)
        if kind == 'corrupt':
            packet['payload'] = b'not a zlib packet'
        elif kind == 'null':
            packet['payload'] = None
        elif kind == 'count':
            packet['members'] = 301
        else:
            values = json.loads(imp.decompress_json(packet['payload']))
            if kind == 'shape':
                values[0].pop()
            else:
                values[0][-1] = None
            raw = json.dumps(values).encode()
            packet['payload'] = struct.pack('<I', len(raw)) + zlib.compress(raw)
        return packet
    monkeypatch.setattr(__import__(__name__), 'pack_day', damaged)
    with pytest.raises(imp.ImportError):
        with imp.read_source(engine, DAY, lambda: None, stage_dir=tmp_path):
            pytest.fail('Damaged compressed history exposed')
    assert all(r.closed for r in engine.results)
    assert not list(tmp_path.iterdir())


def test_compressed_pages_use_date_index_and_keep_source_identity(source, monkeypatch):
    from datetime import timedelta
    from sqlalchemy.dialects import mysql
    engine, _ = source
    engine.dialect.server_version_info = (8, 0, 36)
    monkeypatch.setattr(imp, 'PACKED_DAYS', 2)
    rows = engine.rows[imp.MEMBER.name]
    engine.rows[imp.MEMBER.name] = [dict(r, trade_date=DAY + timedelta(days=i)) for i in range(3) for r in rows]
    restored = list(imp._member_rows(engine, lambda: None))
    assert len(restored) == 900
    assert {r['trade_date'] for r in restored} == {DAY + timedelta(days=i) for i in range(3)}
    assert len(engine.statements) == 4
    date_sql = str(engine.statements[2].compile(dialect=mysql.dialect()))
    assert 'WHERE valuation_ruler_member.trade_date >' in date_sql
    sql = str(engine.statements[3].compile(dialect=mysql.dialect()))
    assert 'WHERE valuation_ruler_member.trade_date IN' in sql
    assert 'GROUP BY valuation_ruler_member.trade_date ORDER BY valuation_ruler_member.trade_date' in sql
    assert 'CAST(valuation_ruler_member.pe_ttm AS CHAR)' in sql
    assert 'compress(json_arrayagg(json_array(' in sql
    assert all(s._limit_clause.value <= 2 for s in engine.statements)
    assert all(r.closed for r in engine.results)
    engine.statements.clear()
    selected = DAY + timedelta(days=2)
    assert {r['trade_date'] for r in imp._member_rows(engine, lambda: None, [selected])} == {selected}
    assert len(engine.statements) == 2


def test_network_failure_is_not_retried_as_uncompressed(source, monkeypatch):
    from sqlalchemy.exc import OperationalError
    engine, _ = source
    engine.dialect.server_version_info = (8, 0, 36)
    attempts = []
    def disconnect(statement):
        attempts.append(statement)
        raise OperationalError('query', {}, RuntimeError(2013))
    monkeypatch.setattr(engine, 'execute', disconnect)
    with pytest.raises(OperationalError):
        list(imp._member_rows(engine, lambda: None))
    assert len(attempts) == 1


def test_future_date_rejected(source):
    engine,_ = source
    with pytest.raises(imp.ImportError):
        with imp.read_source(engine,date(2026,11,9),lambda:None):
            pytest.fail('Future data exposed')


def test_weekend_calendar_rejected_before_exposure(source):
    engine,_ = source
    engine.rows[imp.CALENDAR.name].append(dict(trade_date=date(2026,11,8),source=imp.CALENDAR_SOURCE,updated_at=STAMP))
    with pytest.raises(imp.ImportError):
        with imp.read_source(engine,DAY,lambda:None):
            pytest.fail('Weekend calendar exposed')


@pytest.fixture
def actual_service(tmp_path):
    from app.services.valuation_ruler import ValuationRuler
    return ValuationRuler(SimpleNamespace(log_path=tmp_path,valuation_ruler_worker_enabled=False))


def moment():
    from app.services.valuation_ruler import CHINA
    return STAMP.replace(tzinfo=CHINA)


def seed(service,engine,close='5000',source=imp.SOURCE):
    members = {r['code']:r['pe_ttm'] for r in engine.rows[imp.MEMBER.name]}
    daily = validate_day(DAY,close,members,source)
    with service.db() as db:
        service.publish_day(db,daily,members,{'original':True})


def test_actual_roundtrip_history_quantile_state_and_lock(source,actual_service):
    engine,_ = source
    assert imp.start_import(actual_service,background=False,moment=moment(),engine_factory=lambda:engine)
    history = actual_service.history(moment())
    assert history['latest']['date'] == str(DAY)
    assert history['benchmarks']['sample_days'] == 1
    assert history['benchmarks']['p20'] == history['benchmarks']['p80'] == 12.25
    assert not history['coverage']['calendar_complete']
    assert actual_service.status(moment())['status'] == 'idle'
    assert actual_service.read_state()['phase'] == 'complete'
    assert actual_service.read_state()['imported_days'] == 1
    assert not actual_service.running()
    assert engine.disposed
    with actual_service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM members').fetchone()[0] == 300
        assert db.execute('SELECT COUNT(*) FROM proofs').fetchone()[0] == 1
    lock = actual_service.acquire()
    try:
        assert not imp.start_import(actual_service,background=False,moment=moment(),engine_factory=lambda:engine)
    finally:
        lock.release()


def test_actual_conflict_retains_full_audit_and_original(source,actual_service):
    engine,_ = source
    seed(actual_service,engine)
    imp.start_import(actual_service,background=False,moment=moment(),engine_factory=lambda:engine)
    assert actual_service.history(moment())['latest']['index_close'] == 5000
    assert actual_service.status(moment())['status'] == 'failed'
    assert actual_service.status(moment())['joinquant_conflicts'] == 1
    with actual_service.db() as db:
        audit = json.loads(db.execute('SELECT payload FROM comparisons').fetchone()[0])
        assert len(audit['candidate_members']) == 300
        assert audit['candidate_proof']['validated'] is True
        assert audit['differences']['index_close']['candidate'] == '4000.00000000'
        assert db.execute('SELECT COUNT(*) FROM points').fetchone()[0] == 1


def test_actual_old_gap_pending_after_later_day_success(source,actual_service):
    engine,_ = source
    old = date(2026,11,9)
    engine.rows[imp.CALENDAR.name].insert(0,dict(trade_date=old,source=imp.CALENDAR_SOURCE,updated_at=STAMP))
    imp.start_import(actual_service,background=False,moment=moment(),engine_factory=lambda:engine)
    assert actual_service.read_state()['joinquant_missing_days'] == 1
    assert actual_service.status(moment())['status'] == 'failed'
    with actual_service.db() as db:
        assert db.execute('SELECT trade_date FROM failures').fetchone()[0] == str(old)
        assert old in actual_service.pending(db,DAY)[0]
    assert str(old) in actual_service.history(moment())['gap_dates']


def test_actual_safe_schema_error_releases_lock(source,actual_service):
    engine,inspector = source
    inspector.engine = 'MyISAM'
    imp.start_import(actual_service,background=False,moment=moment(),engine_factory=lambda:engine)
    assert 'InnoDB' in actual_service.read_state()['error']
    assert actual_service.read_state()['phase'] == 'failed'
    assert not actual_service.running()
    assert engine.disposed


def test_actual_matching_cross_source_is_audited(source,actual_service):
    from app.services.valuation_ruler_model import PUBLIC_SOURCE
    engine,_ = source
    seed(actual_service,engine,'4000',PUBLIC_SOURCE)
    imp.start_import(actual_service,background=False,moment=moment(),engine_factory=lambda:engine)
    assert actual_service.status(moment())['status'] == 'idle'
    assert actual_service.read_state()['conflict_days'] == 0
    with actual_service.db() as db:
        audit = json.loads(db.execute('SELECT payload FROM comparisons').fetchone()[0])
        assert audit['match'] is True
        assert len(audit['candidate_members']) == 300
        assert json.loads(db.execute('SELECT payload FROM points').fetchone()[0])['source'] == PUBLIC_SOURCE


@pytest.mark.parametrize('existing',[True,False])
def test_actual_cancel_rollback_including_audit(source,actual_service,existing):
    engine,_ = source
    if existing:
        seed(actual_service,engine)
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('cancel')
    with imp.read_source(engine,DAY,lambda:None) as staged:
        with pytest.raises(RuntimeError):
            imp.publish(actual_service,{},STAMP,staged,cancel)
    with actual_service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM comparisons').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM points').fetchone()[0] == int(existing)
        assert db.execute('SELECT COUNT(*) FROM calendar').fetchone()[0] == int(existing)


def test_actual_state_failure_rolls_back_all(source,actual_service,monkeypatch):
    engine,_ = source
    def fail(*args):
        raise RuntimeError('state failure')
    monkeypatch.setattr(actual_service,'write_state',fail)
    state = {}
    with imp.read_source(engine,DAY,lambda:None) as staged:
        with pytest.raises(RuntimeError):
            imp.publish(actual_service,state,STAMP,staged,lambda:None)
    assert state == {}
    with actual_service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM points').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM calendar').fetchone()[0] == 0
