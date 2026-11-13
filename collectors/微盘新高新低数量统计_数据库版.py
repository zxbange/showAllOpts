# -*- coding: utf-8 -*-
"""聚宽研究：微盘新高新低单文件采集。直接粘贴或 %run -i 本文件。"""
import builtins
import datetime
import json
import hashlib
import math
import re
import time
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import quote, quote_plus
import jqdata as jq
import pandas as pd
from sqlalchemy import Column, Date, DateTime, Integer, String, Text, create_engine, func
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from mysql_connector import get_database_config, get_database_options, mysql_drivername, engine_options
DB_CONFIG = get_database_config()
DB_OPTIONS = get_database_options()




STRATEGY_CONFIG = {
    '微盘新高新低': {
        'start_date': None,
        'end_date': None,  # 每次运行取北京时间当天；16:00 前只采集截至昨日的数据。
        'micro_num': 400,
        'initial_days': 180,  # 仅普通模式默认窗口；完整恢复请调用 run_history()。
        'history_mode': False,  # 与批量版一致，默认近期增量；完整历史须显式开启。
        'batch_size': 500,
        'price_batch_size': 100,
    },
}


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

def _require_append(skip_existing):
    if skip_existing is not True:
        raise ValueError('此采集仅支持只新增，不允许关闭 skip_existing。')


def _save_record_set(Session, model, records, filters, key_fields, skip_existing=True):
    """只新增调用方限定范围内尚不存在的主键；已有记录保持原值。"""
    _require_append(skip_existing)
    if not records:
        return []
    with session_scope(Session) as session:
        rows = session.query(model).filter(*filters)
        keys = {tuple(row) for row in rows.with_entities(*(getattr(model, key) for key in key_fields)).all()}
        records = [r for r in records if tuple(r[key] for key in key_fields) not in keys]
        if not records:
            return []
        session.add_all([model(**record) for record in records])
        session.commit()
        return records

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


def strategy_settings(name):
    """每次运行取得配置快照，先检查控制分页/窗口的参数以免出现空循环。"""
    settings = deepcopy(STRATEGY_CONFIG[name])
    for key in ('retention_trade_days', 'batch_size', 'initial_trade_days', 'window_size', 'page_size', 'initial_days', 'price_batch_size'):
        value = settings.get(key)
        if key in settings and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise ValueError('{} 的 {} 必须为正整数。'.format(name, key))
    return settings


def _closed_day(end, now):
    """日行情在北京时间16点后使用当天；交易日仍由各策略的日历校验。"""
    return end - datetime.timedelta(days=1) if end == now.date() and now.hour < 16 else end


def china_now():
    """所有策略统一按北京时间确定运行日期。"""
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.datetime.strptime(str(value)[:10], '%Y-%m-%d').date()


def connect_database(config):
    """统一建立连接、有限重试；调用策略负责最终释放 engine。"""
    opts = DB_OPTIONS
    if (not isinstance(opts['retry_count'], int) or opts['retry_count'] < 1
            or opts['retry_interval'] < 0 or opts['connect_timeout'] <= 0
            or opts['pool_recycle'] <= 0):
        raise ValueError('DB_OPTIONS 的次数、超时须为正，重试间隔不可为负。')
    url = getattr(URL, 'create', URL)(
        drivername=mysql_drivername(DB_OPTIONS['driver']), host=config['host'], port=config['port'],
        username=config['dbuser'], password=config['dbpassword'], database=config['dbname'],
        query={'charset': DB_OPTIONS['charset']})
    engine = create_engine(url, **engine_options(DB_OPTIONS))
    try:
        for attempt in range(opts['retry_count']):
            try:
                with engine.connect():
                    pass
                return engine
            except Exception:
                if attempt + 1 == opts['retry_count']:
                    raise RuntimeError('MySQL 连接失败，请检查配置、网络和白名单。') from None
                time.sleep(opts['retry_interval'])
    except BaseException:
        engine.dispose()
        raise


