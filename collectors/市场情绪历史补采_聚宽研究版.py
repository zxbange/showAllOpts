# -*- coding: utf-8 -*-
"""聚宽平台独立历史补采。仅写入缺口，不覆盖已存在结果。历史值为平台当前可见修订版本。"""
from __future__ import print_function, division
import datetime as dt
import hashlib
import re
from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_EVEN
import jqdata as jq
from sqlalchemy import Column, Date, DateTime, Integer, Numeric, String, inspect
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

WRITE_DATABASE = True
MAX_DAYS = 20
END_DATE = None
Base = declarative_base()

class HistoryError(ValueError):
    """Fixed credential-free source/schema diagnostics."""
    pass


def china_now():
    return dt.datetime.utcnow() + dt.timedelta(hours=8)


def as_date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if not isinstance(value, str) or not re.match(r'^\d{4}-\d{2}-\d{2}$', value):
        raise HistoryError('日期必须为 YYYY-MM-DD。')
    return dt.datetime.strptime(value, '%Y-%m-%d').date()


def number(value, precision, scale):
    if isinstance(value, bool) or value is None:
        raise HistoryError('源数值缺失或类型错误。')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HistoryError('源数值无效。') from None
    with localcontext() as ctx:
        ctx.prec = 80
        if (not result.is_finite() or result.copy_abs() >= Decimal(10) ** (precision-scale)
                or result != result.quantize(Decimal(10) ** -scale)):
            raise HistoryError('源数值非有限、越界或小数精度超限；不会静默舍入。')
    return result


def derived(value, scale):
    with localcontext() as ctx:
        ctx.prec = 80
        return value.quantize(Decimal(10) ** -scale, rounding=ROUND_HALF_EVEN)


def calendar(start, end):
    # Independent full-calendar crosscheck detects truncated range responses.
    full = [as_date(x) for x in jq.get_all_trade_days()]
    if not full or full != sorted(set(full)):
        raise HistoryError('平台完整交易日历无效。')
    expected = [d for d in full if (start is None or d >= start) and d <= end]
    if not expected:
        raise HistoryError('区间没有平台交易日。')
    actual = [as_date(x) for x in jq.get_trade_days(start_date=expected[0], end_date=end)]
    if actual != expected:
        raise HistoryError('交易日历区间覆盖不完整或乱序。')
    return expected


def range_days(start, end, max_days):
    if max_days is not None and (isinstance(max_days, bool) or not isinstance(max_days, int) or not 1 <= max_days <= 250):
        raise HistoryError('max_days 必须为 None（本次全区间）或 1 至 250 的整数。')
    now = china_now()
    closed = now.date() if (now.hour, now.minute) >= (18, 30) else now.date()-dt.timedelta(days=1)
    end = closed if end is None else as_date(end)
    if end > closed:
        raise HistoryError('结束日期尚未在北京时间18:30收盘确认。')
    start = None if start is None else as_date(start)
    if start is not None and start > end:
        raise HistoryError('开始日期晚于结束日期。')
    return calendar(start, end)


def validate_schema(engine, models):
    inspector = inspect(engine)
    for model in models:
        table = model.__table__
        cols = {c['name']: c for c in inspector.get_columns(table.name)}
        if set(cols) != set(c.name for c in table.columns):
            raise HistoryError('数据库字段集合不一致。')
        if inspector.get_pk_constraint(table.name)['constrained_columns'] != [c.name for c in table.primary_key]:
            raise HistoryError('数据库主键与采集协议不一致。')
        if engine.dialect.name == 'mysql' and inspector.get_table_options(table.name).get('mysql_engine', '').lower() != 'innodb':
            raise HistoryError('历史表必须使用 InnoDB。')
        for col in table.columns:
            actual = cols.get(col.name)
            if actual is None or actual['nullable'] != col.nullable:
                raise HistoryError('数据库字段或可空约束不一致。')
            typ = actual['type']
            if getattr(typ, 'unsigned', False):
                raise HistoryError('数据库数值字段不得使用无符号类型。')
            if isinstance(col.type, Numeric):
                if not isinstance(typ, Numeric) or (typ.precision, typ.scale) != (col.type.precision, col.type.scale):
                    raise HistoryError('数据库数值精度不一致。')
            elif isinstance(col.type, String):
                if not isinstance(typ, String) or typ.length != col.type.length:
                    raise HistoryError('数据库文本类型不一致。')
            elif col.type._type_affinity is not typ._type_affinity:
                raise HistoryError('数据库字段类型不一致。')


