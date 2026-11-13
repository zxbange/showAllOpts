# -*- coding: utf-8 -*-

from __future__ import print_function, division

# 沿用已能运行的原程序导入方式，使用研究环境提供的 API。
# 不用 globals() 判断接口是否存在：函数也可能位于内置命名空间。
# 在 Notebook 中直接粘贴运行；运行文件请使用：
# %run -i 行业动量计算_数据库版.py
# -i 让文件沿用当前 Notebook 的命名空间，避免丢失环境预置的接口。
from jqdata import *

# 放在平台通配符导入之后，避免 datetime 等同名对象被覆盖。
import builtins
import datetime
import time
import json
import hashlib
from decimal import Decimal

import pandas as pd
from sqlalchemy import Column, Date, DateTime, Integer, Numeric, String, Text, create_engine, func
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker


START_DATE = '2025-01-01'  # 该行业分类首次入库时使用的起始日期。
END_DATE = None  # None 表示每次运行时取北京时间当天，也可指定 'YYYY-MM-DD'。
INDUSTRY_TYPE = 'sw_l2'
RETENTION_TRADE_DAYS = 180  # 仅普通模式默认采集窗口；两种模式均不自动删除历史结果。
HISTORY_MODE = True  # 直接运行默认完整恢复；run() 函数仍保留普通模式的既有行为。

Base = declarative_base()
RESULT_COLUMNS = [
    'trade_date', 'industry_type', 'industry_code', 'industry_name',
    'momentum', 'momentum_rank', 'front_stock_count', 'industry_stock_count',
]


# BEGIN SHARED MOMENTUM THRESHOLD
from decimal import Decimal as _MomentumDecimal, InvalidOperation as _MomentumInvalidOperation


def _momentum_front_status(start_close, end_close):
    """Return strict >20% membership, or None for an invalid price pair.

    Use the decimal values supplied by the data source, without float arithmetic
    or rounding. Integer cross-products implement 5 * end > 6 * start exactly,
    including decimal inputs longer than the active Decimal context precision.
    """
    try:
        start = _MomentumDecimal(str(start_close))
        end = _MomentumDecimal(str(end_close))
    except (_MomentumInvalidOperation, TypeError, ValueError):
        return None
    if not start.is_finite() or not end.is_finite() or start <= 0 or end <= 0:
        return None
    start_numerator, start_denominator = start.as_integer_ratio()
    end_numerator, end_denominator = end.as_integer_ratio()
    return (5 * end_numerator * start_denominator
            > 6 * start_numerator * end_denominator)
# END SHARED MOMENTUM THRESHOLD


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

def _checked_trade_days(raw, start=None, end=None, count=None,
                        error='交易日历缺失、重复、乱序或越界。', missing_error=None):
    """Normalize and validate an exchange calendar before calculating or writing."""
    if raw is None:
        raise ValueError(missing_error or error)
    days = [as_date(value) for value in raw]
    if (days != sorted(set(days)) or (count is not None and len(days) != count)
            or builtins.any((start is not None and day < start) or
                            (end is not None and day > end) for day in days)):
        raise ValueError(error)
    return days


def _closed_day(end, now):
    """日行情在北京时间16点后使用当天；交易日仍由各策略的日历校验。"""
    return end - datetime.timedelta(days=1) if end == now.date() and now.hour < 16 else end


class IndustryMomentum(Base):
    __tablename__ = 'industry_momentum'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}

    trade_date = Column(Date, primary_key=True)
    industry_type = Column(String(16), primary_key=True)
    industry_code = Column(String(32), primary_key=True)
    industry_name = Column(String(128), nullable=False)
    momentum = Column(Numeric(18, 2), nullable=False)
    momentum_rank = Column(Integer, nullable=False)
    front_stock_count = Column(Integer, nullable=False)
    industry_stock_count = Column(Integer, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.datetime.now)


from mysql_connector import get_database_config, get_database_options, mysql_drivername, engine_options
DB_CONFIG = get_database_config()
DB_OPTIONS = get_database_options()