@contextmanager
def database_scope(base, config):
    """各策略独立建表及持有连接，正常结束、异常、中断时均释放。"""
    engine = connect_database(config)
    try:
        for table in base.metadata.tables.values():
            table.dialect_options['mysql']['engine'] = 'InnoDB'
            table.dialect_options['mysql']['charset'] = 'utf8mb4'
        base.metadata.create_all(engine)
        yield sessionmaker(bind=engine)
    finally:
        engine.dispose()


@contextmanager
def session_scope(Session):
    """统一回滚和关闭；提交仍由各策略显式决定，查询不触发写入。"""
    session = Session()
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def _safe_error(exc, config):
    message = re.sub(r'(?i)[a-z][a-z0-9+.-]*://[^\s]+', '[连接地址已隐藏]', str(exc))
    message = re.sub(r"(?i)(password|passwd|pwd|token|secret)\s*[:=]\s*('.*?'|\".*?\"|[^\s,;]+)",
                      r'\1=[已隐藏]', message)
    secrets = set()
    for key in ('host', 'dbname', 'dbuser', 'dbpassword'):
        value = str(config[key])
        if value:
            secrets.update((value, quote(value, safe=''), quote_plus(value)))
    for value in sorted(secrets, key=len, reverse=True):
        message = message.replace(value, '[已隐藏]')
    return '{}: {}'.format(type(exc).__name__, ' '.join(message.split())[:400])


