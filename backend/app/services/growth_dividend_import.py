"""Read-only validated JoinQuant snapshot, append-only local publication."""
from datetime import date, datetime
from decimal import Decimal
import math
import threading
import time
import uuid
from sqlalchemy import MetaData, Table, Column, String, Date, DateTime, Float, inspect, select
from app.core.database import build_engine
from app.services.growth_dividend import now, close_cutoff, CollectionCancelled

CODES = ('399006.XSHE', '000922.XSHG', '000688.XSHG')
SOURCE = 'JQDATA.GET_PRICE.INDEX_CLOSE.V1'
CALENDAR_SOURCE = 'JQDATA.GET_TRADE_DAYS'
metadata = MetaData()
PRICES = Table('growth_dividend_index_history', metadata,
    Column('index_code', String(16), primary_key=True), Column('trade_date', Date, primary_key=True),
    Column('close', Float(53), nullable=False), Column('source', String(48), nullable=False),
    Column('updated_at', DateTime, nullable=False), mysql_engine='InnoDB')
CALENDAR = Table('growth_dividend_trading_day', metadata,
    Column('trade_date', Date, primary_key=True), Column('source', String(48), nullable=False),
    Column('updated_at', DateTime, nullable=False), mysql_engine='InnoDB')


def check_schema(connection):
    inspector = inspect(connection)
    for table in (PRICES, CALENDAR):
        columns = {c['name']: c for c in inspector.get_columns(table.name)}
        if set(columns) != set(c.name for c in table.columns):
            raise ValueError('源表字段不兼容')
        if set(inspector.get_pk_constraint(table.name)['constrained_columns']) != set(c.name for c in table.primary_key):
            raise ValueError('源表主键不兼容')
        for col in table.columns:
            actual = columns[col.name]
            typ = actual['type']
            if getattr(typ, 'unsigned', False):
                raise ValueError('源表数值类型不得为无符号')
            if actual['nullable'] or typ._type_affinity is not col.type._type_affinity:
                raise ValueError('源表类型或非空约束不兼容')
            if isinstance(col.type, String) and typ.length != col.type.length:
                raise ValueError('源表字符串长度不兼容')
            if isinstance(col.type, Float) and connection.dialect.name == 'mysql' and type(typ).__name__.upper() not in ('DOUBLE', 'DOUBLE_PRECISION'):
                raise ValueError('收盘点位必须为双精度')
        if connection.dialect.name == 'mysql' and str(inspector.get_table_options(table.name).get('mysql_engine', '')).lower() != 'innodb':
            raise ValueError('源表必须为 InnoDB')


MAX_DAYS = 20000

def read_source(engine, cutoff, check):
    engine.echo, engine.hide_parameters = False, True
    with engine.connect() as conn:
        if conn.dialect.name == 'mysql':
            conn = conn.execution_options(isolation_level='REPEATABLE READ')
        with conn.begin():
            if conn.dialect.name == 'mysql':
                conn.exec_driver_sql('SET TRANSACTION READ ONLY')
                conn.exec_driver_sql('START TRANSACTION WITH CONSISTENT SNAPSHOT')
            check_schema(conn)
            days = []
            for row in conn.execute(select(CALENDAR).order_by(CALENDAR.c.trade_date).limit(MAX_DAYS + 1)).mappings():
                check()
                day = row['trade_date']
                if (type(day) is not date or day > cutoff or day.weekday() >= 5 or
                    row['source'] != CALENDAR_SOURCE or not isinstance(row['updated_at'], datetime)):
                    raise ValueError('源交易日历无效')
                days.append(day)
            if not days or len(days) > MAX_DAYS or len(set(days)) != len(days):
                raise ValueError('源交易日历为空、重复或超限')
            calendar = set(days)
            points, seen = [], set()
            for row in conn.execute(select(PRICES).limit(MAX_DAYS * 3 + 1)).mappings():
                check()
                day, code, value = row['trade_date'], row['index_code'], row['close']
                if (type(day) is not date or day not in calendar or code not in CODES or
                    row['source'] != SOURCE or not isinstance(row['updated_at'], datetime) or
                    isinstance(value, (bool, str)) or value is None):
                    raise ValueError('源指数身份、日期或来源无效')
                number = float(value)
                if not math.isfinite(number) or number <= 0 or (isinstance(value, Decimal) and Decimal(str(number)) != value):
                    raise ValueError('源点位无效或精度不可保留')
                key = code, day
                if key in seen or len(points) >= MAX_DAYS * 3:
                    raise ValueError('源行情重复或超限')
                seen.add(key)
                points.append((code.split('.')[0], str(day), number, '聚宽（当前平台可见历史）',
                    'https://www.joinquant.com/help/api/help#name:get_price', row['updated_at'].isoformat()))
    if not points:
        raise ValueError('源行情为空')
    return days, points