class MysqlMomentum:
    # GitHub 脱敏模板：在聚宽私有副本中填写连接信息，不要提交真实凭据。
    host = DB_CONFIG['host']
    port = DB_CONFIG['port']
    dbname = DB_CONFIG['dbname']
    dbuser = DB_CONFIG['dbuser']
    dbpassword = DB_CONFIG['dbpassword']

    @classmethod
    def connect_to_mysql(cls):
        # URL.create 兼容新 SQLAlchemy；旧版聚宽环境使用 URL 构造函数。
        make_url = getattr(URL, 'create', URL)
        uri = make_url(
            drivername=mysql_drivername(DB_OPTIONS['driver']), host=cls.host, port=cls.port,
            username=cls.dbuser, password=cls.dbpassword, database=cls.dbname,
            query={'charset': DB_OPTIONS['charset']},
        )
        engine = create_engine(uri, **engine_options(DB_OPTIONS))
        if hasattr(engine, 'hide_parameters'):
            engine.hide_parameters = True
        for attempt in range(1, DB_OPTIONS['retry_count'] + 1):
            try:
                with engine.connect():
                    pass
                print('成功连接到 MySQL 数据库。')
                return engine
            except Exception as exc:
                # 不打印连接串或异常全文，避免日志包含密码。
                print('MySQL 连接失败，第 {}/{} 次，异常类型：{}'.format(
                    attempt, DB_OPTIONS['retry_count'], type(exc).__name__))
                engine.dispose()
                if attempt == DB_OPTIONS['retry_count']:
                    raise RuntimeError('MySQL 连接失败，请检查网络、白名单和连接参数。') from None
                time.sleep(DB_OPTIONS['retry_interval'])


def as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.datetime.strptime(str(value)[:10], '%Y-%m-%d').date()


def calculate_momentum(trade_date, industry_type=INDUSTRY_TYPE):
    """计算一个交易日的全部行业；仅获取数据，不连接数据库。"""
    day = as_date(trade_date)
    date_text = day.isoformat()
    industries = get_industries(name=industry_type, date=date_text)
    if industries is None or industries.empty:
        raise ValueError('{} 未获取到行业列表，停止写入。'.format(date_text))

    # 同一行业只请求一次，计数和归属使用完全相同的成分股数据。
    members = {}
    for code in industries.index:
        members[code] = set(get_industry_stocks(code, date=date_text))

    securities = get_all_securities(types=['stock'], date=date_text)
    if securities is None or securities.empty:
        raise ValueError('{} 未获取到股票列表，停止写入。'.format(date_text))
    cutoff = day - datetime.timedelta(days=150)
    eligible = securities[
        (securities['end_date'].map(as_date) > day)
        & (securities['start_date'].map(as_date) < cutoff)
    ].index.tolist()
    if not eligible:
        raise ValueError('{} 没有符合上市期限条件的股票，停止写入。'.format(date_text))

    # 20 日涨幅需要 21 个交易日。用交易日定位端点，避免缺行时取错日期。
    window = [as_date(d) for d in get_trade_days(end_date=date_text, count=21)]
    if len(window) != 21 or window[-1] != day:
        raise ValueError('{} 不是交易日或历史交易日不足 21 天。'.format(date_text))
    prices = get_price(
        eligible, start_date=None, end_date=date_text, frequency='daily',
        fields=['close'], skip_paused=False, fq='pre', count=25, panel=False,
    )
    if prices is None or prices.empty:
        raise ValueError('{} 行情为空，停止写入，避免误存全零结果。'.format(date_text))
    prices = prices.copy()
    prices['price_date'] = pd.to_datetime(prices['time']).dt.date
    front_stocks = set()
    valid_count = 0
    for code, group in prices.groupby('code'):
        if group['price_date'].duplicated().any():
            raise ValueError('{} {} 行情日期重复。'.format(date_text, code))
        close = group.set_index('price_date')['close']
        if window[0] not in close.index or day not in close.index:
            continue
        start_close, end_close = close.loc[window[0]], close.loc[day]
        front_status = _momentum_front_status(start_close, end_close)
        if front_status is None:
            continue
        valid_count += 1
        if front_status:
            front_stocks.add(code)
    if not valid_count:
        raise ValueError('{} 没有可计算涨幅的股票，停止写入。'.format(date_text))
    if valid_count < len(eligible):
        print('{}：{} 只股票价格无效或缺失，未参与涨幅统计。'.format(
            date_text, len(eligible) - valid_count))

    records = []
    for code in industries.index:
        total = len(members[code])
        count = len(members[code] & front_stocks)
        score = count * count / total if total else 0.0
        records.append({
            'trade_date': day,
            'industry_type': industry_type,
            'industry_code': str(code),
            'industry_name': str(industries.loc[code, 'name']),
            'momentum': Decimal(format(score, '.2f')),
            'front_stock_count': count,
            'industry_stock_count': total,
        })
    # 与原程序相同：先按两位小数的得分降序，再按行业代码降序。
    records.sort(key=lambda r: (r['momentum'], r['industry_code']), reverse=True)
    for rank, record in enumerate(records, 1):
        record['momentum_rank'] = rank
    return records


