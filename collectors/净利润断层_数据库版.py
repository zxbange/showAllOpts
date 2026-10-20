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
STRATEGY_CONFIG = {'净利润断层': {'end_date': None, 'page_size': 1000}}

STRATEGY_ORDER = ['净利润断层']
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
_PREVIEW_COLUMNS = {'净利润断层': ('trade_date', 'scope', 'raw_parts', 'compressed_bytes')}


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

    def run(end_date=END_DATE, skip_existing=False):
        now = china_now()
        requested = as_date(end_date) if end_date is not None else now.date()
        if requested > now.date():
            raise ValueError('结束日期不能晚于今天。')
        period = latest_period(requested)
        cutoff = requested - dt.timedelta(days=1) if requested == now.date() and now.hour < 16 else requested
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


STRATEGIES = {'净利润断层': _earnings_strategy}

def run(**options):
    return _load_strategy('净利润断层', DB_CONFIG, globals())(**options)

if __name__ == '__main__':
    batch_summary = run_all()
