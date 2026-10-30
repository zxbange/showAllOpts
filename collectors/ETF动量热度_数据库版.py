# -*- coding: utf-8 -*-
"""聚宽单文件串行采集：直接粘贴到 Notebook，或 %run -i 本文件。
ETF 动量热度在本文件内独立实现；不读取外部策略、配置或日志文件。
依赖和参数集中在开头；共用连接、会话及日期工具。用法见 docs/collector-batch.md。
"""
import bisect
import builtins
import datetime
import io
import json
import math
import re
import sys
import threading
import time
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from statistics import median
from types import SimpleNamespace
from urllib.parse import quote, quote_plus

import jqdata as jq
import pandas as pd
from sqlalchemy import (BigInteger, Column, Date, DateTime, Index, Integer, JSON,
                        Numeric, String, Text, and_, or_, create_engine, func)
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

dt = datetime  # 净利润断层使用的日期别名，共用同一个模块。

# ==================== 集中配置区 ====================
DB_CONFIG = {
    'host': 'your_mysql_host',
    'port': 3306,
    'dbname': 'your_database',
    'dbuser': 'your_database_user',
    'dbpassword': 'your_password',
}
# 连接参数只配置一次；重试仅针对连接，不重放写入事务。
DB_OPTIONS = {'connect_timeout': 10, 'retry_count': 5, 'retry_interval': 10, 'pool_recycle': 3600}

# end_date=None：每次运行取北京时间当天。下列参数按策略分组配置。
STRATEGY_CONFIG = {'ETF动量热度': {'end_date': None, 'initial_trade_days': 180, 'retention_trade_days': 180, 'min_size_yi': 30}}

STRATEGY_ORDER = ['ETF动量热度']
STRATEGY_OPTIONS = {}  # 原六策略固定只新增；高股息选股和高增长每次重算并替换最新快照。
PREVIEW_ROWS = 5  # 每个策略展示最后几条新增记录；0 表示仅展示条数。
PROGRESS_ENABLED = True  # 控制台进度条；关闭后仍保留各策略结果和最终汇总。
PROGRESS_INTERVAL_SECONDS = 5.0  # 最多每隔多少秒刷新；等待接口/数据库时也刷新耗时。
# ==================== 公共能力 ====================
# Notebook 重复粘贴时沿用当前内核的锁；不创建锁文件。
_RUN_LOCK = globals().get('_RUN_LOCK') or threading.Lock()
_API_NAMES = ('get_all_securities', 'get_trade_days', 'get_price', 'get_extras',
              'get_industries', 'get_industry_stocks', 'get_industry', 'get_fundamentals',
              'query', 'valuation', 'indicator', 'income', 'finance', 'jy')
_PREVIEW_COLUMNS = {'ETF动量热度': ('trade_date', 'code', 'name', 'score', 'annual_return', 'r2')}


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


def _decimal_number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None

def _closed_day(end, now):
    """日行情在北京时间16点后使用当天；交易日仍由各策略的日历校验。"""
    return end - datetime.timedelta(days=1) if end == now.date() and now.hour < 16 else end

def _source_pages(fetch_page, key='id', start=-1, label='数据接口', progress=None, fields=(), page_size=None):
    """普通主键分页只保留当前页，读到空页；服务端可能返回少于请求数量。"""
    cursor, count = start, 0
    while True:
        if progress is not None:
            progress('{}：已读取 {} 条，正在请求下一页'.format(label, count))
        frame = fetch_page(cursor)
        if frame is None:
            raise ValueError('{} 未返回结果，取消本次入库。'.format(label))
        if not set(fields).issubset(frame.columns):
            raise ValueError('{} 返回字段不完整，取消本次入库。'.format(label))
        if page_size is not None and len(frame) > page_size:
            raise ValueError('{} 分页超限。'.format(label))
        if frame.empty:
            return
        if key not in frame.columns:
            raise ValueError('{} 缺少分页主键 {}。'.format(label, key))
        ids = [int(value) for value in frame[key]]
        if ids != sorted(set(ids)) or ids[0] <= cursor:
            raise ValueError('{} 分页主键重复、乱序或未前进。'.format(label))
        cursor, count = ids[-1], count + len(frame)
        yield frame
        del frame, ids


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


class BatchRunError(RuntimeError):
    def __init__(self, summary):
        self.summary = summary
        failed = [r['strategy'] for r in summary if r['status'] == 'FAILED']
        super(BatchRunError, self).__init__('采集未全部完成：{}；请查看控制台结果。'.format('、'.join(failed)))


class _QuietStream(io.TextIOBase):
    """丢弃策略过程输出，不在内存累积历史日志，也不创建日志文件。"""
    def write(self, text):
        return len(text)

    def flush(self):
        pass


@contextmanager
def _batch_guard():
    if not _RUN_LOCK.acquire(False):
        raise RuntimeError('当前 Notebook 内核已有采集任务运行，请等待完成。')
    try:
        yield
    finally:
        _RUN_LOCK.release()


def _load_strategy(name, config, api_namespace):
    apis = {key: api_namespace.get(key, getattr(jq, key, getattr(builtins, key, None)))
            for key in _API_NAMES}
    return STRATEGIES[name](apis, config).run


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


