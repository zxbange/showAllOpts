# -*- coding: utf-8 -*-
"""聚宽单文件串行采集：直接粘贴到 Notebook，或 %run -i 本文件。
九个策略在本文件内独立实现；不读取外部策略、配置或日志文件。
依赖和参数集中在开头；共用连接、会话及日期工具。用法见 docs/collector-batch.md。
"""
import decimal
import gzip
import hashlib
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
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from urllib.parse import quote, quote_plus

import jqdata as jq
import pandas as pd
from sqlalchemy import BigInteger, Column, Date, DateTime, Index, Integer, JSON, LargeBinary, Numeric, String, and_, or_, create_engine, func
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
    'ETF动量热度': {'end_date': None, 'initial_trade_days': 180, 'min_size_yi': 30},
    '行业动量': {
        'start_date': '2025-01-01', 'end_date': None,
        'industry_type': 'sw_l2', 'retention_trade_days': 180,
    },
    '抱团拥挤度': {
        'start_date': '2020-01-01', 'end_date': None, 'top_pct': 0.05,
    },
    '微盘温度计': {
        'start_date': '2021-01-01', 'end_date': None,
        'micro_num': 1000, 'sample_step': 5, 'batch_size': 1000,
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

STRATEGY_ORDER = ['行业动量', '抱团拥挤度', '微盘温度计', 'ETF份额', '净利润断层', '微盘新高新低', '高股息选股', '高增长', 'ETF动量热度']
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
_PREVIEW_COLUMNS = {
    'ETF动量热度': ('trade_date', 'code', 'name', 'close', 'adjusted_close'),
    '高增长': ('rank', 'code', 'name', 'industry_name', 'annual_profit_growth', 'pe_ttm', 'pb'),
    '高股息选股': ('method', 'rank', 'code', 'name', 'dividend_yield_1y', 'dividend_ratio_3y', 'pe_ttm'),
    '行业动量': ('trade_date', 'industry_name', 'momentum', 'momentum_rank'),
    '抱团拥挤度': ('trade_date', 'top_pct', 'ratio', 'top_money', 'total_money'),
    '微盘温度计': ('trade_date', 'score', 'micro_index', 'sample_count'),
    'ETF份额': ('trade_date', 'fund_code', 'fund_name', 'shares'),
    '微盘新高新低': ('trade_date', 'sample_count', 'high_5', 'low_5', 'high_30', 'low_30'),
    '净利润断层': ('report_period', 'code', 'name', 'industry_name', 'signal_date', 'profit_yoy'),
}


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
        if progress is not None:
            progress.close(ending)


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
    strategy = STRATEGIES[name](apis, config)
    if name in RAW_TABLES:
        return lambda **options: _raw_run(name, strategy, apis, config, **options)
    return strategy.run


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
                label = '新增/更新原始快照' if name in RAW_TABLES else '新增'
                print('\n【{}】成功：{} {} 条，耗时 {:.2f} 秒。'.format(name, label, row['rows'], row['elapsed_seconds']))
                if not row['rows']:
                    print('原始数据未变化，无重复写入。' if name in RAW_TABLES
                          else '无新增数据；已有记录跳过，或数据源尚无可入库数据。')
                elif preview_rows:
                    try:
                        columns = [c for c in (('trade_date', 'scope', 'raw_parts', 'compressed_bytes') if name in RAW_TABLES else _PREVIEW_COLUMNS[name]) if c in result.columns]
                        print('本次结果预览（最后 {} 条，字段单位沿用策略）：'.format(builtins.min(preview_rows, row['rows'])))
                        print(result[columns].tail(preview_rows).to_string(index=False))
                    except Exception:
                        print('结果预览不可用，入库已完成。')
            else:
                print('\n【{}】失败：{}；已提交部分保留，继续后续策略。'.format(name, row['error']))
            del result
        print('\n采集结果汇总\n策略 | 结果 | 条数（原始采集为快照数，ETF为新增记录数） | 耗时（秒）')
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
    """仅提供原始采集依赖的接口与校验，不计算策略结果。"""
    settings = strategy_settings('行业动量')
    get_trade_days = _apis.get('get_trade_days')
    RETENTION_TRADE_DAYS = settings['retention_trade_days']

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
    return SimpleNamespace(**locals())


# ---------- 抱团拥挤度 ----------
def _crowding_strategy(_apis, _config):
    """仅提供原始采集依赖的接口与校验，不计算策略结果。"""
    return SimpleNamespace(**locals())


# ---------- 微盘温度计 ----------
def _microcap_strategy(_apis, _config):
    """仅提供原始采集依赖的接口与校验，不计算策略结果。"""
    settings = strategy_settings('微盘温度计')
    get_all_securities = _apis.get('get_all_securities')
    get_price = _apis.get('get_price')
    get_extras = _apis.get('get_extras')
    get_fundamentals = _apis.get('get_fundamentals')
    query = _apis.get('query')
    BATCH_SIZE = settings['batch_size']

    def number(value):
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return result if result.is_finite() else None

    def series_key(start, micro_num, sample_step):
        return 'micro{}_{}d_{}_v1'.format(micro_num, sample_step, as_date(start).strftime('%Y%m%d'))

    def chunks(codes):
        for offset in range(0, len(codes), BATCH_SIZE):
            yield codes[offset:offset + BATCH_SIZE]

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

    def run(start_date=START_DATE, end_date=END_DATE, incremental=True, lookback_days=LOOKBACK_DAYS, groups=None, skip_existing=False):
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
    """仅提供原始采集依赖的接口与校验，不计算策略结果。"""
    settings = strategy_settings('净利润断层')
    get_fundamentals = _apis.get('get_fundamentals')
    query = _apis.get('query')
    indicator = _apis.get('indicator')
    income = _apis.get('income')
    finance = _apis.get('finance')
    jy = _apis.get('jy')
    PAGE_SIZE = settings['page_size']

    def latest_period(day):
        # 当前季度开始前一天；10 月运行统一取 9 月 30 日。
        return dt.date(day.year, ((day.month - 1) // 3) * 3 + 1, 1) - dt.timedelta(days=1)

    def number(value):
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except (TypeError, ValueError):
            return None

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

    def checked_rows(frame, fields, label):
        if frame is None or not set(fields).issubset(frame.columns):
            raise ValueError('{} 返回缺失或字段不完整，取消本次入库。'.format(label))
        return frame.to_dict('records')

    def paged(table, filters, fetch):
        # 主键分页，直到空页；避免平台行数限制造成漏数。
        key = getattr(table, 'id', None)
        if key is None:
            key = getattr(table, 'ID', None)
        if key is None:
            raise ValueError('数据表缺少分页主键，不能保证完整采集。')
        after, rows = -1, []
        while True:
            _progress_note('{}：已读取 {} 条，正在请求下一页'.format(
                {'SecuMain': '公司代码映射', 'LC_FSPerformedLetters': '业绩快报', 'STK_FIN_FORCAST': '业绩预告'}.get(
                    getattr(table, '__tablename__', ''), '公告分页'), len(rows)))
            frame = fetch(query(table).filter(*(filters + [key > after])).order_by(key).limit(PAGE_SIZE))
            page = checked_rows(frame, [key.key], '公告接口')
            if not page:
                return rows
            ids = [int(r[key.key]) for r in page]
            if ids != sorted(set(ids)) or ids[0] <= after:
                raise ValueError('公告分页顺序异常，取消本次入库。')
            rows.extend(page)
            after = ids[-1]

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
    return SimpleNamespace(**locals())


# ---------- 微盘新高新低 ----------
def _breadth_strategy(_apis, _config):
    """仅提供原始采集依赖的接口与校验，不计算策略结果。"""
    settings = strategy_settings('微盘新高新低')
    get_all_securities = _apis.get('get_all_securities')
    get_trade_days = _apis.get('get_trade_days')
    get_extras = _apis.get('get_extras')
    get_fundamentals = _apis.get('get_fundamentals')
    query = _apis.get('query')
    valuation = _apis.get('valuation')
    BATCH_SIZE = settings['batch_size']

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
    return SimpleNamespace(**locals())


# ---------- 高股息选股 ----------
def _dividend_strategy(_apis, _config):
    """仅提供原始采集依赖的接口与校验，不计算策略结果。"""
    settings = strategy_settings('高股息选股')
    get_all_securities, get_trade_days = _apis.get('get_all_securities'), _apis.get('get_trade_days')
    get_price, get_extras = _apis.get('get_price'), _apis.get('get_extras')
    get_fundamentals, get_industry = _apis.get('get_fundamentals'), _apis.get('get_industry')
    query, valuation, indicator, finance = [_apis.get(k) for k in ('query', 'valuation', 'indicator', 'finance')]
    BATCH_SIZE, PAGE_SIZE = settings['batch_size'], settings['page_size']

    if BATCH_SIZE > 3000 or PAGE_SIZE > 3000:
        raise ValueError('batch_size 和 page_size 不得超过 3000。')

    def number(value):
        try:
            result = Decimal(str(value))
            return result if result.is_finite() else None
        except (InvalidOperation, ValueError, TypeError):
            return None

    def optional_date(value):
        return None if value is None or pd.isna(value) else as_date(value)

    def rows(frame, required, label):
        if frame is None or not set(required).issubset(frame.columns):
            raise ValueError('{} 返回空对象或缺少字段。'.format(label))
        return frame.to_dict('records')

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
    return SimpleNamespace(**locals())


# ---------- 高增长 ----------
def _growth_strategy(_apis, _config):
    """仅提供原始采集依赖的接口与校验，不计算策略结果。"""
    settings = strategy_settings('高增长')
    get_all_securities, get_trade_days, get_fundamentals, get_industry, query, valuation = [
        _apis.get(k) for k in ('get_all_securities', 'get_trade_days', 'get_fundamentals', 'get_industry', 'query', 'valuation')]
    BATCH_SIZE, PAGE_SIZE = settings['batch_size'], settings['page_size']

    if BATCH_SIZE > 500 or PAGE_SIZE > 3000:
        raise ValueError('batch_size 不得超过 500，page_size 不得超过 3000。')

    GROUPS = {'annual': (12, 31), 'q1': (3, 31), 'q2': (6, 30), 'q3': (9, 30)}

    def number(value):
        try:
            result = Decimal(str(value))
            return result if result.is_finite() else None
        except (InvalidOperation, ValueError, TypeError):
            return None

    def rows(frame, required, label):
        if frame is None or not set(required).issubset(frame.columns):
            raise ValueError('{} 返回空对象或缺少字段，保留原快照。'.format(label))
        return frame.to_dict('records')

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
        try:
            number = Decimal(str(value))
            return number if number.is_finite() and number > 0 else None
        except (InvalidOperation, ValueError):
            return None

    def read_pages(table, filters=()):
        frames, cursor = [], -1
        while True:
            frame = finance.run_query(query(table).filter(*filters).filter(table.id > cursor)
                                      .order_by(table.id.asc()).limit(PAGE_SIZE))
            if frame is None:
                raise ValueError('查询返回 None')
            if frame.empty:
                break
            ids = frame['id'].tolist()
            if ids != sorted(set(ids)) or ids[0] <= cursor:
                raise ValueError('分页主键重复或乱序')
            frames.append(frame)
            cursor = ids[-1]
            if len(frame) < PAGE_SIZE:
                break
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
    # The collector stores provider data; the application computes all momentum scores.
    settings = strategy_settings('ETF动量热度')
    EXPORT_CSV = False
    MIN_SIZE_YI = Decimal(str(settings['min_size_yi']))
    RULE_VERSION = 'etf_raw_market_v1'
    Base = declarative_base()

    class Bar(Base):
        __tablename__ = 'etf_momentum_bar'
        trade_date = Column(Date, primary_key=True)
        code = Column(String(16), primary_key=True)
        name = Column(String(128), nullable=False)
        close = Column(Numeric(24, 8))
        adjusted_close = Column(Numeric(24, 8))
        paused = Column(Integer, nullable=False)
        observation_start = Column(Date, nullable=False)
        updated_at = Column(DateTime, nullable=False)
        __table_args__ = (Index('ix_etf_bar_code_date', 'code', 'trade_date'),)

    class Pool(Base):
        __tablename__ = 'etf_momentum_pool'
        id = Column(Integer, primary_key=True)
        as_of_date = Column(Date, nullable=False)
        rule_version = Column(String(48), nullable=False)
        members = Column(JSON, nullable=False)
        updated_at = Column(DateTime, nullable=False)


    def price_value(value):
        if not positive(value):
            return None
        result = Decimal(str(value)).quantize(Decimal('0.00000001'))
        if result >= Decimal('1e16'):
            raise ValueError('价格超出数据库精度')
        return result


    def save_rows(Session, records):
        keys = {(r['trade_date'], r['code']) for r in records}
        if len(keys) != len(records):
            raise ValueError('交易日和基金代码重复')
        if not records:
            return []
        with session_scope(Session) as session:
            existing = set(session.query(Bar.trade_date, Bar.code).filter(
                Bar.code.in_(set(r['code'] for r in records)),
                Bar.trade_date >= builtins.min(r['trade_date'] for r in records),
                Bar.trade_date <= builtins.max(r['trade_date'] for r in records)).all())
            added = [r for r in records if (r['trade_date'], r['code']) not in existing]
            session.add_all([Bar(**r) for r in added])
            session.commit()
            return added


    def run(end_date=None, skip_existing=True):
        nonlocal AS_OF_DATE
        if skip_existing is not True:
            raise ValueError('只允许新增；已有日期和基金的记录不覆盖')
        now = china_now()
        requested = as_date(end_date or settings['end_date']) if (end_date or settings['end_date']) else now.date()
        if requested > now.date():
            raise ValueError('结束日期不能晚于今天')
        end = requested - datetime.timedelta(days=1) if requested == now.date() and now.hour < 16 else requested
        days = [as_date(d) for d in api('get_trade_days')(end_date=end, count=settings['initial_trade_days'])]
        if not days or days != sorted(set(days)):
            raise ValueError('交易日历无效')
        AS_OF_DATE = str(days[-1])
        _progress_note('筛选 ETF/LOF 候选池；仅采集行情，热度由项目同步时计算')
        chosen, audit, candidates = select_pool()
        if chosen.empty:
            raise ValueError('本次未取得有效候选池，保留旧快照；请运行分类研究脚本查看待核对清单')
        members = []
        for row in chosen.to_dict('records'):
            members.append({key: (str(value) if isinstance(value, Decimal) else
                            (None if pd.isna(value) else (value.item() if hasattr(value, 'item') else value)))
                            for key, value in row.items()})
        members = json.loads(json.dumps(members, ensure_ascii=False, allow_nan=False))
        output = []
        with database_scope(Base, _config) as Session:
            with session_scope(Session) as session:
                head = session.query(Pool).filter_by(id=1).first()
                if head and (head.as_of_date > days[-1] or head.rule_version not in (RULE_VERSION, 'etf_close_momentum_v1')):
                    raise ValueError('已有更新日期或未知规则版本，拒绝混用')
            for member in _progress_items(members, 'ETF/LOF 行情采集', lambda r: r['name']):
                code = member['code']
                with session_scope(Session) as session:
                    existing = {r.trade_date: (r.adjusted_close, r.observation_start)
                                for r in session.query(Bar).filter(Bar.code == code).all()}
                observation_start = builtins.min([days[0]] + [v[1] for v in existing.values()])
                # Include any long collection gap, plus the 25 prior observations needed for regression.
                first = builtins.min(days[0], builtins.max(existing)) if existing else days[0]
                warm = [as_date(d) for d in api('get_trade_days')(end_date=first, count=26)]
                if not warm:
                    raise ValueError('无法取得行情预热日期')
                calendar = [as_date(d) for d in api('get_trade_days')(start_date=warm[0], end_date=days[-1])]
                listed = as_date(member['listed_date']) if member.get('listed_date') else calendar[0]
                expected = [d for d in calendar if d >= listed]
                missing = [d for d in expected if d not in existing]
                if not missing:
                    continue
                # Overlap one stored day to detect inconsistent provider adjustment bases.
                overlap = [d for d in existing if d < builtins.min(missing)]
                start = builtins.min(builtins.min(missing), builtins.max(overlap)) if overlap else builtins.min(missing)
                frames = []
                for fields, adjustment in [(['close', 'paused'], None), (['close'], 'post')]:
                    frame = api('get_price')(code, start_date=start, end_date=days[-1], frequency='daily',
                                             fields=fields, skip_paused=False, fq=adjustment)
                    if frame is None or frame.empty:
                        raise ValueError(code + ' 行情接口无数据，未发布新候选池')
                    frame = frame.copy()
                    frame.index = pd.Index(pd.to_datetime(frame.index).date)
                    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
                        raise ValueError(code + ' 行情日期重复或乱序')
                    frames.append(frame)
                raw, adjusted = frames
                for day in set(existing).intersection(adjusted.index):
                    previous = existing[day][0]
                    current = price_value(adjusted.loc[day, 'close'])
                    if previous is not None and current is not None and abs(float(current-previous)) > builtins.max(1e-7, abs(float(previous))*1e-7):
                        raise ValueError(code + ' 后复权历史发生变化，请核对数据源后重建该基金行情；拒绝拼接不同基准')
                records = []
                for day in missing:
                    if day not in raw.index or day not in adjusted.index:
                        raise ValueError('{} {} 行情缺失，保留已提交数据和旧候选池'.format(code, day))
                    paused = raw.loc[day, 'paused']
                    if pd.isna(paused):
                        raise ValueError(code + ' 停牌状态缺失')
                    close = price_value(raw.loc[day, 'close'])
                    adj = price_value(adjusted.loc[day, 'close'])
                    if not paused and (close is None or adj is None):
                        raise ValueError('{} {} 有效交易日价格缺失'.format(code, day))
                    records.append(dict(trade_date=day, code=code, name=member['name'], close=close,
                                        adjusted_close=adj, paused=int(paused), observation_start=observation_start,
                                        updated_at=china_now()))
                _progress_note('{} 批量入库 {} 条基础行情'.format(code, len(records)))
                output.extend(save_rows(Session, records))
            with session_scope(Session) as session:
                codes = {r[0] for r in session.query(Bar.code).filter(Bar.trade_date == days[-1]).all()}
                if set(m['code'] for m in members) - codes:
                    raise ValueError('最新交易日行情不完整，保留上一候选池快照')
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



RAW_VERSION = 'strategy_inputs_v1'

RAW_CHUNK_SIZE = 48 * 1024

RAW_TABLES = {
    '行业动量': ('industry-momentum', 'raw_industry_momentum'),
    '抱团拥挤度': ('market-crowding', 'raw_market_crowding'),
    '微盘温度计': ('microcap-temperature', 'raw_microcap_temperature'),
    '微盘新高新低': ('microcap-breadth', 'raw_microcap_breadth'),
    '高股息选股': ('dividend-selection', 'raw_dividend_selection'),
    '高增长': ('high-growth', 'raw_high_growth'),
    '净利润断层': ('earnings-gap', 'raw_earnings_gap'),
}

def raw_model(base, table):
    return type('Input_' + table, (base,), {
        '__tablename__': table,
        '__table_args__': {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'},
        'scope': Column(String(96), primary_key=True),
        'trade_date': Column(Date, primary_key=True),
        'part': Column(Integer, primary_key=True),
        'parts': Column(Integer, nullable=False),
        'digest': Column(String(64), nullable=False),
        'version': Column(String(32), nullable=False),
        'payload': Column(LargeBinary, nullable=False),
        'updated_at': Column(DateTime, nullable=False),
    })

def pack_input(value):
    if isinstance(value, pd.DataFrame):
        return {'__kind__': 'frame', 'columns': pack_input(list(value.columns)),
                'index': pack_input(list(value.index)), 'data': pack_input(value.values.tolist())}
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value) and '__kind__' not in value:
            return {k: pack_input(v) for k, v in value.items()}
        return {'__kind__': 'mapping', 'items': [[pack_input(k), pack_input(v)] for k,v in value.items()]}
    if isinstance(value, (set, tuple)):
        items = sorted(value) if isinstance(value, set) else value
        return {'__kind__': 'tuple', 'items': [pack_input(v) for v in items]}
    if isinstance(value, list):
        return [pack_input(v) for v in value]
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, decimal.Decimal):
        return {'__kind__': 'decimal', 'value': str(value)} if value.is_finite() else None
    if isinstance(value, (datetime.datetime, datetime.date)):
        return {'__kind__': 'datetime' if isinstance(value, datetime.datetime) else 'date', 'value': value.isoformat()}
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, 'item'):
        return pack_input(value.item())
    raise ValueError('Unsupported raw input type: ' + type(value).__name__)

def unpack_input(value):
    if isinstance(value, list):
        return [unpack_input(v) for v in value]
    if not isinstance(value, dict):
        return value
    kind = value.get('__kind__')
    if kind == 'date':
        return datetime.datetime.strptime(value['value'], '%Y-%m-%d').date()
    if kind == 'datetime':
        return pd.Timestamp(value['value']).to_pydatetime()
    if kind == 'decimal':
        return decimal.Decimal(value['value'])
    if kind == 'tuple':
        return tuple(unpack_input(v) for v in value['items'])
    if kind == 'mapping':
        return {unpack_input(k): unpack_input(v) for k,v in value['items']}
    if kind == 'frame':
        return pd.DataFrame(unpack_input(value['data']), columns=unpack_input(value['columns']), index=unpack_input(value['index']))
    if kind:
        raise ValueError('Unknown raw input encoding')
    return {k:unpack_input(v) for k,v in value.items()}

def encode_input(data):
    raw = json.dumps(pack_input(data), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode='wb', mtime=0) as stream:
        stream.write(raw)
    payload = buffer.getvalue()
    return hashlib.sha256(payload).hexdigest(), [payload[i:i+RAW_CHUNK_SIZE] for i in range(0,len(payload),RAW_CHUNK_SIZE)]

def decode_input(rows):
    rows = sorted(rows, key=lambda r:r.part)
    if not rows or [r.part for r in rows] != list(range(rows[0].parts)):
        raise ValueError('原始数据分片不完整，不能发布计算结果')
    first = rows[0]
    if any((r.version,r.scope,r.trade_date,r.parts,r.digest) != (RAW_VERSION,first.scope,first.trade_date,first.parts,first.digest) for r in rows):
        raise ValueError('原始数据版本或分片不一致')
    payload = b''.join(r.payload for r in rows)
    if hashlib.sha256(payload).hexdigest() != first.digest:
        raise ValueError('原始数据校验失败')
    return unpack_input(json.loads(gzip.decompress(payload).decode('utf-8')))

# Shared by standalone collectors and the batch entry point; no external files required.
def _collect_raw(name, ns, apis, day, settings, previous=None):
    """Only universe filtering, source validation and field/date normalization run here."""
    get = lambda key: apis[key]
    if name == '行业动量':
        industries = get('get_industries')(name=settings['industry_type'], date=str(day))
        securities = get('get_all_securities')(types=['stock'], date=str(day))
        if industries is None or industries.empty or securities is None or securities.empty:
            raise ValueError('行业或股票池为空')
        members = {str(c): sorted(get('get_industry_stocks')(c, date=str(day))) for c in industries.index}
        codes = [c for c,r in securities.iterrows() if as_date(r['end_date']) > day and as_date(r['start_date']) < day-datetime.timedelta(days=150)]
        window = [as_date(d) for d in get('get_trade_days')(end_date=day,count=21)]
        if len(window)!=21 or window[-1]!=day or not codes:
            raise ValueError('行业动量股票池或窗口无效')
        # Both endpoints are returned in the same provider adjustment basis.
        frame = get('get_price')(codes,end_date=day,count=21,frequency='daily',fields=['close'],skip_paused=False,fq='pre',panel=False)
        if frame is None or frame.empty or not {'time','code','close'}.issubset(frame.columns):
            raise ValueError('行业动量价格缺失')
        dates = pd.to_datetime(frame['time']).dt.date
        frame = frame.loc[dates.isin([window[0],day])].copy()
        return dict(industries=industries,members=members,prices=frame,window=window)
    if name == '抱团拥挤度':
        securities = get('get_all_securities')(types=['stock'],date=day)
        if securities is None or securities.empty:
            raise ValueError('股票池为空')
        codes=list(securities.index)
        frame=get('get_price')(security=codes,end_date=day,count=1,frequency='daily',fields=['money'],skip_paused=False,panel=False)
        if frame is None or not {'code','time','money'}.issubset(frame.columns) or len(frame)!=len(codes) or set(frame['code'])!=set(codes):
            raise ValueError('当日成交额未完整返回')
        if builtins.any(as_date(d)!=day for d in frame['time']):
            raise ValueError('成交额日期不一致')
        return dict(pairs=list(zip(frame['code'],frame['money'])))
    if name == '微盘新高新低':
        codes=ns.choose_stocks(day,settings['micro_num'])
        calendar=ns.trade_calendar(end=day,count=31)
        frames=[]
        for offset in _progress_items(range(0,len(codes),settings['price_batch_size']),'采集微盘历史行情'):
            batch=codes[offset:offset+settings['price_batch_size']]
            frame=get('get_price')(batch,start_date=calendar[0],end_date=day,frequency='daily',fields=['close'],fq='pre',skip_paused=False,panel=False)
            if frame is None or not {'code','time','close'}.issubset(frame.columns):
                raise ValueError('微盘历史行情缺失')
            frames.append(frame)
        return dict(codes=codes,calendar=calendar,prices=pd.concat(frames,ignore_index=True))
    if name == '微盘温度计':
        codes=ns.eligible_codes(day)
        if len(codes)<settings['micro_num']:
            raise ValueError('股票池不足微盘样本数')
        fields=('market_cap','turnover_ratio','pe_ratio','pb_ratio')
        rows=ns.fundamental_rows(codes,apis['valuation'],fields,day)
        window=[as_date(d) for d in get('get_trade_days')(end_date=day,count=5)]
        if len(window)!=5 or window[-1]!=day or window!=sorted(set(window)):
            raise ValueError('换手率窗口无效')
        turnovers={}
        for d in _progress_items(window,'采集历史换手率'):
            daily=rows if d==day else ns.fundamental_rows(codes,apis['valuation'],('turnover_ratio',),d)
            if not daily:
                raise ValueError('换手率数据为空')
            for code,row in daily.items():
                turnovers.setdefault(code,[]).append(ns.number(row.get('turnover_ratio')))
        money=ns.price_rows(codes,day,day,'money')
        field='inc_net_profit_to_shareholders_year_on_year'
        growth=ns.fundamental_rows(codes,apis['indicator'],(field,),day)
        # Previous membership is selected locally, so collect endpoints for its whole eligible pool.
        endpoints={}
        if previous:
            prior=previous['inputs']; prior_day=previous['day']; prior_codes=sorted(prior['rows'])
            quotes=ns.price_rows(prior_codes,prior_day,day,'close')
            endpoints={k:v for k,v in quotes.items() if k[1] in (prior_day,day)}
        return dict(rows=rows,turnovers=turnovers,amounts={c:money.get((c,day)) for c in codes},growth=growth,endpoints=endpoints)
    if name == '高股息选股':
        names,quotes,caps=ns.universe(day)
        codes=sorted(caps)
        # Cash distributions remain individual source rows; ratios/ranks are local calculations.
        table=apis['finance'].STK_XR_XD; distributions=[]
        start3,start1=day-datetime.timedelta(days=1095),day-datetime.timedelta(days=365)
        fields=('id','code','a_registration_date','a_xr_date','bonus_amount_rmb')
        for offset in _progress_items(range(0,len(codes),settings['batch_size']),'采集分红明细'):
            batch=codes[offset:offset+settings['batch_size']]; after=-1
            while True:
                q=apis['query'](*[getattr(table,f) for f in fields]).filter(table.code.in_(batch),table.id>after,
                    or_(and_(table.a_registration_date>=start3,table.a_registration_date<=day),and_(table.a_xr_date>start1,table.a_xr_date<=day))).order_by(table.id).limit(settings['page_size'])
                page=ns.rows(apis['finance'].run_query(q),fields,'分红明细')
                if not page:break
                ids=[int(r['id']) for r in page]
                if ids!=sorted(set(ids)) or ids[0]<=after or builtins.any(r['code'] not in batch for r in page):
                    raise ValueError('分红明细重复或越界')
                distributions.extend(page);after=ids[-1]
        return dict(names=names,quotes=quotes,caps=caps,distributions=distributions,
                    fundamentals=ns.financials(codes,day),industries=_raw_industries(apis,codes,day))
    if name == '高增长':
        as_of=settings.get('as_of_date',day)
        names=_raw_names(apis,as_of)
        reports=ns.financial_reports(sorted(names),as_of)
        if not builtins.any(reports.values()):
            raise ValueError('全部累计财报为空')
        codes=sorted(c for c,r in reports.items() if r)
        return dict(as_of=as_of,names=names,reports=reports,caps=_raw_caps(apis,codes,day),industries=_raw_industries(apis,codes,as_of))
    if name == '净利润断层':
        names=_raw_names(apis,day); period=settings['report_period']
        events=ns.fetch_events(sorted(names),period,day)
        # Profit criteria are cheap source filters; price gaps and final selection are local.
        codes=sorted({e['code'] for e in events if e['eligible']})
        quotes={}; calendars={}
        for code in _progress_items(codes,'采集公告对应行情'):
            first=builtins.min(as_date(e['publication_date']) for e in events if e['code']==code and e['eligible'])
            calendar=[as_date(d) for d in get('get_trade_days')(start_date=first-datetime.timedelta(days=40),end_date=day)]
            if not calendar or calendar!=sorted(set(calendar)):
                raise ValueError('公告交易日历无效')
            frame=get('get_price')(code,start_date=calendar[0],end_date=day,frequency='daily',fields=['open','close','high','low','paused','volume'],skip_paused=False,fq='pre')
            ns.checked_rows(frame,['open','close','high','low','paused','volume'],'公告行情')
            quotes[code]=frame;calendars[code]=calendar
        return dict(names=names,events=events,period=period,prices=quotes,calendars=calendars,
                    caps=_raw_caps(apis,codes,day),industries=_raw_industries(apis,codes,day))
    raise ValueError('未知原始数据策略')


def _raw_names(apis,day):
    frame=apis['get_all_securities'](types=['stock'],date=day)
    if frame is None or frame.empty or 'display_name' not in frame.columns:
        raise ValueError('股票名称列表为空')
    return {str(c):str(r['display_name']) for c,r in frame.iterrows()}


def _raw_caps(apis,codes,day):
    result={}; table=apis['valuation']
    fields=('code','market_cap','circulating_market_cap','pe_ratio','pb_ratio')
    for offset in _progress_items(range(0,len(codes),300),'采集估值'):
        batch=codes[offset:offset+300]
        frame=apis['get_fundamentals'](apis['query'](*[getattr(table,f) for f in fields]).filter(table.code.in_(batch)),date=day)
        if frame is None or not set(fields).issubset(frame.columns):
            raise ValueError('估值字段缺失')
        rows=frame.to_dict('records')
        if len(rows)!=len(batch) or {r['code'] for r in rows}!=set(batch):
            raise ValueError('估值数据未完整返回')
        result.update({r['code']:r for r in rows})
    return result


def _raw_industries(apis,codes,day):
    result={}
    for offset in _progress_items(range(0,len(codes),300),'采集行业分类'):
        batch=codes[offset:offset+300]; values=apis['get_industry'](batch,date=day)
        if not isinstance(values,dict) or set(batch)-set(values):
            raise ValueError('行业分类未完整返回')
        result.update(values)
    return result


def _raw_run(name,ns,apis,config,**options):
    settings=dict(strategy_settings(name)); settings.update({k:v for k,v in options.items() if k in settings})
    unknown=set(options)-set(settings)-{'skip_existing','incremental'}
    if unknown:raise ValueError('不支持的参数: '+', '.join(sorted(unknown)))
    if options.get('incremental',True) is not True:raise ValueError('原始历史采集固定增量，不能关闭 incremental')
    if 'micro_num' in settings and (isinstance(settings['micro_num'],bool) or not isinstance(settings['micro_num'],int) or settings['micro_num']<=0):raise ValueError('micro_num 须为正整数')
    if options.get('skip_existing',True) is not True:
        raise ValueError('原始历史数据仅支持只新增；最新名单按内容变化更新')
    now=china_now(); end=as_date(settings['end_date']) if settings.get('end_date') else now.date()
    if end>now.date():raise ValueError('结束日期不能晚于今天')
    if name=='高增长':settings['as_of_date']=end
    if name=='净利润断层':settings['report_period']=ns.latest_period(end)
    if end==now.date() and now.hour<16:end-=datetime.timedelta(days=1)
    latest=list(apis['get_trade_days'](end_date=end,count=1))
    if len(latest)!=1 or as_date(latest[0])>end:raise ValueError('交易日历为空或越界')
    end=as_date(latest[0]); module,table=RAW_TABLES[name]
    base=declarative_base(); Raw=raw_model(base,table)
    scope='default'
    if name=='行业动量':scope=settings['industry_type']
    if name=='抱团拥挤度':scope=str(Decimal(str(settings['top_pct'])).quantize(Decimal('0.000001')))
    if name=='微盘温度计':scope=ns.series_key(settings['start_date'],settings['micro_num'],settings['sample_step'])
    if name=='微盘新高新低':scope=ns.series_key(settings['micro_num'])
    if name=='净利润断层':scope=str(settings['report_period'])
    latest_only=name in ('高股息选股','高增长')
    output=[]
    with database_scope(base,config) as Session:
        with session_scope(Session) as session:
            if name=='行业动量':
                retain=ns.get_retention_start(now.date(),settings['retention_trade_days'])
                session.query(Raw).filter(Raw.trade_date<retain).delete(synchronize_session=False);session.commit()
            dates=[r[0] for r in session.query(Raw.trade_date).filter_by(scope=scope,part=0).order_by(Raw.trade_date).all()]
            last=dates[-1] if dates else None
            previous=decode_input(session.query(Raw).filter_by(scope=scope,trade_date=last).all()) if last and name=='微盘温度计' else None
        if last and last>now.date():raise ValueError('原始数据最新日期晚于今天')
        if last and last>end:
            if latest_only:raise ValueError('不能使用旧日期覆盖最新原始快照')
            return pd.DataFrame()
        if latest_only:
            pending=[end]
        elif name=='净利润断层':
            pending=[] if end in dates else [end]
        else:
            start=as_date(settings['start_date']) if settings.get('start_date') else end-datetime.timedelta(days=settings.get('initial_days',180))
            if name=='行业动量':start=builtins.max(start,retain)
            if last:start=builtins.max(start,last)
            calendar=[as_date(d) for d in apis['get_trade_days'](start_date=start,end_date=end)]
            if calendar!=sorted(set(calendar)) or builtins.any(d<start or d>end for d in calendar):
                raise ValueError('增量交易日历无效')
            if name=='微盘温度计':
                step=settings['sample_step']
                if not isinstance(step,int) or isinstance(step,bool) or step<=0:raise ValueError('采样步长须为正整数')
                if last and (not calendar or calendar[0]!=last):raise ValueError('历史采样锚点缺失')
                pending=calendar[step::step] if last else calendar[::step]
            else:pending=[d for d in calendar if d not in set(dates)]
        for day in _progress_items(pending,'原始数据采集日'):
            inputs=_collect_raw(name,ns,apis,day,settings,previous)
            _validate_raw(name,ns,inputs,day,settings,previous)
            payload=dict(strategy=name,day=day,scope=scope,expected_latest_day=pending[-1],settings=settings,inputs=inputs)
            digest,parts=encode_input(payload)
            with session_scope(Session) as session:
                if latest_only:
                    newest=session.query(func.max(Raw.trade_date)).filter_by(scope=scope).scalar()
                    if newest and newest>day:raise ValueError('并发任务已有更新快照，拒绝回退')
                old=session.query(Raw).filter_by(scope=scope,trade_date=day,part=0).with_for_update().first()
                if old and old.digest==digest:
                    _progress_note('原始数据未变化，不重复写入');previous=payload;continue
                if old and not latest_only:
                    raise ValueError('已有历史原始快照，拒绝覆盖')
                session.query(Raw).filter_by(scope=scope,trade_date=day).delete(synchronize_session=False)
                stamp=china_now()
                session.add_all([Raw(scope=scope,trade_date=day,part=i,parts=len(parts),digest=digest,version=RAW_VERSION,payload=part,updated_at=stamp) for i,part in enumerate(parts)])
                if latest_only:
                    session.query(Raw).filter(Raw.scope==scope,Raw.trade_date!=day).delete(synchronize_session=False)
                session.commit()
            previous=payload
            output.append(dict(trade_date=day,scope=scope,raw_parts=len(parts),compressed_bytes=builtins.sum(len(p) for p in parts)))
    return pd.DataFrame(output)

def _validate_raw(name, ns, inputs, day, settings, previous):
    """Reject incomplete source batches before advancing the collection watermark."""
    if name == '抱团拥挤度':
        pairs=inputs['pairs'];pct=Decimal(str(settings['top_pct']))
        if len(pairs)<100 or len({c for c,v in pairs})!=len(pairs) or not 0<pct<=1 or pct!=pct.quantize(Decimal('0.000001')):
            raise ValueError('拥挤度样本或比例无效')
        values=[Decimal(str(v)) for c,v in pairs]
        if builtins.any(not v.is_finite() or v<0 for v in values) or not builtins.any(v>0 for v in values):
            raise ValueError('成交额无效或全为零')
    elif name == '微盘新高新低':
        frame=inputs['prices'];codes=inputs['codes'];days=inputs['calendar']
        keys=set()
        for r in frame.to_dict('records'):
            key=(r['code'],as_date(r['time']));value=Decimal(str(r['close']))
            if key in keys or key[0] not in codes or key[1] not in days or not value.is_finite() or value<=0:
                raise ValueError('新高新低原始行情缺失、重复或无效')
            keys.add(key)
        if len(keys)!=len(codes)*len(days):raise ValueError('新高新低原始行情未完整返回')
    elif name == '行业动量':
        keys={};valid=0
        for r in inputs['prices'].to_dict('records'):
            key=(r['code'],as_date(r['time']))
            if key in keys:raise ValueError('行业动量端点行情重复')
            keys[key]=r['close']
        for code in {k[0] for k in keys}:
            try:
                values=[float(keys[(code,d)]) for d in (inputs['window'][0],day)]
                valid+=int(builtins.all(math.isfinite(v) and v>0 for v in values))
            except (KeyError,TypeError,ValueError):pass
        if not valid:raise ValueError('行业动量没有有效端点行情')
    elif name == '微盘温度计':
        def members(data):
            eligible=[]
            for c,r in data['rows'].items():
                cap,pe,pb,turn=[ns.number(r.get(f)) for f in ('market_cap','pe_ratio','pb_ratio','turnover_ratio')]
                money=data['amounts'].get(c);history=[v for v in data['turnovers'].get(c,[]) if v is not None and v>=0]
                if cap is not None and cap>0 and pe is not None and pb is not None and turn is not None and turn>=0 and money is not None and money>=0 and history:
                    eligible.append((cap,c))
            if len(eligible)<settings['micro_num']:raise ValueError('完整有效股票不足微盘样本数量')
            return [c for cap,c in sorted(eligible)[:settings['micro_num']]]
        members(inputs)
        if previous:
            for c in members(previous['inputs']):
                for d in (previous['day'],day):
                    value=inputs['endpoints'].get((c,d))
                    if value is None or value<=0:raise ValueError('微盘上期成分端点行情缺失')
    elif name == '高股息选股':
        for row in inputs['distributions']:
            value=ns.number(row['bonus_amount_rmb'])
            if not pd.isna(row['bonus_amount_rmb']) and (value is None or value<0):raise ValueError('分红金额无效')
    elif name == '高增长':
        if not builtins.any(ns.latest_reports(rows,inputs['as_of']) is not None for rows in inputs['reports'].values()):
            raise ValueError('全部股票均缺少完整四类财报，不能发布原始快照')
    elif name == '净利润断层':
        for code,frame in inputs['prices'].items():
            if frame.index.has_duplicates:raise ValueError('公告行情日期重复')
            bars={as_date(d):r for d,r in frame.iterrows()};calendar=inputs['calendars'][code]
            for event in inputs['events']:
                if event['code']!=code or not event['eligible']:continue
                d=next((d for d in calendar if d>=as_date(event['publication_date'])),None)
                if d is None:continue
                i=calendar.index(d)
                if i and (d not in bars or calendar[i-1] not in bars):raise ValueError('公告对应交易日行情缺失')
                row=bars.get(d)
                if row is None:continue
                if ns.number(row['paused']) is None or ns.number(row['volume']) is None:raise ValueError('停牌或成交状态缺失')
                if not row['paused'] and row['volume']>0 and builtins.any(ns.number(row[f]) is None or ns.number(row[f])<=0 for f in ('open','close','high','low')):
                    raise ValueError('已成交日价格无效')


STRATEGIES = {
    '行业动量': _momentum_strategy,
    '抱团拥挤度': _crowding_strategy,
    '微盘温度计': _microcap_strategy,
    'ETF份额': _etf_strategy,
    '净利润断层': _earnings_strategy,
    '微盘新高新低': _breadth_strategy,
    '高股息选股': _dividend_strategy,
    '高增长': _growth_strategy,
    'ETF动量热度': _etf_heat_strategy,
}

if __name__ == '__main__':
    batch_summary = run_all()
