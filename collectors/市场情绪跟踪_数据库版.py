# -*- coding: utf-8 -*-
"""聚宽研究环境：全市场站上 MA20 股票占比，保留近三年，重复运行只补缺口。"""
import builtins
import datetime
import re
import sys
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import quote, quote_plus
import jqdata as jq
import pandas as pd
import numpy as np
from sqlalchemy import Column, Date, DateTime, Integer, Numeric, create_engine
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

DB_CONFIG = {'host': 'your_mysql_host', 'port': 3306, 'dbname': 'your_database',
             'dbuser': 'your_database_user', 'dbpassword': 'your_password'}
DB_OPTIONS = {'connect_timeout': 10, 'retry_count': 5, 'retry_interval': 10, 'pool_recycle': 3600}
STRATEGY_CONFIG = {'市场情绪跟踪': {'end_date': None, 'batch_size': 300, 'window_size': 40}}
PROGRESS_ENABLED = True
PROGRESS_INTERVAL_SECONDS = 5.0
_PROGRESS_LOCAL = threading.local()

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


class _ConsoleProgress:
    """仅后台刷新文字；采集与数据库操作始终在主线程串行执行。"""
    def __init__(self, name, index, total, stream, interval):
        self.title = '【{}/{} {}】'.format(index, total, name)
        self.stream, self.interval = stream, interval
        self.started = time.monotonic()
        self.last_refresh, self.width = 0.0, 0
        self.frames = []
        self.note = '初始化策略'
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.thread = None
        self.writable = True

    def render(self, force=False, ending=None):
        with self.lock:
            now = time.monotonic()
            if not self.writable or (not force and now - self.last_refresh < self.interval):
                return
            parts = []
            for frame in self.frames:
                total, done = frame['total'], frame['done']
                filled = int(20 * done / total) if total else 0
                bar = '[' + '#' * filled + '-' * (20 - filled) + ']'
                value = '{}/{} {}%'.format(done, total, 100*done//total) if total else '0/0 无待处理项'
                parts.append('{} {} {} {}'.format(frame['label'], bar, value, frame['item']))
            line = '{} {} | {} | 已用 {:.0f}s'.format(
                self.title, ' > '.join(parts) or ('[结束]' if ending else '[处理中]'), self.note, now-self.started)
            if ending:
                line += ' | ' + ending
            try:
                self.stream.write('\r' + line + ' ' * builtins.max(0, self.width-len(line)) + ('\n' if ending else ''))
                self.stream.flush()
            except Exception:
                # 显示失败不改变采集结果，也不打印可能含连接信息的异常。
                self.writable = False
            self.width, self.last_refresh = len(line), now

    def heartbeat(self):
        while not self.stopped.wait(self.interval):
            self.render(force=True)

    def start(self):
        self.render(force=True)
        self.thread = threading.Thread(target=self.heartbeat, name='collector-progress')
        self.thread.daemon = True
        try:
            self.thread.start()
        except Exception:
            # 某些研究容器限制线程：退化为任务推进时刷新，不阻断采集。
            self.thread = None
            self.note = '自动定时刷新不可用，按任务推进刷新'
            self.render(force=True)

    def close(self, ending):
        self.stopped.set()
        if self.thread is not None and self.thread.ident is not None:
            self.thread.join()
        self.render(force=True, ending=ending)

@contextmanager
def _progress_scope(name, index, total):
    progress = None
    ending = '完成'
    _PROGRESS_LOCAL.output = sys.stdout
    try:
        if PROGRESS_ENABLED:
            progress = _ConsoleProgress(name, index, total, sys.stdout, PROGRESS_INTERVAL_SECONDS)
            _PROGRESS_LOCAL.current = progress
            progress.start()
        yield
    except BaseException as exc:
        ending = '已中断' if isinstance(exc, KeyboardInterrupt) else '未完成'
        raise
    finally:
        _PROGRESS_LOCAL.current = None
        _PROGRESS_LOCAL.output = None
        if progress is not None:
            progress.close(ending)

def _progress_message(message):
    """Keep brief collection-scope messages visible through the batch quiet stream."""
    progress = getattr(_PROGRESS_LOCAL, 'current', None)
    stream = getattr(_PROGRESS_LOCAL, 'output', None) or sys.stdout
    if progress is not None:
        with progress.lock:
            stream.write('\r' + ' ' * progress.width + '\r' + message + '\n')
            stream.flush()
            progress.width = 0
            progress.render(force=True)
    else:
        print(message, file=stream, flush=True)

def _progress_note(message):
    progress = getattr(_PROGRESS_LOCAL, 'current', None)
    if progress is not None:
        with progress.lock:
            progress.note = message
            progress.render()

def _progress_items(items, label, describe=str):
    """仅循环体正常返回后计为已处理；不是新增入库条数。"""
    progress = getattr(_PROGRESS_LOCAL, 'current', None)
    if progress is None:
        yield from items
        return
    frame = dict(label=label, total=len(items), done=0, item='')
    with progress.lock:
        progress.frames.append(frame)
        progress.note = '准备处理'
        progress.render(force=True)
    try:
        for item in items:
            with progress.lock:
                frame['item'] = describe(item)
                progress.note = '采集中'
                progress.render(force=frame['done'] == 0)
            yield item
            with progress.lock:
                frame['done'] += 1
                progress.note = '本项已处理'
                progress.render(force=frame['done'] == frame['total'])
    finally:
        with progress.lock:
            progress.frames.remove(frame)

def strategy_settings(name):
    """每次运行取得配置快照，先检查控制分页/窗口的参数以免出现空循环。"""
    settings = deepcopy(STRATEGY_CONFIG[name])
    for key in ('retention_trade_days', 'batch_size', 'initial_trade_days', 'window_size', 'page_size', 'initial_days', 'price_batch_size'):
        value = settings.get(key)
        if key in settings and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise ValueError('{} 的 {} 必须为正整数。'.format(name, key))
    return settings

def china_now():
    """所有策略统一按北京时间确定运行日期。"""
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)