def save_day(Session, trade_date, industry_type, records, skip_existing=False):
    """默认事务替换日快照；只新增模式按主键过滤已存在记录。"""
    day = as_date(trade_date)
    if not records:
        raise ValueError('拒绝写入空结果。')
    # 不使用 any(生成器)：研究环境中的同名函数可能是 numpy.any，
    # 对生成器的处理与 Python 内置 any 不同，会把一致的结果误判为异常。
    for record in records:
        if record['trade_date'] != day or record['industry_type'] != industry_type:
            raise ValueError(
                '结果中的日期或行业分类不一致：行业代码={!r}，'
                '预期日期={!r}、分类={!r}；实际日期={!r}、分类={!r}。'.format(
                    record.get('industry_code'), day, industry_type,
                    record['trade_date'], record['industry_type']))
    installer = globals().get('_install_result_journal')
    if installer is not None:
        installer(Session, (IndustryMomentum,), 'industry-momentum')
    session = Session()
    try:
        query = session.query(IndustryMomentum).filter_by(trade_date=day, industry_type=industry_type)
        if skip_existing:
            existing = {row[0] for row in query.with_entities(IndustryMomentum.industry_code).all()}
            records = [record for record in records if record['industry_code'] not in existing]
            if not records:
                return []
        else:
            if session.info.get('_result_journal_callbacks'):
                _result_bulk(query)
            else:
                query.delete(synchronize_session=False)
        session.add_all([IndustryMomentum(**record) for record in records])
        session.commit()
        return records
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_last_saved_date(Session, industry_type):
    """按行业分类查询最后已提交的交易日期；没有记录时返回 None。"""
    session = Session()
    try:
        return session.query(func.max(IndustryMomentum.trade_date)).filter(
            IndustryMomentum.industry_type == industry_type,
        ).scalar()
    finally:
        session.close()


def get_retention_start(reference_date, count=RETENTION_TRADE_DAYS):
    """先验证交易日历，再允许清理；避免接口异常导致误删。"""
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ValueError('保留交易日数量必须是正整数。')
    reference = as_date(reference_date)
    raw_days = get_trade_days(end_date=reference.isoformat(), count=count)
    if raw_days is None:
        raise ValueError('保留范围的交易日历为空，未清理数据库。')
    days = [as_date(day) for day in raw_days]
    if len(days) != count:
        raise ValueError('交易日历不足 {} 天，未清理数据库。'.format(count))
    return _checked_trade_days(days, end=reference,
        error='交易日历存在未来日期、重复或乱序，未清理数据库。')[0]


