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
START_DATE = '2010-01-01'
METHOD = 'csi300_equal_positive_pe_ttm_v1'
INDEX = '000300.XSHG'
SOURCE = 'JQ_PLATFORM.PE_TTM.V1'
CALENDAR_SOURCE = 'JQDATA.GET_TRADE_DAYS'

class ValuationDaily(Base):
    __tablename__ = 'valuation_ruler_daily'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
    trade_date = Column(Date, primary_key=True)
    index_code = Column(String(16), primary_key=True)
    method_version = Column(String(48), primary_key=True)
    index_close = Column(Numeric(24, 8), nullable=False)
    mean_pe_ttm = Column(Numeric(30, 12), nullable=False)
    pe_sum = Column(Numeric(38, 12), nullable=False)
    member_count = Column(Integer, nullable=False)
    positive_count = Column(Integer, nullable=False)
    loss_count = Column(Integer, nullable=False)
    members_sha256 = Column(String(64), nullable=False)
    source = Column(String(48), nullable=False)
    updated_at = Column(DateTime, nullable=False)

class ValuationMember(Base):
    __tablename__ = 'valuation_ruler_member'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
    trade_date = Column(Date, primary_key=True)
    index_code = Column(String(16), primary_key=True)
    method_version = Column(String(48), primary_key=True)
    code = Column(String(16), primary_key=True)
    pe_ttm = Column(Numeric(30, 12), nullable=False)
    source = Column(String(48), nullable=False)
    updated_at = Column(DateTime, nullable=False)

class ValuationCalendar(Base):
    __tablename__ = 'valuation_ruler_trading_day'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
    trade_date = Column(Date, primary_key=True)
    source = Column(String(48), nullable=False)
    updated_at = Column(DateTime, nullable=False)

MODELS = (ValuationDaily, ValuationMember, ValuationCalendar)


def valid_code(code):
    # Same stock identity contract as the backend importer, including 302/689 CDRs.
    return (isinstance(code, str) and re.match(r'^[036]\d{5}\.(?:XSHG|XSHE)\Z', code) is not None
            and ((code.startswith('6') and code.endswith('.XSHG'))
                 or (code.startswith(('0', '3')) and code.endswith('.XSHE'))))


def summarize(day, close, members):
    if len(members) != 300 or len(set(code for code, pe in members)) != 300:
        raise HistoryError('沪深300必须恰好300个唯一当日成分。')
    if any(not valid_code(code) for code, pe in members):
        raise HistoryError('成分代码格式无效。')
    close = number(close, 24, 8)
    if close <= 0:
        raise HistoryError('指数原始收盘价无效。')
    checked = [(code, number(pe, 30, 12)) for code, pe in members]
    if any(pe == 0 for code, pe in checked):
        raise HistoryError('PE-TTM为零，不能视作亏损或缺失。')
    positive = [pe for code, pe in checked if pe > 0]
    if not positive:
        raise HistoryError('没有正PE样本。')
    with localcontext() as ctx:
        ctx.prec = 80
        total = number(sum(positive, Decimal(0)), 38, 12)
        mean = number(derived(total/Decimal(len(positive)), 12), 30, 12)
    digest = hashlib.sha256('\n'.join(sorted(code for code, pe in checked)).encode('utf-8')).hexdigest()
    stamp = china_now()
    key = dict(trade_date=day, index_code=INDEX, method_version=METHOD, source=SOURCE, updated_at=stamp)
    daily = dict(key, index_close=close, mean_pe_ttm=mean, pe_sum=total, member_count=300,
                 positive_count=len(positive), loss_count=300-len(positive), members_sha256=digest)
    rows = [dict(key, code=code, pe_ttm=pe) for code, pe in sorted(checked)]
    cal = dict(trade_date=day, source=CALENDAR_SOURCE, updated_at=stamp)
    return daily, rows, cal