def database(models, module):
    # Configuration import and credentials are deliberately deferred until write mode.
    from mysql_connector import create_mysql_engine
    engine = create_mysql_engine()
    engine.echo = False
    if hasattr(engine, 'hide_parameters'):
        engine.hide_parameters = True
    try:
        Base.metadata.create_all(engine)
        validate_schema(engine, models)
        Session = sessionmaker(bind=engine)
        _install_result_journal(Session, tuple(m for m in models if m.__tablename__ != 'market_crowding'), module)
        return engine, Session
    except HistoryError:
        engine.dispose()
        raise
    except Exception:
        engine.dispose()
        raise RuntimeError('数据库连接或表结构校验失败；未写入历史结果。') from None


def complete(row, model):
    for col in model.__table__.columns:
        value = getattr(row, col.name)
        if not col.nullable and value is None:
            raise HistoryError('已有记录不完整，需人工核实；不会覆盖。')
        if isinstance(col.type, Numeric):
            number(value, col.type.precision, col.type.scale)
    return True


# BEGIN SHARED RESULT JOURNAL
import json as _journal_json
import uuid as _journal_uuid
from sqlalchemy import BigInteger, Column, Integer, MetaData, String, Table, Text
from sqlalchemy import event as _journal_event, inspect as _journal_inspect
from sqlalchemy.exc import IntegrityError as _JournalIntegrityError
from sqlalchemy.dialects.mysql import LONGTEXT as _JournalLongText
from sqlalchemy.schema import CreateTable as _JournalCreateTable
from sqlalchemy import text as _journal_text

def _result_journal_tables():
    meta = MetaData()
    head = Table('result_sync_head', meta,
                 Column('module', String(48), primary_key=True),
                 Column('protocol', Integer, nullable=False),
                 Column('generation', String(32), nullable=False),
                 Column('revision', BigInteger, nullable=False))
    changes = Table('result_sync_change', meta,
                    Column('module', String(48), primary_key=True),
                    Column('revision', BigInteger, primary_key=True),
                    Column('payload', Text().with_variant(_JournalLongText(), 'mysql'), nullable=False))
    for table in meta.tables.values():
        table.dialect_options['mysql']['engine'] = 'InnoDB'
        table.dialect_options['mysql']['charset'] = 'utf8mb4'
    return meta, head, changes


