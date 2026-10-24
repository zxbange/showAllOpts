# -*- coding: utf-8 -*-
"""聚宽单文件串行采集：直接粘贴到 Notebook，或 %run -i 本文件。
十二个策略在本文件内独立实现；不读取外部策略、配置或日志文件。
依赖和参数集中在开头；共用连接、会话及日期工具。用法见 docs/collector-batch.md。
"""
import bisect
import builtins
import datetime
import uuid
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
import numpy as np
from sqlalchemy import (BigInteger, Column, Date, DateTime, Index, Integer, JSON,
                        Numeric, String, Text, and_, or_, create_engine, func, event, tuple_)
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
STRATEGY_CONFIG = {
    '短线选股': {'end_date': None, 'batch_size': 50},
    '个股机构持仓': {'end_date': None, 'history_years': 5, 'refresh_quarters': 4, 'page_size': 1000, 'stock_batch_size': 50},
    'ETF动量热度': {'retention_trade_days': 180, 'end_date': None, 'initial_trade_days': 180, 'min_size_yi': 30},
    '行业动量': {
        'start_date': '2025-01-01', 'end_date': None,
        'industry_type': 'sw_l2', 'retention_trade_days': 180,
    },
    '市场情绪跟踪': {'end_date': None, 'batch_size': 300, 'window_size': 40},
    '抱团拥挤度': {
        'start_date': '2020-01-01', 'end_date': None, 'top_pct': 0.05,
    },
    '微盘温度计': {
        'start_date': '2021-01-01', 'end_date': None,
        'micro_num': 1000, 'sample_step': 1, 'batch_size': 1000,
    },
    'ETF份额': {
        'start_date': None, 'end_date': None, 'initial_trade_days': 300,
        'window_size': 120, 'lookback_days': 5,  # 只新增模式不会回查覆盖。
        # 固定跟踪名单；金额单位不影响源数据以“份”入库。
        'funds': {
            'hs300': [('510300.XSHG', '沪深300ETF华泰柏瑞'), ('510310.XSHG', '沪深300ETF易方达'),
                      ('510330.XSHG', '沪深300ETF华夏'), ('159919.XSHE', '沪深300ETF嘉实'),
                      ('515330.XSHG', '沪深300ETF天弘')],
            'zz500': [('510500.XSHG', '中证500ETF南方'), ('510510.XSHG', '中证500ETF广发'),
                      ('510580.XSHG', '中证500ETF易方达'), ('512500.XSHG', '中证500ETF华夏'),
                      ('159922.XSHE', '中证500ETF嘉实')],
        },
    },
    '净利润断层': {'end_date': None, 'page_size': 1000},
    '高增长': {'end_date': None, 'batch_size': 100, 'page_size': 2000},
    '高股息选股': {'end_date': None, 'batch_size': 300, 'page_size': 2000},
    '微盘新高新低': {'start_date': None, 'end_date': None, 'micro_num': 400, 'initial_days': 180, 'batch_size': 500, 'price_batch_size': 100},
}

STRATEGY_ORDER = ['行业动量', '抱团拥挤度', '市场情绪跟踪', '微盘温度计', 'ETF份额', '净利润断层', '微盘新高新低', '高股息选股', '高增长', 'ETF动量热度', '个股机构持仓', '短线选股']
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


_PROGRESS_LOCAL = threading.local()


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


def _decimal_number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _float_number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _round_decimal(value, places=6, precision=50):
    value = _decimal_number(value)
    if value is None:
        return None
    with localcontext() as ctx:
        ctx.prec = precision
        return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _optional_date(value):
    return None if value is None or pd.isna(value) else as_date(value)


def _closed_day(end, now):
    """日行情在北京时间16点后使用当天；交易日仍由各策略的日历校验。"""
    return end - datetime.timedelta(days=1) if end == now.date() and now.hour < 16 else end


def _checked_records(frame, fields, label):
    if frame is None or not set(fields).issubset(frame.columns):
        raise ValueError('{} 返回缺失或缺少字段，取消本次入库。'.format(label))
    return frame.to_dict('records')


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
    """依次调用文件内策略；高股息/高增长替换最新结果，机构持仓更新变化，其余只新增；控制台展示进度及结果，失败后继续，最后统一报告。"""
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
            _, write_mode, preview_columns = STRATEGY_SPECS[name]
            try:
                with _progress_scope(name, strategy_index, len(names)), _QuietStream() as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
                    run = _load_strategy(name, config, apis)
                    kwargs = dict(options.get(name, {}))
                    if write_mode == 'latest':
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
                label = {'changes': '新增或更新', 'latest': '当前选股结果', 'append': '新增'}[write_mode]
                print('\n【{}】成功：{} {} 条，耗时 {:.2f} 秒。'.format(name, label, row['rows'], row['elapsed_seconds']))
                if not row['rows']:
                    print('本次选股无匹配，最新结果已保存。' if write_mode == 'latest'
                          else '无新增数据；已有记录跳过，或数据源尚无可入库数据。')
                elif preview_rows:
                    try:
                        columns = [c for c in preview_columns if c in result.columns]
                        print('本次结果预览（最后 {} 条，字段单位沿用策略）：'.format(builtins.min(preview_rows, row['rows'])))
                        print(result[columns].tail(preview_rows).to_string(index=False))
                    except Exception:
                        print('结果预览不可用，入库已完成。')
            else:
                print('\n【{}】失败：{}；已提交部分保留，继续后续策略。'.format(name, row['error']))
            del result
        print('\n采集结果汇总\n策略 | 结果 | 条数（高股息/高增长为当前结果，机构持仓为新增或更新，其余为新增） | 耗时（秒）')
        for row in summary:
            print('{} | {} | {} | {:.2f}'.format(row['strategy'], row['status'],
                  row['rows'] if row['rows'] is not None else '未完成', row['elapsed_seconds']))
        failed = builtins.sum(r['status'] == 'FAILED' for r in summary)
        print('成功 {}，失败 {}。'.format(len(summary) - failed, failed))
        if failed and raise_on_failure:
            raise BatchRunError(summary)
    return summary


# ---------- 行业动量 ----------
def _momentum_strategy(_apis, _config):
    """独立作用域隔离模型、参数和函数，返回本策略的接口。"""
    settings = strategy_settings('行业动量')
    get_all_securities = _apis.get('get_all_securities')
    get_trade_days = _apis.get('get_trade_days')
    get_price = _apis.get('get_price')
    get_industries = _apis.get('get_industries')
    get_industry_stocks = _apis.get('get_industry_stocks')
    query = _apis.get('query')

    START_DATE = settings['start_date']
    END_DATE = settings['end_date']
    INDUSTRY_TYPE = settings['industry_type']
    RETENTION_TRADE_DAYS = settings['retention_trade_days']
    Base = declarative_base()
    RESULT_COLUMNS = [
        'trade_date', 'industry_type', 'industry_code', 'industry_name',
        'momentum', 'momentum_rank', 'front_stock_count', 'industry_stock_count',
    ]

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

    def calculate_momentum(trade_date, industry_type=INDUSTRY_TYPE):
        """计算一个交易日的全部行业；仅获取数据，不连接数据库。"""
        _progress_note('获取行业成分及行情，计算动量')
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
            if pd.isnull(start_close) or pd.isnull(end_close):
                continue
            start_close, end_close = float(start_close), float(end_close)
            if not (math.isfinite(start_close) and math.isfinite(end_close)):
                continue
            if start_close <= 0 or end_close <= 0:
                continue
            valid_count += 1
            if (end_close - start_close) / start_close > 0.2:
                front_stocks.add(code)
        if not valid_count:
            raise ValueError('{} 没有可计算涨幅的股票，停止写入。'.format(date_text))

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
        _progress_note('写入当日行业动量')
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
        with session_scope(Session) as session:
            query = session.query(IndustryMomentum).filter_by(trade_date=day, industry_type=industry_type)
            if skip_existing:
                existing = {row[0] for row in query.with_entities(IndustryMomentum.industry_code).all()}
                records = [record for record in records if record['industry_code'] not in existing]
                if not records:
                    return []
            else:
                query.delete(synchronize_session=False)
            session.add_all([IndustryMomentum(**record) for record in records])
            session.commit()
            return records

    def get_last_saved_date(Session, industry_type):
        """按行业分类查询最后已提交的交易日期；没有记录时返回 None。"""
        with session_scope(Session) as session:
            return session.query(func.max(IndustryMomentum.trade_date)).filter(
                IndustryMomentum.industry_type == industry_type,
            ).scalar()

    def get_retention_start(reference_date, count=RETENTION_TRADE_DAYS):
        """先验证交易日历，再允许清理；避免接口异常导致误删。"""
        _progress_note('获取保留区间交易日历')
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError('保留交易日数量必须是正整数。')
        reference = as_date(reference_date)
        raw_days = get_trade_days(end_date=reference.isoformat(), count=count)
        if raw_days is None:
            raise ValueError('保留范围的交易日历为空，未清理数据库。')
        days = [as_date(day) for day in raw_days]
        if len(days) != count:
            raise ValueError('交易日历不足 {} 天，未清理数据库。'.format(count))
        previous = None
        for day in days:
            if day > reference or (previous is not None and day <= previous):
                raise ValueError('交易日历存在未来日期、重复或乱序，未清理数据库。')
            previous = day
        return days[0]

    def cleanup_old_data(Session, retention_start):
        """一个事务清理整张表的过期数据，保留边界日及之后的快照。"""
        _progress_note('清理过期行业动量数据')
        cutoff = as_date(retention_start)
        with session_scope(Session) as session:
            deleted = session.query(IndustryMomentum).filter(
                IndustryMomentum.trade_date < cutoff,
            ).delete(synchronize_session=False)
            session.commit()
            return deleted

    def run(start_date=START_DATE, end_date=END_DATE, industry_type=INDUSTRY_TYPE, skip_existing=True):
        """先清理过期数据；skip_existing=True 时从最新日期之后只新增。"""
        now_china = china_now()
        today_china = now_china.date()
        if end_date is None:
            end_date = today_china
        initial_start, end = as_date(start_date), as_date(end_date)
        if end > today_china:
            raise ValueError('结束日期不能晚于运行当天。')
        if skip_existing:
            end = _closed_day(end, now_china)
        # 即使手动指定历史 end_date，保留范围仍以实际运行当天为基准。
        # 日历验证在任何数据库修改之前完成，节假日自动使用最近交易日。
        retention_start = get_retention_start(today_china, RETENTION_TRADE_DAYS)
        all_records = []
        with database_scope(Base, _config) as Session:
            cleanup_old_data(Session, retention_start)
            if end < retention_start:
                return pd.DataFrame(columns=RESULT_COLUMNS)
            last_date = get_last_saved_date(Session, industry_type)
            if last_date is None:
                start = initial_start
                if start < retention_start:
                    start = retention_start
                if start > end:
                    raise ValueError('首次入库的开始日期不能晚于结束日期。')
            else:
                start = as_date(last_date)
                if skip_existing:
                    start += datetime.timedelta(days=1)
                if start < retention_start:
                    start = retention_start
                if start > end:
                    return pd.DataFrame(columns=RESULT_COLUMNS)
            trade_days = [as_date(day) for day in get_trade_days(start_date=start, end_date=end)]
            # 先检查整个补录日历，避免异常接口将过期或越界日期写回。
            previous = None
            for day in trade_days:
                if day < start or day > end or (previous is not None and day <= previous):
                    raise ValueError('补录交易日历越界、重复或乱序，停止入库。')
                previous = day
            if not trade_days:
                return pd.DataFrame(columns=RESULT_COLUMNS)
            for day in _progress_items(trade_days, '交易日'):
                records = calculate_momentum(day, industry_type)
                records = save_day(Session, day, industry_type, records, skip_existing=skip_existing)
                all_records.extend(records)
        return pd.DataFrame(all_records, columns=RESULT_COLUMNS)
    return SimpleNamespace(**locals())


# ---------- 市场情绪跟踪 ----------

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
        if values is None:
            raise ValueError('交易日历未返回，取消采集。')
        days = [as_date(value) for value in values]
        if days != sorted(set(days)) or builtins.any(day < start or day > end for day in days):
            raise ValueError('交易日历越界、重复或乱序，取消采集。')
        return days

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
        # 主键 + 入库前复核；已有日期既不覆盖，也不重复插入。
        with session_scope(Session) as session:
            dates = [record['trade_date'] for record in records]
            existing = {row[0] for row in session.query(MarketSentiment.trade_date).filter(MarketSentiment.trade_date.in_(dates)).all()}
            added = [record for record in records if record['trade_date'] not in existing]
            session.add_all([MarketSentiment(**record) for record in added])
            session.commit()
            return added

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
            with session_scope(Session) as session:
                removed = session.query(MarketSentiment).filter(MarketSentiment.trade_date < cutoff).delete(synchronize_session=False)
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


# ---------- 抱团拥挤度 ----------
def _crowding_strategy(_apis, _config):
    """独立作用域隔离模型、参数和函数，返回本策略的接口。"""
    settings = strategy_settings('抱团拥挤度')
    get_all_securities = _apis.get('get_all_securities')
    get_trade_days = _apis.get('get_trade_days')
    get_price = _apis.get('get_price')
    query = _apis.get('query')

    START_DATE = settings['start_date']
    END_DATE = settings['end_date']
    TOP_PCT = settings['top_pct']
    Base = declarative_base()
    RESULT_COLUMNS = ['trade_date', 'top_pct', 'ratio', 'top_money', 'total_money',
                      'stock_count', 'top_stock_count', 'updated_at']

    class MarketCrowding(Base):
        __tablename__ = 'market_crowding'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}

        # 查询固定 TOP_PCT 后按日期范围读取，复合主键同时支持去重与查询。
        top_pct = Column(Numeric(8, 6), primary_key=True)
        trade_date = Column(Date, primary_key=True)
        ratio = Column(Numeric(18, 10), nullable=False)
        top_money = Column(Numeric(24, 2), nullable=False)
        total_money = Column(Numeric(24, 2), nullable=False)
        stock_count = Column(Integer, nullable=False)
        top_stock_count = Column(Integer, nullable=False)
        updated_at = Column(DateTime, nullable=False)

    def summarize_money(pairs, top_pct=TOP_PCT):
        """纯计算：零成交额股票计入总数量；无效/负数/重复数据拒绝入库。"""
        pct = Decimal(str(top_pct))
        if not pct.is_finite() or not Decimal('0') < pct <= Decimal('1'):
            raise ValueError('TOP_PCT 必须在 (0, 1] 内。')
        if pct != pct.quantize(Decimal('0.000001')):
            raise ValueError('TOP_PCT 最多支持六位小数。')
        values = []
        codes = set()
        for code, money in pairs:
            if code in codes:
                raise ValueError('单日行情存在重复股票，停止写入。')
            codes.add(code)
            try:
                amount = Decimal(str(money))
            except (InvalidOperation, ValueError):
                raise ValueError('成交额不是有效数值，停止写入。') from None
            if not amount.is_finite() or amount < 0:
                raise ValueError('成交额缺失、非有限或为负数，停止写入。')
            values.append(amount)
        if len(values) < 100:
            raise ValueError('有效股票不足 100 只，停止写入。')
        count = builtins.max(int(Decimal(len(values)) * pct), 1)
        total = builtins.sum(values, Decimal('0'))
        if total <= 0:
            raise ValueError('全市场成交额为零，停止写入。')
        top = builtins.sum(sorted(values, reverse=True)[:count], Decimal('0'))
        return dict(top_pct=pct, ratio=(top / total).quantize(Decimal('0.0000000001')),
                    top_money=top.quantize(Decimal('0.01')),
                    total_money=total.quantize(Decimal('0.01')),
                    stock_count=len(values), top_stock_count=count)

    def calculate_crowding(trade_date, top_pct=TOP_PCT):
        _progress_note('获取全市场成交额，计算拥挤度')
        day = as_date(trade_date)
        securities = get_all_securities(types=['stock'], date=day.isoformat())
        if securities is None or securities.empty:
            raise ValueError('{} 股票列表为空。'.format(day))
        stocks = securities.index.tolist()
        prices = get_price(
            security=stocks, end_date=day.isoformat(), count=1, frequency='daily',
            fields=['money'], skip_paused=False, panel=False,
        )
        if prices is None or prices.empty:
            raise ValueError('{} 行情为空。'.format(day))
        if not {'code', 'time', 'money'}.issubset(prices.columns):
            raise ValueError('行情缺少 code、time 或 money 字段。')
        # 防止接口部分返回或返回上一交易日行情，被当成当日完整快照。
        if set(prices['code']) != set(stocks):
            raise ValueError('{} 行情股票与当日列表不一致，停止写入。'.format(day))
        for value in prices['time']:
            if as_date(value) != day:
                raise ValueError('{} 行情日期不一致，停止写入。'.format(day))
        record = summarize_money(zip(prices['code'], prices['money']), top_pct)
        record.update(trade_date=day, updated_at=china_now())
        return record

    def save_day(Session, day, top_pct, record, skip_existing=False):
        _progress_note('写入当日拥挤度')
        if record['trade_date'] != as_date(day) or record['top_pct'] != Decimal(str(top_pct)):
            raise ValueError('结果日期或 TOP_PCT 不一致，拒绝入库。')
        with session_scope(Session) as session:
            query = session.query(MarketCrowding).filter_by(
                trade_date=as_date(day), top_pct=Decimal(str(top_pct)))
            if skip_existing:
                if query.first() is not None:
                    return False
            else:
                query.delete(synchronize_session=False)
            session.add(MarketCrowding(**record))
            session.commit()
            return True

    def get_last_saved_date(Session, top_pct):
        """利用 (top_pct, trade_date) 主键，仅查询当前口径的最新日期。"""
        with session_scope(Session) as session:
            row = session.query(MarketCrowding.trade_date).filter(
                MarketCrowding.top_pct == Decimal(str(top_pct)),
            ).order_by(MarketCrowding.trade_date.desc()).first()
            return as_date(row[0]) if row is not None else None

    def run(start_date=START_DATE, end_date=END_DATE, top_pct=TOP_PCT, incremental=True, skip_existing=True):
        """默认从最后已入库日（含）增量更新；手动补历史时传 incremental=False。"""
        now = china_now()
        today = now.date()
        start = as_date(start_date)
        end = today if end_date is None else as_date(end_date)
        if start > end or end > today:
            raise ValueError('开始日期不能晚于结束日期，结束日期不能晚于运行当天。')
        if skip_existing:
            end = _closed_day(end, now)
        pct = Decimal(str(top_pct))
        if not pct.is_finite() or not Decimal('0') < pct <= Decimal('1'):
            raise ValueError('TOP_PCT 必须在 (0, 1] 内。')
        if pct != pct.quantize(Decimal('0.000001')):
            raise ValueError('TOP_PCT 最多支持六位小数。')
        if not isinstance(incremental, bool):
            raise ValueError('incremental 必须是 True 或 False。')
        records = []
        with database_scope(Base, _config) as Session:
            if incremental:
                last = get_last_saved_date(Session, top_pct)
                if last is not None:
                    if last > today:
                        raise ValueError('数据库最新日期晚于运行当天，请检查已有数据。')
                    start = builtins.max(start, last + datetime.timedelta(days=1) if skip_existing else last)
            else:
                pass
            if start > end:
                return pd.DataFrame(columns=RESULT_COLUMNS)
            # 仅请求实际增量区间的日历；全部验证通过后才计算、写入日记录。
            raw_days = get_trade_days(start_date=start, end_date=end)
            if raw_days is None:
                raise ValueError('获取交易日历失败，未写入数据。')
            days = [as_date(day) for day in raw_days]
            previous = None
            for day in days:
                if day < start or day > end or (previous is not None and day <= previous):
                    raise ValueError('交易日历越界、重复或乱序，未写入数据。')
                previous = day
            if not days:
                return pd.DataFrame(columns=RESULT_COLUMNS)
            # 默认模式刷新末日；skip_existing=True 仅新增，更早缺口不自动扫描。
            for index, day in enumerate(_progress_items(days, '交易日'), 1):
                try:
                    record = calculate_crowding(day, top_pct)
                    written = save_day(Session, day, top_pct, record, skip_existing=skip_existing)
                    if not written:
                        continue
                except Exception as exc:
                    # 不输出数据库异常全文，避免日志泄露账号、地址或 SQL。
                    if isinstance(exc, ValueError):
                        raise
                    raise RuntimeError('{} 采集失败，请检查聚宽接口、数据权限和数据库连接。'.format(day)) from None
                records.append(record)
        return pd.DataFrame(records, columns=RESULT_COLUMNS)
    return SimpleNamespace(**locals())