def cleanup_old_data(Session, retention_start, history_model=None):
    """一个事务清理整张表的过期数据，保留边界日及之后的快照。"""
    cutoff = as_date(retention_start)
    installer = globals().get('_install_result_journal')
    if installer is not None:
        installer(Session, (IndustryMomentum,), 'industry-momentum')
    session = Session()
    try:
        query = session.query(IndustryMomentum).filter(IndustryMomentum.trade_date < cutoff)
        if history_model is not None:
            protected = session.query(history_model).filter(
                history_model.trade_date == IndustryMomentum.trade_date,
                history_model.industry_type == IndustryMomentum.industry_type,
                history_model.status == 'complete').exists()
            query = query.filter(~protected)
        deleted = (_result_bulk(query) if session.info.get('_result_journal_callbacks')
                   else query.delete(synchronize_session=False))
        session.commit()
        return deleted
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run(start_date=None, end_date=END_DATE, industry_type=INDUSTRY_TYPE, skip_existing=True,
        history_mode=False, max_days=None):
    """普通模式使用近期采集窗口但不删历史；历史模式修复全部日期缺口。"""
    if history_mode:
        return run_history(start_date=start_date, end_date=end_date, industry_type=industry_type,
                           skip_existing=skip_existing, max_days=max_days)
    now_china = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    today_china = now_china.date()
    if end_date is None:
        end_date = today_china
    initial_start, end = as_date(START_DATE if start_date is None else start_date), as_date(end_date)
    if end > today_china:
        raise ValueError('结束日期不能晚于运行当天。')
    if skip_existing:
        end = _closed_day(end, now_china)
        print('只新增模式：16:00 前不写入当天未完成快照。')
    # 即使手动指定历史 end_date，保留范围仍以实际运行当天为基准。
    # 日历验证在任何数据库修改之前完成，节假日自动使用最近交易日。
    retention_start = get_retention_start(today_china, RETENTION_TRADE_DAYS)
    engine = MysqlMomentum.connect_to_mysql()
    all_records = []
    try:
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        # Even an up-to-date source needs a journal head when upgrading collectors.
        _install_result_journal(Session, (IndustryMomentum,), 'industry-momentum')
        print('普通模式从最近 {} 个交易日窗口续采，窗口起始日 {}；不自动删除任何历史记录。'.format(
            RETENTION_TRADE_DAYS, retention_start))
        if end < retention_start:
            print('结束日期 {} 早于普通采集窗口 {}；旧数据保留，补采请使用 run_history()。'.format(end, retention_start))
            return pd.DataFrame(columns=RESULT_COLUMNS)
        last_date = get_last_saved_date(Session, industry_type)
        if last_date is None:
            start = initial_start
            if start < retention_start:
                start = retention_start
            if start > end:
                raise ValueError('首次入库的开始日期不能晚于结束日期。')
            print('{} 尚无保留范围内的记录，从 {} 开始入库。'.format(industry_type, start))
        else:
            start = as_date(last_date)
            if skip_existing:
                start += datetime.timedelta(days=1)
            if start < retention_start:
                start = retention_start
            if start > end:
                print('最后已入库日期 {} 晚于结束日期 {}，无需更新。'.format(start, end))
                return pd.DataFrame(columns=RESULT_COLUMNS)
            print('{} 最后已入库日期为 {}，从 {}（含）采集至 {}。'.format(
                industry_type, last_date, start, end))
        trade_days = [as_date(day) for day in get_trade_days(start_date=start, end_date=end)]
        # 先检查整个补录日历，避免异常接口将过期或越界日期写回。
        previous = None
        for day in trade_days:
            if day < start or day > end or (previous is not None and day <= previous):
                raise ValueError('补录交易日历越界、重复或乱序，停止入库。')
            previous = day
        if not trade_days:
            print('日期范围内没有交易日，无需写入。')
            return pd.DataFrame(columns=RESULT_COLUMNS)
        for day in trade_days:
            records = calculate_momentum(day, industry_type)
            try:
                records = save_day(Session, day, industry_type, records, skip_existing=skip_existing)
            except Exception:
                print('{} 校验或写入失败；修复后重跑会从最后已入库日期继续。'.format(day))
                raise
            all_records.extend(records)
            print('{} 计算并入库完成，共 {} 个行业（含零分行业）。'.format(day, len(records)))
    finally:
        engine.dispose()
    return pd.DataFrame(all_records, columns=RESULT_COLUMNS)