def _install_result_journal(Session, models, module):
    """Journal every ORM write and explicitly wrapped bulk DML in the same transaction."""
    if getattr(Session, '_result_journal_module', None) == module:
        return
    meta, head, changes = _result_journal_tables()
    engine = Session.kw['bind']
    # Compile explicitly for SQLAlchemy versions predating CreateTable(if_not_exists=True).
    # Database-side IF NOT EXISTS avoids the check-then-create race across collectors.
    with engine.begin() as connection:
        for table in meta.sorted_tables:
            ddl = str(_JournalCreateTable(table).compile(dialect=engine.dialect))
            connection.execute(_journal_text(ddl.replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS', 1)))
    # Initialization races are harmless; never reset an existing generation/revision.
    with engine.begin() as connection:
        exists = connection.execute(head.select().where(head.c.module == module)).first()
    if exists is None:
        try:
            with engine.begin() as connection:
                connection.execute(head.insert().values(module=module, protocol=1,
                                   generation=_journal_uuid.uuid4().hex, revision=0))
        except _JournalIntegrityError:
            with engine.begin() as connection:
                if connection.execute(head.select().where(head.c.module == module)).first() is None:
                    raise
    tracked = tuple(models)
    info_key = 'result_journal:' + module

    def lock(session):
        info = session.info.get(info_key)
        if info is None:
            with session.no_autoflush:
                row = session.execute(head.select().where(head.c.module == module).with_for_update()).first()
            if row is None or row.protocol != 1:
                raise ValueError('结果同步日志版本无效，请检查采集版本。')
            info = {'revision': row.revision, 'records': {}, 'written': False}
            session.info[info_key] = info
        return info

    def record(session, table, keys, deleted):
        if not keys:
            return
        info = lock(session)
        for key in keys:
            values = [value.isoformat() if hasattr(value, 'isoformat') else value for value in key]
            identity = (table.name, _journal_json.dumps(values, ensure_ascii=True, separators=(',', ':')))
            info['records'][identity] = {'table': table.name, 'key': values, 'deleted': bool(deleted)}
        payload = _journal_json.dumps(list(info['records'].values()), ensure_ascii=True, separators=(',', ':'))
        with session.no_autoflush:
            if not info['written']:
                info['revision'] += 1
                session.execute(head.update().where(head.c.module == module).values(revision=info['revision']))
                session.execute(changes.insert().values(module=module, revision=info['revision'], payload=payload))
                info['written'] = True
            else:
                session.execute(changes.update().where((changes.c.module == module) &
                                (changes.c.revision == info['revision'])).values(payload=payload))

    def before_flush(session, context, instances):
        rows = [row for row in session.new if isinstance(row, tracked)]
        rows += [row for row in session.dirty if isinstance(row, tracked)
                 and session.is_modified(row, include_collections=False)]
        rows += [row for row in session.deleted if isinstance(row, tracked)]
        groups = {}
        for row in rows:
            state = _journal_inspect(row)
            keys = list(row.__table__.primary_key.columns)
            if state.persistent and state.identity != tuple(getattr(row, column.name) for column in keys):
                raise ValueError('结果同步不允许修改主键；请删除旧记录再插入新记录。')
            deleted = row in session.deleted
            group = (row.__table__, deleted)
            groups.setdefault(group, []).append(tuple(getattr(row, column.name) for column in keys))
        for (table, deleted), keys in groups.items():
            record(session, table, keys, deleted)

    def reset(session):
        session.info.pop(info_key, None)

    _journal_event.listen(Session, 'before_flush', before_flush)
    _journal_event.listen(Session, 'after_commit', reset)
    _journal_event.listen(Session, 'after_rollback', reset)
    # sessionmaker copies this info dictionary into each new Session.
    Session.configure(info=dict(Session.kw.get('info', {}), _result_journal_callbacks=(tracked, lock, record)))
    Session._result_journal_module = module


def _result_bulk(query, values=None):
    """Serialize before selecting affected keys, then journal the exact bulk write."""
    session = query.session
    model = query.column_descriptions[0]['entity']
    registry = session.info.get('_result_journal_callbacks')
    if registry is None or model not in registry[0]:
        raise ValueError('批量写入缺少结果同步日志，拒绝无日志修改。')
    tracked, lock, record = registry
    lock(session)
    session.flush()
    table = model.__table__
    keys = query.with_entities(*table.primary_key.columns).with_for_update().all()
    if not keys:
        return 0
    record(session, table, keys, values is None)
    if values is None:
        return query.delete(synchronize_session=False)
    return query.update(values, synchronize_session=False)

# END SHARED RESULT JOURNAL
START_DATE = None
BATCH_SIZE = 200

class MarketSentiment(Base):
    __tablename__ = 'market_sentiment'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
    trade_date = Column(Date, primary_key=True)
    ratio = Column(Numeric(18, 10), nullable=False)
    above_count = Column(Integer, nullable=False)
    eligible_count = Column(Integer, nullable=False)
    universe_count = Column(Integer, nullable=False)
    updated_at = Column(DateTime, nullable=False)

class MarketCrowding(Base):
    __tablename__ = 'market_crowding'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
    top_pct = Column(Numeric(8, 6), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    ratio = Column(Numeric(18, 10), nullable=False)
    top_money = Column(Numeric(24, 2), nullable=False)
    total_money = Column(Numeric(24, 2), nullable=False)
    stock_count = Column(Integer, nullable=False)
    top_stock_count = Column(Integer, nullable=False)
    updated_at = Column(DateTime, nullable=False)


def validate_existing(row, model):
    complete(row, model)
    if model is MarketSentiment:
        if not (0 <= row.above_count <= row.eligible_count <= row.universe_count and row.eligible_count > 0):
            raise HistoryError('已有热度计数不完整或无效。')
        expected = derived(Decimal(row.above_count)/Decimal(row.eligible_count), 10)
    else:
        if (row.top_pct != Decimal('0.05') or row.stock_count <= 0
                or row.top_stock_count != max(int(row.stock_count*Decimal('0.05')), 1)
                or not Decimal(0) < row.top_money <= row.total_money):
            raise HistoryError('已有拥挤度计数或成交额无效。')
        expected = derived(row.top_money/row.total_money, 10)
    if row.ratio != expected:
        raise HistoryError('已有汇总比例与原始计数不一致。')
    return True


def source_day(day, heat=True, crowding=True):
    securities = jq.get_all_securities(types=['stock'], date=day)
    if securities is None or securities.empty or securities.index.has_duplicates:
        raise HistoryError('当日股票池缺失或重复。')
    codes = list(securities.index)
    if any(not isinstance(c, str) for c in codes):
        raise HistoryError('股票代码无效。')
    money = []
    eligible = above = universe = 0
    warmup = [as_date(x) for x in jq.get_trade_days(end_date=day, count=20)] if heat else []
    if heat and (len(warmup) != 20 or warmup[-1] != day or warmup != sorted(set(warmup))):
        raise HistoryError('MA20预热日历缺失。')
    if heat and calendar(warmup[0], day) != warmup:
        raise HistoryError('MA20预热覆盖不完整。')
    for offset in range(0, len(codes), BATCH_SIZE):
        batch = codes[offset:offset+BATCH_SIZE]
        if crowding:
            frame = jq.get_price(security=batch, start_date=day, end_date=day, frequency='daily',
                                 fields=['money'], fq=None, skip_paused=False, panel=False)
            if frame is None or not {'code', 'time', 'money'}.issubset(frame.columns) or len(frame) != len(batch):
                raise HistoryError('拥挤度行情缺失。')
            if set(frame['code']) != set(batch) or any(as_date(t) != day for t in frame['time']):
                raise HistoryError('拥挤度行情代码或日期不匹配。')
            amounts = [number(x, 24, 2) for x in frame['money']]
            if any(x < 0 for x in amounts):
                raise HistoryError('成交额为负数。')
            money.extend(amounts)
        if heat:
            active = [c for c in batch if not c.startswith(('200', '900'))]
            universe += len(active)
            if not active:
                continue
            frame = jq.get_price(security=active, start_date=warmup[0], end_date=day, frequency='daily',
                                 fields=['close', 'paused'], fq='pre', skip_paused=False, panel=False)
            if frame is None or not {'code', 'time', 'close', 'paused'}.issubset(frame.columns):
                raise HistoryError('热度行情缺失。')
            records = {}
            for _, row in frame.iterrows():
                key = (row['code'], as_date(row['time']))
                if key in records or key[0] not in active or key[1] not in warmup:
                    raise HistoryError('热度行情重复或越界。')
                records[key] = row
            for code in active:
                listed = as_date(securities.loc[code, 'start_date'])
                expected = [d for d in warmup if d >= listed]
                closes = []
                for d in expected:
                    row = records.get((code, d))
                    if row is None or row['paused'] not in (0, 1):
                        raise HistoryError('上市期间行情或停牌状态缺失。')
                    close = number(row['close'], 38, 16)
                    if close <= 0:
                        raise HistoryError('收盘价无效。')
                    closes.append(close)
                if len(closes) == 20 and records[(code, day)]['paused'] == 0:
                    eligible += 1
                    # Preserve the original MA20 comparison tolerance.
                    mean = sum(closes, Decimal(0))/Decimal(20)
                    above += int(closes[-1]-mean > max(abs(closes[-1]), abs(mean))*Decimal('1e-12'))
    result = {}
    if crowding:
        count = max(int(len(money)*Decimal('0.05')), 1)
        total = number(sum(money, Decimal(0)), 24, 2)
        top = number(sum(sorted(money, reverse=True)[:count], Decimal(0)), 24, 2)
        if total <= 0:
            raise HistoryError('成交额总计无效。')
        result['crowding'] = dict(trade_date=day, top_pct=Decimal('0.05'), ratio=derived(top/total, 10),
            top_money=top, total_money=total, stock_count=len(money), top_stock_count=count, updated_at=china_now())
    if heat:
        if eligible <= 0:
            raise HistoryError('没有有效MA20样本，不能写入零值。')
        result['heat'] = dict(trade_date=day, ratio=derived(Decimal(above)/eligible, 10), above_count=above,
            eligible_count=eligible, universe_count=universe, updated_at=china_now())
    return result


def run(start_date=START_DATE, end_date=END_DATE, write_database=WRITE_DATABASE, max_days=MAX_DAYS):
    if not isinstance(write_database, bool):
        raise HistoryError('write_database 必须为 True 或 False。')
    days = range_days(start_date, end_date, max_days)
    all_days = calendar(None, days[-1])
    heat_days = set(all_days[19:])
    engine = Session = None
    current_day = None
    models = {'heat': MarketSentiment, 'crowding': MarketCrowding}
    existing = {name: set() for name in models}
    try:
        if write_database:
            engine, Session = database(tuple(models.values()), 'market-sentiment')
            session = Session()
            try:
                for name, model in models.items():
                    query = session.query(model).filter(model.trade_date >= days[0], model.trade_date <= days[-1])
                    if name == 'crowding':
                        query = query.filter(model.top_pct == Decimal('0.05'))
                    for row in query.yield_per(250):
                        validate_existing(row, model)
                        existing[name].add(row.trade_date)
            finally:
                session.close()
        pending = [d for d in days if d not in existing['crowding'] or (d in heat_days and d not in existing['heat'])]
        results = []
        for day in pending[:max_days]:
            current_day = day
            data = source_day(day, heat=day in heat_days and day not in existing['heat'], crowding=day not in existing['crowding'])
            if write_database:
                session = Session()
                try:
                    # Journal row lock serializes collectors before insert/recheck.
                    session.info['_result_journal_callbacks'][1](session)
                    for name, record in data.items():
                        model = models[name]
                        query = session.query(model).filter(model.trade_date == day)
                        if name == 'crowding':
                            query = query.filter(model.top_pct == Decimal('0.05'))
                        old = query.one_or_none()
                        if old is not None:
                            validate_existing(old, model)
                            if any(getattr(old, k) != v for k, v in record.items() if k != 'updated_at'):
                                raise HistoryError('并发已有结果冲突；本日事务取消。')
                        else:
                            session.add(model(**record))
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                finally:
                    session.close()
            results.append(data)
        print('本轮处理 {} 日，剩余 {} 日；重复运行继续扫描全部缺口。'.format(len(results), max(0, len(pending)-len(results))))
        return results
    except HistoryError as exc:
        raise HistoryError('{}：{} 本日未提交；之前完成日期保留。'.format(current_day or '初始化', exc)) from None
    except Exception:
        raise RuntimeError('市场历史采集中止：源数据、已有记录或数据库校验失败；本日回滚，之前完成日期保留。') from None
    finally:
        if engine is not None:
            engine.dispose()

if __name__ == '__main__':
    run()
