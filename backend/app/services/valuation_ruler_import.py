"""Read-only consistent MySQL snapshot, disk staging, atomic append-only publication."""
from contextlib import contextmanager
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import time

from sqlalchemy import Column, Date, DateTime, Integer, MetaData, Numeric, String, Table, inspect, select
from app.core.database import build_engine
from app.services.valuation_ruler_model import ValuationError, ValuationConflict, validate_day

METHOD = 'csi300_equal_positive_pe_ttm_v1'
INDEX = '000300.XSHG'
SOURCE = 'JQ_PLATFORM.PE_TTM.V1'
CALENDAR_SOURCE = 'JQDATA.GET_TRADE_DAYS'
CHUNK_SIZE = 1000
MAX_DAYS = 20000
MAX_MEMBERS = MAX_DAYS * 300
MAX_SECONDS = 1800
metadata = MetaData()


def _keys():
    return [Column('trade_date', Date, primary_key=True), Column('index_code', String(16), primary_key=True),
            Column('method_version', String(48), primary_key=True)]


DAILY = Table('valuation_ruler_daily', metadata, *_keys(),
    Column('index_close', Numeric(24, 8), nullable=False), Column('mean_pe_ttm', Numeric(30, 12), nullable=False),
    Column('pe_sum', Numeric(38, 12), nullable=False), Column('member_count', Integer, nullable=False),
    Column('positive_count', Integer, nullable=False), Column('loss_count', Integer, nullable=False),
    Column('members_sha256', String(64), nullable=False), Column('source', String(48), nullable=False),
    Column('updated_at', DateTime, nullable=False))
MEMBER = Table('valuation_ruler_member', metadata, *_keys(), Column('code', String(16), primary_key=True),
    Column('pe_ttm', Numeric(30, 12), nullable=True), Column('source', String(48), nullable=False),
    Column('updated_at', DateTime, nullable=False))
CALENDAR = Table('valuation_ruler_trading_day', metadata, Column('trade_date', Date, primary_key=True),
    Column('source', String(48), nullable=False), Column('updated_at', DateTime, nullable=False))


class ImportError(ValuationError):
    """Safe fixed error descriptions; never expose connection or source values."""


def check_schema(connection):
    inspector = inspect(connection)
    if not {t.name for t in (DAILY, MEMBER, CALENDAR)}.issubset(inspector.get_table_names()):
        raise ImportError('源库缺少估值标尺表，请先运行聚宽历史采集脚本。')
    for table in (DAILY, MEMBER, CALENDAR):
        columns = {c['name']: c for c in inspector.get_columns(table.name)}
        incompatible = (set(columns) != {c.name for c in table.columns} or
                        list(inspector.get_pk_constraint(table.name).get('constrained_columns') or []) != [c.name for c in table.primary_key])
        for expected in table.columns:
            actual = columns.get(expected.name)
            if actual is None:
                incompatible = True
                continue
            typ = actual['type']
            # Both old NOT NULL and upgraded nullable PE columns are readable.
            incompatible |= typ._type_affinity is not expected.type._type_affinity or (
                actual.get('nullable', True) and not expected.nullable)
            incompatible |= bool(getattr(typ, 'unsigned', False))
            if isinstance(expected.type, Numeric):
                incompatible |= (getattr(typ, 'precision', None), getattr(typ, 'scale', None)) != (expected.type.precision, expected.type.scale)
            if isinstance(expected.type, String):
                incompatible |= getattr(typ, 'length', None) != expected.type.length
            if expected.type._type_affinity is Integer:
                incompatible |= type(typ).__name__.lower() in ('tinyint', 'smallint', 'mediumint')
        if incompatible:
            raise ImportError(table.name + ' 字段类型、精度、非空约束或主键不兼容。')
        if connection.dialect.name == 'mysql':
            engine = inspector.get_table_options(table.name).get('mysql_engine', '')
            if isinstance(engine, bytes):
                engine = engine.decode('ascii', errors='replace')
            if not isinstance(engine, str) or engine.strip().lower() != 'innodb':
                raise ImportError(table.name + ' 必须使用 InnoDB 一致快照。')


def _rows(connection, table, limit, check):
    statement = select(table).order_by(*table.primary_key.columns).limit(limit + 1)
    result = connection.execution_options(stream_results=True, max_row_buffer=CHUNK_SIZE).execute(statement).mappings()
    count = 0
    try:
        while True:
            check()
            chunk = result.fetchmany(CHUNK_SIZE)
            if not chunk:
                return
            count += len(chunk)
            if count > limit:
                raise ImportError('估值标尺源数据超出安全导入上限，未发布。')
            yield from chunk
    finally:
        result.close()


def _identity(row, cutoff, calendar=False):
    day = row['trade_date']
    if (type(day) is not date or not date(2010, 1, 1) <= day <= cutoff or day.weekday() >= 5
            or not isinstance(row['updated_at'], datetime)
            or row['source'] != (CALENDAR_SOURCE if calendar else SOURCE)):
        raise ImportError('估值标尺源日期、来源或更新时间无效。')
    if not calendar and (row['index_code'] != INDEX or row['method_version'] != METHOD):
        raise ImportError('估值标尺指数或计算口径不兼容。')
    return str(day)


class StagedSource:
    def __init__(self, db):
        self.db = db

    def days(self):
        return [date.fromisoformat(r[0]) for r in self.db.execute('SELECT day FROM calendar ORDER BY day')]

    def points(self):
        for daily, members in self.db.execute('SELECT daily,members FROM validated ORDER BY day'):
            yield json.loads(daily), json.loads(members)


