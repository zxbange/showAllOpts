"""Public index prices in the existing MySQL tables and local history."""
from datetime import date, datetime, timezone
from decimal import Decimal
import math
from sqlalchemy import MetaData, Table, Column, String, Date, DateTime, Float, inspect, select, bindparam
from sqlalchemy.dialects.mysql import insert as mysql_insert
from app.core.cache_errors import SnapshotDataError
from app.services.growth_dividend_source import INDEXES, START

CODES = ('399006.XSHE', '000922.XSHG', '000688.XSHG')
LEGACY_SOURCE = 'JQDATA.GET_PRICE.INDEX_CLOSE.V1'
SOURCES = {'腾讯财经': 'PUBLIC.TENCENT.INDEX_CLOSE.V1', '东方财富': 'PUBLIC.EASTMONEY.INDEX_CLOSE.V1'}
SOURCE = SOURCES['腾讯财经']
CALENDAR_SOURCE = 'PUBLIC.SINA.TRADE_DAYS'
LEGACY_CALENDAR_SOURCE = 'JQDATA.GET_TRADE_DAYS'
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
            for row in conn.execute(select(CALENDAR).where(CALENDAR.c.trade_date <= cutoff).order_by(CALENDAR.c.trade_date).limit(MAX_DAYS + 1)).mappings():
                check()
                day = row['trade_date']
                if (type(day) is not date or day > cutoff or day.weekday() >= 5 or
                    row['source'] not in (CALENDAR_SOURCE, LEGACY_CALENDAR_SOURCE) or not isinstance(row['updated_at'], datetime)):
                    raise ValueError('源交易日历无效')
                days.append(day)
            if not days or len(days) > MAX_DAYS or len(set(days)) != len(days):
                raise ValueError('源交易日历为空、重复或超限')
            calendar = set(days)
            points, seen = [], set()
            for row in conn.execute(select(PRICES).where(PRICES.c.source.in_(tuple(SOURCES.values())),
                    PRICES.c.trade_date <= cutoff).limit(MAX_DAYS * 3 + 1)).mappings():
                check()
                day, code, value = row['trade_date'], row['index_code'], row['close']
                if (type(day) is not date or day not in calendar or code not in CODES or
                    row['source'] not in SOURCES.values() or not isinstance(row['updated_at'], datetime) or
                    isinstance(value, (bool, str)) or value is None):
                    raise ValueError('源指数身份、日期或来源无效')
                number = float(value)
                if not math.isfinite(number) or number <= 0 or (isinstance(value, Decimal) and Decimal(str(number)) != value):
                    raise ValueError('源点位无效或精度不可保留')
                key = code, day
                if key in seen or len(points) >= MAX_DAYS * 3:
                    raise ValueError('源行情重复或超限')
                seen.add(key)
                points.append((code.split('.')[0], str(day), number,
                    next(name for name, key in SOURCES.items() if key == row['source']),
                    'https://finance.qq.com/' if row['source'] == SOURCE else 'https://quote.eastmoney.com/',
                    row['updated_at'].replace(tzinfo=timezone.utc).isoformat()))
    if not points:
        raise SnapshotDataError('创红比源库尚无外部行情，请在页面更新；旧聚宽记录不再用于展示。')
    return days, points


def apply_points(db, points, check):
    inserted = updated = existing = 0
    for row in points:
        check()
        if row[3] not in SOURCES:
            raise ValueError('只发布已验证的外部行情')
        old = db.execute('SELECT close,source,updated_at FROM points WHERE code=? AND trade_date=?', row[:2]).fetchone()
        if old and old[1] in SOURCES and datetime.fromisoformat(old[2]) > datetime.fromisoformat(row[5]):
            existing += 1
            continue
        if old and old[0] != row[2]:
            db.execute('INSERT OR IGNORE INTO comparisons VALUES (?,?,?,?,?)', (*row[:2], old[0], row[2], row[5]))
        if old is None:
            inserted += 1
        elif old[0] != row[2] or old[1] != row[3]:
            updated += 1
        else:
            existing += 1
        db.execute('INSERT OR REPLACE INTO points VALUES (?,?,?,?,?,?)', row)
    return dict(inserted=inserted, updated=updated, existing=existing, conflicts=0)


