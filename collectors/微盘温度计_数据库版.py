# -*- coding: utf-8 -*-
"""聚宽单文件串行采集：直接粘贴到 Notebook，或 %run -i 本文件。
本策略在本文件内独立实现；不读取外部策略、配置或日志文件。
依赖和参数集中在开头；共用连接、会话及日期工具。用法见 docs/collector-batch.md。
"""
import bisect
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
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from statistics import median
from types import SimpleNamespace
from urllib.parse import quote, quote_plus

import jqdata as jq
import pandas as pd
from sqlalchemy import (BigInteger, Column, Date, DateTime, Index, Integer, JSON, LargeBinary,
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
STRATEGY_CONFIG = {'微盘温度计': {'start_date': '2021-01-01', 'end_date': None, 'micro_num': 1000, 'sample_step': 5, 'batch_size': 1000}}

STRATEGY_ORDER = ['微盘温度计']
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
_PREVIEW_COLUMNS = {'微盘温度计': ('trade_date', 'scope', 'raw_parts', 'compressed_bytes')}


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

    def number(value):
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return result if result.is_finite() else None

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

    def save_period(Session, record, skip_existing=False):
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
            session.commit()
            return True

    def run(start_date=START_DATE, end_date=END_DATE, micro_num=MICRO_NUM, sample_step=SAMPLE_STEP, skip_existing=False):
        start = as_date(start_date)
        now = china_now()
        today = now.date()
        end = today if end_date is None else as_date(end_date)
        if start > end or end > today:
            raise ValueError('开始日期不能晚于结束日期，结束日期不能晚于今天。')
        if skip_existing and end == today and now.hour < 16:
            end = today - datetime.timedelta(days=1)
        for value in (micro_num, sample_step):
            if isinstance(value, builtins.bool) or not isinstance(value, builtins.int) or value <= 0:
                raise ValueError('微盘数量和采样步长必须是正整数。')
        series_id = series_key(start, micro_num, sample_step)
        records = []
        with database_scope(Base, _config) as Session:
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


STRATEGIES = {'微盘温度计': _microcap_strategy}

def run(**options):
    return _load_strategy('微盘温度计', DB_CONFIG, globals())(**options)

if __name__ == '__main__':
    batch_summary = run_all()