@contextmanager
def read_source(engine, cutoff, check_cancel, *, stage_dir=None):
    """Only expose completely validated snapshots, with bounded memory and cleanup."""
    started = time.monotonic()
    def check():
        check_cancel()
        if time.monotonic() - started > MAX_SECONDS:
            raise ImportError('估值标尺导入超过安全时间上限，未发布。')
    engine.echo, engine.hide_parameters = False, True
    with tempfile.TemporaryDirectory(prefix='valuation-import-', dir=stage_dir) as folder:
        db = sqlite3.connect(str(Path(folder) / 'stage.sqlite3'))
        try:
            db.executescript('CREATE TABLE calendar(day TEXT PRIMARY KEY); CREATE TABLE raw(day TEXT PRIMARY KEY,payload TEXT); CREATE TABLE validated(day TEXT PRIMARY KEY,daily TEXT,members TEXT);')
            with engine.connect() as connection:
                if connection.dialect.name == 'mysql':
                    connection = connection.execution_options(isolation_level='REPEATABLE READ')
                with connection.begin():
                    if connection.dialect.name == 'mysql':
                        connection.exec_driver_sql('SET TRANSACTION READ ONLY')
                        connection.exec_driver_sql('START TRANSACTION WITH CONSISTENT SNAPSHOT')
                    check_schema(connection)
                    for row in _rows(connection, CALENDAR, MAX_DAYS, check):
                        day = _identity(row, cutoff, True)
                        db.execute('INSERT INTO calendar VALUES (?)', (day,))
                    for row in _rows(connection, DAILY, MAX_DAYS, check):
                        day = _identity(row, cutoff)
                        if not db.execute('SELECT 1 FROM calendar WHERE day=?', (day,)).fetchone():
                            raise ImportError('估值标尺日线日期不在源交易日历中。')
                        db.execute('INSERT INTO raw VALUES (?,?)', (day, json.dumps(dict(row), default=str)))
                    current, members = None, {}
                    def finish():
                        if current is None:
                            return
                        check()
                        raw = db.execute('SELECT payload FROM raw WHERE day=?', (current,)).fetchone()
                        if raw is None:
                            raise ImportError('估值标尺成分股缺少对应日线。')
                        expected = json.loads(raw[0])
                        daily = validate_day(date.fromisoformat(current), expected['index_close'], members, SOURCE, expected=expected)
                        db.execute('INSERT INTO validated VALUES (?,?,?)', (current, json.dumps(daily, default=str), json.dumps(members, default=str)))
                    for row in _rows(connection, MEMBER, MAX_MEMBERS, check):
                        day = _identity(row, cutoff)
                        if day != current:
                            finish()
                            current, members = day, {}
                        code = row['code']
                        if code in members or len(members) >= 300:
                            raise ImportError('估值标尺成分股重复或超过300只。')
                        members[code] = row['pe_ttm']
                    finish()
                    if not db.execute('SELECT 1 FROM validated LIMIT 1').fetchone():
                        raise ImportError('源库尚无有效估值标尺历史。')
                    if db.execute('SELECT 1 FROM raw WHERE day NOT IN (SELECT day FROM validated) LIMIT 1').fetchone():
                        raise ImportError('估值标尺日线缺少完整成分股。')
                    check()
            db.commit()
            yield StagedSource(db)
        finally:
            db.close()


def publish(service, state, moment, staged, check_cancel):
    added = conflicts = total = 0
    next_state = dict(state)
    with service.db() as db:
        db.execute('BEGIN IMMEDIATE')
        source_days = staged.days()
        service.save_calendar(db, source_days, CALENDAR_SOURCE)
        for daily, members in staged.points():
            check_cancel()
            total += 1
            try:
                added += bool(service.publish_day(db, daily, members, {'source': SOURCE, 'validated': True}))
            except ValuationConflict:
                # publish_day has already recorded the full comparison in this transaction.
                conflicts += 1
        missing = 0
        for day in source_days:
            check_cancel()
            if not db.execute('SELECT 1 FROM points WHERE trade_date=?', (str(day),)).fetchone():
                missing += 1
                db.execute('INSERT OR IGNORE INTO failures VALUES (?,?,?,?,?)',
                    (str(day), 'joinquant_missing', '聚宽交易日历已有该日，但尚缺完整估值数据。', 0, moment.isoformat()))
        check_cancel()
        error = ('聚宽交易日历中仍有缺失日期，已保留缺口以供重试。' if missing else
                 '对照发现来源差异，已有数据未覆盖，请查看对照结果。' if conflicts else None)
        next_state.update(completed_days=total, imported_days=added, conflict_days=conflicts,
            processed=total, total=total, percent=100, error=error, joinquant_missing_days=missing,
            stage='聚宽历史同步完成，新增 {} 天；冲突 {} 天已保留原数据'.format(added, conflicts),
            joinquant_import_at=moment.isoformat(), joinquant_imported=added, joinquant_conflicts=conflicts)
        service.write_state(next_state, db)
    state.update(next_state)


def start_import(service, *, background=True, moment=None, engine_factory=None):
    def runner(monitor, state, check_cancel, moment):
        from app.services.valuation_ruler import close_cutoff
        check_cancel()
        engine = (engine_factory or (lambda: build_engine(monitor.settings, for_cache=True)))()
        try:
            with read_source(engine, close_cutoff(moment), check_cancel, stage_dir=monitor.root) as staged:
                publish(monitor, state, moment, staged, check_cancel)
        finally:
            engine.dispose()
    return service.start_job('joinquant', runner, background=background, moment=moment)