def as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.datetime.strptime(str(value)[:10], '%Y-%m-%d').date()

def _closed_day(end, now):
    """日行情在北京时间16点后使用当天；交易日仍由各策略的日历校验。"""
    return end - datetime.timedelta(days=1) if end == now.date() and now.hour < 16 else end

def connect_database(config):
    """统一建立连接、有限重试；调用策略负责最终释放 engine。"""
    opts = DB_OPTIONS
    if (not isinstance(opts['retry_count'], int) or opts['retry_count'] < 1
            or opts['retry_interval'] < 0 or opts['connect_timeout'] <= 0
            or opts['pool_recycle'] <= 0):
        raise ValueError('DB_OPTIONS 的次数、超时须为正，重试间隔不可为负。')
    url = getattr(URL, 'create', URL)(
        drivername='mysql+mysqldb', host=config['host'], port=config['port'],
        username=config['dbuser'], password=config['dbpassword'], database=config['dbname'],
        query={'charset': 'utf8mb4'})
    engine = create_engine(url, echo=False, pool_size=1, max_overflow=0,
                           pool_pre_ping=True, pool_recycle=opts['pool_recycle'],
                           connect_args={'connect_timeout': opts['connect_timeout']})
    try:
        for attempt in range(opts['retry_count']):
            _progress_note('连接数据库（第 {} / {} 次）'.format(attempt + 1, opts['retry_count']))
            try:
                with engine.connect():
                    pass
                return engine
            except Exception:
                if attempt + 1 == opts['retry_count']:
                    raise RuntimeError('MySQL 连接失败，请检查配置、网络和白名单。') from None
                _progress_note('连接暂未成功，等待重试')
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
        _progress_note('检查数据库表结构')
        base.metadata.create_all(engine)
        _progress_note('读取已有数据与增量范围')
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