def source_day(day):
    codes = jq.get_index_stocks(INDEX, date=day)
    if codes is None or len(codes) != 300 or len(set(codes)) != 300:
        raise HistoryError('当日沪深300成分缺失或重复。')
    query = jq.query(jq.valuation.code, jq.valuation.day, jq.valuation.pe_ratio).filter(jq.valuation.code.in_(codes))
    frame = jq.get_fundamentals(query, date=day)
    if (frame is None or not {'code', 'day', 'pe_ratio'}.issubset(frame.columns)
            or len(frame) != 300 or set(frame['code']) != set(codes)
            or any(as_date(x) != day for x in frame['day'])):
        raise HistoryError('当日PE-TTM数据缺失、重复或日期不匹配。')
    prices = jq.get_price(INDEX, start_date=day, end_date=day, frequency='daily',
                          fields=['close'], fq=None, skip_paused=False)
    if prices is None or len(prices) != 1 or 'close' not in prices.columns or as_date(prices.index[0]) != day:
        raise HistoryError('指数原始收盘行情缺失或日期不匹配。')
    return summarize(day, prices.iloc[0]['close'], list(zip(frame['code'], frame['pe_ratio'])))


def existing_day(session, day):
    daily = session.query(ValuationDaily).filter_by(trade_date=day, index_code=INDEX, method_version=METHOD).one_or_none()
    members = session.query(ValuationMember).filter_by(trade_date=day, index_code=INDEX, method_version=METHOD).all()
    cal = session.query(ValuationCalendar).filter_by(trade_date=day).one_or_none()
    if daily is None and not members and cal is None:
        return False
    if daily is None or len(members) != 300 or cal is None:
        raise HistoryError('已有单日数据不完整，禁止覆盖或拼接；请先人工核实。')
    complete(daily, ValuationDaily)
    complete(cal, ValuationCalendar)
    if cal.source != CALENDAR_SOURCE or daily.source != SOURCE:
        raise HistoryError('已有记录来源不匹配。')
    for member in members:
        complete(member, ValuationMember)
        if member.source != SOURCE:
            raise HistoryError('已有成分来源不匹配。')
    computed, _, _ = summarize(day, daily.index_close, [(m.code, m.pe_ttm) for m in members])
    if any(getattr(daily, key) != value for key, value in computed.items() if key != 'updated_at'):
        raise HistoryError('已有汇总与原始成分不一致，禁止覆盖。')
    return True


def run(start_date=START_DATE, end_date=END_DATE, write_database=WRITE_DATABASE, max_days=MAX_DAYS):
    if as_date(start_date) < dt.date(2010, 1, 1):
        raise HistoryError('估值标尺历史起点不得早于2010年。')
    if not isinstance(write_database, bool):
        raise HistoryError('write_database 必须为 True 或 False。')
    days = range_days(start_date, end_date, max_days)
    engine = Session = None
    current_day = None
    try:
        pending = list(days)
        if write_database:
            engine, Session = database(MODELS, 'valuation-ruler')
            session = Session()
            try:
                # Every requested date is checked, including gaps before MAX(date).
                pending = [day for day in days if not existing_day(session, day)]
            finally:
                session.close()
        results = []
        for day in pending[:max_days]:
            current_day = day
            daily, members, cal = source_day(day)
            if write_database:
                session = Session()
                try:
                    session.info['_result_journal_callbacks'][1](session)
                    if existing_day(session, day):
                        old = session.query(ValuationDaily).filter_by(trade_date=day, index_code=INDEX, method_version=METHOD).one()
                        saved_members = session.query(ValuationMember).filter_by(trade_date=day, index_code=INDEX, method_version=METHOD).all()
                        if (any(getattr(old, k) != v for k, v in daily.items() if k != 'updated_at')
                                or {m.code: m.pe_ttm for m in saved_members} != {m['code']: m['pe_ttm'] for m in members}):
                            raise HistoryError('并发已有结果冲突，单日事务取消。')
                    else:
                        session.add(ValuationDaily(**daily))
                        session.add_all([ValuationMember(**m) for m in members])
                        session.add(ValuationCalendar(**cal))
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                finally:
                    session.close()
            results.append(daily)
        print('本轮处理 {} 日，剩余 {} 日；重复运行继续扫描全部缺口。'.format(len(results), max(0, len(pending)-len(results))))
        return results
    except HistoryError as exc:
        raise HistoryError('{}：{} 本日未提交；之前完成日期保留。'.format(current_day or '初始化', exc)) from None
    except Exception:
        raise RuntimeError('估值历史采集中止：源数据、已有记录或数据库校验失败；本日回滚，之前完成日期保留。') from None
    finally:
        if engine is not None:
            engine.dispose()

if __name__ == '__main__':
    run()