def run_all(strategy_names=None, options=None, db_config=None, preview_rows=None,
            raise_on_failure=True, api_namespace=None):
    """依次调用文件内策略；高股息/高增长替换最新结果，其余只新增；控制台展示进度及结果，失败后继续，最后统一报告。"""
    names = list(STRATEGY_ORDER if strategy_names is None else strategy_names)
    config = dict(DB_CONFIG if db_config is None else db_config)
    options = dict(STRATEGY_OPTIONS if options is None else options)
    preview_rows = PREVIEW_ROWS if preview_rows is None else preview_rows
    if not names or len(names) != len(set(names)) or builtins.any(n not in STRATEGIES for n in names):
        raise ValueError('策略列表不能为空、重复或包含未知名称。')
    if set(config) != {'host', 'port', 'dbname', 'dbuser', 'dbpassword'}:
        raise ValueError('DB_CONFIG 必须包含 host、port、dbname、dbuser、dbpassword。')
    if builtins.any(n not in STRATEGIES or not isinstance(v, dict) for n, v in options.items()):
        raise ValueError('策略参数必须为按策略名称组织的字典。')
    if builtins.any(v.get('skip_existing', True) is not True for v in options.values()):
        raise ValueError('不允许关闭 skip_existing；已有股票及其他明细保持原值。')
    if not isinstance(preview_rows, int) or preview_rows < 0:
        raise ValueError('preview_rows 必须是非负整数。')
    if not isinstance(PROGRESS_ENABLED, bool):
        raise ValueError('PROGRESS_ENABLED 必须为布尔值。')
    if (isinstance(PROGRESS_INTERVAL_SECONDS, bool) or not isinstance(PROGRESS_INTERVAL_SECONDS, (int, float))
            or not math.isfinite(PROGRESS_INTERVAL_SECONDS) or PROGRESS_INTERVAL_SECONDS <= 0):
        raise ValueError('PROGRESS_INTERVAL_SECONDS 必须是有限正数。')
    apis = globals() if api_namespace is None else api_namespace
    summary = []
    with _batch_guard():
        for strategy_index, name in enumerate(names, 1):
            started = time.monotonic()
            row = dict(strategy=name, status='FAILED', rows=None, error_type=None, error=None)
            result = None
            try:
                with _progress_scope(name, strategy_index, len(names)), _QuietStream() as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
                    run = _load_strategy(name, config, apis)
                    kwargs = dict(options.get(name, {}))
                    if name in ('高股息选股', '高增长'):
                        kwargs.pop('skip_existing', None)  # 此模块显式授权每次更新最新结果。
                    else:
                        kwargs['skip_existing'] = True
                    result = run(**kwargs)
                    row.update(status='SUCCESS', rows=len(result))
            except KeyboardInterrupt:
                print('【{}】已中断；已提交数据保留，后续策略不再执行。'.format(name))
                raise
            except Exception as exc:
                row.update(error_type=type(exc).__name__, error=_safe_error(exc, config))
            row['elapsed_seconds'] = builtins.round(time.monotonic() - started, 3)
            summary.append(row)
            if row['status'] == 'SUCCESS':
                label = '当前选股结果' if name in ('高股息选股', '高增长') else '新增'
                print('\n【{}】成功：{} {} 条，耗时 {:.2f} 秒。'.format(name, label, row['rows'], row['elapsed_seconds']))
                if not row['rows']:
                    print('本次选股无匹配，最新结果已保存。' if name in ('高股息选股', '高增长')
                          else '无新增数据；已有记录跳过，或数据源尚无可入库数据。')
                elif preview_rows:
                    try:
                        columns = [c for c in _PREVIEW_COLUMNS[name] if c in result.columns]
                        print('本次结果预览（最后 {} 条，字段单位沿用策略）：'.format(builtins.min(preview_rows, row['rows'])))
                        print(result[columns].tail(preview_rows).to_string(index=False))
                    except Exception:
                        print('结果预览不可用，入库已完成。')
            else:
                print('\n【{}】失败：{}；已提交部分保留，继续后续策略。'.format(name, row['error']))
            del result
        print('\n采集结果汇总\n策略 | 结果 | 条数（高股息/高增长为当前结果，其余为新增） | 耗时（秒）')
        for row in summary:
            print('{} | {} | {} | {:.2f}'.format(row['strategy'], row['status'],
                  row['rows'] if row['rows'] is not None else '未完成', row['elapsed_seconds']))
        failed = builtins.sum(r['status'] == 'FAILED' for r in summary)
        print('成功 {}，失败 {}。'.format(len(summary) - failed, failed))
        if failed and raise_on_failure:
            raise BatchRunError(summary)
    return summary