def publish(monitor, days, points, check):
    with monitor.db() as db:
        db.execute('BEGIN IMMEDIATE')
        summary = apply_points(db, points, check)
        db.executemany('INSERT OR IGNORE INTO calendar VALUES (?)', [(str(d),) for d in days])
        check()
    indices = {}
    for code in CODES:
        first_day = next(index.first_date for index in INDEXES if index.code == code.split('.')[0])
        expected = {str(day) for day in days if day >= max(START, first_day)}
        observed = [r[1] for r in points if r[0] == code.split('.')[0]]
        indices[code] = dict(observed_first_day=min(observed) if observed else None,
                            available=len(observed), missing=len(expected - set(observed)), earlier_missing='unknown')
    return dict(summary, indices=indices, source='public', first_day=str(days[0]), last_day=str(days[-1]))


def write_public(engine, payload, check):
    """One bounded transaction; never roll a newer public observation backwards."""
    points = payload['points']
    if not points:
        return payload
    records = []
    for code, day, close, source, url, timestamp in points:
        stamp = datetime.fromisoformat(timestamp).astimezone(timezone.utc).replace(tzinfo=None)
        if source not in SOURCES or isinstance(close, bool) or not math.isfinite(close) or not 0 < close < 10000000:
            raise ValueError('公开指数行情无效')
        records.append(dict(index_code=next(c for c in CODES if c.startswith(code + '.')),
                            trade_date=date.fromisoformat(day), close=close, source=SOURCES[source], updated_at=stamp))
    with engine.begin() as conn:
        metadata.create_all(conn)
        check_schema(conn)
        days = sorted({r['trade_date'] for r in records})
        codes = sorted({r['index_code'] for r in records})
        existing = {(r['index_code'], r['trade_date']): r for r in conn.execute(
            select(PRICES).where(PRICES.c.index_code.in_(codes), PRICES.c.trade_date.in_(days))
            .order_by(PRICES.c.index_code, PRICES.c.trade_date).with_for_update()).mappings()}
        calendar = set(conn.execute(select(CALENDAR.c.trade_date).where(CALENDAR.c.trade_date.in_(days))).scalars())
        missing_days = [dict(trade_date=d, source=CALENDAR_SOURCE, updated_at=records[0]['updated_at'])
                        for d in days if d not in calendar]
        if missing_days:
            conn.execute(CALENDAR.insert(), missing_days)
        canonical, inserts, updates = [], [], []
        for record, row in zip(records, points):
            check()
            key = record['index_code'], record['trade_date']
            old = existing.get(key)
            if old and old['source'] in SOURCES.values() and old['updated_at'] > record['updated_at']:
                if not math.isfinite(old['close']) or not 0 < old['close'] < 10000000:
                    raise ValueError('数据库中的外部指数行情无效')
                canonical.append((row[0], row[1], old['close'],
                    next(n for n, s in SOURCES.items() if s == old['source']), '',
                    old['updated_at'].replace(tzinfo=timezone.utc).isoformat()))
                continue
            if old:
                if any(old[k] != record[k] for k in ('close', 'source', 'updated_at')):
                    updates.append(record)
            else:
                inserts.append(record)
            canonical.append(row)
        if conn.dialect.name == 'mysql' and (inserts or updates):
            statement = mysql_insert(PRICES).values(inserts + updates)
            conn.execute(statement.on_duplicate_key_update(**{
                key: statement.inserted[key] for key in ('close', 'source', 'updated_at')}))
        elif inserts:
            conn.execute(PRICES.insert(), inserts)
        # MySQL uses one multi-row upsert; SQLite supports the offline test path.
        if updates and conn.dialect.name != 'mysql':
            statement = PRICES.update().where(PRICES.c.index_code == bindparam('key_code'),
                PRICES.c.trade_date == bindparam('key_day')).values(close=bindparam('new_close'),
                source=bindparam('new_source'), updated_at=bindparam('new_time'))
            conn.execute(statement, [dict(key_code=r['index_code'], key_day=r['trade_date'],
                new_close=r['close'], new_source=r['source'], new_time=r['updated_at']) for r in updates])
        check()
    return dict(payload, points=canonical)