def _breadth_strategy(_apis, _config):
    """微盘 400 新高新低：每日动态选股，一次历史行情计算四个周期。"""
    settings = strategy_settings('微盘新高新低')
    get_all_securities = _apis.get('get_all_securities')
    get_trade_days = _apis.get('get_trade_days')
    get_all_trade_days = _apis.get('get_all_trade_days')
    get_extras = _apis.get('get_extras')
    get_fundamentals = _apis.get('get_fundamentals')
    get_price = _apis.get('get_price')
    query = _apis.get('query')
    valuation = _apis.get('valuation')
    PERIODS = (5, 10, 20, 30)
    START_DATE = settings['start_date']
    END_DATE = settings['end_date']
    MICRO_NUM = settings['micro_num']
    INITIAL_DAYS = settings['initial_days']
    BATCH_SIZE = settings['batch_size']
    PRICE_BATCH_SIZE = settings['price_batch_size']
    Base = declarative_base()

    class MicrocapBreadth(Base):
        __tablename__ = 'microcap_breadth'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        series_id = Column(String(64), primary_key=True)
        trade_date = Column(Date, primary_key=True)
        sample_count = Column(Integer, nullable=False)
        high_5 = Column(Integer, nullable=False)
        low_5 = Column(Integer, nullable=False)
        high_10 = Column(Integer, nullable=False)
        low_10 = Column(Integer, nullable=False)
        high_20 = Column(Integer, nullable=False)
        low_20 = Column(Integer, nullable=False)
        high_30 = Column(Integer, nullable=False)
        low_30 = Column(Integer, nullable=False)
        member_codes = Column(Text, nullable=False)
        updated_at = Column(DateTime, nullable=False)

    RESULT_COLUMNS = [c.name for c in MicrocapBreadth.__table__.columns if c.name != 'member_codes']

    def series_key(micro_num):
        return 'micro{}_hl_v1'.format(micro_num)

    def trade_calendar(start=None, end=None, count=None):
        args = {'end_date': end}
        if count is None:
            args['start_date'] = start
        else:
            args['count'] = count
        return _checked_trade_days(get_trade_days(**args), start, end, count,
            error='交易日历缺失、重复或越界。', missing_error='交易日历返回空对象。')

    def choose_stocks(day, micro_num, history_mode=False):
        securities = get_all_securities(types=['stock'], date=day)
        if securities is None or securities.empty or 'start_date' not in securities.columns:
            raise ValueError('股票列表为空或缺少上市日期。')
        if history_mode:
            if 'end_date' not in securities.columns:
                raise ValueError('历史股票列表缺少退市日期，不能验证当日成员。')
            for code, row in securities.iterrows():
                if as_date(row['start_date']) > day or as_date(row['end_date']) < day:
                    raise ValueError('历史股票列表包含未上市或已退市的成员。')
            if len(securities.index) != len(set(str(code) for code in securities.index)):
                raise ValueError('历史股票列表包含重复代码。')
        cutoff = day - datetime.timedelta(days=240)
        codes = sorted(str(code) for code, row in securities.iterrows()
                       if not str(code).startswith(('68', '4', '8')) and as_date(row['start_date']) < cutoff)
        if len(codes) != len(set(codes)):
            raise ValueError('股票列表包含重复代码。')
        eligible = []
        for offset in range(0, len(codes), BATCH_SIZE):
            batch = codes[offset:offset+BATCH_SIZE]
            st = get_extras('is_st', batch, start_date=day, end_date=day, df=True)
            if (st is None or len(st.index) != 1 or as_date(st.index[0]) != day
                    or set(st.columns) != set(batch) or len(st.columns) != len(batch)):
                raise ValueError('ST 状态未完整返回，停止该日采集。')
            for code in batch:
                flag = st.iloc[0][code]
                if getattr(pd, 'isnull', getattr(pd, 'isna', None))(flag) or flag not in (True, False):
                    raise ValueError('ST 状态无效。')
                if not flag:
                    eligible.append(code)
        caps = {}
        for offset in range(0, len(eligible), BATCH_SIZE):
            batch = eligible[offset:offset+BATCH_SIZE]
            frame = get_fundamentals(query(valuation.code, valuation.market_cap).filter(
                valuation.code.in_(batch)), date=day)
            if frame is None or not {'code', 'market_cap'}.issubset(frame.columns):
                raise ValueError('市值数据字段缺失。')
            rows = frame.to_dict('records')
            if len(rows) != len(batch) or {str(r['code']) for r in rows} != set(batch):
                raise ValueError('市值数据不完整或重复，不能确定真实的微盘股名单。')
            for row in rows:
                try:
                    cap = float(row['market_cap'])
                except (TypeError, ValueError):
                    raise ValueError('市值无效。') from None
                if not math.isfinite(cap) or cap <= 0:
                    raise ValueError('市值缺失或非正数。')
                caps[str(row['code'])] = cap
        selected = sorted(caps, key=lambda code: (caps[code], code))[:micro_num]
        if len(selected) != micro_num:
            if history_mode:
                raise BreadthHistoryUnavailable('verified_insufficient_pool_{}_of_{}'.format(len(selected), micro_num))
            raise ValueError('可选股票不足 {} 只。'.format(micro_num))
        return selected

    def high_low_counts(closes):
        if len(closes) != 31 or builtins.any(not math.isfinite(float(p)) or p <= 0 for p in closes):
            raise ValueError('新高新低需要连续 31 个有效交易日收盘价。')
        current = closes[-1]
        return {key: int(hit) for period in PERIODS for key, hit in (
            ('high_{}'.format(period), current >= builtins.max(closes[-period-1:-1])),
            ('low_{}'.format(period), current <= builtins.min(closes[-period-1:-1])),
        )}

    def count_prices(frame, codes, calendar):
        if frame is None or not {'code', 'time', 'close'}.issubset(frame.columns):
            raise ValueError('历史行情字段缺失。')
        rows = frame.to_dict('records')
        if len(rows) != len(codes)*len(calendar):
            raise ValueError('历史行情未完整返回，拒绝将缺失记录按零统计。')
        prices = {code: {} for code in codes}
        for row in rows:
            code, day = str(row['code']), as_date(row['time'])
            if code not in prices or day not in calendar or day in prices[code]:
                raise ValueError('行情包含重复、越界日期或股票。')
            try:
                price = float(row['close'])
            except (TypeError, ValueError):
                raise ValueError('收盘价无效。') from None
            if not math.isfinite(price) or price <= 0:
                raise ValueError('收盘价缺失或非正数。')
            prices[code][day] = price
        counts = {'{}_{}'.format(side, period): 0 for period in PERIODS for side in ('high', 'low')}
        for code in codes:
            if set(prices[code]) != set(calendar):
                raise ValueError('个股历史行情缺失。')
            for key, value in high_low_counts([prices[code][day] for day in calendar]).items():
                counts[key] += value
        return counts

    def calculate_day(day, micro_num=MICRO_NUM, history_window=None):
        codes = choose_stocks(day, micro_num, history_mode=history_window is not None)
        calendar = trade_calendar(end=day, count=31)
        if history_window is not None and calendar != history_window:
            raise ValueError('31 日窗口与完整交易日历不一致，不能视为历史预热不足。')
        if calendar[-1] != day:
            raise ValueError('采集日期不是交易日。')
        counts = {'{}_{}'.format(side, period): 0 for period in PERIODS for side in ('high', 'low')}
        for offset in range(0, len(codes), PRICE_BATCH_SIZE):
            batch = codes[offset:offset+PRICE_BATCH_SIZE]
            frame = get_price(batch, start_date=calendar[0], end_date=day, frequency='daily',
                              fields=['close'], fq='pre', skip_paused=False, panel=False)
            for key, value in count_prices(frame, batch, calendar).items():
                counts[key] += value
        return dict(counts, series_id=series_key(micro_num), trade_date=day, sample_count=micro_num,
                    member_codes=json.dumps(codes), updated_at=china_now())

    def last_date(Session, series_id):
        with session_scope(Session) as session:
            return session.query(func.max(MicrocapBreadth.trade_date)).filter_by(series_id=series_id).scalar()

    def validate_record(record):
        size = record['sample_count']
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ValueError('样本数量须为正整数。')
        if record['series_id'] != series_key(size):
            raise ValueError('序列与样本数量不一致。')
        members = json.loads(record['member_codes'])
        if not isinstance(members, list) or len(members) != size or len(set(members)) != size:
            raise ValueError('成分股名单与样本数量不一致。')
        for period in PERIODS:
            for side in ('high', 'low'):
                value = record['{}_{}'.format(side, period)]
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= size:
                    raise ValueError('新高新低数量超出范围。')
                if period > 5 and value > record['{}_5'.format(side)]:
                    raise ValueError('长期新高新低数量不能超过 5 日数量。')
    def save_day(Session, record):
        validate_record(record)
        _install_result_journal(Session, (MicrocapBreadth,), 'microcap-breadth')
        return bool(_save_record_set(Session, MicrocapBreadth, [record],
            (MicrocapBreadth.series_id == record['series_id'], MicrocapBreadth.trade_date == record['trade_date']),
            ('series_id', 'trade_date')))

    def run(start_date=START_DATE, end_date=END_DATE, micro_num=MICRO_NUM, skip_existing=True,
            history_mode=False, max_days=None, retry_unavailable=False):
        if history_mode:
            return run_history(start_date, end_date, micro_num, skip_existing, max_days, retry_unavailable)
        if skip_existing is not True:
            raise ValueError('微盘新高新低仅支持只新增，不覆盖已入库快照。')
        if isinstance(micro_num, bool) or not isinstance(micro_num, int) or micro_num <= 0:
            raise ValueError('micro_num 必须为正整数。')
        now = china_now()
        end = as_date(end_date) if end_date is not None else now.date()
        initial = as_date(start_date) if start_date is not None else end-datetime.timedelta(days=INITIAL_DAYS)
        if end > now.date() or initial > end:
            raise ValueError('日期范围无效或结束日期晚于今天。')
        end = _closed_day(end, now)
        output = []
        with database_scope(Base, _config) as Session:
            _install_result_journal(Session, (MicrocapBreadth,), 'microcap-breadth')
            latest = last_date(Session, series_key(micro_num))
            if latest and latest > now.date():
                raise ValueError('数据库最新日期晚于今天。')
            start = latest+datetime.timedelta(days=1) if latest else initial
            if start > end:
                return pd.DataFrame(columns=RESULT_COLUMNS)
            for day in trade_calendar(start=start, end=end):
                record = calculate_day(day, micro_num)
                # 每日一个事务；任何缺失或失败立即停止，不越过缺失日续传。
                if save_day(Session, record):
                    output.append({key: record[key] for key in RESULT_COLUMNS})
        return pd.DataFrame(output, columns=RESULT_COLUMNS)
    HISTORY_SOURCE_BASIS = 'joinquant_visible_revised_not_original_pit_v1'

    class BreadthHistoryUnavailable(ValueError):
        """A verified insufficient pool or warmup in the initial prefix only."""

    class BreadthHistoryStatus(Base):
        __tablename__ = 'microcap_breadth_history_status'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        series_id = Column(String(64), primary_key=True)
        trade_date = Column(Date, primary_key=True)
        status = Column(String(32), nullable=False)
        reason = Column(String(128), nullable=False)
        source_basis = Column(String(64), nullable=False)
        payload_digest = Column(String(64), nullable=False)
        updated_at = Column(DateTime, nullable=False)

    def history_calendar(start, end):
        if not callable(get_all_trade_days):
            raise ValueError('历史恢复需要聚宽 get_all_trade_days，以验证最早边界和全部日期。')
        full = _checked_trade_days(get_all_trade_days())
        if not full:
            raise ValueError('完整交易日历为空。')
        available = [day for day in full if day <= end]
        if not available:
            raise ValueError('结束日期前没有可验证交易日。')
        first = available[0] if start is None else as_date(start)
        expected = [day for day in available if day >= first]
        if trade_calendar(start=first, end=end) != expected:
            raise ValueError('范围交易日历与完整交易日历不一致。')
        return available, expected

    def history_digest(record):
        values = [record['series_id'], record['sample_count'], sorted(json.loads(record['member_codes']))]
        values += [record['{}_{}'.format(side, period)] for period in PERIODS for side in ('high', 'low')]
        return hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(',', ':')).encode('utf-8')).hexdigest()

    def history_existing(Session, day, series_id, retry_unavailable=False):
        with session_scope(Session) as session:
            record = session.query(MicrocapBreadth).filter_by(trade_date=day, series_id=series_id).first()
            status = session.query(BreadthHistoryStatus).filter_by(trade_date=day, series_id=series_id).first()
            if status is None or status.source_basis != HISTORY_SOURCE_BASIS:
                return None
            if record is None:
                if status.status == 'unavailable_prefix' and not retry_unavailable:
                    return 'unavailable_prefix'
                return None
            values = {column.name: getattr(record, column.name) for column in MicrocapBreadth.__table__.columns}
            if status.status == 'complete' and status.payload_digest == history_digest(values):
                return 'complete'
            return None

    def save_history_day(Session, day, micro_num, record=None, reason=None):
        """One append-only daily transaction includes its reconstruction receipt."""
        series_id = series_key(micro_num)
        _install_result_journal(Session, (MicrocapBreadth,), 'microcap-breadth')
        with session_scope(Session) as session:
            previous = session.query(MicrocapBreadth).filter_by(trade_date=day, series_id=series_id).first()
            added = False
            if record is not None:
                if record['trade_date'] != day or record['series_id'] != series_id or record['sample_count'] != micro_num:
                    raise ValueError('历史结果日期、序列或样本数量不一致。')
                validate_record(record)
                if previous is not None:
                    values = {column.name: getattr(previous, column.name) for column in MicrocapBreadth.__table__.columns}
                    if history_digest(values) != history_digest(record):
                        raise ValueError('已存微盘结果与当前历史重建值冲突，保留原值并停止。')
                else:
                    session.add(MicrocapBreadth(**record))
                    added = True
            elif previous is not None:
                raise ValueError('已有微盘结果不能标记为历史不可用。')
            status = session.query(BreadthHistoryStatus).filter_by(trade_date=day, series_id=series_id).first()
            if status is None:
                status = BreadthHistoryStatus(trade_date=day, series_id=series_id)
                session.add(status)
            status.status = 'complete' if record is not None else 'unavailable_prefix'
            status.reason = '' if record is not None else reason
            status.source_basis = HISTORY_SOURCE_BASIS
            status.payload_digest = history_digest(record) if record is not None else ''
            status.updated_at = china_now()
            session.commit()
            return added

    def run_history(start_date=None, end_date=None, micro_num=MICRO_NUM, skip_existing=True,
                    max_days=None, retry_unavailable=False):
        if skip_existing is not True:
            raise ValueError('历史恢复仅补缺，不覆盖已有结果。')
        if isinstance(micro_num, bool) or not isinstance(micro_num, int) or micro_num <= 0:
            raise ValueError('micro_num 必须为正整数。')
        if max_days is not None and (isinstance(max_days, bool) or not isinstance(max_days, int) or max_days <= 0):
            raise ValueError('max_days 必须为正整数或 None。')
        now = china_now()
        end = as_date(end_date) if end_date is not None else now.date()
        if end > now.date() or (start_date is not None and as_date(start_date) > end):
            raise ValueError('历史恢复日期范围无效。')
        end = _closed_day(end, now)
        if start_date is not None and as_date(start_date) > end:
            return pd.DataFrame(columns=RESULT_COLUMNS)
        full, days = history_calendar(start_date, end)
        output, attempted, available_started = [], 0, False
        with database_scope(Base, _config) as Session:
            _install_result_journal(Session, (MicrocapBreadth,), 'microcap-breadth')
            # A requested suffix after previously successful dates is not a prefix.
            with session_scope(Session) as session:
                first_saved = session.query(func.min(MicrocapBreadth.trade_date)).filter_by(series_id=series_key(micro_num)).scalar()
            for day in days:
                state = history_existing(Session, day, series_key(micro_num), retry_unavailable)
                if state == 'complete':
                    available_started = True
                    continue
                if state == 'unavailable_prefix':
                    if first_saved is not None and day >= first_saved:
                        raise ValueError('历史不可用标记位于已有成功日期之后，须人工核查或重试。')
                    continue
                if max_days is not None and attempted >= max_days:
                    break
                attempted += 1
                known = [value for value in full if value <= day]
                try:
                    if len(known) < 31:
                        raise BreadthHistoryUnavailable('verified_calendar_warmup_31')
                    record = calculate_day(day, micro_num, history_window=known[-31:])
                except BreadthHistoryUnavailable as exc:
                    if available_started or (first_saved is not None and day >= first_saved):
                        raise ValueError('{} 成功历史之后股票池不足，停止而不跳过。'.format(day)) from None
                    save_history_day(Session, day, micro_num, reason=str(exc))
                    print('{} 历史前缀不可计算：{}；未写入零值。'.format(day, str(exc)))
                    continue
                if save_history_day(Session, day, micro_num, record=record):
                    output.append({key: record[key] for key in RESULT_COLUMNS})
                available_started = True
                print('{} 新高新低历史恢复已验证；非原始时点快照。'.format(day))
            with session_scope(Session) as session:
                unavailable_count = session.query(BreadthHistoryStatus).filter(
                    BreadthHistoryStatus.series_id == series_key(micro_num),
                    BreadthHistoryStatus.status == 'unavailable_prefix',
                    BreadthHistoryStatus.trade_date.in_(days)).count()
            print('本次检查 {} 个待计算日；新增 {} 天；范围内 {} 个历史前缀日期不可计算，未当作完成或零值。'.format(
                attempted, len(output), unavailable_count))
            if max_days is not None and attempted >= max_days:
                print('已达到 max_days；按相同参数重跑会继续其余未认证日期。')
        return pd.DataFrame(output, columns=RESULT_COLUMNS)

    return SimpleNamespace(**locals())


_API_NAMES = ('get_all_securities', 'get_all_trade_days', 'get_trade_days', 'get_price', 'get_extras',
              'get_fundamentals', 'query', 'valuation')


def run(**options):
    apis = {key: globals().get(key, getattr(jq, key, getattr(builtins, key, None))) for key in _API_NAMES}
    return _breadth_strategy(apis, DB_CONFIG).run(**options)


def run_history(**options):
    options['history_mode'] = True
    return run(**options)


if __name__ == '__main__':
    try:
        breadth_df = run(history_mode=STRATEGY_CONFIG['微盘新高新低']['history_mode'])
        print('微盘新高新低：本次新增 {} 个交易日。'.format(len(breadth_df)))
        if len(breadth_df):
            print(breadth_df.tail(10).to_string(index=False))
    except Exception as exc:
        raise RuntimeError(_safe_error(exc, DB_CONFIG)) from None