# ---------- 行业动量 ----------
def _etf_heat_strategy(_apis, _config):
    finance, query = _apis.get('finance'), _apis.get('query')
    AS_OF_DATE = None

    LOOKBACK_DAYS = 15

    MAX_STALE_TRADE_DAYS = 3

    BATCH_SIZE = 50

    PAGE_SIZE = 2000



    CROSS_BORDER_CODES = {
        '513100.XSHG', '159509.XSHE', '513030.XSHG', '513520.XSHG',
        '513690.XSHG', '513130.XSHG', '159550.XSHE',
    }

    BOND_CODES = {'511220.XSHG'}

    INCLUDE_LOF = True


    MAX_LOF_REPORT_AGE_DAYS = 180

    COMMODITY_DIRECTIONS = {
        '518880.XSHG': ('黄金现货方向', 'commodity:gold'),
        '501018.XSHG': ('原油方向（南方原油LOF）', 'commodity:oil'),
        '159985.XSHE': ('豆粕期货方向', 'commodity:soymeal'),
        '159980.XSHE': ('有色金属期货方向', 'commodity:nonferrous'),
    }

    FOCUS_CODES = tuple(COMMODITY_DIRECTIONS)

    CATEGORIES = ['宽基ETF', '行业ETF', '主题ETF', '策略/风格ETF', '跨境ETF', '商品ETF', '债券ETF', '国内LOF']

    OVERRIDES = {}

    RULES = [
        ('排除：货币ETF', '货币 现金 日利 添益 快线'),
        ('债券ETF', '债 国开 政金 政策性金融'),
        ('跨境ETF', '恒生 港股 香港 沪港深 中概 纳斯达克 纳指 标普 日经 德国 DAX 法国 东证 沙特 亚太 海外 全球 美国 日本 韩国 MSCI中国'),
        ('策略/风格ETF', '红利 低波 价值 质量 基本面 等权 高股息 自由现金流 成长 动量'),
        ('宽基ETF', '沪深300 中证A50 中证A100 中证A500 中证500 中证800 中证1000 中证2000 上证50 上证180 上证380 上证综 深证100 深证成 深证50 深证300 创业板指 创业板50 创业板综 科创50 科创100 科创200 科创综 科创创业50 国证2000 国证A'),
        ('主题ETF', '人工智能 机器人 新能源 光伏 电池 储能 智能 数字 云计算 大数据 物联网 互联网 网络安全 信息安全 创新药 疫苗 养老 环保 碳中和 绿色 一带一路 国企 央企 国资 改革 科技 创新 新材料 稀土 稀有金属 资源 5G 低空 卫星 航天'),
        ('行业ETF', '银行 证券 券商 保险 金融 医药 中药 医疗 生物 半导体 芯片 军工 国防 煤炭 钢铁 有色 化工 地产 房地产 食品 饮料 酒 农业 养殖 畜牧 家电 汽车 机械 电力 公用事业 建筑 建材 交通 运输 物流 航空 通信 传媒 计算机 软件 电子 消费 石油 油气'),
    ]

    def api(name):
        value = _apis.get(name)
        if value is None:
            raise ValueError('缺少聚宽接口：' + name)
        return value

    def text(value):
        return '' if value is None or pd.isna(value) else str(value).strip()

    def positive(value):
        value = _decimal_number(value)
        return value if value is not None and value > 0 else None

    def read_pages(table, filters=()):
        fetch_page = lambda cursor: finance.run_query(query(table).filter(*filters).filter(table.id > cursor)
                                                     .order_by(table.id).limit(PAGE_SIZE))
        frames = list(_source_pages(fetch_page, label='基金资料', progress=_progress_note))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def classify(target, override):
        if override.get('category'):
            category = override['category']
            if category not in CATEGORIES and category != '排除：货币ETF':
                raise ValueError('无效人工分类：' + category)
            return category, '人工指定'
        label = re.sub(r'\s+', '', target).upper()
        if ('黄金' in label and not builtins.any(w in label for w in ('股票', '黄金股', '产业'))) or \
                builtins.any(w in label for w in ('豆粕期货', '有色金属期货', '能源化工期货')):
            return '商品ETF', '商品标的关键词'
        for category, words in RULES:
            hit = next((word for word in words.split() if word.upper() in label), None)
            if hit and category == '宽基ETF':
                # 避免把‘中证500医药’等行业子指数误识别为宽基。
                suffix = label.split(hit.upper(), 1)[1]
                suffix = re.sub(r'[（(][^）)]*[）)]', '', suffix)
                for word in ('全收益', '净收益', '价格', '指数', '收益'):
                    suffix = suffix.replace(word, '')
                if suffix:
                    continue
            if hit:
                return category, '关键词：' + hit
        return '待核对', '请通过 OVERRIDES 指定分类'

    def commodity_direction(code, name, target_name=''):
        if code in COMMODITY_DIRECTIONS:
            return COMMODITY_DIRECTIONS[code]
        label = (name + ' ' + target_name).upper()
        # 黄金股/黄金产业股票与现货黄金不同，不能混为一个方向。
        if '黄金' in label and not builtins.any(w in label for w in ('股票', '黄金股', '产业', '矿业')):
            return '黄金现货方向', 'commodity:gold'
        if ('有色' in label and '期货' in label):
            return '有色金属期货方向', 'commodity:nonferrous'
        if '豆粕' in label and '期货' in label:
            return '豆粕期货方向', 'commodity:soymeal'
        return None

    def permitted(code, category):
        if category == '跨境ETF' and code not in CROSS_BORDER_CODES:
            return '跨境基金不在原 ETF 动量策略的七只白名单中'
        if category == '债券ETF' and code not in BOND_CODES:
            return '债券仅允许城投债 ETF 511220.XSHG'
        return None

    def lof_catalog(universe, day):
        """Use fund master classification; unknown/foreign/bond types fail closed."""
        if universe.empty:
            return [], []
        table = finance.FUND_MAIN_INFO
        codes = [str(c)[:6] for c in universe.index]
        info, audit, records = {}, [], []
        for offset in range(0, len(codes), BATCH_SIZE):
            frame = read_pages(table, (table.main_code.in_(codes[offset:offset+BATCH_SIZE]), table.start_date <= day))
            for row in frame.to_dict('records'):
                if text(row.get('end_date')) and pd.Timestamp(row['end_date']).date() < day:
                    continue
                info.setdefault(text(row['main_code']), []).append(row)
        foreign = ('QDII', '跨境', '海外', '境外', '全球', '环球', '香港', '港股', '沪港深',
                   '纳指', '纳斯达克', '恒生', '标普', '美国', '日本', '德国', '亚太', '原油')
        for code, fund in universe.iterrows():
            name = text(fund['display_name'])
            rows = info.get(code[:6], [])
            if len(rows) != 1:
                audit.append(dict(code=code, name=name, reason='LOF 主体信息缺失或冲突，无法确认国内投资范围'))
                continue
            row = rows[0]
            description = ' '.join(text(row.get(k)) for k in ('name', 'underlying_asset_type', 'invest_style', 'operate_mode')) + ' ' + name
            description = description.upper()
            if builtins.any(word in description for word in foreign):
                audit.append(dict(code=code, name=name, reason='跨境/海外 LOF 不在七只 ETF 白名单中'))
                continue
            asset = text(row.get('underlying_asset_type'))
            if '债' in asset or '债券' in description:
                audit.append(dict(code=code, name=name, reason='债券仅允许城投债 ETF，不纳入债券 LOF'))
                continue
            if not builtins.any(word in asset for word in ('股票', '混合')):
                audit.append(dict(code=code, name=name, reason='LOF 投资类型不在已确认的国内股票/混合范围', details=description))
                continue
            # Actively managed LOFs have no common tracking index. Do not merge unrelated portfolios.
            records.append(dict(code=code, name=name, category='国内LOF', fund_type='LOF',
                                target_code='', target_name=text(row.get('name')) or name,
                                group='lof:'+code, target_source='FUND_MAIN_INFO 主体信息',
                                classification_reason='国内股票/混合 LOF，按基金代码独立保留',
                                listed_date=text(fund.get('start_date'))[:10]))
        return records, audit

    def target_catalog(universe, day):
        table = finance.FUND_INVEST_TARGET
        frame = read_pages(table, (table.pub_date <= day, table.start_date <= day))
        if frame.empty:
            raise ValueError('有效跟踪标的表为空，请核对数据权限')
        required = {'code', 'end_date', 'traced_index_code', 'traced_index_name'}
        if not required.issubset(frame.columns):
            raise ValueError('跟踪标的字段不符：' + str(list(frame.columns)))
        mapping = {}
        for row in frame.to_dict('records'):
            if text(row['end_date']) and pd.Timestamp(row['end_date']).date() < day:
                continue
            if row['code'] in universe.index:
                pair = (text(row['traced_index_code']), text(row['traced_index_name']))
                if pair[0] or pair[1]:
                    mapping.setdefault(row['code'], set()).add(pair)
        records, audit = [], []
        for code, info in universe.iterrows():
            name = text(info['display_name'])
            override = OVERRIDES.get(code, {})
            targets = mapping.get(code, set())
            if override.get('target_name'):
                targets = {(override.get('target_code', ''), override['target_name'])}
            if len(targets) > 1:
                audit.append(dict(code=code, name=name, reason='有效跟踪标的版本冲突，请人工核对', details=str(sorted(targets))))
                continue
            target_code, target_name = next(iter(targets)) if targets else ('', '')
            direction = commodity_direction(code, name, target_name)
            source = '聚宽跟踪标的表'
            if not target_name:
                if direction:
                    target_name, _ = direction
                elif code in CROSS_BORDER_CODES or code in BOND_CODES:
                    target_name = name + '（指定基金方向）'
                else:
                    audit.append(dict(code=code, name=name, reason='跟踪标的缺失，请人工核对'))
                    continue
                source = '指定基金/商品名称方向补充（非指数代码）'
            override = dict(OVERRIDES.get(target_code, {}), **override)
            category, reason = classify(target_name, override)
            group = override.get('group') or ('code:' + target_code if target_code else 'name:' + re.sub(r'\s+', '', target_name).upper())
            if direction and not override.get('category'):
                category, reason = '商品ETF', '商品方向规则：' + direction[0]
                group = override.get('group') or direction[1]
            if code in CROSS_BORDER_CODES:
                category, reason = '跨境ETF', '原 ETF 动量策略跨境白名单'
            if code in BOND_CODES:
                category, reason = '债券ETF', '指定城投债 ETF'
            excluded = permitted(code, category)
            if excluded:
                audit.append(dict(code=code, name=name, reason=excluded))
                continue
            records.append(dict(code=code, name=name, category=category,
                                fund_type='ETF', listed_date=text(info.get('start_date'))[:10],
                                target_code=target_code, target_name=target_name, group=group,
                                target_source=source, classification_reason=reason))
        return records, audit

    def load_sizes(codes, days):
        table, output = finance.FUND_SHARE_DAILY, {}
        for offset in range(0, len(codes), BATCH_SIZE):
            batch = codes[offset:offset + BATCH_SIZE]
            print('规模采集：{}/{} ETF'.format(builtins.min(offset + len(batch), len(codes)), len(codes)), flush=True)
            shares = read_pages(table, (table.code.in_(batch), table.date >= days[0], table.date <= days[-1]))
            navs = api('get_extras')('unit_net_value', batch, start_date=days[0], end_date=days[-1], df=True)
            if navs is None:
                raise ValueError('单位净值接口返回 None')
            navs = navs.copy()
            navs.index = pd.Index(pd.to_datetime(navs.index).date)
            if navs.index.has_duplicates:
                raise ValueError('单位净值日期重复')
            for row in shares.to_dict('records'):
                code, day = row['code'], pd.Timestamp(row['date']).date()
                if code not in batch or day not in days:
                    raise ValueError('份额数据超出查询范围')
                amount = positive(row['shares'])  # 聚宽该字段单位：份
                nav = positive(navs.at[day, code]) if day in navs.index and code in navs else None
                if amount is None or nav is None:
                    continue
                value = (amount * nav, amount, nav)
                previous = output.setdefault(code, {}).get(day)
                if previous is not None and previous != value:
                    raise ValueError('{} {} 份额记录冲突'.format(code, day))
                output[code][day] = value
        return output

    def load_lof_reports(records, as_of):
        selected, audit, candidates = [], [], []
        table = finance.FUND_FIN_INDICATOR
        for record in records:
            code = record['code']
            print('LOF 财报规模：' + code, flush=True)
            frame = read_pages(table, (table.code == code[:6], table.pub_date <= as_of,
                                      table.period_end <= as_of))
            if frame.empty:
                audit.append(dict(code=code, reason='缺少已披露的 LOF 对应份额类别净资产'))
                continue
            rows = frame.to_dict('records')
            rows.sort(key=lambda r: (pd.Timestamp(r['period_end']).date(),
                                     pd.Timestamp(r['pub_date']).date(), r['id']), reverse=True)
            row = rows[0]
            period, published = pd.Timestamp(row['period_end']).date(), pd.Timestamp(row['pub_date']).date()
            values = {positive(r['total_tna']) for r in rows
                      if pd.Timestamp(r['period_end']).date() == period and
                      pd.Timestamp(r['pub_date']).date() == published}
            size = positive(row['total_tna'])  # 元；对应六位基金代码的份额类别（501018 为 A 类）
            if len(values) != 1 or size is None:
                audit.append(dict(code=code, reason='最新 LOF 披露规模缺失或冲突'))
                continue
            if (as_of - period).days > MAX_LOF_REPORT_AGE_DAYS:
                audit.append(dict(code=code, reason='LOF 报告期过旧', details=str(period)))
                continue
            item = dict(record, size_yuan=size, size_yi=size / Decimal('100000000'),
                        shares=None, unit_nav=positive(row['nav']), size_date=str(period),
                        size_pub_date=str(published), stale_trade_days=None,
                        size_source='FUND_FIN_INDICATOR.total_tna：对应份额类别净资产（非全类别合计）',
                        group_count=1, group_rank=1)
            candidates.append(item)
            if item['size_yi'] > MIN_SIZE_YI:
                selected.append(item)
            else:
                audit.append(dict(code=code, name=record['name'], group=record['group'],
                                  reason='LOF 对应份额类别规模未严格超过 {} 亿元'.format(MIN_SIZE_YI),
                                  details='规模 {:.4f} 亿元；报告期 {}；披露日 {}'.format(item['size_yi'], period, published)))
        return selected, audit, candidates

    def select_largest(records, sizes, days):
        groups, selected, audit, candidates = {}, [], [], []
        for row in records:
            groups.setdefault(row['group'], []).append(row)
        for group, members in groups.items():
            categories = {row['category'] for row in members}
            if len(categories) != 1 or next(iter(categories)) not in CATEGORIES:
                audit.append(dict(group=group, reason='分类待核对、冲突或不在所选七类', details='; '.join('{} {} [{}] {}'.format(row['code'], row['name'], row['target_name'], row['classification_reason']) for row in members)))
                continue
            common = set(days)
            for row in members:
                common.intersection_update(sizes.get(row['code'], {}))
            if not common:
                audit.append(dict(group=group, reason='所有候选无共同有效规模日期，整组暂不选', details='; '.join('{} {} [{}] {}'.format(row['code'], row['name'], row['target_name'], row['classification_reason']) for row in members)))
                continue
            day = builtins.max(common)
            lag = len(days) - 1 - days.index(day)
            if lag > MAX_STALE_TRADE_DAYS:
                audit.append(dict(group=group, reason='共同规模日期过旧', details=str(day)))
                continue
            ranked = []
            for row in members:
                size, shares, nav = sizes[row['code']][day]
                ranked.append(dict(row, size_yuan=size, size_yi=size / Decimal('100000000'), shares=shares,
                                   size_source='同日基金份额 × 单位净值', size_pub_date=None,
                                   unit_nav=nav, size_date=str(day), stale_trade_days=lag, group_count=len(members)))
            ranked.sort(key=lambda row: (-row['size_yuan'], row['code']))
            for rank, row in enumerate(ranked, 1):
                row['group_rank'] = rank
            candidates.extend(ranked)
            if ranked[0]['size_yi'] > MIN_SIZE_YI:
                selected.append(ranked[0])
            else:
                audit.append(dict(group=group, code=ranked[0]['code'], name=ranked[0]['name'],
                                  reason='组内最大规模未严格超过 {} 亿元'.format(MIN_SIZE_YI),
                                  details='规模 {:.4f} 亿元；日期 {}'.format(ranked[0]['size_yi'], day)))
        selected.sort(key=lambda row: (CATEGORIES.index(row['category']), -row['size_yuan'], row['code']))
        return selected, audit, candidates

    def select_pool():
        if not 1 <= PAGE_SIZE <= 3000 or not 1 <= BATCH_SIZE <= 100:
            raise ValueError('PAGE_SIZE 必须为 1~3000；BATCH_SIZE 必须为 1~100')
        if LOOKBACK_DAYS <= MAX_STALE_TRADE_DAYS or MAX_STALE_TRADE_DAYS < 0:
            raise ValueError('回看天数必须大于允许滞后天数')
        if positive(MIN_SIZE_YI) is None:
            raise ValueError('规模门槛必须是正数')
        today = china_now().date()
        end = pd.Timestamp(AS_OF_DATE).date() if AS_OF_DATE else today - dt.timedelta(days=1)
        if end > today:
            raise ValueError('请指定已结束的日期，不能使用当日或未来日期')
        days = [pd.Timestamp(day).date() for day in api('get_trade_days')(end_date=end, count=LOOKBACK_DAYS)]
        if not days or days != sorted(set(days)):
            raise ValueError('交易日历为空或乱序')
        universe = api('get_all_securities')(types=['etf'], date=days[-1])
        if universe is None or universe.empty:
            raise ValueError('ETF 列表为空')
        lof = api('get_all_securities')(types=['lof'], date=days[-1])
        if lof is None:
            raise ValueError('LOF 列表查询失败')
        lof = lof.loc[~lof.index.isin(universe.index)] if INCLUDE_LOF else lof.iloc[0:0]
        _progress_note('截至 {}：筛选 {} 只 ETF、{} 只 LOF'.format(days[-1], len(universe), len(lof)))
        daily_records, audit = target_catalog(universe, days[-1])
        lof_records, lof_audit = lof_catalog(lof, days[-1]) if not lof.empty else ([], [])
        audit.extend(lof_audit)
        sizes = load_sizes([row['code'] for row in daily_records], days)
        selected, issues, candidates = select_largest(daily_records, sizes, days)
        lof_selected, lof_issues, lof_candidates = load_lof_reports(lof_records, days[-1]) if lof_records else ([], [], [])
        selected.extend(lof_selected)
        candidates.extend(lof_candidates)
        selected.sort(key=lambda r: (CATEGORIES.index(r['category']), -r['size_yuan'], r['code']))
        audit.extend(issues + lof_issues)
        for code in FOCUS_CODES:
            if code not in universe.index and code not in lof.index:
                audit.append(dict(code=code, reason='指定基金不在截止日期的聚宽上市名单中'))
        result, review, all_candidates = pd.DataFrame(selected), pd.DataFrame(audit), pd.DataFrame(candidates)
        _progress_note('候选池筛选完成：入选 {} 只，待核对/排除 {} 条'.format(len(result), len(review)))
        return result, review, all_candidates
    # Collect and calculate in JoinQuant; MySQL contains final display results.
    settings = strategy_settings('ETF动量热度')
    MIN_SIZE_YI = Decimal(str(settings['min_size_yi']))
    RULE_VERSION = 'etf_close_momentum_v3'
    CALCULATION_VERSION = 'weighted-momentum-26-v1'
    Base = declarative_base()

    class Heat(Base):
        __tablename__ = 'etf_momentum_heat'
        trade_date = Column(Date, primary_key=True)
        code = Column(String(16), primary_key=True)
        name = Column(String(128), nullable=False)
        score = Column(Numeric(28, 10))
        close = Column(Numeric(24, 8))
        annual_return = Column(Numeric(28, 10))
        r2 = Column(Numeric(24, 10))
        reference_pass = Column(Integer)
        details = Column(JSON, nullable=False)
        updated_at = Column(DateTime, nullable=False)
        __table_args__ = (Index('ix_etf_heat_code_date', 'code', 'trade_date'),)

    class Pool(Base):
        __tablename__ = 'etf_momentum_pool'
        id = Column(Integer, primary_key=True)
        as_of_date = Column(Date, nullable=False)
        rule_version = Column(String(48), nullable=False)
        members = Column(JSON, nullable=False)
        updated_at = Column(DateTime, nullable=False)


    def metrics(prices, paused=False):
        empty = dict(score=None, annual_return=None, r2=None, reference_pass=None,
                     details={'status': 'missing', 'reason': '有效价格不足26期或当日停牌'})
        if paused or len(prices) != 26 or builtins.any(p is None or not math.isfinite(float(p)) or p <= 0 for p in prices):
            return empty
        # A constant price scale does not affect regression slope or R².
        y = [math.log(float(p) / float(prices[0])) for p in prices]
        weights = [1 + i / 25 for i in range(26)]
        squared = [w*w for w in weights]
        total = builtins.sum(squared)
        mx = builtins.sum(w*i for i, w in enumerate(squared)) / total
        my = builtins.sum(w*v for w, v in zip(squared, y)) / total
        slope = builtins.sum(w*(i-mx)*(v-my) for i, (w,v) in enumerate(zip(squared,y))) / builtins.sum(w*(i-mx)**2 for i,w in enumerate(squared))
        intercept = my-slope*mx
        residual = builtins.sum(w*(v-(slope*i+intercept))**2 for i,(w,v) in enumerate(zip(weights,y)))
        average = builtins.sum(y)/26
        variance = builtins.sum(w*(v-average)**2 for w,v in zip(weights,y))
        r2 = 1-residual/variance if variance > 1e-25 else 0.0
        try:
            annual = math.expm1(slope*250)
            values = [annual*r2, annual, r2]
            if builtins.any(not math.isfinite(v) or abs(v) >= 1e18 for v in values):
                raise OverflowError
        except OverflowError:
            return dict(empty, details={'status': 'missing', 'reason': '异常价格导致计算越界'})
        score, annual, r2 = [Decimal(str(v)).quantize(Decimal('0.0000000001')) for v in values]
        return dict(score=score, annual_return=annual, r2=r2, reference_pass=None,
                    details={'status': 'ok', 'calculation_version': CALCULATION_VERSION})

    def price_value(value):
        if not positive(value):
            return None
        result = Decimal(str(value)).quantize(Decimal('0.00000001'))
        if result >= Decimal('1e16'):
            raise ValueError('价格超出数据库精度')
        return result


    def save_rows(Session, records, repair_missing=False):
        _install_result_journal(Session, (Heat, Pool), 'etf-momentum')
        keys = {(r['trade_date'], r['code']) for r in records}
        if len(keys) != len(records):
            raise ValueError('交易日和基金代码重复')
        if not records:
            return []
        with session_scope(Session) as session:
            existing = {(r[0], r[1]): r[2] for r in session.query(Heat.trade_date, Heat.code, Heat.score).filter(
                Heat.code.in_(set(r['code'] for r in records)),
                Heat.trade_date >= builtins.min(r['trade_date'] for r in records),
                Heat.trade_date <= builtins.max(r['trade_date'] for r in records)).all()}
            added = [r for r in records if (r['trade_date'], r['code']) not in existing]
            session.add_all([Heat(**r) for r in added])
            repaired = []
            if repair_missing:
                for row in records:
                    if (row['trade_date'], row['code']) not in existing or existing[(row['trade_date'], row['code'])] is not None or row['score'] is None:
                        continue
                    values = {key: value for key, value in row.items() if key not in ('trade_date', 'code')}
                    changed = _result_bulk(session.query(Heat).filter(
                        Heat.trade_date == row['trade_date'], Heat.code == row['code'],
                        Heat.score.is_(None)), values)
                    if changed:
                        repaired.append(row)
            session.commit()
            if repaired:
                _progress_note('修复 {} 条历史空热度，已有有效得分保持不变'.format(len(repaired)))
            return added + repaired


    def get_retention_start(reference_date):
        """Validate the complete calendar before allowing any database deletion."""
        count, reference = settings['retention_trade_days'], as_date(reference_date)
        return _checked_trade_days(api('get_trade_days')(end_date=reference, count=count),
            end=reference, count=count,
            error='保留范围交易日历不足、重复、乱序或含未来日期，未清理数据库')[0]

    def cleanup_old_data(Session, retention_start):
        _install_result_journal(Session, (Heat, Pool), 'etf-momentum')
        """Remove expired results for all funds, including funds outside the current pool."""
        with session_scope(Session) as session:
            deleted = _result_bulk(session.query(Heat).filter(Heat.trade_date < retention_start))
            session.commit()
        _progress_note('保留最近 {} 个交易日（{} 起），已清理 {} 条过期热度'.format(
            settings['retention_trade_days'], retention_start, deleted))
        return deleted

    def run(end_date=None, skip_existing=True, repair_missing=False):
        nonlocal AS_OF_DATE
        if not isinstance(repair_missing, bool):
            raise ValueError('repair_missing 须为 True 或 False')
        if skip_existing is not True:
            raise ValueError('只允许新增；已有日期和基金的记录不覆盖')
        now = china_now()
        requested = as_date(end_date or settings['end_date']) if (end_date or settings['end_date']) else now.date()
        if requested > now.date():
            raise ValueError('结束日期不能晚于今天')
        # Retention follows the actual run date even when end_date is historical.
        retention_start = get_retention_start(now.date())
        end = _closed_day(requested, now)
        days = [as_date(d) for d in api('get_trade_days')(end_date=end, count=settings['initial_trade_days'])]
        if not days or days != sorted(set(days)) or days[-1] > end:
            raise ValueError('交易日历无效')
        days = [day for day in days if day >= retention_start]
        output = []
        with database_scope(Base, _config) as Session:
            _install_result_journal(Session, (Heat, Pool), 'etf-momentum')
            cleanup_old_data(Session, retention_start)
            if not days:
                return pd.DataFrame(output)
            AS_OF_DATE = str(days[-1])
            _progress_note('筛选 ETF/LOF 候选池，计算热度后入库')
            chosen, audit, candidates = select_pool()
            if chosen.empty:
                raise ValueError('本次未取得有效候选池，保留旧快照；请运行分类研究脚本查看待核对清单')
            members = []
            for row in chosen.to_dict('records'):
                members.append({key: (str(value) if isinstance(value, Decimal) else
                                (None if pd.isna(value) else (value.item() if hasattr(value, 'item') else value)))
                                for key, value in row.items()})
            members = json.loads(json.dumps(members, ensure_ascii=False, allow_nan=False))
            with session_scope(Session) as session:
                head = session.query(Pool).filter_by(id=1).first()
                if head and (head.as_of_date > days[-1] or head.rule_version not in
                             (RULE_VERSION, 'etf_close_momentum_v1', 'etf_close_momentum_v2', 'etf_raw_market_v1')):
                    raise ValueError('已有更新日期或未知规则版本，拒绝混用')
                repair = repair_missing or head is None or head.rule_version != RULE_VERSION
            if repair:
                _progress_note('检查当前候选的历史空热度，重新计算后仅填补有效结果')
            for member in _progress_items(members, 'ETF/LOF 热度计算', lambda r: r['name']):
                code = member['code']
                with session_scope(Session) as session:
                    saved = session.query(Heat.trade_date, Heat.score).filter(Heat.code == code).all()
                    existing = {r[0] for r in saved}
                    empty_dates = {r[0] for r in saved if r[1] is None} if repair else set()
                if existing and builtins.max(existing) > days[-1]:
                    raise ValueError('已有更新日期的基金结果，拒绝回退')
                first = builtins.max(retention_start, builtins.min(
                    [days[0]] + ([builtins.max(existing)] if existing else []) + list(empty_dates)))
                calendar = [as_date(d) for d in api('get_trade_days')(start_date=first, end_date=days[-1])]
                if not calendar or calendar != sorted(set(calendar)) or calendar[0] < first or calendar[-1] != days[-1]:
                    raise ValueError('增量交易日历无效')
                listed = as_date(member['listed_date']) if member.get('listed_date') else calendar[0]
                missing = [d for d in calendar if d >= listed and (d not in existing or d in empty_dates)]
                if not missing:
                    continue
                warm = [as_date(d) for d in api('get_trade_days')(end_date=missing[0], count=26)]
                if not warm or warm != sorted(set(warm)) or warm[-1] != missing[0]:
                    raise ValueError('无法取得行情预热日期')
                window_days = [as_date(d) for d in api('get_trade_days')(start_date=warm[0], end_date=days[-1])]
                if window_days != sorted(set(window_days)) or not set(missing).issubset(window_days):
                    raise ValueError('计算窗口交易日历无效')
                frames = []
                for fields, adjustment in [(['close', 'paused'], None), (['close'], 'post')]:
                    frame = api('get_price')(code, start_date=warm[0], end_date=days[-1], frequency='daily',
                                             fields=fields, skip_paused=False, fq=adjustment)
                    if frame is None or frame.empty or not set(fields).issubset(frame.columns):
                        raise ValueError(code + ' 行情接口无数据，未发布新候选池')
                    frame = frame.copy()
                    frame.index = pd.Index(pd.to_datetime(frame.index).date)
                    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
                        raise ValueError(code + ' 行情日期重复或乱序')
                    frames.append(frame)
                raw, adjusted = frames
                # Each regression window comes from one provider request and one adjustment basis.
                for day in window_days:
                    if day < listed:
                        continue
                    if day not in raw.index or day not in adjusted.index:
                        raise ValueError('{} {} 行情缺失，保留已提交数据和旧候选池'.format(code, day))
                    paused = raw.loc[day, 'paused']
                    if pd.isna(paused) or paused not in (0, 1):
                        raise ValueError(code + ' 停牌状态缺失或无效')
                    if not paused and (positive(raw.loc[day, 'close']) is None or positive(adjusted.loc[day, 'close']) is None):
                        raise ValueError('{} {} 有效交易日价格缺失'.format(code, day))
                positions = {day: i for i, day in enumerate(window_days)}
                records = []
                for day in missing:
                    i = positions[day]
                    dates = window_days[builtins.max(0, i-25):i+1]
                    prices = [positive(adjusted.loc[d, 'close']) if d in adjusted.index else None for d in dates]
                    values = metrics(prices, bool(raw.loc[day, 'paused']))
                    records.append(dict(trade_date=day, code=code, name=member['name'],
                                        close=price_value(raw.loc[day, 'close']), updated_at=china_now(), **values))
                _progress_note('{} 计算完成，入库 {} 条热度结果'.format(code, len(records)))
                output.extend(save_rows(Session, records, repair_missing=repair))
            with session_scope(Session) as session:
                codes = {r[0] for r in session.query(Heat.code).filter(Heat.trade_date == days[-1]).all()}
                if set(m['code'] for m in members) - codes:
                    raise ValueError('最新交易日热度结果不完整，保留上一候选池快照')
                head = session.query(Pool).filter_by(id=1).with_for_update().first()
                if head and head.as_of_date > days[-1]:
                    raise ValueError('并发采集已有更新快照，拒绝回退')
                if head is None:
                    session.add(Pool(id=1, as_of_date=days[-1], rule_version=RULE_VERSION, members=members, updated_at=china_now()))
                elif head.as_of_date != days[-1] or head.members != members or head.rule_version != RULE_VERSION:
                    head.as_of_date, head.members, head.rule_version, head.updated_at = days[-1], members, RULE_VERSION, china_now()
                session.commit()
        return pd.DataFrame(output)
    return SimpleNamespace(**locals())


STRATEGIES = {'ETF动量热度': _etf_heat_strategy}

if __name__ == '__main__':
    batch_summary = run_all()