# ---------- 微盘温度计 ----------
def _microcap_strategy(_apis, _config):
    """独立作用域隔离模型、参数和函数，返回本策略的接口。"""
    settings = strategy_settings('微盘温度计')
    get_all_securities = _apis.get('get_all_securities')
    get_trade_days = _apis.get('get_trade_days')
    get_price = _apis.get('get_price')
    get_extras = _apis.get('get_extras')
    get_fundamentals = _apis.get('get_fundamentals')
    query = _apis.get('query')
    valuation = _apis.get('valuation')
    indicator = _apis.get('indicator')

    START_DATE = settings['start_date']
    END_DATE = settings['end_date']
    MICRO_NUM = settings['micro_num']
    SAMPLE_STEP = settings['sample_step']
    BATCH_SIZE = settings['batch_size']
    RAW_FIELDS = ('micro_median', 'relative_ratio', 'crowding_ratio', 'amount_crowding_ratio')
    PCT_FIELDS = ('cap_pct', 'relative_pct', 'crowding_pct', 'amount_crowding_pct')
    Q = Decimal('0.0000000001')
    Base = declarative_base()

    class MicrocapTemperature(Base):
        __tablename__ = 'microcap_temperature'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}

        series_id = Column(String(64), primary_key=True)
        trade_date = Column(Date, primary_key=True)
        micro_num = Column(Integer, nullable=False)
        sample_step = Column(Integer, nullable=False)
        sample_count = Column(Integer, nullable=False)
        universe_count = Column(Integer, nullable=False)
        profit_growth_count = Column(Integer, nullable=False)
        micro_median = Column(Numeric(24, 10), nullable=False)
        all_median = Column(Numeric(24, 10), nullable=False)
        relative_ratio = Column(Numeric(24, 10), nullable=False)
        crowding_ratio = Column(Numeric(24, 10), nullable=False)
        amount_crowding_ratio = Column(Numeric(24, 10), nullable=False)
        pe_median = Column(Numeric(24, 10), nullable=False)
        pb_median = Column(Numeric(24, 10), nullable=False)
        avg_np_growth = Column(Numeric(24, 10), nullable=True)
        cap_pct = Column(Numeric(18, 10), nullable=False)
        relative_pct = Column(Numeric(18, 10), nullable=False)
        crowding_pct = Column(Numeric(18, 10), nullable=False)
        amount_crowding_pct = Column(Numeric(18, 10), nullable=False)
        score = Column(Numeric(18, 10), nullable=False)
        micro_index = Column(Numeric(24, 8), nullable=False)
        period_return = Column(Numeric(24, 10), nullable=False)
        member_codes = Column(Text, nullable=False)  # JSON 成分股，供下一期指数续算。
        updated_at = Column(DateTime, nullable=False)

    RESULT_COLUMNS = [column.name for column in MicrocapTemperature.__table__.columns
                      if column.name != 'member_codes']

    number = _decimal_number

    def rounded(value):
        return Decimal(value).quantize(Q)

    def series_key(start, micro_num, sample_step):
        return 'micro{}_{}d_{}_v1'.format(micro_num, sample_step, as_date(start).strftime('%Y%m%d'))

    def chunks(codes):
        for offset in range(0, len(codes), BATCH_SIZE):
            yield codes[offset:offset + BATCH_SIZE]

    def calendar(start, end):
        raw = get_trade_days(start_date=as_date(start), end_date=as_date(end))
        if raw is None:
            raise ValueError('交易日历获取失败。')
        days = [as_date(day) for day in raw]
        previous = None
        for day in days:
            if day < as_date(start) or day > as_date(end) or (previous is not None and day <= previous):
                raise ValueError('交易日历越界、重复或乱序。')
            previous = day
        return days

    def fundamental_rows(codes, table, fields, day):
        _progress_note('获取估值和财务指标')
        result = {}
        for batch in chunks(codes):
            q = query(*[getattr(table, field) for field in ('code',) + tuple(fields)]).filter(table.code.in_(batch))
            frame = get_fundamentals(q, date=as_date(day).isoformat())
            if frame is None:
                raise ValueError('{} 财务接口返回空对象。'.format(day))
            if not {'code'}.union(fields).issubset(frame.columns):
                raise ValueError('财务数据缺少请求字段。')
            for row in frame.to_dict('records'):
                code = row['code']
                if code not in batch or code in result:
                    raise ValueError('财务数据股票重复或越界。')
                result[code] = row
        return result

    def price_rows(codes, start, end, field):
        _progress_note('获取历史行情')
        result = {}
        for batch in chunks(codes):
            frame = get_price(
                security=batch, start_date=as_date(start).isoformat(), end_date=as_date(end).isoformat(),
                frequency='daily', fields=[field], skip_paused=False, fq='pre', panel=False,
            )
            if frame is None or frame.empty or not {'code', 'time', field}.issubset(frame.columns):
                raise ValueError('{} 至 {} 的 {} 行情缺失。'.format(start, end, field))
            for row in frame.to_dict('records'):
                day = as_date(row['time'])
                key = (row['code'], day)
                if row['code'] not in batch or day < as_date(start) or day > as_date(end) or key in result:
                    raise ValueError('行情存在重复、日期或股票范围不一致。')
                result[key] = number(row[field])
        return result

    def eligible_codes(day):
        _progress_note('获取股票池及 ST 状态')
        securities = get_all_securities(types=['stock'], date=day.isoformat())
        if securities is None or securities.empty or 'start_date' not in securities.columns:
            raise ValueError('股票列表或上市日期缺失。')
        codes = []
        for code, info in securities.to_dict('index').items():
            # 补充 920 号段，避免北交所代码切换后漏过滤。
            if str(code).startswith(('4', '8', '920')):
                continue
            if (day - as_date(info['start_date'])).days >= 250:
                codes.append(code)
        eligible = []
        for batch in chunks(codes):
            frame = get_extras('is_st', batch, start_date=day.isoformat(), end_date=day.isoformat())
            if frame is None or len(frame.index) != 1 or as_date(frame.index[0]) != day:
                raise ValueError('ST 状态缺失或日期不一致。')
            flags = frame.to_dict('records')[0]
            for code in batch:
                value = flags.get(code)
                if value not in (True, False):
                    raise ValueError('ST 状态无效，停止计算。')
                if not value:
                    eligible.append(code)
        return eligible

    def summarize_universe(rows, turnovers, amounts, micro_num):
        valid = []
        for code, row in rows.items():
            cap, pe, pb = [number(row.get(field)) for field in ('market_cap', 'pe_ratio', 'pb_ratio')]
            today_turnover = number(row.get('turnover_ratio'))
            history = [value for value in turnovers.get(code, []) if value is not None and value >= 0]
            money = amounts.get(code)
            # 沿用原程序 dropna 口径：当期估值、当日换手率、成交额及过去换手率均须有值。
            if cap is None or cap <= 0 or pe is None or pb is None or today_turnover is None:
                continue
            if today_turnover < 0 or not history or money is None or money < 0:
                continue
            valid.append(dict(code=code, market_cap=cap, pe=pe, pb=pb, money=money,
                              turnover=builtins.sum(history) / len(history)))
        if len(valid) < micro_num:
            raise ValueError('完整有效股票不足 {} 只，停止写入。'.format(micro_num))
        valid.sort(key=lambda row: (row['market_cap'], row['code']))
        micro = valid[:micro_num]
        micro_median = median([row['market_cap'] for row in micro])
        all_median = median([row['market_cap'] for row in valid])
        all_turnover = builtins.sum(row['turnover'] for row in valid) / len(valid)
        micro_turnover = builtins.sum(row['turnover'] for row in micro) / micro_num
        total_money = builtins.sum(row['money'] for row in valid)
        if all_turnover <= 0 or total_money <= 0:
            raise ValueError('市场换手率或成交额分母为零，停止写入。')
        metrics = dict(
            universe_count=len(valid), micro_median=rounded(micro_median), all_median=rounded(all_median),
            relative_ratio=rounded(micro_median / all_median),
            crowding_ratio=rounded(micro_turnover / all_turnover),
            amount_crowding_ratio=rounded(builtins.sum(row['money'] for row in micro) / total_money),
            pe_median=rounded(median([row['pe'] for row in micro])),
            pb_median=rounded(median([row['pb'] for row in micro])),
        )
        return metrics, [row['code'] for row in micro]

    def index_return(previous, day, micro_num):
        if previous is None:
            return Decimal('1000.00000000'), Decimal('0')
        codes = json.loads(previous['member_codes'])
        if len(codes) != micro_num or len(set(codes)) != micro_num:
            raise ValueError('上一期成分股数量不一致，无法续算指数。')
        start = as_date(previous['trade_date'])
        prices = price_rows(codes, start, day, 'close')
        returns = []
        for code in codes:
            before, after = prices.get((code, start)), prices.get((code, day))
            if before is None or after is None or before <= 0 or after <= 0:
                raise ValueError('{} 上期成分股 {} 端点价格缺失，停止，不能静默剔除后重配权重。'.format(day, code))
            returns.append(after / before - 1)
        period_return = builtins.sum(returns) / micro_num
        index = (Decimal(previous['micro_index']) * (1 + period_return)).quantize(Decimal('0.00000001'))
        return index, rounded(period_return)

    def calculate_period(day, previous, micro_num=MICRO_NUM):
        day = as_date(day)
        codes = eligible_codes(day)
        if len(codes) < micro_num:
            raise ValueError('筛选后股票不足微盘样本数量。')
        fields = ('market_cap', 'turnover_ratio', 'pe_ratio', 'pb_ratio')
        rows = fundamental_rows(codes, valuation, fields, day)
        if not rows:
            raise ValueError('估值数据为空。')
        raw_window = get_trade_days(end_date=day.isoformat(), count=5)
        window = [] if raw_window is None else [as_date(value) for value in raw_window]
        if len(window) != 5 or window[-1] != day or window != sorted(set(window)):
            raise ValueError('近 5 个交易日窗口无效。')
        turnovers = {}
        for trade_day in window:
            daily = rows if trade_day == day else fundamental_rows(codes, valuation, ('turnover_ratio',), trade_day)
            if not daily:
                raise ValueError('{} 换手率数据为空。'.format(trade_day))
            for code, row in daily.items():
                turnovers.setdefault(code, []).append(number(row.get('turnover_ratio')))
        prices = price_rows(codes, day, day, 'money')
        amounts = {code: prices.get((code, day)) for code in codes}
        metrics, members = summarize_universe(rows, turnovers, amounts, micro_num)
        growth_field = 'inc_net_profit_to_shareholders_year_on_year'
        fundamentals = fundamental_rows(members, indicator, (growth_field,), day)
        growth = [number(row.get(growth_field)) for row in fundamentals.values()]
        growth = [value for value in growth if value is not None]
        metrics.update(
            trade_date=day, micro_num=micro_num, member_codes=json.dumps(members),
            avg_np_growth=rounded(median(growth)) if growth else None,
            profit_growth_count=len(growth), updated_at=china_now(),
        )
        metrics['micro_index'], metrics['period_return'] = index_return(previous, day, micro_num)
        return metrics

    def append_percentiles(record, histories):
        # pandas rank(pct=True, method='average')，包括当前值；相等值取平均名次。
        for raw_field, pct_field in zip(RAW_FIELDS, PCT_FIELDS):
            # 先统一到数据库精度，避免一次性计算与从数据库续算的并列排名不同。
            value = rounded(record[raw_field])
            record[raw_field] = value
            history = histories[raw_field]
            left, right = bisect.bisect_left(history, value), bisect.bisect_right(history, value)
            record[pct_field] = rounded(Decimal(left + right + 2) / (2 * (len(history) + 1)))
            bisect.insort(history, value)
        record['score'] = rounded(record['relative_pct'] * Decimal('0.50')
                                  + record['crowding_pct'] * Decimal('0.25')
                                  + record['amount_crowding_pct'] * Decimal('0.25'))
        record['sample_count'] = len(histories[RAW_FIELDS[0]])

    def load_resume(Session, series_id, include_latest=False):
        with session_scope(Session) as session:
            last = session.query(MicrocapTemperature.trade_date).filter_by(series_id=series_id).order_by(
                MicrocapTemperature.trade_date.desc()).first()
            histories = {field: [] for field in RAW_FIELDS}
            if last is None:
                return None, None, histories
            day = as_date(last[0])
            boundary = (MicrocapTemperature.trade_date <= day if include_latest
                        else MicrocapTemperature.trade_date < day)
            # 只新增时纳入最新一期；从已保存的分位基准和指数继续。
            baseline = session.query(*[getattr(MicrocapTemperature, field) for field in RAW_FIELDS]).filter(
                MicrocapTemperature.series_id == series_id, boundary).all()
            for row in baseline:
                for field, value in zip(RAW_FIELDS, row):
                    histories[field].append(Decimal(value))
            for history in histories.values():
                history.sort()
            prev = session.query(MicrocapTemperature).filter(
                MicrocapTemperature.series_id == series_id, boundary,
            ).order_by(MicrocapTemperature.trade_date.desc()).first()
            previous = None if prev is None else {
                column.name: getattr(prev, column.name) for column in MicrocapTemperature.__table__.columns}
            if previous is not None and previous['sample_count'] != len(baseline):
                raise ValueError('历史样本不连续，请检查历史删除或外部修改，不能直接续算分位。')
            return day, previous, histories

    def save_period(Session, record, skip_existing=False, invalidate_next=None):
        _progress_note('写入当前采样期温度数据')
        required = RESULT_COLUMNS + ['member_codes']
        for field in required:
            if field not in record or (record[field] is None and field != 'avg_np_growth'):
                raise ValueError('快照字段 {} 不完整。'.format(field))
        for field in PCT_FIELDS + ('score',):
            value = number(record[field])
            if value is None or not 0 <= value <= 1:
                raise ValueError('分位或温度不在 0～1 范围内。')
        with session_scope(Session) as session:
            query = session.query(MicrocapTemperature).filter_by(
                series_id=record['series_id'], trade_date=record['trade_date'])
            if skip_existing:
                if query.first() is not None:
                    return False
            else:
                query.delete(synchronize_session=False)
            session.add(MicrocapTemperature(**record))
            mark_daily_pending(session, record['series_id'], invalidate_next)
            session.commit()
            return True

    def mark_daily_pending(session, series_id, day):
        if day is not None:
            # 与当前日期一起提交：即使下一行原计数恰好正确，也能识别尚未完成的派生修复。
            changed = session.query(MicrocapTemperature).filter_by(series_id=series_id, trade_date=day).update(
                {'sample_count': 0, 'updated_at': china_now()}, synchronize_session=False)
            if changed != 1:
                raise ValueError('待衔接日期被其他任务修改，请停止并重跑。')


    def load_daily_history(Session, series_id, end):
        """只读日期、计数及四个分位基准；无缺口时不下载成分股名单。"""
        session = Session()
        try:
            fields = ('trade_date', 'sample_count', 'sample_step') + RAW_FIELDS
            rows = session.query(*[getattr(MicrocapTemperature, field) for field in fields]).filter(
                MicrocapTemperature.series_id == series_id, MicrocapTemperature.trade_date <= end,
            ).order_by(MicrocapTemperature.trade_date).all()
            return {as_date(row[0]): dict(zip(fields, row)) for row in rows}
        finally:
            session.close()


    def stored_period(Session, series_id, day):
        session = Session()
        try:
            row = session.query(MicrocapTemperature).filter_by(series_id=series_id, trade_date=day).first()
            return None if row is None else {
                column.name: getattr(row, column.name) for column in MicrocapTemperature.__table__.columns}
        finally:
            session.close()


    def repair_daily_derived(Session, record, old, invalidate_next=None):
        # 补齐更早日期后，仅修正依赖历史序列的列，不重新采集或覆盖原始指标、成分股。
        fields = ('sample_step', 'sample_count') + PCT_FIELDS + ('score', 'micro_index', 'period_return')
        updates = {field: record[field] for field in fields if record[field] != old[field]}
        if not updates:
            return
        updates['updated_at'] = china_now()
        session = Session()
        try:
            changed = session.query(MicrocapTemperature).filter_by(
                series_id=record['series_id'], trade_date=record['trade_date']).update(updates, synchronize_session=False)
            if changed != 1:
                raise ValueError('每日序列被其他任务修改，请停止并重跑。')
            mark_daily_pending(session, record['series_id'], invalidate_next)
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()


    def collect_daily(Session, start, end, micro_num):
        days = calendar(start, end)
        if not days:
            return pd.DataFrame(columns=RESULT_COLUMNS)
        series_id = series_key(start, micro_num, 1)
        legacy_id = series_key(start, micro_num, 5)
        all_saved = load_daily_history(Session, series_id, china_now().date())
        saved = {day: row for day, row in all_saved.items() if day <= end}
        saved_dates = sorted(all_saved)
        if set(saved) - set(days):
            raise ValueError('已有每日记录不在完整交易日历中，停止写入。')
        # sample_count 也是连续性检查点。补缺后若中断，后续行计数仍旧，下次继续修正。
        first = next((i for i, day in enumerate(days)
                      if day not in saved or saved[day]['sample_count'] != i + 1 or saved[day]['sample_step'] != 1), len(days))
        missing = len(set(days) - set(saved))
        _progress_message('微盘温度计：检查 {} 至 {}，缺失 {} 个交易日。'.format(start, end, missing))
        if first == len(days):
            _progress_note('历史交易日完整，跳过行情采集和写入。')
            return pd.DataFrame(columns=RESULT_COLUMNS)
        histories = {field: sorted(Decimal(saved[day][field]) for day in days[:first]) for field in RAW_FIELDS}
        previous = stored_period(Session, series_id, days[first-1]) if first else None
        legacy = load_daily_history(Session, legacy_id, end)
        records = []
        reused = 0
        for day in _progress_items(days[first:], '补齐交易日'):
            _progress_note('检查已有指标并补采缺失日期 {}'.format(day))
            old = stored_period(Session, series_id, day) if day in saved else None
            record = dict(old) if old is not None else (stored_period(Session, legacy_id, day) if day in legacy else None)
            if record is None:
                record = calculate_period(day, previous, micro_num)
            else:
                # 五日采样的原始指标仍有效；指数必须按前一交易日的成分股衔接。
                record['micro_index'], record['period_return'] = index_return(previous, day, micro_num)
                reused += int(old is None)
            if record['trade_date'] != day or record['micro_num'] != micro_num:
                raise ValueError('计算结果日期或微盘数量不一致。')
            record.update(series_id=series_id, sample_step=1)
            append_percentiles(record, histories)
            following = bisect.bisect_right(saved_dates, day)
            next_day = saved_dates[following] if following < len(saved_dates) else None
            if old is not None:
                repair_daily_derived(Session, record, old, invalidate_next=next_day)
            else:
                record['updated_at'] = china_now()
                if not save_period(Session, record, skip_existing=True, invalidate_next=next_day):
                    raise ValueError('该日期已被其他任务写入，请重跑加载最新基准。')
                records.append({field: record[field] for field in RESULT_COLUMNS})
            previous = record
        _progress_message('每日序列新增 {} 行，其中复用旧采样日原始指标 {} 行。'.format(len(records), reused))
        return pd.DataFrame(records, columns=RESULT_COLUMNS)


    def run(start_date=START_DATE, end_date=END_DATE, micro_num=MICRO_NUM, sample_step=SAMPLE_STEP, skip_existing=True):
        start = as_date(start_date)
        now = china_now()
        today = now.date()
        end = today if end_date is None else as_date(end_date)
        if start > end or end > today:
            raise ValueError('开始日期不能晚于结束日期，结束日期不能晚于今天。')
        if skip_existing:
            end = _closed_day(end, now)
        for value in (micro_num, sample_step):
            if isinstance(value, builtins.bool) or not isinstance(value, builtins.int) or value <= 0:
                raise ValueError('微盘数量和采样步长必须是正整数。')
        series_id = series_key(start, micro_num, sample_step)
        records = []
        with database_scope(Base, _config) as Session:
            if sample_step == 1:
                return collect_daily(Session, start, end, micro_num)
            last, previous, histories = load_resume(Session, series_id, include_latest=skip_existing)
            if last is not None and last > today:
                raise ValueError('最新入库日期晚于今天，请检查已有数据。')
            begin = last or start
            if begin > end:
                return pd.DataFrame(columns=RESULT_COLUMNS)
            days = calendar(begin, end)
            if last is not None and (not days or days[0] != last):
                raise ValueError('最新入库日不在交易日历中，停止写入。')
            pending = days[sample_step::sample_step] if skip_existing and last is not None else days[::sample_step]
            for day in _progress_items(pending, '采样日'):
                try:
                    record = calculate_period(day, previous, micro_num)
                    if record['trade_date'] != day or record['micro_num'] != micro_num:
                        raise ValueError('计算结果日期或微盘数量不一致。')
                    record.update(series_id=series_id, sample_step=sample_step)
                    append_percentiles(record, histories)
                    if not save_period(Session, record, skip_existing=skip_existing):
                        raise ValueError('该采样期已被其他任务写入，未覆盖；请重跑以加载已保存的指数和分位基准。')
                except Exception as exc:
                    if isinstance(exc, ValueError):
                        raise
                    raise RuntimeError('{} 采集失败，请检查聚宽接口、权限及数据库连接。'.format(day)) from None
                previous = record
                records.append({field: record[field] for field in RESULT_COLUMNS})
        return pd.DataFrame(records, columns=RESULT_COLUMNS)
    return SimpleNamespace(**locals())