# Historical reconstruction is based on today's visible/revised JoinQuant data.
# It is not a replay of original point-in-time observations.
HISTORY_SOURCE_BASIS = 'joinquant_visible_revised_not_original_pit_v1'


class MomentumHistoryStatus(Base):
    __tablename__ = 'industry_momentum_history_status'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
    trade_date = Column(Date, primary_key=True)
    industry_type = Column(String(16), primary_key=True)
    status = Column(String(32), nullable=False)
    reason = Column(String(128), nullable=False)
    source_basis = Column(String(64), nullable=False)
    payload_digest = Column(String(64), nullable=False)
    industry_codes = Column(Text, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class MomentumHistoryUnavailable(ValueError):
    """Only verified history-prefix limitations, never an API exception."""


def momentum_history_calendar(start, end):
    # get_all_trade_days is independent of the requested window. A short range
    # response must never masquerade as an early history boundary.
    all_days = _checked_trade_days(get_all_trade_days())
    if not all_days:
        raise ValueError('完整交易日历为空，不能证明历史边界。')
    available = [d for d in all_days if d <= end]
    if not available:
        raise ValueError('结束日期前没有可验证的交易日。')
    first = available[0] if start is None else as_date(start)
    expected = [d for d in available if first <= d]
    actual = _checked_trade_days(get_trade_days(start_date=first, end_date=end), first, end)
    if actual != expected:
        raise ValueError('范围交易日历与完整交易日历不一致，停止历史恢复。')
    return available, expected


def momentum_history_industries(day, industry_type, frame):
    if frame is None or frame.empty:
        raise ValueError('{} 行业分类为空，不能把源缺失当成零行业。'.format(day))
    codes = [str(code) for code in frame.index]
    if (len(codes) != len(set(codes)) or 'name' not in frame.columns
            or builtins.any(not code or pd.isnull(frame.loc[code, 'name']) for code in codes)):
        raise ValueError('行业列表包含重复代码或缺少名称。')
    return frame


def calculate_momentum_history(day, industry_type, full_calendar, industry_inception=None):
    """Validate every dated universe/member/price before calculating any result."""
    day = as_date(day)
    known = [d for d in full_calendar if d <= day]
    if not known or known[-1] != day:
        raise ValueError('待恢复日期不在完整交易日历内。')
    if len(known) < 21:
        raise MomentumHistoryUnavailable('verified_calendar_warmup_21')
    window = known[-21:]
    actual = _checked_trade_days(get_trade_days(end_date=day.isoformat(), count=21), end=day, count=21)
    if actual != window:
        raise ValueError('21 日交易窗口与完整交易日历不一致。')
    raw_industries = get_industries(name=industry_type, date=day.isoformat())
    if raw_industries is not None and raw_industries.empty and industry_inception is not None and day < industry_inception:
        raise MomentumHistoryUnavailable('verified_classification_inception_' + industry_inception.isoformat())
    # None, exceptions, and empty results at/after inception remain source failures.
    industries = momentum_history_industries(day, industry_type, frame=raw_industries)
    securities = get_all_securities(types=['stock'], date=day.isoformat())
    if (securities is None or securities.empty
            or not {'start_date', 'end_date'}.issubset(securities.columns)):
        raise ValueError('历史股票列表为空或字段缺失。')
    codes = [str(code) for code in securities.index]
    if len(codes) != len(set(codes)):
        raise ValueError('历史股票列表包含重复代码。')
    cutoff = day - datetime.timedelta(days=150)
    eligible = []
    for code, row in securities.iterrows():
        start, end = as_date(row['start_date']), as_date(row['end_date'])
        if start > day or end < day or start > end:
            raise ValueError('历史股票列表含未上市或已退市股票。')
        if start < cutoff and end > day:
            eligible.append(str(code))
    if not eligible:
        raise MomentumHistoryUnavailable('verified_listing_warmup_150_calendar_days')
    members = {}
    for code in industries.index:
        raw = get_industry_stocks(code, date=day.isoformat())
        if raw is None:
            raise ValueError('行业成分返回空对象。')
        values = [str(value) for value in raw]
        if len(values) != len(set(values)) or not set(values).issubset(set(codes)):
            raise ValueError('行业成分重复或不在当日股票列表内。')
        members[str(code)] = set(values)
    # Cross-check the same dated membership from the per-security endpoint.
    # An empty industry's legitimate zero is allowed only after this check.
    assigned = {str(code): set() for code in industries.index}
    for offset in range(0, len(codes), 100):
        batch = codes[offset:offset + 100]
        info = get_industry(batch, date=day.isoformat())
        if not isinstance(info, dict) or set(info) != set(batch):
            raise ValueError('逐股行业归属未完整返回。')
        for code in batch:
            if not isinstance(info[code], dict):
                raise ValueError('逐股行业归属格式无效。')
            industry = info[code].get(industry_type)
            if industry is not None:
                if not isinstance(industry, dict) or industry.get('industry_code') not in assigned:
                    raise ValueError('逐股行业归属不在当日行业列表内。')
                assigned[industry['industry_code']].add(code)
    if assigned != members:
        raise ValueError('行业成分与逐股归属不一致，不能认证成分完整性。')
    front = set()
    for offset in range(0, len(eligible), 100):
        batch = eligible[offset:offset + 100]
        frame = get_price(batch, start_date=window[0], end_date=day, frequency='daily',
                          fields=['close'], skip_paused=False, fq='pre', panel=False)
        if frame is None or not {'code', 'time', 'close'}.issubset(frame.columns):
            raise ValueError('历史行情为空或字段缺失。')
        rows = frame.to_dict('records')
        if len(rows) != len(batch) * 21:
            raise ValueError('历史行情日期/成员不完整，不能按零填补。')
        prices = {code: {} for code in batch}
        for row in rows:
            code, date = str(row['code']), as_date(row['time'])
            if code not in prices or date not in window or date in prices[code]:
                raise ValueError('历史行情含重复、越界日期或股票。')
            if _momentum_front_status(row['close'], row['close']) is None:
                raise ValueError('历史收盘价非正或无效。')
            prices[code][date] = row['close']
        for code in batch:
            if set(prices[code]) != set(window):
                raise ValueError('个股历史行情缺日。')
            if _momentum_front_status(prices[code][window[0]], prices[code][day]):
                front.add(code)
    result = []
    for code in industries.index:
        total = len(members[str(code)])
        count = len(members[str(code)] & front)
        score = Decimal(format(count * count / total if total else 0.0, '.2f'))
        result.append(dict(trade_date=day, industry_type=industry_type, industry_code=str(code),
            industry_name=str(industries.loc[code, 'name']), momentum=score,
            front_stock_count=count, industry_stock_count=total))
    result.sort(key=lambda row: (row['momentum'], row['industry_code']), reverse=True)
    for rank, row in enumerate(result, 1):
        row['momentum_rank'] = rank
    return result


def momentum_history_digest(records):
    values = []
    for row in sorted(records, key=lambda item: item['industry_code']):
        values.append([row['industry_code'], row['industry_name'], format(Decimal(str(row['momentum'])), '.2f'),
                       row['momentum_rank'], row['front_stock_count'], row['industry_stock_count']])
    return hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def momentum_history_existing(Session, day, industry_type):
    session = Session()
    try:
        rows = session.query(IndustryMomentum).filter_by(trade_date=day, industry_type=industry_type).all()
        records = [{key: getattr(row, key) for key in RESULT_COLUMNS} for row in rows]
        status = session.query(MomentumHistoryStatus).filter_by(trade_date=day, industry_type=industry_type).first()
        if status is None or status.status != 'complete':
            return False
        expected = json.loads(status.industry_codes)
        return (status.source_basis == HISTORY_SOURCE_BASIS and bool(records)
                and sorted(row['industry_code'] for row in records) == sorted(expected)
                and momentum_history_digest(records) == status.payload_digest)
    finally:
        session.close()


def save_momentum_history(Session, day, industry_type, records=None, reason=None):
    """Append missing industry keys and its completeness receipt in one transaction."""
    if records is not None:
        if not records:
            raise ValueError('历史行业结果为空。')
        for row in records:
            if row['trade_date'] != day or row['industry_type'] != industry_type:
                raise ValueError('历史行业结果日期或分类不一致。')
            total, count, rank = row['industry_stock_count'], row['front_stock_count'], row['momentum_rank']
            if (builtins.any(isinstance(value, bool) or not isinstance(value, int) for value in (total, count, rank))
                    or total < 0 or not 0 <= count <= total or rank < 1):
                raise ValueError('历史行业计数或排名无效。')
            score = Decimal(format(count * count / total if total else 0.0, '.2f'))
            if Decimal(str(row['momentum'])) != score:
                raise ValueError('历史行业动量与成分统计不一致。')
        ordered = sorted(records, key=lambda row: (row['momentum'], row['industry_code']), reverse=True)
        if [row['momentum_rank'] for row in ordered] != list(range(1, len(records) + 1)):
            raise ValueError('历史行业排名不完整。')
    _install_result_journal(Session, (IndustryMomentum,), 'industry-momentum')
    session = Session()
    try:
        existing = session.query(IndustryMomentum).filter_by(trade_date=day, industry_type=industry_type).all()
        previous = {row.industry_code: {key: getattr(row, key) for key in RESULT_COLUMNS} for row in existing}
        added = []
        if records is not None:
            expected = {row['industry_code']: row for row in records}
            if not expected or len(expected) != len(records):
                raise ValueError('历史行业结果为空或重复。')
            if not set(previous).issubset(expected):
                raise ValueError('已存行业与历史源行业列表不一致，保留原值并停止。')
            for code, record in previous.items():
                if momentum_history_digest([record]) != momentum_history_digest([expected[code]]):
                    raise ValueError('已存行业与重建值冲突，可能来自不同修订版本；保留原值并停止。')
            added = [row for code, row in expected.items() if code not in previous]
            session.add_all([IndustryMomentum(**row) for row in added])
        elif previous:
            raise ValueError('已有行业记录的日期不能标记为历史不可用。')
        status = session.query(MomentumHistoryStatus).filter_by(trade_date=day, industry_type=industry_type).first()
        if status is None:
            status = MomentumHistoryStatus(trade_date=day, industry_type=industry_type)
            session.add(status)
        status.status = 'complete' if records is not None else 'unavailable_prefix'
        status.reason = '' if records is not None else reason
        status.source_basis = HISTORY_SOURCE_BASIS
        status.payload_digest = momentum_history_digest(records) if records is not None else ''
        status.industry_codes = json.dumps(sorted(row['industry_code'] for row in records)) if records is not None else '[]'
        status.updated_at = datetime.datetime.utcnow()
        session.commit()
        return added
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def run_history(start_date=None, end_date=None, industry_type=INDUSTRY_TYPE,
                skip_existing=True, max_days=None, retry_unavailable=False):
    """Full history by default; never retain-only, skip MAX or overwrite old results."""
    if skip_existing is not True:
        raise ValueError('历史恢复仅补缺，不允许覆盖已存行业结果。')
    if max_days is not None and (isinstance(max_days, bool) or not isinstance(max_days, int) or max_days <= 0):
        raise ValueError('max_days 必须为正整数或 None。')
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    end = as_date(end_date) if end_date is not None else now.date()
    if end > now.date() or (start_date is not None and as_date(start_date) > end):
        raise ValueError('历史恢复日期范围无效。')
    end = _closed_day(end, now)
    if start_date is not None and as_date(start_date) > end:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    full, days = momentum_history_calendar(start_date, end)
    # Classification metadata provides positive evidence for its start date.
    # Never infer prehistory merely from a failed/empty API response.
    metadata = get_industries(name=industry_type, date=end.isoformat())
    inception = None
    if metadata is not None and not metadata.empty and 'start_date' in metadata.columns:
        inception = builtins.min(as_date(value) for value in metadata['start_date'])
    engine = MysqlMomentum.connect_to_mysql()
    output, attempted, available_started = [], 0, False
    try:
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        _install_result_journal(Session, (IndustryMomentum,), 'industry-momentum')
        session = Session()
        try:
            first_saved = session.query(func.min(IndustryMomentum.trade_date)).filter_by(industry_type=industry_type).scalar()
        finally:
            session.close()
        for day in days:
            if not retry_unavailable:
                session = Session()
                try:
                    receipt = session.query(MomentumHistoryStatus).filter_by(trade_date=day, industry_type=industry_type).first()
                    unavailable = (receipt is not None and receipt.status == 'unavailable_prefix'
                                   and receipt.source_basis == HISTORY_SOURCE_BASIS)
                    present = session.query(IndustryMomentum).filter_by(trade_date=day, industry_type=industry_type).first()
                    if unavailable and present is None:
                        if first_saved is not None and day >= first_saved:
                            raise ValueError('历史不可用标记位于已有成功日期之后，须核查或重试。')
                        continue
                finally:
                    session.close()
            if momentum_history_existing(Session, day, industry_type):
                available_started = True
                continue
            # A bound applies to actual calculations, not skipped complete days.
            if max_days is not None and attempted >= max_days:
                break
            attempted += 1
            try:
                records = calculate_momentum_history(day, industry_type, full, industry_inception=inception)
            except MomentumHistoryUnavailable as exc:
                if available_started or (first_saved is not None and day >= first_saved):
                    raise ValueError('{} 成功历史之后出现不足样本，停止而不跳过。'.format(day)) from None
                save_momentum_history(Session, day, industry_type, reason=str(exc))
                print('{} 历史前缀不可计算：{}；未写入零值。'.format(day, str(exc)))
                continue
            output.extend(save_momentum_history(Session, day, industry_type, records=records))
            available_started = True
            print('{} 历史行业恢复已验证；来源为当前可见修订数据，非原始时点快照。'.format(day))
        session = Session()
        try:
            unavailable_count = session.query(MomentumHistoryStatus).filter(
                MomentumHistoryStatus.industry_type == industry_type,
                MomentumHistoryStatus.status == 'unavailable_prefix',
                MomentumHistoryStatus.trade_date.in_(days)).count()
        finally:
            session.close()
        print('本次检查 {} 个待计算日；新增 {} 条；范围内 {} 个历史前缀日期不可计算，未当作完成或零值。'.format(
            attempted, len(output), unavailable_count))
        if max_days is not None and attempted >= max_days:
            print('已达到 max_days；按相同参数重跑会继续其余未认证日期。')
    finally:
        engine.dispose()
    return pd.DataFrame(output, columns=RESULT_COLUMNS)


if __name__ == '__main__':
    try:
        momentum_df = run(history_mode=HISTORY_MODE)
        print('本次执行结束，共写入 {} 条行业动量记录。'.format(len(momentum_df)))
        print(momentum_df.tail(15).to_string(index=False))
    except Exception as exc:
        # API/driver exception text can contain credentials; expose only the type.
        print('采集失败；此前已提交结果保留，失败日未认证。请修复源/连接后按原参数续跑。')
        raise RuntimeError('行业动量采集停止，异常类型：{}'.format(type(exc).__name__)) from None