def publish(monitor, days, points, check):
    """Validate conflicts before any insert; one SQLite transaction publishes all rows."""
    inserted = existing = conflicts = 0
    with monitor.db() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in points:
            check()
            old = db.execute('SELECT close FROM points WHERE code=? AND trade_date=?', row[:2]).fetchone()
            if old:
                existing += 1
                conflicts += old[0] != row[2]
            else:
                db.execute('INSERT INTO points VALUES (?,?,?,?,?,?)', row)
                inserted += 1
        check()
        db.executemany('INSERT OR IGNORE INTO calendar VALUES (?)', [(str(d),) for d in days])
    indices = {}
    for code in CODES:
        observed = [r[1] for r in points if r[0] == code.split('.')[0]]
        indices[code] = dict(observed_first_day=min(observed) if observed else None,
                            available=len(observed), missing=len(days)-len(observed), earlier_missing='unknown')
    return dict(inserted=inserted, existing=existing, conflicts=conflicts, indices=indices,
                source=SOURCE, first_day=str(days[0]), last_day=str(days[-1]),
                coverage='已导入可见记录；空缺不代表指数不存在，不保证完整最早历史')


def start_import(monitor, background=True, engine_factory=None):
    if monitor.stop_event.is_set():
        return False
    lock = monitor.acquire()
    if lock is None:
        return False
    try:
        state = monitor.read_state()
    except BaseException:
        lock.release()
        raise
    state.update(run_id=uuid.uuid4().hex, active=True, stage='校验聚宽历史快照',
                 error=None, processed=0, total=None, percent=None,
                 last_attempt_at=now().isoformat(), trigger='joinquant')
    started = time.monotonic()
    marker = monitor.root / ('cancel-' + state['run_id'])
    def check():
        if time.monotonic() - started > 1800:
            raise ValueError('同步超过安全时间上限')
        if monitor.stop_event.is_set() or marker.exists():
            raise CollectionCancelled()
    def work():
        engine = None
        try:
            check()
            engine = (engine_factory or (lambda: build_engine(monitor.settings)))()
            days, points = read_source(engine, close_cutoff(now()), check)
            summary = publish(monitor, days, points, check)
            state.update(sync_summary=summary, stage='聚宽历史同步完成；保留已有值，冲突未覆盖',
                processed=len(points), total=len(points), percent=100, last_success_at=now().isoformat())
        except CollectionCancelled:
            state.update(stage='聚宽同步已停止，未发布的快照已丢弃')
        except Exception:
            state.update(stage='聚宽同步失败', error='源库读取或校验失败，已有数据保留。请检查采集表和数据库配置。')
        finally:
            try:
                if engine is not None:
                    engine.dispose()
                state['active'] = False
                monitor.write_state(state)
                marker.unlink(missing_ok=True)
            finally:
                lock.release()
    try:
        monitor.write_state(state)
        if background:
            monitor.worker = threading.Thread(target=work, name='growth-dividend-import', daemon=True)
            monitor.worker.start()
        else:
            work()
    except BaseException:
        lock.release()
        raise
    return True