# ---------- ETF份额 ----------
def _etf_strategy(_apis, _config):
    """独立作用域隔离模型、参数和函数，返回本策略的接口。"""
    settings = strategy_settings('ETF份额')
    get_trade_days = _apis.get('get_trade_days')
    query = _apis.get('query')
    finance = _apis.get('finance')

    START_DATE = settings['start_date']
    INITIAL_TRADE_DAYS = settings['initial_trade_days']
    END_DATE = settings['end_date']
    LOOKBACK_DAYS = settings['lookback_days']
    WINDOW_SIZE = settings['window_size']
    ETF_GROUPS = settings['funds']
    SOURCE = 'jqdata.FUND_SHARE_DAILY'
    Base = declarative_base()

    class ETFShare(Base):
        __tablename__ = 'etf_share_daily'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        fund_code = Column(String(16), primary_key=True)
        trade_date = Column(Date, primary_key=True)
        fund_name = Column(String(128), nullable=False)
        shares = Column(BigInteger, nullable=False)  # 原始单位：份；不用亿元或成交量替代。
        previous_trade_date = Column(Date, nullable=False)
        source = Column(String(48), nullable=False)
        updated_at = Column(DateTime, nullable=False)

    RESULT_COLUMNS = [column.name for column in ETFShare.__table__.columns]

    def share_table():
        try:
            apis = (finance, query, get_trade_days)
        except NameError:
            apis = (None,)
        if builtins.any(api is None for api in apis):
            raise ValueError('缺少聚宽研究 API，请在聚宽 Notebook 直接粘贴运行，或使用 %run -i。')
        table = getattr(finance, 'FUND_SHARE_DAILY', None)
        if table is None or builtins.any(not hasattr(table, field) for field in ('code', 'date', 'shares')):
            raise ValueError('当前环境未提供 finance.FUND_SHARE_DAILY 或所需字段，请核对聚宽数据权限。')
        return table

    def validate_days(raw, start, end):
        if raw is None:
            raise ValueError('交易日历接口返回空对象。')
        days = [as_date(value) for value in raw]
        if days != sorted(set(days)) or builtins.any(day < start or day > end for day in days):
            raise ValueError('交易日历重复、越界或乱序。')
        return days

    def trading_days(start, end):
        return validate_days(get_trade_days(start_date=start, end_date=end), start, end)

    def previous_days(start, count):
        raw = get_trade_days(end_date=start, count=count)
        if raw is None:
            raise ValueError('交易日历获取失败。')
        return validate_days(raw, datetime.date(1900, 1, 1), start)

    def normalize_shares(value):
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError('基金份额不是有效数字。') from None
        if not number.is_finite() or number < 0 or number != number.to_integral_value() or number > 9223372036854775807:
            raise ValueError('基金份额缺失、非整数、负数或超出 BIGINT 范围。')
        return int(number)

    def fetch_window(code, name, days, before, require_start=False):
        table = share_table()
        frame = finance.run_query(query(table).filter(
            table.code == code, table.date >= days[0], table.date <= days[-1],
        ).order_by(table.date).limit(len(days) + 1))
        if frame is None or not {'code', 'date', 'shares'}.issubset(frame.columns):
            raise ValueError('ETF 份额接口未返回有效表格或缺少必要字段。')
        records = []
        allowed = set(days)
        seen = set()
        for item in frame.to_dict('records'):
            day = as_date(item['date'])
            if item['code'] != code or day not in allowed or day in seen:
                raise ValueError('ETF 份额返回代码、日期越界或重复记录。')
            seen.add(day)
            records.append(dict(fund_code=code, fund_name=name, trade_date=day,
                                shares=normalize_shares(item['shares']), previous_trade_date=before[day],
                                source=SOURCE, updated_at=china_now()))
        records.sort(key=lambda record: record['trade_date'])
        if records:
            first, last = records[0]['trade_date'], records[-1]['trade_date']
            expected = {day for day in days if first <= day <= last}
            if seen != expected or (require_start and first != days[0]):
                raise ValueError('{} 份额历史中间缺失，停止该基金，不能跳过缺口更新进度。'.format(code))
        return records

    def last_dates(Session, codes):
        with session_scope(Session) as session:
            return {code: (first, last) for code, first, last in session.query(
                ETFShare.fund_code, func.min(ETFShare.trade_date), func.max(ETFShare.trade_date),
            ).filter(ETFShare.fund_code.in_(codes)).group_by(ETFShare.fund_code).all()}

    def save_window(Session, code, records, skip_existing=False):
        _progress_note('写入当前份额窗口')
        if not records:
            return []
        dates = [record['trade_date'] for record in records]
        if len(set(dates)) != len(dates) or builtins.any(r['fund_code'] != code or r['previous_trade_date'] >= r['trade_date'] for r in records):
            raise ValueError('待写入份额快照不一致。')
        with session_scope(Session) as session:
            query = session.query(ETFShare).filter(ETFShare.fund_code == code, ETFShare.trade_date.in_(dates))
            if skip_existing:
                existing = {row[0] for row in query.with_entities(ETFShare.trade_date).all()}
                records = [record for record in records if record['trade_date'] not in existing]
                if not records:
                    return []
            else:
                # 旧模式只替换源实际返回的日期；不补零。
                query.delete(synchronize_session=False)
            session.add_all([ETFShare(**record) for record in records])
            session.commit()
            return records

    def probe(code='510300.XSHG', end_date=None):
        """只测试聚宽最近份额接口，不连接 MySQL；用于确认权限、日期和份额单位。"""
        end = china_now().date() if end_date is None else as_date(end_date)
        days = previous_days(end, 10)
        if len(days) < 2:
            raise ValueError('交易日历不足，无法验证。')
        records = fetch_window(code, code, days[1:], dict(zip(days[1:], days[:-1])))
        return pd.DataFrame(records, columns=RESULT_COLUMNS)

    def run(start_date=START_DATE, end_date=END_DATE, incremental=True, lookback_days=LOOKBACK_DAYS, groups=None, skip_existing=True):
        today = china_now().date()
        end = today if end_date is None else as_date(end_date)
        start = None if start_date is None else as_date(start_date)
        if end > today or (start is not None and start > end):
            raise ValueError('开始日期不能晚于结束日期，结束日期不能晚于今天。')
        if not isinstance(incremental, builtins.bool) or isinstance(lookback_days, builtins.bool) or not isinstance(lookback_days, builtins.int) or lookback_days < 1:
            raise ValueError('incremental 必须为布尔值，回查交易日数必须为正整数。')
        selected = list(ETF_GROUPS) if groups is None else list(groups)
        if not selected or builtins.any(group not in ETF_GROUPS for group in selected):
            raise ValueError('分组仅支持 hs300、zz500。')
        funds = dict(pair for group in selected for pair in ETF_GROUPS[group])
        share_table()  # 在连接数据库前检查环境。
        if start is None:
            initial_days = previous_days(end, INITIAL_TRADE_DAYS)
            if not initial_days:
                raise ValueError('无法取得首次采集范围的交易日历。')
            start = initial_days[0]
        output, failures = [], []
        with database_scope(Base, _config) as Session:
            latest = last_dates(Session, list(funds)) if incremental else {}
            for code, name in _progress_items(list(funds.items()), '基金', lambda item: '{} {}'.format(*item)):
                try:
                    first, last = latest.get(code, (None, None))
                    if last and as_date(last) > today:
                        raise ValueError('最新入库日期晚于今天。')
                    if last and as_date(last) > end:
                        continue
                    begin = start
                    if last:
                        recent = previous_days(as_date(last), 1 if skip_existing else lookback_days)
                        if not recent or recent[-1] != as_date(last):
                            raise ValueError('最新记录不在交易日历中。')
                        # 已有数据以水位为准；即使一年以上没运行，也不能跳过中间缺口。
                        if skip_existing:
                            begin = as_date(last) + datetime.timedelta(days=1)
                            if start_date is not None:
                                begin = builtins.max(start, begin)
                        else:
                            begin = builtins.max(as_date(first), recent[0]) if start_date is None else builtins.max(start, recent[0])
                    if begin > end:
                        continue
                    days = trading_days(begin, end)
                    if not days:
                        continue
                    prior = previous_days(days[0] - datetime.timedelta(days=1), 1)
                    if not prior:
                        raise ValueError('无法确定首日的前一交易日。')
                    before = dict(zip(days, prior + days[:-1]))
                    # 回查可能早于基金首条有效数据；已有记录本身作为连续性锚点。
                    seen_data = builtins.bool(last and first and days[0] >= as_date(first))
                    for offset in range(0, len(days), WINDOW_SIZE):
                        window = days[offset:offset + WINDOW_SIZE]
                        _progress_note('份额窗口 {} / {}：{} 至 {}'.format(offset//WINDOW_SIZE+1, (len(days)+WINDOW_SIZE-1)//WINDOW_SIZE, window[0], window[-1]))
                        records = fetch_window(code, name, window, before, require_start=seen_data)
                        if records:
                            # 已知最新日期处于此窗口时，接口应仍包含它，防止覆盖不完整快照。
                            if last and window[0] <= as_date(last) <= window[-1] and as_date(last) not in {r['trade_date'] for r in records}:
                                raise ValueError('源数据未包含已入库的最新日期，停止该基金。')
                            written = save_window(Session, code, records, skip_existing=skip_existing)
                            output.extend(written)
                            seen_data = True
                            if records[-1]['trade_date'] < window[-1]:
                                break
                        elif seen_data or last:
                            break
                except Exception:
                    # 每只基金独立恢复；不打印数据库连接或原始驱动异常。
                    failures.append(code)
            if failures:
                raise RuntimeError('以下 ETF 未完成：{}。成功窗口已提交，修复后重跑可续传。'.format(', '.join(failures)))
        return pd.DataFrame(output, columns=RESULT_COLUMNS)
    return SimpleNamespace(**locals())


# ---------- 净利润断层 ----------
def _earnings_strategy(_apis, _config):
    """独立作用域隔离模型、参数和函数，返回本策略的接口。"""
    settings = strategy_settings('净利润断层')
    get_all_securities = _apis.get('get_all_securities')
    get_trade_days = _apis.get('get_trade_days')
    get_price = _apis.get('get_price')
    get_industry = _apis.get('get_industry')
    get_fundamentals = _apis.get('get_fundamentals')
    query = _apis.get('query')
    valuation = _apis.get('valuation')
    indicator = _apis.get('indicator')
    income = _apis.get('income')
    finance = _apis.get('finance')
    jy = _apis.get('jy')

    END_DATE = settings['end_date']
    RULE_VERSION = 'announcement-open-v1'
    PAGE_SIZE = settings['page_size']
    Base = declarative_base()

    class EarningsRun(Base):
        __tablename__ = 'earnings_gap_run'
        report_period = Column(Date, primary_key=True)
        as_of_date = Column(Date, nullable=False)
        updated_at = Column(DateTime, nullable=False)
        universe_count = Column(Integer, nullable=False)
        matched_count = Column(Integer, nullable=False)
        rule_version = Column(String(32), nullable=False)

    class EarningsStock(Base):
        __tablename__ = 'earnings_gap_stock'
        __table_args__ = (
            Index('ix_earnings_period_industry', 'report_period', 'industry_code'),
            Index('ix_earnings_period_signal', 'report_period', 'signal_date'),
        )
        report_period = Column(Date, primary_key=True)
        code = Column(String(16), primary_key=True)
        name = Column(String(128), nullable=False)
        industry_code = Column(String(32), nullable=True)
        industry_name = Column(String(128), nullable=True)
        industry_level = Column(String(16), nullable=True)
        signal_date = Column(Date, nullable=False)
        announcement_date = Column(Date, nullable=False)
        signal_source = Column(String(16), nullable=False)
        profit_basis = Column(String(32), nullable=False)
        profit_yoy = Column(Numeric(24, 6), nullable=False)
        net_profit = Column(Numeric(28, 4), nullable=True)
        open_gap_pct = Column(Numeric(20, 6), nullable=False)
        intraday_pct = Column(Numeric(20, 6), nullable=False)
        market_cap = Column(Numeric(24, 6), nullable=True)
        circulating_market_cap = Column(Numeric(24, 6), nullable=True)
        pe_ttm = Column(Numeric(24, 6), nullable=True)
        pb = Column(Numeric(24, 6), nullable=True)
        valuation_date = Column(Date, nullable=False)
        # 兼容旧表的非空列；不再生成分析内容，新记录写入空字符串。
        analysis = Column(Text, nullable=False, default='')
        analysis_basis = Column(String(32), nullable=False, default='')
        events = Column(JSON, nullable=False)
        prices = Column(JSON, nullable=False)

    def latest_period(day):
        # 当前季度开始前一天；10 月运行统一取 9 月 30 日。
        return dt.date(day.year, ((day.month - 1) // 3) * 3 + 1, 1) - dt.timedelta(days=1)

    number = _float_number

    def clean(value):
        # 原始字段用于审计；JSON 不允许 NaN/Infinity。
        if isinstance(value, dict):
            return {str(k): clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value]
        if value is None or pd.isna(value):
            return None
        if isinstance(value, (dt.datetime, dt.date)):
            return value.isoformat()
        if isinstance(value, (str, bool)):
            return value
        return number(value)

    checked_rows = _checked_records

    def paged(table, filters, fetch):
        key = getattr(table, 'id', None)
        if key is None:
            key = getattr(table, 'ID', None)
        if key is None:
            raise ValueError('数据表缺少分页主键，不能保证完整采集。')
        label = {'SecuMain': '公司代码映射', 'LC_FSPerformedLetters': '业绩快报',
                 'STK_FIN_FORCAST': '业绩预告'}.get(getattr(table, '__tablename__', ''), '公告分页')
        fetch_page = lambda cursor: fetch(query(table).filter(*filters).filter(key > cursor).order_by(key).limit(PAGE_SIZE))
        for frame in _source_pages(fetch_page, key=key.key, label=label, progress=_progress_note, fields=(key.key,)):
            yield from frame.to_dict('records')

    SOURCE_NAMES = {'express': '业绩快报', 'forecast': '业绩预告', 'formal': '正式财报'}

    def event(code, kind, pub, period, profit, growth, raw, growth_max=None):
        profit, growth = number(profit), number(growth)
        eligible = growth is not None and growth >= 20 and (
            kind == 'forecast' or (profit is not None and profit > 0))
        # 仅明确原因字段作为经营归因，绝不把任意摘要当成原因。
        reason_field = next((f for f in ('change_reason', 'profit_change_reason', 'reason')
                             if isinstance(raw.get(f), str) and raw[f].strip()), None)
        return dict(code=code, kind=kind, publication_date=str(as_date(pub)),
                    report_period=str(period),
                    source_table={'express': 'jy.LC_FSPerformedLetters', 'forecast': 'finance.STK_FIN_FORCAST',
                                  'formal': 'income + indicator'}[kind],
                    net_profit=profit, profit_yoy=growth,
                    profit_yoy_max=number(growth_max), eligible=eligible,
                    profit_basis='单季度' if kind == 'formal' else '报告期累计',
                    reason=raw[reason_field].strip() if reason_field else None,
                    reason_field=reason_field, raw=clean(raw), signal=None)

    def fetch_events(codes, period, as_of):
        _progress_note('获取本季度公告及财报')
        events = []
        code_map = {code[:6]: code for code in codes}
        code_set = set(codes)
        _progress_note('获取公司代码映射')
        t = jy.SecuMain
        mappings = paged(t, [t.SecuCategory == 1], jy.run_query)
        companies = {}
        for row in mappings:
            code = code_map.get(str(row['SecuCode']))
            if code:
                companies.setdefault(row['CompanyCode'], set()).add(code)
        if not companies:
            raise ValueError('A 股公司代码映射为空，不能覆盖数据库。')
        cutoff = as_of + dt.timedelta(days=1)
        t = jy.LC_FSPerformedLetters
        for row in paged(t, [t.EndDate == period, t.InfoPublDate < cutoff], jy.run_query):
            for code in sorted(companies.get(row['CompanyCode'], [])):
                events.append(event(code, 'express', row['InfoPublDate'], period,
                                    row['NPParentCompanyOwners'], row['NPCOwnersYOY'], row))
        t = finance.STK_FIN_FORCAST
        for row in paged(t, [t.end_date == period, t.pub_date <= as_of], finance.run_query):
            if row['code'] in code_set:
                # 不猜测可选预告金额字段的单位；原始字段完整保存在 raw。
                events.append(event(row['code'], 'forecast', row['pub_date'], period, None,
                                    row['profit_ratio_min'], row, row.get('profit_ratio_max')))
        for start in _progress_items(range(0, len(codes), 300), '正式财报批次', lambda offset: '股票 {}—{}'.format(offset+1, builtins.min(offset+300, len(codes)))):
            q = query(income.code, income.pubDate.label('publication_date'),
                      income.statDate.label('report_period'),
                      income.np_parent_company_owners.label('net_profit'),
                      indicator.inc_net_profit_to_shareholders_year_on_year.label('profit_yoy'),
                      indicator.pubDate.label('indicator_publication_date'),
                      indicator.statDate.label('indicator_period')).filter(
                          income.code.in_(codes[start:start+300]), income.statDate == period,
                          indicator.statDate == period)
            frame = get_fundamentals(q, date=as_of)
            for row in checked_rows(frame, ['code', 'publication_date', 'report_period', 'net_profit',
                                            'profit_yoy', 'indicator_publication_date', 'indicator_period'], '正式财报'):
                if pd.isna(row['publication_date']):
                    continue
                if as_date(row['report_period']) != period or as_date(row['indicator_period']) != period:
                    raise ValueError('财报与指标的报告期不一致。')
                pub = builtins.max(as_date(row['publication_date']), as_date(row['indicator_publication_date']))
                if pub <= as_of:
                    events.append(event(row['code'], 'formal', pub, period, row['net_profit'], row['profit_yoy'], row))
        unique = {json.dumps(e, ensure_ascii=False, sort_keys=True): e for e in events}
        return sorted(unique.values(), key=lambda e: (e['code'], e['publication_date'], e['kind']))

    def price_signal(previous_close, open_price, close_price):
        values = [number(v) for v in (previous_close, open_price, close_price)]
        if builtins.any(v is None or v <= 0 for v in values):
            return None
        previous, opening, closing = [Decimal(str(v)) for v in values]
        # 精确等于 3% 不通过，避免二进制浮点边界误差。
        if opening <= previous * Decimal('1.03') or closing < opening:
            return None
        return dict(open_gap_pct=float((opening / previous - 1) * 100),
                    intraday_pct=float((closing / opening - 1) * 100))

    def screen(codes, names, events, period, as_of):
        grouped = {}
        for e in events:
            if e['report_period'] != str(period) or as_date(e['publication_date']) > as_of or e['code'] not in names:
                raise ValueError('公告报告期、股票代码或截止日期异常。')
            grouped.setdefault(e['code'], []).append(e)
        results = []
        for index, (code, disclosures) in enumerate(_progress_items(sorted(grouped.items()), '股票筛选', lambda item: item[0]), 1):
            qualifying = [e for e in disclosures if e['eligible']]
            if not qualifying:
                continue
            first = builtins.min(as_date(e['publication_date']) for e in qualifying)
            calendar = [as_date(d) for d in get_trade_days(start_date=first-dt.timedelta(days=40), end_date=as_of)]
            if not calendar or calendar != sorted(set(calendar)):
                raise ValueError('交易日历为空或顺序异常。')
            frame = get_price(code, start_date=calendar[0], end_date=as_of, frequency='daily',
                              fields=['open', 'close', 'high', 'low', 'paused', 'volume'],
                              skip_paused=False, fq='pre')
            checked_rows(frame, ['open', 'close', 'high', 'low', 'paused', 'volume'], '股票行情')
            bars = {as_date(day): row.to_dict() for day, row in frame.iterrows()}
            hits = []
            for e in qualifying:
                pub = as_date(e['publication_date'])
                # 原策略的公告日行情；非交易日公告对应下一个交易日，不倒取公告前行情。
                day = next((d for d in calendar if d >= pub), None)
                if day is None:
                    continue
                position = calendar.index(day)
                if position == 0:
                    continue
                previous = calendar[position-1]
                current, prior = bars.get(day), bars.get(previous)
                if current is None or prior is None:
                    raise ValueError('{} 行情缺失，取消本次入库。'.format(code))
                if number(current['paused']) is None:
                    raise ValueError('{} 停牌状态缺失。'.format(code))
                if number(current['volume']) is None:
                    raise ValueError('{} 成交量缺失。'.format(code))
                if current['paused'] or number(current['volume']) == 0:
                    continue
                if builtins.any(number(current[k]) is None or number(current[k]) <= 0 for k in ('open', 'close', 'high', 'low')):
                    raise ValueError('{} 已成交日的价格不完整。'.format(code))
                # 上市前缺失前收盘，不构造跳空信号；不延迟到复牌日追认。
                signal = price_signal(prior['close'], current['open'], current['close'])
                if signal:
                    signal['date'] = str(day)
                    e['signal'] = signal
                    hits.append(e)
            if not hits:
                continue
            selected = sorted(hits, key=lambda e: (e['signal']['date'], e['signal']['open_gap_pct'], e['kind']))[-1]
            industry_data = get_industry([code], date=as_of)
            if not isinstance(industry_data, dict) or code not in industry_data:
                raise ValueError('{} 行业接口返回异常。'.format(code))
            industries = industry_data[code]
            level = next((k for k in ('sw_l3', 'sw_l2', 'sw_l1') if industries.get(k)), None)
            industry = industries.get(level, {})
            signal_day = as_date(selected['signal']['date'])
            offset = calendar.index(signal_day)
            chart_days = calendar[builtins.max(0, offset-10):offset+21]
            prices = [dict(date=str(d), **{k: number(bars[d][k]) for k in ('open', 'close', 'high', 'low')})
                      for d in chart_days if d in bars]
            record = dict(report_period=period, code=code, name=names[code],
                          industry_code=industry.get('industry_code'), industry_name=industry.get('industry_name'),
                          industry_level=level, signal_date=signal_day,
                          announcement_date=as_date(selected['publication_date']), signal_source=selected['kind'],
                          profit_basis=selected['profit_basis'], profit_yoy=selected['profit_yoy'],
                          net_profit=selected['net_profit'], open_gap_pct=selected['signal']['open_gap_pct'],
                          intraday_pct=selected['signal']['intraday_pct'], valuation_date=as_of,
                          events=disclosures, prices=prices)
            results.append(record)
        selected_codes = [r['code'] for r in results]
        valuations = {}
        for start in _progress_items(range(0, len(selected_codes), 300), '入选股票估值批次', lambda offset: '股票 {}—{}'.format(offset+1, builtins.min(offset+300, len(selected_codes)))):
            q = query(valuation.code, valuation.market_cap, valuation.circulating_market_cap,
                      valuation.pe_ratio, valuation.pb_ratio).filter(valuation.code.in_(selected_codes[start:start+300]))
            frame = get_fundamentals(q, date=as_of)
            valuations.update({r['code']: r for r in checked_rows(frame,
                ['code', 'market_cap', 'circulating_market_cap', 'pe_ratio', 'pb_ratio'], '估值')})
        for r in results:
            v = valuations.get(r['code'], {})
            for target, source in [('market_cap', 'market_cap'), ('circulating_market_cap', 'circulating_market_cap'),
                                   ('pe_ttm', 'pe_ratio'), ('pb', 'pb_ratio')]:
                r[target] = number(v.get(source))
        return results

    def database_record(record):
        """按表定义量化全部 DECIMAL 列，保留 Decimal 交给 MySQL 驱动。"""
        result = dict(record)
        for column in EarningsStock.__table__.columns:
            if not isinstance(column.type, Numeric):
                continue
            value = record.get(column.name)
            if value is None:
                if not column.nullable:
                    raise ValueError('{} 不允许为空。'.format(column.name))
                result[column.name] = None
                continue
            precision, scale = column.type.precision, column.type.scale
            try:
                decimal_value = Decimal(str(value))
                limit = Decimal(10) ** (precision - scale)
                if not decimal_value.is_finite() or decimal_value.copy_abs() >= limit:
                    raise ValueError('{} 不是有限值或超出字段范围。'.format(column.name))
                with localcontext() as context:
                    context.prec = precision + 2
                    rounded = decimal_value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
                if rounded.copy_abs() >= limit:
                    raise ValueError('{} 四舍五入后超出字段范围。'.format(column.name))
            except (InvalidOperation, TypeError):
                raise ValueError('{} 无法转换为数据库定点数。'.format(column.name)) from None
            result[column.name] = rounded
        return result

    def save_results(Session, period, as_of, universe_count, records, skip_existing=False):
        _progress_note('写入本季度新入选股票')
        if len({r['code'] for r in records}) != len(records):
            raise ValueError('筛选结果包含重复股票。')
        for r in records:
            if (r['report_period'] != period or r['valuation_date'] != as_of
                    or r['signal_date'] > as_of or r['announcement_date'] > r['signal_date']
                    or r['open_gap_pct'] <= 3 or r['intraday_pct'] < 0 or r['profit_yoy'] < 20):
                raise ValueError('筛选结果校验失败，拒绝覆盖数据库。')
        # 先验证原始筛选阈值，再量化写入值；任何转换失败均发生在删除旧快照之前。
        # 不修改原始 records 和公告 JSON，也不把 Decimal 转回 float。
        db_records = [database_record(r) for r in records]
        with session_scope(Session) as session:
            if skip_existing:
                # 股票快照只插入首次命中的结果；不覆盖、不删除已入库股票。
                existing = {row[0] for row in session.query(EarningsStock.code).filter(
                    EarningsStock.report_period == period).all()}
                fresh = [record for record in records if record['code'] not in existing]
                fresh_db = [record for record in db_records if record['code'] not in existing]
                run = session.query(EarningsRun).filter(EarningsRun.report_period == period).first()
                total = len(existing) + len(fresh_db)
                # 元数据用于网页展示累计命中数量和扫描截止日；仅值有变化时更新。
                metadata_changed = False
                if run is None:
                    session.add(EarningsRun(report_period=period, as_of_date=as_of, updated_at=china_now(),
                        universe_count=universe_count, matched_count=total, rule_version=RULE_VERSION))
                    metadata_changed = True
                else:
                    values = {'matched_count': total}
                    if as_of >= run.as_of_date:
                        values.update(as_of_date=as_of, universe_count=universe_count, rule_version=RULE_VERSION)
                    for key, value in values.items():
                        if getattr(run, key) != value:
                            setattr(run, key, value)
                            metadata_changed = True
                    if metadata_changed:
                        run.updated_at = china_now()
                if not fresh_db and not metadata_changed:
                    return []
                session.add_all([EarningsStock(**record) for record in fresh_db])
                session.commit()
                return fresh
            # 清空与重建只针对当前季度，并在同一个事务提交，合法零命中也有元数据。
            session.query(EarningsStock).filter(EarningsStock.report_period == period).delete(synchronize_session=False)
            session.query(EarningsRun).filter(EarningsRun.report_period == period).delete(synchronize_session=False)
            session.add_all([EarningsStock(**r) for r in db_records])
            session.add(EarningsRun(report_period=period, as_of_date=as_of, updated_at=china_now(),
                                   universe_count=universe_count, matched_count=len(records), rule_version=RULE_VERSION))
            session.commit()
            return records

    def run(end_date=END_DATE, skip_existing=True):
        now = china_now()
        requested = as_date(end_date) if end_date is not None else now.date()
        if requested > now.date():
            raise ValueError('结束日期不能晚于今天。')
        period = latest_period(requested)
        cutoff = _closed_day(requested, now)
        days = get_trade_days(end_date=cutoff, count=1)
        if days is None or len(days) != 1:
            raise ValueError('无法获取最近完整交易日。')
        as_of = as_date(days[0])
        universe = get_all_securities(types=['stock'], date=as_of)
        if universe is None or universe.empty or 'display_name' not in universe.columns:
            raise ValueError('A 股股票池获取失败。')
        # 平台提供的全部 A 股，不使用原行业白名单、黑名单、ST/持仓或数量限制。
        names = {str(code): str(row['display_name']) for code, row in universe.iterrows()}
        codes = sorted(names)
        if not skip_existing:
            events = fetch_events(codes, period, as_of)
            records = screen(codes, names, events, period, as_of)
        with database_scope(Base, _config) as Session:
            # 显式指定 InnoDB，确保旧数据库默认引擎不会破坏整批事务。
            if skip_existing:
                with session_scope(Session) as session:
                    existing = {row[0] for row in session.query(EarningsStock.code).filter(
                        EarningsStock.report_period == period).all()}
                _progress_note('已有 {} 只股票，本次仅筛选尚未入库股票'.format(len(existing)))
                pending = [code for code in codes if code not in existing]
                events = fetch_events(pending, period, as_of) if pending else []
                records = screen(pending, names, events, period, as_of) if pending else []
            records = save_results(Session, period, as_of, len(codes), records, skip_existing=skip_existing)
        return pd.DataFrame(records)
    return SimpleNamespace(**locals())


# ---------- 微盘新高新低 ----------
def _breadth_strategy(_apis, _config):
    """微盘 400 新高新低：每日动态选股，一次历史行情计算四个周期。"""
    settings = strategy_settings('微盘新高新低')
    get_all_securities = _apis.get('get_all_securities')
    get_trade_days = _apis.get('get_trade_days')
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
        raw = get_trade_days(**args)
        if raw is None:
            raise ValueError('交易日历返回空对象。')
        days = [as_date(d) for d in raw]
        if (days != sorted(set(days)) or builtins.any(d > end or (start and d < start) for d in days)
                or (count is not None and len(days) != count)):
            raise ValueError('交易日历缺失、重复或越界。')
        return days

    def choose_stocks(day, micro_num):
        _progress_note('获取股票池、ST 状态和市值')
        securities = get_all_securities(types=['stock'], date=day)
        if securities is None or securities.empty or 'start_date' not in securities.columns:
            raise ValueError('股票列表为空或缺少上市日期。')
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
                if pd.isna(flag) or flag not in (True, False):
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

    def calculate_day(day, micro_num=MICRO_NUM):
        codes = choose_stocks(day, micro_num)
        calendar = trade_calendar(end=day, count=31)
        if calendar[-1] != day:
            raise ValueError('采集日期不是交易日。')
        counts = {'{}_{}'.format(side, period): 0 for period in PERIODS for side in ('high', 'low')}
        for offset in range(0, len(codes), PRICE_BATCH_SIZE):
            batch = codes[offset:offset+PRICE_BATCH_SIZE]
            _progress_note('行情批次 {} / {}'.format(offset//PRICE_BATCH_SIZE+1, (len(codes)+PRICE_BATCH_SIZE-1)//PRICE_BATCH_SIZE))
            frame = get_price(batch, start_date=calendar[0], end_date=day, frequency='daily',
                              fields=['close'], fq='pre', skip_paused=False, panel=False)
            for key, value in count_prices(frame, batch, calendar).items():
                counts[key] += value
        return dict(counts, series_id=series_key(micro_num), trade_date=day, sample_count=micro_num,
                    member_codes=json.dumps(codes), updated_at=china_now())

    def last_date(Session, series_id):
        with session_scope(Session) as session:
            return session.query(func.max(MicrocapBreadth.trade_date)).filter_by(series_id=series_id).scalar()

    def save_day(Session, record):
        _progress_note('写入当日新高新低统计')
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
        with session_scope(Session) as session:
            if session.query(MicrocapBreadth).filter_by(
                    series_id=record['series_id'], trade_date=record['trade_date']).first() is not None:
                return False
            session.add(MicrocapBreadth(**record))
            session.commit()
            return True

    def run(start_date=START_DATE, end_date=END_DATE, micro_num=MICRO_NUM, skip_existing=True):
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
            latest = last_date(Session, series_key(micro_num))
            if latest and latest > now.date():
                raise ValueError('数据库最新日期晚于今天。')
            start = latest+datetime.timedelta(days=1) if latest else initial
            if start > end:
                return pd.DataFrame(columns=RESULT_COLUMNS)
            for day in _progress_items(trade_calendar(start=start, end=end), '交易日'):
                record = calculate_day(day, micro_num)
                # 每日一个事务；任何缺失或失败立即停止，不越过缺失日续传。
                if save_day(Session, record):
                    output.append({key: record[key] for key in RESULT_COLUMNS})
        return pd.DataFrame(output, columns=RESULT_COLUMNS)
    return SimpleNamespace(**locals())


# ---------- 高股息选股 ----------
def _dividend_strategy(_apis, _config):
    """两组高股息 Top25；每次重算，事务替换最新快照，不下单。"""
    settings = strategy_settings('高股息选股')
    get_all_securities, get_trade_days = _apis.get('get_all_securities'), _apis.get('get_trade_days')
    get_price, get_extras = _apis.get('get_price'), _apis.get('get_extras')
    get_fundamentals, get_industry = _apis.get('get_fundamentals'), _apis.get('get_industry')
    query, valuation, indicator, finance = [_apis.get(k) for k in ('query', 'valuation', 'indicator', 'finance')]
    BATCH_SIZE, PAGE_SIZE = settings['batch_size'], settings['page_size']
    if BATCH_SIZE > 3000 or PAGE_SIZE > 3000:
        raise ValueError('batch_size 和 page_size 不得超过 3000。')
    END_DATE = settings['end_date']
    RULE_VERSION = 'dividend_top25_v2'
    METHODS = ('classic', 'value')
    Base = declarative_base()

    class DividendSnapshot(Base):
        __tablename__ = 'dividend_selection_snapshot'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        id = Column(Integer, primary_key=True)
        trade_date = Column(Date, nullable=False)
        updated_at = Column(DateTime, nullable=False)
        rule_version = Column(String(32), nullable=False)
        universe_count = Column(Integer, nullable=False)
        eligible_count = Column(Integer, nullable=False)
        classic_candidates = Column(Integer, nullable=False)
        value_candidates = Column(Integer, nullable=False)
        classic_count = Column(Integer, nullable=False)
        value_count = Column(Integer, nullable=False)

    class DividendStock(Base):
        __tablename__ = 'dividend_selection_stock'
        __table_args__ = (Index('ux_dividend_method_rank', 'method', 'rank', unique=True),
                          {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'})
        method = Column(String(16), primary_key=True)
        code = Column(String(16), primary_key=True)
        trade_date = Column(Date, nullable=False)
        rank = Column(Integer, nullable=False)
        name = Column(String(128), nullable=False)
        industry_code = Column(String(32), nullable=True)
        industry_name = Column(String(128), nullable=True)
        industry_level = Column(String(16), nullable=True)
        dividend_yield_1y = Column(Numeric(24, 6), nullable=False)
        dividend_ratio_3y = Column(Numeric(24, 6), nullable=False)
        dividend_cash_1y = Column(Numeric(28, 6), nullable=False)
        dividend_cash_3y = Column(Numeric(28, 6), nullable=False)
        pe_ttm = Column(Numeric(24, 6), nullable=False)
        pb = Column(Numeric(24, 6), nullable=True)
        market_cap = Column(Numeric(24, 6), nullable=False)
        close = Column(Numeric(24, 6), nullable=False)
        revenue_growth = Column(Numeric(24, 6), nullable=True)
        profit_growth = Column(Numeric(24, 6), nullable=True)
        adjusted_roe = Column(Numeric(24, 6), nullable=True)
        peg = Column(Numeric(24, 6), nullable=True)
        report_period = Column(Date, nullable=True)
        publication_date = Column(Date, nullable=True)

    RESULT_COLUMNS = [c.name for c in DividendStock.__table__.columns]
    META_COLUMNS = [c.name for c in DividendSnapshot.__table__.columns if c.name not in ('id', 'updated_at')]

    number = _decimal_number

    def rounded(value):
        return _round_decimal(value, precision=40)

    optional_date = _optional_date

    rows = _checked_records

    def by_code(frame, batch, required, label, complete=True):
        values = rows(frame, ['code']+list(required), label)
        result = {}
        for row in values:
            code = str(row['code'])
            if code not in batch or code in result:
                raise ValueError('{} 包含重复或范围外代码。'.format(label))
            result[code] = row
        if complete and set(result) != set(batch):
            raise ValueError('{} 未完整返回，保留数据库原快照。'.format(label))
        return result

    def universe(day):
        _progress_note('读取历史股票池')
        frame = get_all_securities(types=['stock'], date=day)
        if frame is None or frame.empty or 'display_name' not in frame.columns:
            raise ValueError('股票列表为空或缺少名称。')
        names = {str(code): str(row['display_name']) for code, row in frame.iterrows()}
        if len(names) != len(frame):
            raise ValueError('股票列表代码重复。')
        # 无真实持仓，不使用原交易策略的持仓豁免；两个筛选共用可选池。
        codes = sorted(code for code, name in names.items() if not code.startswith(('4', '8'))
                       and not builtins.any(tag in name for tag in ('ST', '*', '退')))
        quotes, caps = {}, {}
        for offset in _progress_items(range(0, len(codes), BATCH_SIZE), '股票池批次'):
            batch = codes[offset:offset+BATCH_SIZE]
            st = get_extras('is_st', batch, start_date=day, end_date=day, df=True)
            if (st is None or len(st.index) != 1 or as_date(st.index[0]) != day
                    or set(st.columns) != set(batch) or len(st.columns) != len(batch)):
                raise ValueError('ST 状态不完整。')
            # 价格仅用于展示；不按停牌、开盘价或涨跌停状态筛选。
            # skip_paused=False 保留停牌股票，由数据源提供填充后的收盘价。
            prices = by_code(get_price(batch, start_date=day, end_date=day, frequency='daily',
                                      fields=['close'], fq=None, skip_paused=False, panel=False),
                             batch, ('time', 'close'), '行情')
            eligible = []
            for code in batch:
                flag = st.iloc[0][code]
                bar = prices[code]
                if pd.isna(flag) or flag not in (True, False) or as_date(bar['time']) != day:
                    raise ValueError('ST 状态或行情日期无效。')
                if flag:
                    continue
                closing = number(bar['close'])
                if closing is None or closing <= 0:
                    raise ValueError('收盘价缺失或无效。')
                eligible.append(code)
                quotes[code] = closing
            if eligible:
                q = query(valuation.code, valuation.market_cap, valuation.pe_ratio, valuation.pb_ratio).filter(valuation.code.in_(eligible))
                caps.update(by_code(get_fundamentals(q, date=day), eligible,
                                    ('market_cap', 'pe_ratio', 'pb_ratio'), '估值'))
        if not caps:
            raise ValueError('可选股票池为空，拒绝用异常空池覆盖结果。')
        for row in caps.values():
            if number(row['market_cap']) is None or number(row['market_cap']) <= 0:
                raise ValueError('总市值缺失或无效。')
        return names, quotes, caps

    def dividends(codes, day):
        table = finance.STK_XR_XD
        start3, start1 = day-datetime.timedelta(days=1095), day-datetime.timedelta(days=365)
        total = {}
        for offset in _progress_items(range(0, len(codes), BATCH_SIZE), '分红批次'):
            batch = codes[offset:offset+BATCH_SIZE]
            fields = ('id', 'code', 'a_registration_date', 'a_xr_date', 'bonus_amount_rmb')
            selection = query(*[getattr(table, f) for f in fields]).filter(
                table.code.in_(batch),
                or_(and_(table.a_registration_date >= start3, table.a_registration_date <= day),
                    and_(table.a_xr_date > start1, table.a_xr_date <= day)))
            fetch_page = lambda cursor: finance.run_query(selection.filter(table.id > cursor).order_by(table.id).limit(PAGE_SIZE))
            for frame in _source_pages(fetch_page, label='分红明细', progress=_progress_note, fields=fields, page_size=PAGE_SIZE):
                page = frame.to_dict('records')
                for row in page:
                    code = str(row['code'])
                    if code not in batch:
                        raise ValueError('分红代码越界。')
                    registration = optional_date(row['a_registration_date'])
                    ex_date = optional_date(row['a_xr_date'])
                    in3 = registration is not None and start3 <= registration <= day
                    in1 = ex_date is not None and start1 < ex_date <= day
                    if not (in3 or in1):
                        raise ValueError('分红日期越界。')
                    # 只有登记/除息日期已发生的记录；空派现金额沿用原程序按 0 处理。
                    cash = Decimal(0) if pd.isna(row['bonus_amount_rmb']) else number(row['bonus_amount_rmb'])
                    if cash is None or cash < 0:
                        raise ValueError('派现金额无效。')
                    item = total.setdefault(code, dict(cash_1y=Decimal(0), cash_3y=Decimal(0), has_3y=False))
                    if in3:
                        item['cash_3y'] += cash
                        item['has_3y'] = True
                    if in1:
                        item['cash_1y'] += cash
        return total

    def financials(codes, day):
        result = {}
        fields = ('inc_total_revenue_year_on_year', 'inc_net_profit_year_on_year', 'inc_return', 'statDate', 'pubDate')
        for offset in _progress_items(range(0, len(codes), BATCH_SIZE), '财务指标批次'):
            batch = codes[offset:offset+BATCH_SIZE]
            q = query(indicator.code, *[getattr(indicator, f) for f in fields]).filter(indicator.code.in_(batch))
            result.update(by_code(get_fundamentals(q, date=day), batch, fields, '财务指标', complete=False))
        if codes and not result:
            raise ValueError('全股票池财务指标为空，保留原快照。')
        for row in result.values():
            pub = optional_date(row['pubDate'])
            period = optional_date(row['statDate'])
            if (pub and pub > day) or (period and period > day):
                raise ValueError('财务指标包含未来日期。')
        return result

    def select(caps, cash, fundamentals):
        # 比较使用未舍入值；相同分红比例按代码排序，结果稳定。
        ratios = {}
        for code, cap in caps.items():
            item = cash.get(code, {})
            market_cap = number(cap['market_cap'])
            if market_cap is None or market_cap <= 0:
                raise ValueError('市值无效。')
            ratios[code] = (item.get('cash_1y', Decimal(0))/100/market_cap,
                            item.get('cash_3y', Decimal(0))/100/market_cap)
        dividend_codes = sorted((c for c in caps if cash.get(c, {}).get('has_3y')),
                                key=lambda c: (-ratios[c][1], c))
        classic = []
        for code in dividend_codes[:len(dividend_codes)//10]:
            pe = number(caps[code]['pe_ratio'])
            metrics = fundamentals.get(code, {})
            revenue, profit, roe = [number(metrics.get(f)) for f in
                                   ('inc_total_revenue_year_on_year', 'inc_net_profit_year_on_year', 'inc_return')]
            if None not in (pe, revenue, profit, roe) and revenue > 3 and profit > 8 and roe > Decimal('4.5'):
                if Decimal('.1') < pe/profit < 2:
                    classic.append(code)
        value = sorted((c for c in caps if number(caps[c]['pe_ratio']) is not None
                        and 0 < number(caps[c]['pe_ratio']) < 8 and ratios[c][0] > 4),
                       key=lambda c: (-ratios[c][0], c))
        return dict(classic=classic[:25], value=value[:25]), ratios, dict(classic=len(classic), value=len(value))

    def calculate(day):
        names, quotes, caps = universe(day)
        codes = sorted(caps)
        cash = dividends(codes, day)
        fundamentals = financials(codes, day)
        selected, ratios, counts = select(caps, cash, fundamentals)
        chosen = sorted(set(selected['classic']+selected['value']))
        _progress_note('补充细分行业并生成两组 Top25')
        industries = get_industry(chosen, date=day) if chosen else {}
        if not isinstance(industries, dict) or set(chosen)-set(industries):
            raise ValueError('入选股票行业查询未完整返回。')
        records = []
        for method in METHODS:
            for rank, code in enumerate(selected[method], 1):
                info = industries.get(code) or {}
                level = next((k for k in ('sw_l3', 'sw_l2', 'sw_l1') if info.get(k)), None)
                industry = info.get(level, {})
                cap, fin, payout = caps[code], fundamentals.get(code, {}), cash.get(code, {})
                profit = number(fin.get('inc_net_profit_year_on_year'))
                pe = number(cap['pe_ratio'])
                records.append(dict(method=method, code=code, trade_date=day, rank=rank, name=names[code],
                    industry_code=industry.get('industry_code'), industry_name=industry.get('industry_name'), industry_level=level,
                    dividend_yield_1y=rounded(ratios[code][0]), dividend_ratio_3y=rounded(ratios[code][1]),
                    dividend_cash_1y=rounded(payout.get('cash_1y', 0)), dividend_cash_3y=rounded(payout.get('cash_3y', 0)),
                    pe_ttm=rounded(pe), pb=rounded(cap['pb_ratio']), market_cap=rounded(cap['market_cap']), close=rounded(quotes[code]),
                    revenue_growth=rounded(fin.get('inc_total_revenue_year_on_year')), profit_growth=rounded(profit),
                    adjusted_roe=rounded(fin.get('inc_return')), peg=rounded(pe/profit) if profit and profit > 0 else None,
                    report_period=optional_date(fin.get('statDate')), publication_date=optional_date(fin.get('pubDate'))))
        meta = dict(trade_date=day, rule_version=RULE_VERSION, universe_count=len(names), eligible_count=len(caps),
                    classic_candidates=counts['classic'], value_candidates=counts['value'],
                    classic_count=len(selected['classic']), value_count=len(selected['value']))
        return meta, records

    def save_latest(Session, meta, records):
        if len({(r['method'], r['code']) for r in records}) != len(records):
            raise ValueError('同组股票重复。')
        for method in METHODS:
            group = [r for r in records if r['method'] == method]
            if len(group) != meta[method+'_count'] or len(group) > 25 or sorted(r['rank'] for r in group) != list(range(1, len(group)+1)):
                raise ValueError('数量或排名与快照不一致。')
        if builtins.any(r['method'] not in METHODS or r['trade_date'] != meta['trade_date'] for r in records):
            raise ValueError('方法或日期不一致。')
        _progress_note('事务更新两组最新选股结果')
        with session_scope(Session) as session:
            head = session.query(DividendSnapshot).filter_by(id=1).with_for_update().first()
            if head is not None and head.trade_date > meta['trade_date']:
                raise ValueError('数据库已有更新日期，拒绝旧日期覆盖。')
            existing = session.query(DividendStock).all()
            def signature(values):
                return sorted(tuple(r[c] for c in RESULT_COLUMNS) for r in values)
            old_records = [{c: getattr(r, c) for c in RESULT_COLUMNS} for r in existing]
            if head is not None and all(getattr(head, c) == meta[c] for c in META_COLUMNS) and signature(old_records) == signature(records):
                return False
            if head is None:
                head = DividendSnapshot(id=1, updated_at=china_now(), **meta)
                session.add(head)
                session.flush()  # 首次并发创建冲突时整个事务回滚，不覆盖他人结果。
            else:
                for key, value in meta.items():
                    setattr(head, key, value)
                head.updated_at = china_now()
            for previous in existing:
                session.expunge(previous)
            session.query(DividendStock).delete(synchronize_session=False)
            session.add_all([DividendStock(**r) for r in records])
            session.commit()
            return True

    def run(end_date=END_DATE):
        now = china_now()
        end = as_date(end_date) if end_date is not None else now.date()
        if end > now.date():
            raise ValueError('结束日期不能晚于今天。')
        end = _closed_day(end, now)
        _progress_note('确定最近完整交易日')
        days = get_trade_days(end_date=end, count=1)
        if days is None or len(days) != 1 or as_date(days[0]) > end:
            raise ValueError('交易日历无效。')
        day = as_date(days[0])
        with database_scope(Base, _config) as Session:
            for current in _progress_items([day], '最新选股日'):
                meta, records = calculate(current)
                if not save_latest(Session, meta, records):
                    _progress_note('选股结果未变化，未重复写入')
        return pd.DataFrame(records, columns=RESULT_COLUMNS)
    return SimpleNamespace(**locals())


# ---------- 高增长 ----------
def _growth_strategy(_apis, _config):
    """四类累计财报，各取最新三期；归母净利润和基本 EPS 双增长。"""
    settings = strategy_settings('高增长')
    get_all_securities, get_trade_days, get_fundamentals, get_industry, query, valuation = [
        _apis.get(k) for k in ('get_all_securities', 'get_trade_days', 'get_fundamentals', 'get_industry', 'query', 'valuation')]
    BATCH_SIZE, PAGE_SIZE = settings['batch_size'], settings['page_size']
    if BATCH_SIZE > 500 or PAGE_SIZE > 3000:
        raise ValueError('batch_size 不得超过 500，page_size 不得超过 3000。')
    END_DATE = settings['end_date']
    RULE_VERSION = 'growth_cumulative_parent_eps_v1'
    GROUPS = {'annual': (12, 31), 'q1': (3, 31), 'q2': (6, 30), 'q3': (9, 30)}
    Base = declarative_base()

    class GrowthSnapshot(Base):
        __tablename__ = 'high_growth_snapshot'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        id = Column(Integer, primary_key=True)
        as_of_date = Column(Date, nullable=False)
        valuation_date = Column(Date, nullable=False)
        updated_at = Column(DateTime, nullable=False)
        rule_version = Column(String(48), nullable=False)
        universe_count = Column(Integer, nullable=False)
        evaluated_count = Column(Integer, nullable=False)
        matched_count = Column(Integer, nullable=False)


    class GrowthStock(Base):
        __tablename__ = 'high_growth_stock'
        __table_args__ = (Index('ux_high_growth_rank', 'rank', unique=True),
                          {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'})
        code = Column(String(16), primary_key=True)
        as_of_date = Column(Date, nullable=False)
        rank = Column(Integer, nullable=False)
        name = Column(String(128), nullable=False)
        industry_code = Column(String(32), nullable=True)
        industry_name = Column(String(128), nullable=True)
        industry_level = Column(String(16), nullable=True)
        pe_ttm = Column(Numeric(24, 6), nullable=True)
        pb = Column(Numeric(24, 6), nullable=True)
        market_cap = Column(Numeric(24, 6), nullable=True)
        annual_profit_growth = Column(Numeric(28, 8), nullable=False)
        reports = Column(JSON, nullable=False)

    RESULT_COLUMNS = [c.name for c in GrowthStock.__table__.columns]
    META_COLUMNS = [c.name for c in GrowthSnapshot.__table__.columns if c.name not in ('id', 'updated_at')]

    number = _decimal_number

    rounded = _round_decimal

    rows = _checked_records

    def latest_reports(source, as_of):
        # 先选最新三期再检查质量，不可用旧的合格报表顶替最新不合格报表。
        unique = {}
        for raw in source:
            period, pub = as_date(raw['report_period']), as_date(raw['publication_date'])
            if (period.month, period.day) not in GROUPS.values() or period > as_of or pub > as_of:
                raise ValueError('财报报告期或公告日期无效。')
            if pub < period:
                raise ValueError('财报公告早于报告期末。')
            row = dict(raw, report_period=period, publication_date=pub)
            old = unique.get(period)
            if old is None or pub > old['publication_date']:
                unique[period] = row
            elif pub == old['publication_date'] and builtins.any(number(row[k]) != number(old[k]) for k in ('net_profit', 'eps')):
                raise ValueError('相同报告期和公告日期存在冲突财务数据。')
        selected = {}
        for name, month_day in GROUPS.items():
            group = sorted((r for d, r in unique.items() if (d.month, d.day) == month_day), key=lambda r: r['report_period'])[-3:]
            if len(group) != 3:
                return None
            years = [r['report_period'].year for r in group]
            if years != list(range(years[0], years[0]+3)):
                return None  # 同比必须比较连续年份的同类报告。
            if builtins.any(number(r[k]) is None for r in group for k in ('net_profit', 'eps')):
                return None
            selected[name] = group
        return selected

    def evaluate(groups):
        if groups is None:
            return None
        evidence = {}
        annual_growth = None
        for name, group in groups.items():
            if builtins.any(number(group[-1][key]) <= 0 for key in ('net_profit', 'eps')):
                return None
            output = []
            for i, row in enumerate(group):
                record = {key: str(row[key]) for key in ('report_period', 'publication_date')}
                for field, target in (('net_profit', 'profit_growth'), ('eps', 'eps_growth')):
                    value = number(row[field])
                    record[field] = str(value)
                    record[target] = None
                    if i:
                        previous = number(group[i-1][field])
                        if previous == 0:
                            return None
                        # 先用原值交叉相乘判断，避免把 19.999...% 四舍五入成达标。
                        if (value-previous)*100 < builtins.abs(previous)*20:
                            return None
                        rate = (value-previous)/builtins.abs(previous)*100
                        record[target] = str(rounded(rate, 8))
                        if name == 'annual' and i == 2 and field == 'net_profit':
                            annual_growth = rate
                output.append(record)
            evidence[name] = output
        return annual_growth, evidence

    def select(all_reports, as_of):
        chosen, evaluated = [], 0
        for code in _progress_items(sorted(all_reports), '检查四类财报'):
            groups = latest_reports(all_reports[code], as_of)
            if groups is not None:
                evaluated += 1
                result = evaluate(groups)
                if result is not None:
                    chosen.append((code, result[0], result[1]))
        chosen.sort(key=lambda item: (-item[1], item[0]))
        return chosen, evaluated

    def financial_reports(codes, as_of):
        # 聚宽合并利润表：不使用 *_PARENT 母公司报表，也不把单季度 EPS 相加。
        finance = _apis.get('finance')
        if finance is None or query is None:
            raise RuntimeError('当前环境缺少 finance/query，请在聚宽研究环境运行。')
        table = finance.STK_INCOME_STATEMENT
        fields = ('id', 'code', 'pub_date', 'start_date', 'end_date', 'report_date',
                  'report_type', 'np_parent_company_owners', 'basic_eps')
        missing = [f for f in fields if not hasattr(table, f)]
        if missing:
            raise ValueError('聚宽累计利润表缺少字段：{}'.format(', '.join(missing)))
        result = {code: [] for code in codes}
        for offset in _progress_items(range(0, len(codes), BATCH_SIZE), '累计财报批次'):
            pending = set(codes[offset:offset+BATCH_SIZE])
            cursor = None
            while pending:
                _progress_note('倒序读取累计财报：本批待完成 {} 只'.format(len(pending)))
                q = query(*[getattr(table, f) for f in fields]).filter(
                    table.code.in_(sorted(pending)), table.pub_date <= as_of,
                    table.end_date <= as_of, table.report_date == table.end_date)
                if cursor is not None:
                    q = q.filter(or_(table.end_date < cursor[0],
                                     and_(table.end_date == cursor[0], table.id < cursor[1])))
                q = q.order_by(table.end_date.desc(), table.id.desc()).limit(PAGE_SIZE)
                page = rows(finance.run_query(q), fields, '累计利润表')
                if not page:
                    break
                keys = [(as_date(r['end_date']), int(r['id'])) for r in page]
                if len(page) > PAGE_SIZE or keys != sorted(set(keys), reverse=True) or (cursor is not None and keys[0] >= cursor):
                    raise ValueError('累计财报分页重复、乱序或超限。')
                for raw in page:
                    code = str(raw['code'])
                    period, pub = as_date(raw['end_date']), as_date(raw['pub_date'])
                    report_date, start = as_date(raw['report_date']), as_date(raw['start_date'])
                    if code not in pending or period > as_of or pub > as_of or report_date != period:
                        raise ValueError('累计财报返回越界股票、未来数据或比较期数据。')
                    # 必须从当年 1 月 1 日开始，排除半年/三季报告内的单季度栏。
                    if start != datetime.date(period.year, 1, 1) or (period.month, period.day) not in GROUPS.values():
                        continue
                    result[code].append(dict(report_period=period, publication_date=pub,
                        net_profit=raw['np_parent_company_owners'], eps=raw['basic_eps']))
                cursor = keys[-1]
                completed = set()
                for code in pending:
                    values = result[code]
                    keep = set()
                    enough = True
                    for month_day in GROUPS.values():
                        periods = sorted({r['report_period'] for r in values
                                          if (r['report_period'].month, r['report_period'].day) == month_day}, reverse=True)[:3]
                        keep.update(periods)
                        enough = enough and len(periods) == 3
                    result[code] = [r for r in values if r['report_period'] in keep]
                    # 游标越过最早所需报告期，才证明该期所有更正版本均已读取。
                    if enough and cursor[0] < builtins.min(keep):
                        completed.add(code)
                pending -= completed
                if len(page) < PAGE_SIZE:
                    break
        return result

    def calculate(as_of, valuation_day):
        _progress_note('读取 A 股股票池')
        frame = get_all_securities(types=['stock'], date=as_of)
        if frame is None or frame.empty or 'display_name' not in frame.columns:
            raise ValueError('股票池为空或缺少名称，保留原快照。')
        names = {str(code): str(row['display_name']) for code, row in frame.iterrows()}
        if len(names) != len(frame):
            raise ValueError('股票代码重复。')
        # get_all_securities(stock) 的平台 A 股范围，不额外剔除 ST、科创板、停牌或涨跌停。
        all_reports = financial_reports(sorted(names), as_of)
        if not builtins.any(all_reports.values()):
            raise ValueError('全部财报为空，保留原快照。')
        chosen, evaluated = select(all_reports, as_of)
        if evaluated == 0:
            raise ValueError('没有任何股票取得完整四类三期财报，检查数据权限及字段；保留原快照。')
        codes = [item[0] for item in chosen]
        caps, industries = {}, {}
        for offset in _progress_items(range(0, len(codes), BATCH_SIZE), '补充估值与行业'):
            batch = codes[offset:offset+BATCH_SIZE]
            fields = ('code', 'pe_ratio', 'pb_ratio', 'market_cap')
            q = query(*[getattr(valuation, f) for f in fields]).filter(valuation.code.in_(batch))
            values = rows(get_fundamentals(q, date=valuation_day), fields, '估值')
            for row in values:
                code = str(row['code'])
                if code not in batch or code in caps:
                    raise ValueError('估值包含重复或越界股票。')
                caps[code] = row
            if set(batch)-set(caps):
                raise ValueError('估值未完整返回，保留原快照。')
            result = get_industry(batch, date=as_of)
            if not isinstance(result, dict) or set(batch)-set(result):
                raise ValueError('行业未完整返回，保留原快照。')
            industries.update(result)
        records = []
        for rank, (code, growth, reports) in enumerate(chosen, 1):
            info = industries.get(code) or {}
            level = next((k for k in ('sw_l3', 'sw_l2', 'sw_l1') if info.get(k)), None)
            industry = info.get(level, {})
            cap = caps.get(code, {})
            records.append(dict(code=code, as_of_date=as_of, rank=rank, name=names[code],
                industry_code=industry.get('industry_code'), industry_name=industry.get('industry_name'), industry_level=level,
                pe_ttm=rounded(cap.get('pe_ratio')), pb=rounded(cap.get('pb_ratio')), market_cap=rounded(cap.get('market_cap')),
                annual_profit_growth=rounded(growth, 8), reports=reports))
        meta = dict(as_of_date=as_of, valuation_date=valuation_day, rule_version=RULE_VERSION,
                    universe_count=len(names), evaluated_count=evaluated, matched_count=len(records))
        return meta, records

    def save_latest(Session, meta, records):
        if (len({r['code'] for r in records}) != len(records) or len(records) != meta['matched_count']
                or sorted(r['rank'] for r in records) != list(range(1, len(records)+1))
                or builtins.any(r['as_of_date'] != meta['as_of_date'] for r in records)):
            raise ValueError('股票重复、日期、数量或排名与快照不一致。')
        _progress_note('事务更新最新高增长名单')
        with session_scope(Session) as session:
            head = session.query(GrowthSnapshot).filter_by(id=1).with_for_update().first()
            if head is not None and head.as_of_date > meta['as_of_date']:
                raise ValueError('数据库已有更新日期，拒绝旧日期覆盖。')
            existing = session.query(GrowthStock).all()
            def signature(values):
                return sorted(tuple(json.dumps(r[c], sort_keys=True, ensure_ascii=False) if c == 'reports' else r[c]
                                    for c in RESULT_COLUMNS) for r in values)
            old = [{c: getattr(r, c) for c in RESULT_COLUMNS} for r in existing]
            if head is not None and builtins.all(getattr(head, c) == meta[c] for c in META_COLUMNS) and signature(old) == signature(records):
                return False
            if head is None:
                head = GrowthSnapshot(id=1, updated_at=china_now(), **meta)
                session.add(head)
                session.flush()  # 并发首次创建时唯一键冲突会回滚，避免交叉覆盖。
            else:
                for key, value in meta.items():
                    setattr(head, key, value)
                head.updated_at = china_now()
            for previous in existing:
                session.expunge(previous)
            session.query(GrowthStock).delete(synchronize_session=False)
            session.add_all([GrowthStock(**r) for r in records])
            session.commit()
            return True

    def run(end_date=END_DATE):
        now = china_now()
        as_of = as_date(end_date) if end_date is not None else now.date()
        if as_of > now.date():
            raise ValueError('结束日期不能晚于今天。')
        valuation_end = _closed_day(as_of, now)
        days = get_trade_days(end_date=valuation_end, count=1)
        if days is None or len(days) != 1 or as_date(days[0]) > valuation_end:
            raise ValueError('交易日历无效。')
        # 全部采集成功后再进入写入流程，任何源接口失败都不会清空旧名单。
        meta, records = calculate(as_of, as_date(days[0]))
        with database_scope(Base, _config) as Session:
            if not save_latest(Session, meta, records):
                _progress_note('名单与指标未变化，未重复写入')
        return pd.DataFrame(records, columns=RESULT_COLUMNS)
    return SimpleNamespace(**locals())




def _etf_heat_strategy(_apis, _config):
    finance, query = _apis.get('finance'), _apis.get('query')
    AS_OF_DATE = None

    LOOKBACK_DAYS = 15

    MAX_STALE_TRADE_DAYS = 3

    BATCH_SIZE = 50

    PAGE_SIZE = 2000

    EXPORT_CSV = True

    MIN_SIZE_YI = Decimal('30')

    CROSS_BORDER_CODES = {
        '513100.XSHG', '159509.XSHE', '513030.XSHG', '513520.XSHG',
        '513690.XSHG', '513130.XSHG', '159550.XSHE',
    }

    BOND_CODES = {'511220.XSHG'}

    INCLUDE_LOF = True

    EXTRA_LOF_CODES = set()

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
        print('截止日期：{}；聚宽 ETF 数量：{}'.format(days[-1], len(universe)), flush=True)
        print('规模门槛：严格大于 {} 亿元；跨境七只白名单；债券仅城投债。'.format(MIN_SIZE_YI))
        print('ETF 规模 = 同日份额 × 单位净值；LOF = 最新已披露对应份额类别净资产。', flush=True)
        records, audit = target_catalog(universe, days[-1])
        daily_records = records
        lof_records, lof_audit = lof_catalog(lof, days[-1]) if not lof.empty else ([], [])
        audit.extend(lof_audit)
        records = daily_records + lof_records
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
        for code in FOCUS_CODES:
            chosen = next((r for r in selected if r['code'] == code), None)
            record = next((r for r in records if r['code'] == code), None)
            relevant = [r for r in audit if r.get('code') == code or
                        (record is not None and r.get('group') == record['group'])]
            if chosen:
                status = '入选，规模 {:.4f} 亿元'.format(chosen['size_yi'])
            elif relevant:
                status = '; '.join(r['reason'] + ' ' + str(r.get('details', '')) for r in relevant)
            elif record:
                status = '同方向已有更大规模基金入选'
            else:
                status = '未识别，请检查 audit_df'
            print('关注基金 {}：{}'.format(code, status))
        result, review, all_candidates = pd.DataFrame(selected), pd.DataFrame(audit), pd.DataFrame(candidates)
        if not result.empty:
            cols = ['category', 'target_name', 'code', 'name', 'size_yi', 'size_date', 'group_count', 'size_source']
            for category in CATEGORIES:
                section = result[result.category == category]
                print('\n{}：{} 只'.format(category, len(section)))
                print(section[cols].to_string(index=False, formatters={'size_yi': lambda v: '{:.2f}'.format(v)}))
        print('\n选出 {} 只；待核对/排除 {} 条，完整明细见 audit_df。'.format(len(result), len(review)))
        print('结果仅覆盖聚宽已识别、数据完整的候选，不代表已经核验全市场覆盖。')
        if not review.empty:
            print(review.head(30).to_string(index=False))
        if EXPORT_CSV:
            for label, frame in [('结果', result), ('待核对', review), ('候选明细', all_candidates)]:
                frame.to_csv('ETF分类去重_{}_{}.csv'.format(label, days[-1]), index=False, encoding='utf-8-sig')
        return result, review, all_candidates
    # Collect and calculate in JoinQuant; MySQL contains final display results.
    settings = strategy_settings('ETF动量热度')
    EXPORT_CSV = False
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
                    changed = session.query(Heat).filter(
                        Heat.trade_date == row['trade_date'], Heat.code == row['code'],
                        Heat.score.is_(None)).update(values, synchronize_session=False)
                    if changed:
                        repaired.append(row)
            session.commit()
            if repaired:
                _progress_note('修复 {} 条历史空热度，已有有效得分保持不变'.format(len(repaired)))
            return added + repaired


    def get_retention_start(reference_date):
        """Validate the complete calendar before allowing any database deletion."""
        count = settings['retention_trade_days']
        reference = as_date(reference_date)
        raw = api('get_trade_days')(end_date=reference, count=count)
        days = [] if raw is None else [as_date(day) for day in raw]
        if len(days) != count or days != sorted(set(days)) or days[-1] > reference:
            raise ValueError('保留范围交易日历不足、重复、乱序或含未来日期，未清理数据库')
        return days[0]

    def cleanup_old_data(Session, retention_start):
        """Remove expired results for all funds, including funds outside the current pool."""
        with session_scope(Session) as session:
            deleted = session.query(Heat).filter(Heat.trade_date < retention_start).delete(synchronize_session=False)
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



def _ownership_strategy(_apis, _config):
    """披露持仓、股东户数与社保新进；仅公开披露口径，无交易操作。"""
    settings = strategy_settings('个股机构持仓')
    finance, query = _apis.get('finance'), _apis.get('query')
    price_universes = {}  # Report-date stock membership, reused across stock batches.
    Base = declarative_base()

    class OwnershipStock(Base):
        __tablename__ = 'ownership_stock'
        code = Column(String(16), primary_key=True)
        name = Column(String(128), nullable=False, index=True)
        industry = Column(String(128))
        updated_at = Column(DateTime, nullable=False)

    class OwnershipReport(Base):
        __tablename__ = 'ownership_report'
        code = Column(String(16), primary_key=True)
        report_date = Column(Date, primary_key=True, index=True)
        payload = Column(JSON, nullable=False)
        updated_at = Column(DateTime, nullable=False)

    class OwnershipHolders(Base):
        __tablename__ = 'ownership_holders'
        code = Column(String(16), primary_key=True)
        end_date = Column(Date, primary_key=True)
        pub_date = Column(Date)
        payload = Column(JSON, nullable=False)
        updated_at = Column(DateTime, nullable=False)

    class Checkpoint(Base):
        __tablename__ = 'ownership_checkpoint'
        key = Column(String(32), primary_key=True)
        as_of = Column(Date, nullable=False)

    class DisclosureProbe(Base):
        __tablename__ = 'ownership_disclosure_probe'
        key = Column(String(16), primary_key=True)
        payload = Column(JSON, nullable=False)
        as_of = Column(Date, nullable=False)

    def disclosure_signature(as_of):
        # Six one-row queries, no holdings payload or all-stock history scan.
        result = {'history_years': settings['history_years'], 'refresh_quarters': settings['refresh_quarters']}
        for name, table, period in (
                ('fund', finance.FUND_PORTFOLIO_STOCK, finance.FUND_PORTFOLIO_STOCK.period_end),
                ('float', finance.STK_SHAREHOLDER_FLOATING_TOP10, finance.STK_SHAREHOLDER_FLOATING_TOP10.end_date),
                ('holders', finance.STK_HOLDER_NUM, finance.STK_HOLDER_NUM.end_date)):
            filters = [period <= as_of, or_(table.pub_date == None, table.pub_date <= as_of)]
            markers = []
            for order in ((table.id.desc(),), (table.pub_date.desc(), table.id.desc())):
                df = finance.run_query(query(table.id, table.pub_date).filter(*filters).order_by(*order).limit(1))
                if df is None:
                    raise ValueError('新增披露检查失败：' + name)
                if df.empty:
                    markers.append(None)
                else:
                    row = df.iloc[0]
                    pub = day(row['pub_date'])
                    markers.append([int(row['id']), pub.isoformat() if pub else None])
            result[name] = markers
        return result

    class SyncHead(Base):
        __tablename__ = 'ownership_sync_head'
        id = Column(String(16), primary_key=True)
        generation = Column(String(32), nullable=False)
        revision = Column(BigInteger, nullable=False)

    class Change(Base):
        __tablename__ = 'ownership_change'
        table_name = Column(String(32), primary_key=True)
        code = Column(String(16), primary_key=True)
        record_date = Column(Date, primary_key=True)
        revision = Column(BigInteger, nullable=False, index=True)
        deleted = Column(Integer, nullable=False)

    def install_change_tracking(Session):
        """Coalesced change keys, committed atomically with the business rows.

        The locked head serializes writers before any tracked DML. Unlike an
        auto-increment event ID, its committed order cannot skip a slow writer.
        """
        with session_scope(Session) as session:
            if session.query(SyncHead).filter_by(id='main').first() is None:
                session.add(SyncHead(id='main',generation=uuid.uuid4().hex,revision=0))
                session.commit()
        tracked = (OwnershipStock,OwnershipReport,OwnershipHolders)
        def before_flush(session, flush_context, instances):
            pending = [r for r in session.new if isinstance(r,tracked)]
            pending.extend(r for r in session.dirty if isinstance(r,tracked) and session.is_modified(r,include_collections=False))
            pending.extend(r for r in session.deleted if isinstance(r,tracked))
            if not pending:
                return
            revision = session.info.get('ownership_revision')
            if revision is None:
                head = session.query(SyncHead).filter_by(id='main').populate_existing().with_for_update().one()
                head.revision += 1
                revision = head.revision
                session.info['ownership_revision'] = revision
            keys = {}
            for row in pending:
                date = getattr(row,'report_date',getattr(row,'end_date',datetime.date(1970,1,1)))
                keys[(row.__tablename__,row.code,date)] = int(row in session.deleted)
            # Bound SQL parameter counts even if a caller flushes a large batch.
            key_list = list(keys)
            for offset in range(0,len(key_list),200):
                batch = key_list[offset:offset+200]
                existing = {(r.table_name,r.code,r.record_date):r for r in session.query(Change).filter(
                    tuple_(Change.table_name,Change.code,Change.record_date).in_(batch)).all()}
                for key in batch:
                    row = existing.get(key)
                    if row is None:
                        session.add(Change(table_name=key[0],code=key[1],record_date=key[2],revision=revision,deleted=keys[key]))
                    else:
                        row.revision, row.deleted = revision, keys[key]
        def reset(session):
            session.info.pop('ownership_revision',None)
        event.listen(Session,'before_flush',before_flush)
        event.listen(Session,'after_commit',reset)
        event.listen(Session,'after_rollback',reset)

    CATEGORY_IDS = {'qfii': (307007,), 'social': (307004,),
                    'broker': (307002, 307015, 307022), 'insurance': (307006,307014,307023),
                    'trust': (307009,307016,307025), 'private': (307028,)}

    def number(value, integer=False):
        if value is None or pd.isnull(value):
            return None
        n = float(value)
        if not math.isfinite(n) or n < 0:
            raise ValueError('持仓数据出现无效负值或非有限数')
        return int(n) if integer else n

    def text_value(value):
        return '' if value is None or pd.isnull(value) else str(value).strip()

    day = _optional_date

    def previous_quarter(period):
        return datetime.date(period.year, ((period.month-1)//3)*3+1, 1)-datetime.timedelta(days=1)

    def pages(table, *conditions, columns=None):
        selection = query(*columns) if columns is not None else query(table)
        size = builtins.min(settings['page_size'], 2000)
        fetch_page = lambda cursor: finance.run_query(selection.filter(table.id > cursor, *conditions)
                                                     .order_by(table.id).limit(size))
        for frame in _source_pages(fetch_page, start=0, label=table.__name__, progress=_progress_note):
            yield from frame.to_dict('records')
            # Query until empty even when upstream caps below requested page size.

    def grouped_pages(table, columns, *conditions, label='报告索引'):
        """Group at source; paginate by unique minimum ID without splitting a group."""
        cursor, count = 0, 0
        size = builtins.min(settings['page_size'], 2000)
        group_id = func.min(table.id)
        while True:
            _progress_note('{}：已读取 {} 条去重索引'.format(label, count))
            q = (query(group_id.label('id'), *columns).filter(*conditions)
                 .group_by(*columns).having(group_id > cursor).order_by(group_id).limit(size))
            frame = finance.run_query(q)
            if frame is None:
                raise ValueError('去重索引接口未返回结果：' + label)
            if frame.empty:
                break
            rows = frame.to_dict('records')
            ids = [int(r['id']) for r in rows]
            if ids != sorted(set(ids)) or ids[0] <= cursor:
                raise ValueError('去重索引分页未按组 ID 递增：' + label)
            cursor = ids[-1]
            count += len(rows)
            yield from rows
            # Continue to empty, even when the provider caps below requested size.

    def batches(items):
        for offset in range(0, len(items), settings['stock_batch_size']):
            yield items[offset:offset+settings['stock_batch_size']]

    def fund_document(row, fund_map):
        fund = text_value(row['code']).split('.')[0]
        canonical = fund_map.get(fund, fund)
        full = text_value(row.get('report_type')) in ('年度','年度报告','年报','半年度','半年度报告')
        priority = (int(full), day(row['pub_date']), int(fund == canonical), fund)
        document = (fund, day(row['pub_date']), int(row['report_type_id']))
        return canonical, priority, document

    def select_documents(period, as_of, fund_map, codes):
        # Discover only funds that have held the requested stocks in this quarter.
        # Then inspect all share classes of those funds, including documents where
        # the requested stock disappeared. Never scan the whole market's documents.
        t = finance.FUND_PORTFOLIO_STOCK
        canonical_funds, observed_funds = set(), set()
        for batch in batches(sorted(set(codes))):
            symbols = sorted(set(batch) | {c.split('.')[0] for c in batch})
            for row in grouped_pages(t, [t.code], t.period_end == period, t.pub_date <= as_of,
                                     t.symbol.in_(symbols), label='关联基金'):
                fund = text_value(row['code']).split('.')[0]
                observed_funds.add(fund)
                canonical_funds.add(fund_map.get(fund, fund))
        related = sorted(observed_funds | canonical_funds |
                         {f for f, c in fund_map.items() if c in canonical_funds})
        _progress_message('{}：发现 {} 个相关基金代码（含关联份额），仅查询去重报告版本。'.format(period, len(related)))
        selected = {}
        columns = [t.code, t.pub_date, t.report_type_id, t.report_type]
        for batch in _progress_items(list(batches(related)), '相关基金报告版本', lambda b: b[0]):
            for row in grouped_pages(t, columns, t.period_end == period, t.pub_date <= as_of,
                                     t.code.in_(batch), label='相关基金报告版本'):
                canonical, priority, document = fund_document(row, fund_map)
                if canonical not in selected or priority > selected[canonical][0]:
                    selected[canonical] = (priority, document)
        return selected

    def affected_stocks(previous, previous_day, current, as_of, fund_map, known_codes, periods):
        """Find affected stocks from newly inserted/published source rows, not all A shares."""
        affected, changed_funds, ignored = set(), set(), set()
        known_codes = set(known_codes)
        code_map = {c.split('.')[0]: c for c in known_codes}
        def stock_code(value):
            value = text_value(value)
            # Resolve bare fund symbols, but never replace an explicit exchange.
            code = value if '.' in value else code_map.get(value)
            return code if code in known_codes else None
        specs = (('fund', finance.FUND_PORTFOLIO_STOCK, 'period_end'),
                 ('float', finance.STK_SHAREHOLDER_FLOATING_TOP10, 'end_date'),
                 ('holders', finance.STK_HOLDER_NUM, 'end_date'))
        for name, table, period_name in specs:
            if previous.get(name) == current.get(name):
                continue
            old_marker = previous.get(name, [None])[0]
            old_id = old_marker[0] if old_marker else 0
            new_marker = current.get(name, [None])[0]
            if new_marker is None:
                raise ValueError('披露来源记录消失，请使用完整刷新复核：' + name)
            filters = [table.id <= new_marker[0], getattr(table, period_name) <= as_of,
                       or_(table.pub_date == None, table.pub_date <= as_of),
                       or_(table.id > old_id, table.pub_date > previous_day)]
            if name == 'float':
                filters += [table.change_reason_id == 306019, table.sharesnature_id == 308007]
            columns = [table.id, table.code]
            if name == 'fund':
                columns.append(table.symbol)
            for row in pages(table, *filters, columns=columns):
                if name == 'fund':
                    fund = text_value(row['code']).split('.')[0]
                    changed_funds.add(fund_map.get(fund, fund))
                    code = stock_code(row['symbol'])
                else:
                    code = stock_code(row['code'])
                    if code is None:
                        ignored.add(text_value(row['code']))
                if code:
                    affected.add(code)
        # Include old constituents: a new fund report may remove a stock entirely.
        if changed_funds:
            table = finance.FUND_PORTFOLIO_STOCK
            funds = sorted(set(changed_funds) | {f for f, c in fund_map.items() if c in changed_funds})
            for batch in batches(funds):
                for row in grouped_pages(table, [table.symbol], table.code.in_(batch), table.period_end.in_(periods),
                                         table.pub_date <= as_of, label='新旧报告关联股票'):
                    code = stock_code(row['symbol'])
                    if code:
                        affected.add(code)
        if ignored:
            _progress_message('个股机构持仓：忽略 {} 个不在股票范围内的披露代码（如 {}）。'.format(
                len(ignored), '、'.join(sorted(ignored)[:5])))
        return sorted(affected)

    def batch_funds(codes, period, as_of, fund_map, selected):
        # Compare all documents of related funds, including other share classes;
        # otherwise stock filtering could resurrect an older position.
        t = finance.FUND_PORTFOLIO_STOCK
        code_map = {c.split('.')[0]:c for c in codes}
        candidates = {}
        columns = [t.id,t.code,t.symbol,t.pub_date,t.report_type_id,t.report_type,t.shares,t.market_cap]
        for row in pages(t, t.period_end == period, t.pub_date <= as_of,
                         t.symbol.in_(list(code_map)+list(codes)), columns=columns):
            canonical, _, document = fund_document(row, fund_map)
            if canonical not in selected or document != selected[canonical][1]:
                continue
            code = code_map.get(text_value(row['symbol']).split('.')[0])
            if code:
                # IDs are ascending, so the last occurrence wins within a document.
                candidates[(code,canonical)] = (number(row['shares']),number(row['market_cap']))
        totals = {}
        for (code,_),(shares,value) in candidates.items():
            if code not in totals:
                totals[code] = {'shares':0.,'market_value':0.,'holder_count':0,
                                'scope':'fund_disclosed','value_method':'reported'}
            total = totals[code]
            total['holder_count'] += 1
            total['shares'] = None if shares is None or total['shares'] is None else total['shares']+shares
            total['market_value'] = None if value is None or total['market_value'] is None else total['market_value']+value
        return totals

    def batch_floats(codes, period, as_of):
        t = finance.STK_SHAREHOLDER_FLOATING_TOP10
        latest = {}
        for row in pages(t, t.end_date == period, t.pub_date <= as_of, t.code.in_(codes),
                         t.change_reason_id == 306019, t.sharesnature_id == 308007):
            code, pub = row['code'], day(row['pub_date'])
            if code not in latest or pub > latest[code][0]:
                latest[code] = (pub,{})
            if pub == latest[code][0]:
                latest[code][1][int(row['shareholder_rank'])] = row
        return {code:list(ranks.values()) for code,(_,ranks) in latest.items()}

    def save_report_batch(session, codes, period, as_of, fund_map, selected, changed):
        fund_groups = batch_funds(codes,period,as_of,fund_map,selected)
        by_stock = batch_floats(codes,period,as_of)
        closes = prices(sorted(by_stock),period)
        old_rows = {r.code:r for r in session.query(OwnershipReport).filter(
            OwnershipReport.report_date == period,OwnershipReport.code.in_(codes)).all()}
        previous_rows = {r.code:r for r in session.query(OwnershipReport).filter(
            OwnershipReport.report_date == previous_quarter(period),OwnershipReport.code.in_(codes)).all()}
        changed_float_codes = set()
        for code in sorted(set(by_stock) | set(fund_groups) | set(old_rows)):
            members, complete, pub = float_snapshot(by_stock.get(code,[]),closes.get(code))
            categories = {'public_fund':fund_groups.get(code)}
            for category, ids in CATEGORY_IDS.items():
                categories[category] = aggregate([r for r in members if r['class_id'] in ids],'top10_float','estimated')
            payload = {'schema_version':1, 'categories':categories, 'float_members':members,
                       'float_complete':complete, 'float_pub_date':pub,
                       'public_fund_scope':'full_disclosure' if period.month in (6,12) else 'quarterly_top_holdings',
                       'fund_identity_rule':'statistics_main_code_else_product_code'}
            previous = previous_rows.get(code)
            payload['social_entry'] = social_entry(payload,previous.payload if previous else None)
            old_payload = old_rows[code].payload if code in old_rows else {}
            if old_payload.get('float_members') != members or old_payload.get('float_complete') != complete:
                changed_float_codes.add(code)
            if upsert(session,OwnershipReport,{'code':code,'report_date':period},{'payload':payload},
                      existing=old_rows.get(code),loaded=True):
                changed.add(('持仓',code,period))
        next_quarter = period+datetime.timedelta(days=95)
        next_period = previous_quarter(datetime.date(next_quarter.year,next_quarter.month,1))
        if changed_float_codes:
            followers = session.query(OwnershipReport).filter(OwnershipReport.report_date == next_period,
                OwnershipReport.code.in_(changed_float_codes)).all()
            for follower in followers:
                follower.payload = dict(follower.payload,social_entry={'status':'unknown'})
                follower.updated_at = china_now()
        # Flush, not commit: release ORM state while retaining quarter-level rollback.
        session.flush()
        session.expunge_all()

    def repair_social_batch(session, codes, changed):
        records = session.query(OwnershipReport).filter(OwnershipReport.code.in_(codes)).all()
        lookup = {(r.code,r.report_date):r for r in records}
        for row in records:
            previous = lookup.get((row.code,previous_quarter(row.report_date)))
            social = social_entry(row.payload,previous.payload if previous else None)
            if row.payload.get('social_entry') != social:
                row.payload = dict(row.payload,social_entry=social)
                row.updated_at = china_now()
                changed.add(('持仓',row.code,row.report_date))
        session.flush()
        session.expunge_all()

    def save_holder_batch(session, codes, conditions, start, changed):
        t = finance.STK_HOLDER_NUM
        latest = {}
        for row in pages(t,t.code.in_(codes),*conditions):
            key = (row['code'],day(row['end_date']))
            priority = (day(row['pub_date']) or datetime.date.min,int(row['id']))
            if key not in latest or priority > latest[key][0]:
                latest[key] = (priority,row)
        if not latest:
            return
        existing = {(r.code,r.end_date):r for r in session.query(OwnershipHolders).filter(
            OwnershipHolders.end_date >= start,OwnershipHolders.code.in_(codes)).all()}
        for (code,end),(_,row) in latest.items():
            payload = {key:number(row[key],True) for key in
                       ('share_holders','a_share_holders','b_share_holders','h_share_holders')}
            payload['schema_version'] = 1
            if upsert(session,OwnershipHolders,{'code':code,'end_date':end},
                      {'pub_date':day(row['pub_date']),'payload':payload},existing=existing.get((code,end)),loaded=True):
                changed.add(('户数',code,end))
        session.flush()
        session.expunge_all()

    def aggregate(items, scope, method, close=None):
        if not items:
            return None  # absence of disclosure is not zero ownership
        shares = [r.get('shares') for r in items]
        values = [r.get('market_value') for r in items]
        return {'shares': builtins.sum(shares) if builtins.all(v is not None for v in shares) else None,
                'market_value': builtins.sum(values) if builtins.all(v is not None for v in values) else None,
                'holder_count': len(items), 'scope': scope, 'value_method': method}

    def float_snapshot(rows, close):
        if not rows:
            return [], False, None
        latest = builtins.max(day(r['pub_date']) for r in rows)
        rows = [r for r in rows if day(r['pub_date']) == latest]
        by_rank = {}
        for r in sorted(rows, key=lambda r: r['id']):
            by_rank[int(r['shareholder_rank'])] = r
        complete = set(by_rank) == set(range(1,11))
        result = []
        for rank, r in sorted(by_rank.items()):
            shares = number(r['share_number'])
            identity = text_value(r.get('shareholder_id'))
            if identity.endswith('.0'):
                identity = identity[:-2]
            name = text_value(r['shareholder_name'])
            category = number(r.get('shareholder_class_id'), True)
            result.append({'id': identity or None, 'name': name, 'rank': rank,
                           'class_id': category, 'shares': shares,
                           'ratio': number(r.get('share_ratio')),
                           'market_value': shares*close if shares is not None and close is not None else None})
        # Unknown classifications or identities make absence/newness unreliable.
        complete = complete and builtins.all(r['class_id'] is not None and r['name'] for r in result)
        return result, bool(complete), latest.isoformat()

    def social_entry(current, previous):
        empty = {'status': 'unknown', 'first_appearance': False, 'new_combinations': [],
                 'new_shares': None, 'new_market_value': None}
        if not previous or not current.get('float_complete') or not previous.get('float_complete'):
            return empty
        current_members = [r for r in current['float_members'] if r['class_id'] == 307004]
        previous_members = previous['float_members']
        def same(a, b):
            return (a.get('id') and b.get('id') and a['id'] == b['id']) or re.sub(r'\s+', '', a['name']) == re.sub(r'\s+', '', b['name'])
        # Compare all prior shareholders: reclassification is not a new holding.
        new = [r for r in current_members if not builtins.any(same(r,p) for p in previous_members)]
        old_social = [r for r in previous_members if r['class_id'] == 307004]
        totals = aggregate(new, 'top10_float', 'estimated') or {}
        return {'status': 'comparable', 'first_appearance': bool(new) and not old_social,
                'new_combinations': new, 'new_shares': totals.get('shares'),
                'new_market_value': totals.get('market_value')}

    def upsert(session, model, keys, values, existing=None, loaded=False):
        row = existing if loaded else session.query(model).filter_by(**keys).first()
        if row is not None and builtins.all(getattr(row,k) == v for k,v in values.items()):
            return False
        if row is None:
            session.add(model(updated_at=china_now(), **dict(keys, **values)))
        else:
            for k,v in values.items():
                setattr(row,k,v)
            row.updated_at = china_now()
        return True

    def prices(codes, period):
        if not codes:
            return {}
        trade_days = _apis['get_trade_days'](end_date=period, count=1)
        if trade_days is None or len(trade_days) != 1:
            raise ValueError('报告期末交易日不可用')
        price_date = as_date(trade_days[0])
        if price_date not in price_universes:
            _progress_note('核对 {} 报告期末股票清单'.format(price_date))
            securities = _apis['get_all_securities'](types=['stock'], date=price_date)
            if securities is None or len(securities.index) == 0:
                raise ValueError('报告期末股票清单为空，停止估值与写入')
            price_universes[price_date] = set(str(code) for code in securities.index)
        available = [code for code in codes if code in price_universes[price_date]]
        if len(available) != len(codes):
            _progress_message('{}：{} 只股票无当期上市行情，持仓数量保留，估算市值留空。'.format(
                price_date, len(codes)-len(available)))
        result = {}
        for offset in range(0,len(available),300):
            batch = available[offset:offset+300]
            _progress_note('获取 {} 报告期末行情（{} 只）'.format(price_date, len(batch)))
            frame = _apis['get_price'](batch, start_date=price_date,
                end_date=price_date, frequency='daily', fields=['close'], fq=None, panel=False)
            if frame is None:
                raise ValueError('报告期末行情获取失败')
            if not frame.empty:
                for r in frame.to_dict('records'):
                    v = number(r['close'])
                    if v and v > 0:
                        result[r['code']] = v
        return result

    def run(end_date=None, full_refresh=False, skip_existing=True):
        price_universes.clear()
        as_of = as_date(end_date or settings.get('end_date') or china_now().date())
        if as_of > china_now().date():
            raise ValueError('结束日期不能晚于今天')
        for option in ('refresh_quarters','stock_batch_size'):
            if isinstance(settings[option],bool) or not isinstance(settings[option],int) or settings[option] < 1:
                raise ValueError(option+' 必须为正整数')
        if settings['stock_batch_size'] > 300:
            raise ValueError('stock_batch_size 最大为 300，防止研究环境内存溢出')
        years = settings['history_years']
        if not isinstance(years,int) or years < 1 or years > 20:
            raise ValueError('history_years 必须在 1 到 20 之间')
        start = as_of.replace(year=as_of.year-years, day=builtins.min(as_of.day,28))
        periods = [datetime.date(y,m,d) for y in range(start.year,as_of.year+1)
                   for m,d in ((3,31),(6,30),(9,30),(12,31))
                   if start <= datetime.date(y,m,d) <= as_of]
        periods.insert(0, previous_quarter(periods[0]))
        _progress_message('个股机构持仓 [股票范围校验版 v5]：开始检查新增披露。')
        signature = disclosure_signature(as_of)
        previous_probe, previous_day = None, None
        with database_scope(Base, _config) as Session:
            with session_scope(Session) as session:
                probe = session.query(DisclosureProbe).filter_by(key='main').first()
                if probe is not None:
                    previous_probe, previous_day = dict(probe.payload), probe.as_of
                if probe is not None and probe.as_of > as_of:
                    raise ValueError('已有较新采集结果，拒绝历史运行回退')
                if not full_refresh and probe is not None and probe.payload == signature:
                    _progress_message('个股机构持仓：未检测到新增披露，跳过持仓复查、户数采集和社保比较。')
                    return pd.DataFrame(columns=['kind', 'code', 'date'])

            securities = _apis['get_all_securities'](types=['stock'],date=as_of)
            if securities is None or securities.empty:
                raise ValueError('股票清单为空，停止写入')
            codes = sorted(str(code) for code in securities.index)
            fund_map = {}
            main = finance.FUND_MAIN_INFO
            for row in pages(main,columns=[main.id,main.main_code,main.statistics_main_code]):
                fund = text_value(row.get('main_code')).split('.')[0]
                canonical = text_value(row.get('statistics_main_code')).split('.')[0] or fund
                if fund:
                    fund_map[fund] = canonical
            targeted = (not full_refresh and previous_probe is not None
                        and previous_probe.get('history_years') == settings['history_years']
                        and previous_probe.get('refresh_quarters') == settings['refresh_quarters'])
            review_codes = None
            if targeted:
                with session_scope(Session) as session:
                    known = set(codes) | {r[0] for r in session.query(OwnershipStock.code).all()}
                review_codes = affected_stocks(previous_probe, previous_day, signature, as_of,
                                               fund_map, known, periods[-settings['refresh_quarters']:])
                affected = set(review_codes)
                codes = [c for c in codes if c in affected]
                periods = periods[-settings['refresh_quarters']:] if review_codes else []
                _progress_message('个股机构持仓：新增披露影响 {} 只股票，仅复查这些股票最近 {} 个季度。'.format(len(review_codes), settings['refresh_quarters']))
            else:
                _progress_message('个股机构持仓：首次建立基线或手动完整刷新，按原有断点采集。')
            # Compact keys only: no raw rows or JSON payloads are accumulated for the preview.
            changed = set()
            install_change_tracking(Session)
            with session_scope(Session) as session:
                for batch in _progress_items(list(batches(codes)),'股票资料',lambda b:b[0]):
                    industries = _apis['get_industry'](batch,date=as_of)
                    if industries is None:
                        raise ValueError('行业查询失败')
                    existing = {r.code:r for r in session.query(OwnershipStock).filter(OwnershipStock.code.in_(batch)).all()}
                    for code in batch:
                        industry = industries.get(code,{}).get('sw_l2',{}).get('industry_name')
                        upsert(session,OwnershipStock,{'code':code},
                               {'name':str(securities.loc[code,'display_name']),'industry':industry},
                               existing=existing.get(code),loaded=True)
                    session.flush()
                    session.expunge_all()
                    del existing, industries
                session.commit()
            for period in _progress_items(periods,'报告期',str):
                with session_scope(Session) as session:
                    checkpoint = session.query(Checkpoint).get('report:'+str(period))
                    if checkpoint and checkpoint.as_of > as_of:
                        raise ValueError('已有较新采集结果，拒绝历史运行回退')
                    if checkpoint and not full_refresh and period not in periods[-settings['refresh_quarters']:]:
                        continue
                    # Only scalar codes; JSON payloads are loaded by bounded stock batch below.
                    old_codes = [] if targeted else [r[0] for r in session.query(OwnershipReport.code).filter_by(report_date=period).all()]
                scope_codes = review_codes if targeted else sorted(set(codes)|set(old_codes))
                _progress_message('{}：复查 {} 只股票，按去重索引核对相关基金报告。'.format(period, len(scope_codes)))
                selected = select_documents(period,as_of,fund_map,scope_codes)
                with session_scope(Session) as session:
                    for batch in _progress_items(list(batches(scope_codes)), '持仓分批',lambda b:b[0]):
                        save_report_batch(session,batch,period,as_of,fund_map,selected,changed)
                    head = session.query(Checkpoint).get('report:'+str(period))
                    if head is None:
                        session.add(Checkpoint(key='report:'+str(period),as_of=as_of))
                    else:
                        head.as_of = as_of
                    session.commit()
                del selected, old_codes
            # Include historical/delisted stock codes, but never load every historical JSON at once.
            with session_scope(Session) as session:
                social_codes = review_codes if targeted else [r[0] for r in session.query(OwnershipReport.code).distinct().all()]
                for batch in _progress_items(list(batches(social_codes)),'社保比较',lambda b:b[0]):
                    repair_social_batch(session,batch,changed)
                session.commit()
            t = finance.STK_HOLDER_NUM
            with session_scope(Session) as session:
                head = session.query(Checkpoint).get('holders')
                if head and head.as_of > as_of:
                    raise ValueError('股东户数已有较新结果，拒绝回退')
                since = None if full_refresh or head is None else head.as_of-datetime.timedelta(days=30)
            conditions = [t.end_date >= start,t.end_date <= as_of,or_(t.pub_date == None,t.pub_date <= as_of)]
            if since is not None and not targeted:
                conditions.append(or_(t.pub_date >= since,t.end_date >= since))
            with session_scope(Session) as session:
                for batch in _progress_items(list(batches(review_codes if targeted else codes)),'股东户数',lambda b:b[0]):
                    save_holder_batch(session,batch,conditions,start,changed)
                head = session.query(Checkpoint).get('holders')
                if head is None:
                    session.add(Checkpoint(key='holders',as_of=as_of))
                else:
                    head.as_of = as_of
                session.commit()
            # Advance only after every phase succeeds; failed runs must retry.
            with session_scope(Session) as session:
                probe = session.query(DisclosureProbe).filter_by(key='main').first()
                if probe is None:
                    session.add(DisclosureProbe(key='main', payload=signature, as_of=as_of))
                else:
                    probe.payload, probe.as_of = signature, as_of
                session.commit()
            return pd.DataFrame(sorted(changed),columns=['kind','code','date'])
    return SimpleNamespace(**locals())


def _short_term_strategy(_apis, _config):
    """原 stock 项目三战法；研究环境筛选、均线计算及近三日快照入库。"""
    settings = strategy_settings('短线选股')
    if settings['batch_size'] > 100:
        raise ValueError('短线选股 batch_size 不得超过100，避免行情请求过大。')
    get_all_securities, get_trade_days, get_price, get_extras, get_industry, get_fundamentals, query, valuation = [
        _apis.get(k) for k in ('get_all_securities', 'get_trade_days', 'get_price', 'get_extras', 'get_industry', 'get_fundamentals', 'query', 'valuation')]
    METHODS = {'bukou': '补票龙', 'redhover': '红悬停龙', 'xuli': '蓄力龙'}
    MA_PERIODS = (5, 10, 20, 30, 60)
    RULE_VERSION = 'stock_three_patterns_v2_nonst_cap30'
    Base = declarative_base()

    class ShortTermRun(Base):
        __tablename__ = 'short_term_run'
        trade_date = Column(Date, primary_key=True)
        payload = Column(JSON, nullable=False)
        updated_at = Column(DateTime, nullable=False)

    class ShortTermStock(Base):
        __tablename__ = 'short_term_stock'
        trade_date = Column(Date, primary_key=True)
        code = Column(String(16), primary_key=True)
        payload = Column(JSON, nullable=False)
        updated_at = Column(DateTime, nullable=False)

    RISE_THRESHOLD, VOLUME_ENLARGE_T2, VOLUME_SHRINK_T1, VOLUME_ENLARGE_T = 3.0, 1.2, 0.8, 1.2

    def compute_bbi(df):
        ma3 = df["close"].rolling(3).mean()
        ma6 = df["close"].rolling(6).mean()
        ma12 = df["close"].rolling(12).mean()
        ma24 = df["close"].rolling(24).mean()
        return (ma3 + ma6 + ma12 + ma24) / 4

    def compute_rsv(df, n):
        low_n = df["low"].rolling(window=n, min_periods=1).min()
        high_close_n = df["close"].rolling(window=n, min_periods=1).max()
        return (df["close"] - low_n) / (high_close_n - low_n + 1e-9) * 100.0

    def compute_dif(df, fast=12, slow=26):
        ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
        return ema_fast - ema_slow

    def bbi_deriv_uptrend(bbi, *, min_window, max_window=None, q_threshold=0.0):
        """BBI整体上升判断（原Selector.py逻辑）"""
        bbi = bbi.dropna()
        if len(bbi) < min_window:
            return False
        longest = min(len(bbi), max_window or len(bbi))
        for w in range(longest, min_window - 1, -1):
            seg = bbi.iloc[-w:]
            norm = seg / seg.iloc[0]
            diffs = np.diff(norm.values)
            if np.percentile(diffs, q_threshold * 100) >= 0:
                return True
        return False

    class BBIShortLongSelector:
        def __init__(self, n_short=3, n_long=21, m=3,
                     bbi_min_window=2, max_window=60, bbi_q_threshold=0.2):
            if m < 2:
                raise ValueError("m must be >= 2")
            self.n_short = n_short
            self.n_long = n_long
            self.m = m
            self.bbi_min_window = bbi_min_window
            self.max_window = max_window
            self.bbi_q_threshold = bbi_q_threshold

        def _passes_filters(self, hist):
            hist = hist.copy()
            hist["BBI"] = compute_bbi(hist)
            if not bbi_deriv_uptrend(
                hist["BBI"],
                min_window=self.bbi_min_window,
                max_window=self.max_window,
                q_threshold=self.bbi_q_threshold,
            ):
                return False
            hist["RSV_short"] = compute_rsv(hist, self.n_short)
            hist["RSV_long"] = compute_rsv(hist, self.n_long)
            if len(hist) < self.m:
                return False
            win = hist.iloc[-self.m:]
            long_ok = (win["RSV_long"] >= 80).all()
            short_series = win["RSV_short"]
            short_ok = (short_series.iloc[0] >= 80 and short_series.iloc[-1] >= 80
                        and (short_series < 20).any())
            if not (long_ok and short_ok):
                return False
            hist["DIF"] = compute_dif(hist)
            return hist["DIF"].iloc[-1] > 0

        def select(self, date, data):
            picks = []
            for code, df in data.items():
                hist = df[df["date"] <= date]
                if hist.empty:
                    continue
                need_len = (max(self.n_short, self.n_long)
                            + self.bbi_min_window + self.m)
                hist = hist.tail(max(need_len, self.max_window))
                if self._passes_filters(hist):
                    picks.append(code)
            return picks

    def get_limit_rate(ts_code: str) -> float:
        """根据股票代码判断涨停阈值（10%/20%/30%）"""
        code = ts_code.upper()
        if code.startswith("300") or code.startswith("301") or code.startswith("688"):
            return 0.20
        if code.startswith("9"):
            return 0.30
        return 0.10

    def is_limit_up(ts_code: str, pct_chg: float) -> bool:
        """判断是否涨停（根据板块不同阈值）"""
        rate = get_limit_rate(ts_code)
        threshold = 9.5 if rate == 0.10 else (19.5 if rate == 0.20 else 29.5)
        return pct_chg >= threshold

    def analyze_stock(df, ts_code):
        if len(df) < 3:
            return None

        row_t2 = df.iloc[-3]
        row_t1 = df.iloc[-2]
        row_t = df.iloc[-1]

        # 1. T-2日必须涨停
        if not is_limit_up(ts_code, row_t2["pct_chg"]):
            return None

        # 计算T-2涨停价
        rate_t2 = get_limit_rate(ts_code)
        t2_limit_price = round(row_t2["pre_close"] * (1 + rate_t2), 2)

        # 2. T-1日条件（不限是否涨停，只看最低价）
        if row_t1["low"] < t2_limit_price:
            return None

        # 3. T日条件（未涨停 + 低价过涨停价）
        if is_limit_up(ts_code, row_t["pct_chg"]):
            return None
        if row_t["low"] < t2_limit_price:
            return None

        return True


    def analyze_one(code, df):
        """
        分析单只股票是否满足蓄力龙条件
        df: 按date升序排列的日K线数据（最后一行=T日）
        需要至少65个交易日（算MA60）
        """
        if df is None or len(df) < 65:
            return False

        # 取最近65个交易日
        df = df.tail(65).copy().reset_index(drop=True)

        if len(df) < 5:
            return False

        day_t3  = df.iloc[-4]  # T-3
        day_t2  = df.iloc[-3]  # T-2
        day_t1  = df.iloc[-2]  # T-1
        day_t   = df.iloc[-1]  # T

        # ---- 条件1: T-2日阳线 + 涨幅 > 3% + 放量 ----
        t2_pct   = float(day_t2['pct_chg'])
        t2_open  = float(day_t2['open'])
        t2_close = float(day_t2['close'])
        t2_vol   = float(day_t2['volume'])
        t3_vol   = float(day_t3['volume'])

        cond1_ok = (
            t2_close > t2_open and                          # 阳线
            t2_pct > RISE_THRESHOLD and                     # 涨幅>3%
            t2_vol > t3_vol * VOLUME_ENLARGE_T2            # 放量>1.2倍
        )

        # ---- 条件2: T-1日阴线 + 收盘>T-2开盘 + 跌幅<4% + 缩量 ----
        t1_pct   = float(day_t1['pct_chg'])
        t1_open  = float(day_t1['open'])
        t1_close = float(day_t1['close'])
        t1_vol   = float(day_t1['volume'])

        cond2_ok = (
            t1_close < t1_open and                           # 阴线（收<开）
            t1_close > t2_open and                            # 收盘>T-2开盘价
            -4.0 < t1_pct < 0 and                             # 跌幅低于4%（-4% < 跌幅 < 0）
            t1_vol < t2_vol * VOLUME_SHRINK_T1               # 缩量<0.8倍
        )

        # ---- 条件3: T日阳线 + 收盘>T-2收盘 + 涨幅>3% + 放量 ----
        t_pct   = float(day_t['pct_chg'])
        t_open  = float(day_t['open'])
        t_close = float(day_t['close'])
        t_vol   = float(day_t['volume'])

        cond3_ok = (
            t_close > t_open and                            # 阳线
            t_close > t2_close and                          # 收盘>T-2收盘价
            t_pct > RISE_THRESHOLD and                       # 涨幅>3%
            t_vol > t1_vol * VOLUME_ENLARGE_T               # 放量>1.2倍
        )

        # ---- 条件4: 均线多头（MA10>MA20>MA30>MA60） ----
        closes = df['close'].values.astype(float)
        ma10 = closes[-10:].mean() if len(closes) >= 10 else 0
        ma20 = closes[-20:].mean() if len(closes) >= 20 else 0
        ma30 = closes[-30:].mean() if len(closes) >= 30 else 0
        ma60 = closes[-60:].mean() if len(closes) >= 60 else 0

        cond4_ok = ma10 > ma20 > ma30 > ma60

        return cond1_ok and cond2_ok and cond3_ok and cond4_ok

    finite = _float_number

    bbi_selector = BBIShortLongSelector()

    def evaluate(history, code, is_st=False):
        # Keep original strategy windows: BBI tail 60, xuli tail 65.
        matched = []
        if bbi_selector.select(history['date'].iloc[-1], {code: history}):
            matched.append('bukou')
        if not is_st and analyze_stock(history, code):
            matched.append('redhover')
        if analyze_one(code, history):
            matched.append('xuli')
        return matched

    def chart_bars(history):
        frame = history.copy()
        for n in MA_PERIODS:
            frame['ma' + str(n)] = frame['close'].rolling(n).mean()
        bars = []
        for _, r in frame.tail(30).iterrows():
            bar = {key: finite(r[key]) for key in ('open', 'close', 'low', 'high', 'volume')}
            bar['date'] = as_date(r['date']).isoformat()
            bar['ma'] = {str(n): finite(r['ma' + str(n)]) for n in MA_PERIODS}
            bars.append(bar)
        return bars

    def clean_history(raw, code, days, listing):
        if raw is None or raw.empty or not {'code', 'time', 'open', 'close', 'high', 'low', 'volume'}.issubset(raw.columns):
            raise ValueError('行情接口缺少必要字段，拒绝保存不完整快照。')
        frame = raw[raw['code'] == code].copy()
        frame['date'] = pd.to_datetime(frame['time']).dt.date
        frame = frame[frame['date'] >= listing].sort_values('date')
        expected = [d for d in days if d >= listing]
        if frame['date'].duplicated().any() or list(frame['date']) != expected:
            raise ValueError('行情日期不完整：{}，未写入该日结果。'.format(code))
        for col in ('open', 'high', 'low', 'close', 'volume'):
            frame[col] = pd.to_numeric(frame[col], errors='coerce')
            if not frame[col].map(lambda x: finite(x) is not None and x >= 0).all():
                raise ValueError('行情数值不完整：{}，未写入该日结果。'.format(code))
        if (frame[['open', 'high', 'low', 'close']] <= 0).any().any():
            raise ValueError('行情价格非正：{}'.format(code))
        frame['pre_close'] = frame['close'].shift(1)
        frame['pct_chg'] = (frame['close'] / frame['pre_close'] - 1) * 100
        return frame

    def read_st_flags(codes, day):
        if not codes:
            return {}
        # Read a date range, but only accept the target day's actual status.
        raw = get_extras('is_st', codes, start_date=day - datetime.timedelta(days=7), end_date=day, df=True)
        flags = {}
        if raw is not None and not raw.empty:
            rows = raw.loc[[as_date(d) == day for d in raw.index]]
            if len(rows) == 1:
                for code in codes:
                    if code in rows.columns:
                        value = rows[code].iloc[0]
                        if not pd.isnull(value) and isinstance(value, (bool, np.bool_, int, float, np.integer, np.floating)) and value in (0, 1):
                            flags[code] = bool(value)
        for code in codes:
            if code in flags:
                continue
            # Single-stock/single-day dict response avoids batch DataFrame omissions.
            extra = get_extras('is_st', [code], start_date=day, end_date=day, df=False)
            values = extra.get(code, []) if isinstance(extra, dict) else []
            if len(values) == 1:
                value = values[0]
                if not pd.isnull(value) and isinstance(value, (bool, np.bool_, int, float, np.integer, np.floating)) and value in (0, 1):
                    flags[code] = bool(value)
        missing = [code for code in codes if code not in flags]
        if missing:
            raise ValueError('短线股票池ST状态缺失：日期 {}，股票 {}；批量查询及逐只补查后仍无有效数据，该日未写入，请稍后重试。'.format(day, ', '.join(missing)))
        return flags

    def collect(day):
        days = [as_date(d) for d in get_trade_days(end_date=day, count=90)]
        if not days or days[-1] != day:
            raise ValueError('交易日日历未覆盖目标日。')
        securities = get_all_securities(types=['stock'], date=day)
        codes = sorted(c for c in securities.index if c.startswith(('0', '3', '4', '6', '8', '9')))
        if not codes:
            raise ValueError('A股股票池为空，拒绝写入。')
        size = settings['batch_size']
        selected, evaluated, young = [], 0, 0
        caps = {}
        batches = [codes[i:i+size] for i in range(0, len(codes), size)]
        for batch in _progress_items(batches, '{} 扫描股票'.format(day), lambda b: b[0]):
            df = get_fundamentals(query(valuation.code, valuation.market_cap, valuation.pe_ratio, valuation.pb_ratio).filter(valuation.code.in_(batch)), date=day)
            if df is not None:
                caps.update({r['code']: r for _, r in df.iterrows()})
            missing = [c for c in batch if finite(caps.get(c, {}).get('market_cap')) is None]
            if missing:
                raise ValueError('总市值缺失：日期 {}，股票 {}；该日未写入。'.format(day, ', '.join(missing)))
            batch = [c for c in batch if finite(caps[c]['market_cap']) >= 30]
            flags = read_st_flags(batch, day)
            batch = [c for c in batch if not flags[c]]
            if not batch:
                continue
            raw = get_price(batch, start_date=days[0], end_date=day, frequency='daily',
                            fields=['open', 'high', 'low', 'close', 'volume'], fq='pre', skip_paused=False, panel=False)
            batch_selected = []
            for code in batch:
                listing = as_date(securities.loc[code, 'start_date'])
                frame = clean_history(raw, code, days, listing)
                if len(frame) < 30:
                    young += 1  # 新股展示实际可用K线，不额外改变原战法的上市天数门槛。
                evaluated += 1
                methods = evaluate(frame, code)
                if not methods:
                    continue
                last = frame.iloc[-1]
                batch_selected.append({'code': code, 'trade_date': day, 'name': str(securities.loc[code, 'display_name']),
                    'methods': methods, 'close': finite(last['close']), 'pct_change': finite(last['pct_chg']),
                    'volume': finite(last['volume']), 'bars': chart_bars(frame)})
            selected.extend(batch_selected)
        picked = [r['code'] for r in selected]
        industries = {}
        for batch in _progress_items([picked[i:i+size] for i in range(0, len(picked), size)], '补充行业与估值', lambda b: b[0]):
            industries.update(get_industry(batch, date=day) or {})

        for r in selected:
            industry = industries.get(r['code'], {}).get('sw_l2', {})
            cap = caps.get(r['code'], {})
            r.update(industry=industry.get('industry_name'), market_cap=finite(cap.get('market_cap')),
                     pe=finite(cap.get('pe_ratio')), pb=finite(cap.get('pb_ratio')))
        meta = {'rule_version': RULE_VERSION, 'universe_count': len(codes), 'evaluated_count': evaluated,
                'short_history_count': young, 'counts': {m: sum(m in r['methods'] for r in selected) for m in METHODS},
                'ma_periods': list(MA_PERIODS), 'chart_days': 30, 'adjustment': 'pre', 'retention_trade_days': 3, 'exclude_st': True, 'min_market_cap': 30}
        return meta, selected

    def save_day(Session, day, meta, records):
        stamp = china_now()
        with session_scope(Session) as session:
            previous = session.query(ShortTermRun).filter_by(trade_date=day).first()
            if previous is not None and previous.payload.get('rule_version') == meta.get('rule_version'):
                return False
            # Replace older rules atomically; a failed collection never removes the old snapshot.
            if previous is not None:
                session.query(ShortTermStock).filter_by(trade_date=day).delete(synchronize_session=False)
                session.delete(previous)
                session.flush()
            for r in records:
                payload = {k: v for k, v in r.items() if k != 'trade_date'}
                # Validate JSON before entering the database driver (no NaN/Infinity).
                json.dumps(payload, allow_nan=False)
                session.add(ShortTermStock(trade_date=day, code=r['code'], payload=payload, updated_at=stamp))
            session.add(ShortTermRun(trade_date=day, payload=meta, updated_at=stamp))
            session.commit()
        return True

    def run(end_date=None, skip_existing=True):
        if skip_existing is not True:
            raise ValueError('短线选股固定只新增完整交易日快照。')
        clock = china_now()
        latest = _closed_day(clock.date(), clock)
        keep_days = [as_date(d) for d in get_trade_days(end_date=latest, count=3)]
        if len(keep_days) != 3:
            raise ValueError('最近三个完整交易日日历不足。')
        requested = end_date if end_date is not None else settings['end_date']
        end = keep_days[-1] if requested is None else as_date(requested)
        if end < keep_days[0] or end > latest:
            raise ValueError('end_date 必须位于保留窗口内且不晚于最新完整交易日。')
        results = []
        with database_scope(Base, _config) as Session:
            with session_scope(Session) as session:
                for model in (ShortTermStock, ShortTermRun):
                    session.query(model).filter(model.trade_date < keep_days[0]).delete(synchronize_session=False)
                session.commit()
                existing = set(r.trade_date for r in session.query(ShortTermRun).all()
                               if r.payload.get('rule_version') == RULE_VERSION)
            pending = [d for d in keep_days if d <= end and d not in existing]
            for day in _progress_items(pending, '短线选股交易日', str):
                meta, records = collect(day)
                if save_day(Session, day, meta, records):
                    results.extend(records)
        return pd.DataFrame(results, columns=['trade_date', 'code', 'name', 'methods', 'close', 'pct_change', 'industry', 'pe', 'pb', 'market_cap', 'bars'])

    return SimpleNamespace(**locals())


# 注册信息：(策略工厂, 写入模式, 预览字段)。顺序由顶部 STRATEGY_ORDER 控制。
STRATEGY_SPECS = {
    '短线选股': (_short_term_strategy, 'append',
        ('trade_date', 'code', 'name', 'methods', 'close')),
    '个股机构持仓': (_ownership_strategy, 'changes',
        ('kind', 'code', 'date')),
    '行业动量': (_momentum_strategy, 'append',
        ('trade_date', 'industry_name', 'momentum', 'momentum_rank')),
    '市场情绪跟踪': (_sentiment_strategy, 'append',
        ('trade_date', 'ratio', 'above_count', 'eligible_count', 'universe_count')),
    '抱团拥挤度': (_crowding_strategy, 'append',
        ('trade_date', 'top_pct', 'ratio', 'top_money', 'total_money')),
    '微盘温度计': (_microcap_strategy, 'append',
        ('trade_date', 'score', 'micro_index', 'sample_count')),
    'ETF份额': (_etf_strategy, 'append',
        ('trade_date', 'fund_code', 'fund_name', 'shares')),
    '净利润断层': (_earnings_strategy, 'append',
        ('report_period', 'code', 'name', 'industry_name', 'signal_date', 'profit_yoy')),
    '微盘新高新低': (_breadth_strategy, 'append',
        ('trade_date', 'sample_count', 'high_5', 'low_5', 'high_30', 'low_30')),
    '高股息选股': (_dividend_strategy, 'latest',
        ('method', 'rank', 'code', 'name', 'dividend_yield_1y', 'dividend_ratio_3y', 'pe_ttm')),
    '高增长': (_growth_strategy, 'latest',
        ('rank', 'code', 'name', 'industry_name', 'annual_profit_growth', 'pe_ttm', 'pb')),
    'ETF动量热度': (_etf_heat_strategy, 'append',
        ('trade_date', 'code', 'name', 'score', 'annual_return', 'r2')),
}
STRATEGIES = {name: spec[0] for name, spec in STRATEGY_SPECS.items()}

if __name__ == '__main__':
    batch_summary = run_all()