def _sentiment_strategy(_apis, _config):
    """全市场 MA20 广度；分窗口复用行情，只保存每日汇总。"""
    settings = strategy_settings('市场情绪跟踪')
    get_all_securities = _apis.get('get_all_securities')
    get_trade_days = _apis.get('get_trade_days')
    get_price = _apis.get('get_price')
    Base = declarative_base()
    RESULT_COLUMNS = ['trade_date', 'ratio', 'above_count', 'eligible_count', 'universe_count', 'updated_at']

    class MarketSentiment(Base):
        __tablename__ = 'market_sentiment'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        trade_date = Column(Date, primary_key=True)
        ratio = Column(Numeric(18, 10), nullable=False)
        above_count = Column(Integer, nullable=False)
        eligible_count = Column(Integer, nullable=False)
        universe_count = Column(Integer, nullable=False)
        updated_at = Column(DateTime, nullable=False)

    def three_years_ago(day):
        try:
            return day.replace(year=day.year - 3)
        except ValueError:  # 2 月 29 日对应三年前的 2 月 28 日。
            return day.replace(year=day.year - 3, day=28)

    def checked_days(values, start, end):
        return _checked_trade_days(values, start, end,
            error='交易日历越界、重复或乱序，取消采集。', missing_error='交易日历未返回，取消采集。')

    def security_dates():
        # date=None 获取含已退市股票的完整资料，再按各历史日期筛选，避免幸存者偏差。
        frame = get_all_securities(types=['stock'], date=None)
        if frame is None or frame.empty or not {'start_date', 'end_date'}.issubset(frame.columns):
            raise ValueError('股票上市/退市资料缺失，取消采集。')
        if frame.index.has_duplicates:
            raise ValueError('股票资料代码重复，取消采集。')
        dates = {}
        for code, row in frame.iterrows():
            if not isinstance(code, str):
                raise ValueError('股票代码无效，取消采集。')
            # 若接口提供 B 股，排除其六位代码；不排除 ST 或任何 A 股板块。
            if code.split('.')[0].startswith(('200', '900')):
                continue
            if pd.isna(row['start_date']) or pd.isna(row['end_date']):
                raise ValueError('股票上市/退市日期缺失，取消采集。')
            start, end = as_date(row['start_date']), as_date(row['end_date'])
            if start > end:
                raise ValueError('股票上市/退市日期无效，取消采集。')
            dates[code] = (start, end)
        if not dates:
            raise ValueError('未取得 A 股股票池，取消采集。')
        return dates

    def batch_counts(frame, codes, calendar, metadata):
        """校验上市期间数据完整，再计算当日收盘严格高于含当日的 MA20。"""
        if frame is None or frame.empty or not {'code', 'time', 'close', 'paused'}.issubset(frame.columns):
            raise ValueError('行情为空或缺少 close/paused/code/time，取消当前窗口入库。')
        frame = frame[['code', 'time', 'close', 'paused']].copy()
        frame['day'] = [as_date(value) for value in frame['time']]
        if (frame.duplicated(['day', 'code']).any()
                or not set(frame['code']).issubset(codes)
                or not set(frame['day']).issubset(calendar)):
            raise ValueError('行情存在重复或越界数据，取消当前窗口入库。')
        close = frame.pivot(index='day', columns='code', values='close').reindex(index=calendar, columns=codes).astype(float)
        paused = frame.pivot(index='day', columns='code', values='paused').reindex(index=calendar, columns=codes).astype(float)
        listed = np.array([[metadata[code][0] <= day <= metadata[code][1] for code in codes] for day in calendar])
        values, pauses = close.values, paused.values
        valid_price = np.isfinite(values) & (values > 0)
        valid_status = (pauses == 0) | (pauses == 1)
        if np.any(listed & ~(valid_price & valid_status)):
            raise ValueError('上市期间行情或停牌状态缺失，拒绝缩小分母写入。')
        # 历史停牌日沿用平台填充收盘价；当日停牌不计入当日分母。
        close = close.where(listed)
        mean = close.rolling(window=20, min_periods=20).mean().values
        eligible = listed & valid_price & np.isfinite(mean) & (pauses == 0)
        # 防止相等价格因浮点求均值的尾差被当作站上均线。
        above = eligible & ((values - mean) > np.maximum(np.abs(values), np.abs(mean)) * 1e-12)
        return np.column_stack((listed.sum(axis=1), eligible.sum(axis=1), above.sum(axis=1)))

    def collect_window(days, pending, metadata, batch_size):
        warmup = checked_days(get_trade_days(end_date=days[0], count=20), datetime.date(1990, 1, 1), days[0])
        if len(warmup) != 20 or warmup[-1] != days[0]:
            raise ValueError('MA20 预热交易日不足或错位，取消当前窗口入库。')
        calendar = warmup[:-1] + days
        codes = sorted(code for code, (start, end) in metadata.items() if start <= days[-1] and end >= days[0])
        if not codes:
            raise ValueError('历史股票池为空，取消当前窗口入库。')
        counts = np.zeros((len(calendar), 3), dtype=np.int64)
        offsets = list(range(0, len(codes), batch_size))
        for offset in _progress_items(offsets, '行情分批', lambda i: '{} / {} 只'.format(i, len(codes))):
            batch = codes[offset:offset + batch_size]
            _progress_note('读取 {} 至 {} 的前复权收盘价和停牌状态'.format(calendar[0], calendar[-1]))
            frame = get_price(security=batch, start_date=calendar[0], end_date=calendar[-1], frequency='daily',
                              fields=['close', 'paused'], fq='pre', skip_paused=False, panel=False)
            counts += batch_counts(frame, batch, calendar, metadata)
            del frame
        records = []
        stamp = china_now()
        for day, (universe, eligible, above) in zip(calendar, counts):
            if day not in pending:
                continue
            if eligible <= 0:
                raise ValueError('{} 没有有效 MA20 样本，拒绝写入零值。'.format(day))
            records.append(dict(trade_date=day, ratio=(Decimal(int(above)) / Decimal(int(eligible))).quantize(Decimal('0.0000000001')),
                                above_count=int(above), eligible_count=int(eligible), universe_count=int(universe), updated_at=stamp))
        return records

    def save_records(Session, records):
        _install_result_journal(Session, (MarketSentiment,), 'market-sentiment')
        dates = [record['trade_date'] for record in records]
        return _save_record_set(Session, MarketSentiment, records,
            (MarketSentiment.trade_date.in_(dates),), ('trade_date',))

    def run(end_date=settings['end_date'], start_date=None, batch_size=settings['batch_size'],
            window_size=settings['window_size'], skip_existing=True):
        if skip_existing is not True:
            raise ValueError('市场情绪仅新增，已有日期不覆盖。')
        for name, value in (('batch_size', batch_size), ('window_size', window_size)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError('{} 必须是正整数。'.format(name))
        now = china_now()
        end = now.date() if end_date is None else as_date(end_date)
        if end > now.date():
            raise ValueError('结束日期不能晚于今天。')
        end = _closed_day(end, now)
        cutoff = three_years_ago(now.date())
        start = cutoff if start_date is None else builtins.max(cutoff, as_date(start_date))
        if start > end:
            raise ValueError('采集范围须在最近三年且开始日期不晚于结束日期。')
        days = checked_days(get_trade_days(start_date=start, end_date=end), start, end)
        if not days:
            raise ValueError('指定区间未返回交易日，未清理或写入数据。')
        results = []
        with database_scope(Base, _config) as Session:
            _install_result_journal(Session, (MarketSentiment,), 'market-sentiment')
            with session_scope(Session) as session:
                removed = _result_bulk(session.query(MarketSentiment).filter(MarketSentiment.trade_date < cutoff))
                session.commit()
                existing = {as_date(row[0]) for row in session.query(MarketSentiment.trade_date).filter(
                    MarketSentiment.trade_date >= start, MarketSentiment.trade_date <= end).all()}
            pending = set(days) - existing
            _progress_message('市场情绪：近三年缺失 {} 个交易日；已清理过期 {} 行。'.format(len(pending), removed))
            if not pending:
                _progress_note('已有日期完整，跳过股票资料和行情查询')
                return pd.DataFrame(columns=RESULT_COLUMNS)
            metadata = security_dates()
            # 沿完整日历分窗，避免少数跨年缺口被拼成一个巨大的行情请求。
            windows = [days[i:i + window_size] for i in range(0, len(days), window_size)]
            windows = [window for window in windows if pending.intersection(window)]
            for window in _progress_items(windows, '历史窗口', lambda ds: '{}—{}'.format(ds[0], ds[-1])):
                wanted = sorted(pending.intersection(window))
                window = window[window.index(wanted[0]):window.index(wanted[-1]) + 1]
                records = collect_window(window, set(wanted), metadata, batch_size)
                _progress_note('保存 {} 个交易日的情绪汇总'.format(len(records)))
                results.extend(save_records(Session, records))
        return pd.DataFrame(results, columns=RESULT_COLUMNS)

    return SimpleNamespace(**locals())

_API_NAMES = ('get_all_securities', 'get_trade_days', 'get_price')

def run(**options):
    apis = {key: globals().get(key, getattr(jq, key, getattr(builtins, key, None))) for key in _API_NAMES}
    with _progress_scope('市场情绪跟踪', 1, 1):
        return _sentiment_strategy(apis, DB_CONFIG).run(**options)

if __name__ == '__main__':
    try:
        sentiment_df = run()
        print('市场情绪跟踪：新增 {} 个交易日，保留最近三年。'.format(len(sentiment_df)))
        if len(sentiment_df):
            print(sentiment_df.tail(5).to_string(index=False))
    except Exception as exc:
        raise RuntimeError(_safe_error(exc, DB_CONFIG)) from None
