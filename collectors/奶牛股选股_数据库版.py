# -*- coding: utf-8 -*-
"""聚宽研究环境：奶牛股近3/5/10年筛选、静态股息率及三年分红展示。
与 mysql_connector.py 一起上传后，完整运行本文件即计算并写库。
数据库配置仅在 mysql_connector.py；不调用其他策略脚本。
只读取候选股的历史日线；当日完整快照已存在则跳过，三组结果原子发布。
默认最近收市交易日；run(end_date='2025-12-31', write_database=False) 可历史复算预览。
复算使用当前数据库的财报修订值，不声称还原当时原始披露版本。
运行后在网站同步“奶牛股”；原东方财富采集保持可用。详见 docs/data/cash-cow.md。
"""
import builtins
import datetime
import gc
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from builtins import all, any, bool, float, int, isinstance, len, max, min, range, set, sorted, str, sum, type
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import quote, quote_plus
import jqdata as jq
import pandas as pd
from sqlalchemy import Column, Date, DateTime, Integer, JSON, create_engine
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from mysql_connector import get_database_config, get_database_options, mysql_drivername, engine_options

DB_CONFIG = get_database_config()
DB_OPTIONS = get_database_options()
STRATEGY_CONFIG = {'奶牛股': {'end_date': None, 'batch_size': 300}}
PROGRESS_ENABLED = True
PROGRESS_INTERVAL_SECONDS = 5.0
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


def _progress_note(message):
    progress = getattr(_PROGRESS_LOCAL, 'current', None)
    if progress is not None:
        with progress.lock:
            progress.note = message
            progress.render()


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


def _float_number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


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
        retry = False
        try:
            frame = fetch_page(cursor)
        except OSError as exc:
            if exc.errno != 24:
                raise
            retry = True
        if retry:
            # Retry outside except: the failed traceback must not retain SDK objects.
            # Never close arbitrary descriptors owned by the notebook or the platform SDK.
            gc.collect()
            try:
                handles = len(os.listdir('/proc/self/fd'))
            except OSError:
                handles = '未知'
            if progress is not None:
                progress('{}：句柄不足；回收后 {} 个，重试当前页 1 次'.format(label, handles))
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
        drivername=mysql_drivername(DB_OPTIONS['driver']), host=config['host'], port=config['port'],
        username=config['dbuser'], password=config['dbpassword'], database=config['dbname'],
        query={'charset': DB_OPTIONS['charset']})
    engine = create_engine(url, **engine_options(DB_OPTIONS))
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
    locations, trace = [], exc.__traceback__
    while trace is not None:
        locations.append('{}:{}'.format(trace.tb_frame.f_code.co_name, trace.tb_lineno))
        trace = trace.tb_next
    if locations:
        message += '；位置=' + ' > '.join(locations[-6:])
    secrets = set()
    for key in ('host', 'dbname', 'dbuser', 'dbpassword'):
        value = str(config[key])
        if value:
            secrets.update((value, quote(value, safe=''), quote_plus(value)))
    for value in sorted(secrets, key=len, reverse=True):
        message = message.replace(value, '[已隐藏]')
    return '{}: {}'.format(type(exc).__name__, ' '.join(message.split())[:400])


def _cash_cow_strategy(_apis, _config):
    settings = strategy_settings('奶牛股')
    Base = declarative_base()
    RULE_VERSION = 'cash_cow_static_display_v2'
    get_price = _apis.get('get_price')

    class CashCowSnapshot(Base):
        __tablename__ = 'cash_cow_snapshot'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        id = Column(Integer, primary_key=True)
        as_of_date = Column(Date, nullable=False)
        updated_at = Column(DateTime, nullable=False)
        payload = Column(JSON, nullable=False)

    # BEGIN CASH COW RULES
    def numeric(value):
        return type(value) in (int, float) and math.isfinite(value)


    def evaluate_year(year, dividend, financial):
        """A known failing predicate is a rejection, null data are never zero."""
        dividend = dividend or {}
        financial = financial or {}
        cash = dividend.get('cash_dividend')
        adjusted = financial.get('adjusted_profit')
        profit = financial.get('net_profit')
        operating = financial.get('operating_cashflow')
        if any(numeric(value) and value <= 0 for value in (cash, adjusted, profit)):
            return 'excluded', None
        if numeric(cash) and numeric(operating) and operating < cash:
            return 'excluded', None
        payout = 100 * cash / profit if numeric(cash) and numeric(profit) and profit > 0 else None
        if payout is not None and not 30 <= payout <= 70:
            return 'excluded', None
        if not all(numeric(value) for value in (cash, adjusted, profit, operating)):
            return 'insufficient', None
        return 'eligible', dict(year=year, cash_dividend=cash, adjusted_profit=adjusted,
                               net_profit=profit, operating_cashflow=operating,
                               payout_ratio=payout, cashflow_coverage=operating / cash)


    def finish_stock(stock, years, dividends, prices, period=10):
        """Dividend per share and unadjusted close must share a valid share basis."""
        window = [row['year'] for row in years]
        if len(window) != period or sorted(window) != list(range(min(window), min(window) + period)):
            return 'insufficient', None
        result = []
        for annual in years:
            year = annual['year']
            dps = dividends.get(year)
            price = prices.get(year) or {}
            close = price.get('year_average_close')
            day = price.get('price_date')
            # A suspended stock's stale monthly close is not its year-end close.
            if (not numeric(dps) or dps <= 0 or not numeric(close) or close <= 0
                    or not isinstance(day, str) or not day.startswith(f'{year}-12-')):
                return 'insufficient', None
            result.append(dict(annual, dividend_per_share=dps, year_average_close=close,
                               price_date=day, dividend_yield=100 * dps / close))
        average = sum(row['dividend_yield'] for row in result) / period
        if average <= 4:
            return 'excluded', None
        return 'matched', dict(code=stock['code'], name=stock['name'], exchange=stock['exchange'],
                               average_yield=average,
                               payout_min=min(row['payout_ratio'] for row in result),
                               payout_max=max(row['payout_ratio'] for row in result),
                               coverage_min=min(row['cashflow_coverage'] for row in result),
                               years=result)


    PERIODS = (3, 5, 10)

    def screen_cash_cows(source, end_year, progress, check_cancel=lambda: None):
        stocks = source.universe()
        if not stocks:
            raise ValueError('股票池为空，未替换已有结果。')
        universe = {stock['code']: stock for stock in stocks}
        if len(universe) != len(stocks):
            raise ValueError('股票池代码重复，未替换已有结果。')
        pending = dict(universe)
        annual = {code: [] for code in universe}
        candidates = {period: set() for period in PERIODS}
        outcomes = {period: {} for period in PERIODS}

        def reject(code, outcome, depth):
            pending.pop(code)
            # An older failure belongs only to windows containing that
            # year. Shorter windows' survivors are already frozen.
            for period in PERIODS:
                if period >= depth:
                    outcomes[period][code] = outcome

        # Cheap, newest-first annual predicates are evaluated once. At
        # each boundary freeze the eligible set, then prune only longer
        # windows. Never prune by a shorter window's average yield.
        for depth in range(1, max(PERIODS) + 1):
            if not pending:
                break
            year = end_year - depth + 1
            progress(f'初筛{year}年分红与财报（3/5/10年共用）',
                     len(stocks) - len(pending), len(stocks))
            dividends = source.dividends(year, codes=list(pending))
            for code in list(pending):
                cash = (dividends.get(code) or {}).get('cash_dividend')
                if numeric(cash) and cash <= 0:
                    reject(code, 'excluded', depth)
            financials = source.financials(year, codes=list(pending)) if pending else {}
            for code in list(pending):
                outcome, row = evaluate_year(year, dividends.get(code), financials.get(code))
                if outcome == 'eligible':
                    annual[code].append(row)
                else:
                    reject(code, outcome, depth)
            if depth in candidates:
                candidates[depth] = set(pending)

        items = {period: [] for period in PERIODS}
        survivors = set().union(*candidates.values())
        for index, code in enumerate(sorted(survivors)):
            check_cancel()
            stock = universe[code]
            periods = [period for period in PERIODS if code in candidates[period]]
            progress(f'核对候选{stock["name"]}的股息率（共用分红与行情）',
                     len(stocks) - len(survivors) + index, len(stocks))
            dps = source.per_share_dividends_by_period(code, end_year, periods)
            usable = []
            for period in periods:
                window = range(end_year - period + 1, end_year + 1)
                if all(numeric(dps[period].get(year)) and dps[period][year] > 0 for year in window):
                    usable.append(period)
                else:
                    outcomes[period][code] = 'insufficient'
            # Invalid share basis needs no expensive historical prices.
            if not usable:
                continue
            prices = source.prices(code, end_year - max(usable) + 1, end_year)
            matched = []
            for period in usable:
                rows = sorted((row for row in annual[code] if row['year'] > end_year - period),
                              key=lambda row: row['year'])
                outcome, item = finish_stock(stock, rows, dps[period], prices, period)
                outcomes[period][code] = outcome
                if outcome == 'matched':
                    matched.append(item)
                    items[period].append(item)
            if matched:
                current = source.current_dividend_yield(code)
                for item in matched:
                    item.update(current)
        return items, outcomes, stocks
    # END CASH COW RULES

    class JoinQuantSource:
        def __init__(self, day, batch_size):
            self.day, self.as_of, self.batch_size = day, day.isoformat(), batch_size
            self.codes, self.events, self.capital, self.daily = {}, {}, {}, {}
            self.finance, self.query = _apis['finance'], _apis['query']

        def universe(self):
            frame = _apis['get_all_securities'](types=['stock'], date=self.day)
            if frame is None or frame.empty or 'display_name' not in frame.columns:
                raise ValueError('聚宽股票池缺失，保留已有结果。')
            result = []
            for full_code, row in frame.iterrows():
                code = str(full_code).split('.')[0]
                if not re.fullmatch(r'\d{6}', code) or code in self.codes:
                    raise ValueError('股票池包含无效或重复代码。')
                self.codes[code] = str(full_code)
                exchange = 'SH' if code.startswith('6') else 'BJ' if code.startswith(('4', '8', '92')) else 'SZ'
                result.append(dict(code=code, name=str(row['display_name']), exchange=exchange))
            return result

        def read(self, table, fields, conditions):
            selection = self.query(*[getattr(table, key) for key in fields]).filter(*conditions)
            fetch = lambda cursor: self.finance.run_query(selection.filter(table.id > cursor).order_by(table.id).limit(2000))
            for page in _source_pages(fetch, fields=fields, label='奶牛股公开报表', page_size=2000):
                for row in page.to_dict('records'):
                    yield row

        def dividend_rows(self, codes, start_year=None, end_year=None):
            table = self.finance.STK_XR_XD
            fields = ('id', 'code', 'report_date', 'implementation_pub_date', 'a_bonus_date', 'a_xr_date',
                      'bonus_amount_rmb', 'bonus_ratio_rmb', 'dividend_ratio', 'transfer_ratio',
                      'implementation_bonusnote', 'bonus_cancel_pub_date')
            for offset in range(0, len(codes), self.batch_size):
                batch = codes[offset:offset + self.batch_size]
                conditions = [table.code.in_([self.codes[c] for c in batch]),
                              table.implementation_pub_date <= self.day]
                if start_year is not None:
                    conditions += [table.report_date >= datetime.date(start_year, 1, 1),
                                   table.report_date <= datetime.date(end_year, 12, 31)]
                for row in self.read(table, fields, conditions):
                    code = str(row['code']).split('.')[0]
                    if code not in batch:
                        raise ValueError('分红记录包含范围外股票。')
                    row['paid'] = _optional_date(row['a_bonus_date']) or _optional_date(row['a_xr_date'])
                    cancelled = _optional_date(row['bonus_cancel_pub_date'])
                    if row['paid'] and row['paid'] <= self.day and not (cancelled and cancelled <= self.day):
                        yield row

        def dividends(self, year, codes):
            result, seen, invalid = {}, set(), set()
            for row in self.dividend_rows(codes, year, year):
                code = str(row['code']).split('.')[0]
                fiscal = _optional_date(row['report_date'])
                key = (code, fiscal, row['paid'])
                value = _float_number(row['bonus_amount_rmb'])
                if fiscal is None or fiscal.year != year or key in seen or value is None or value < 0:
                    invalid.add(code)
                else:
                    result[code] = result.get(code, 0.0) + value * 10000  # 万元 -> 元
                seen.add(key)
            return {code: dict(cash_dividend=None if code in invalid else result.get(code)) for code in codes}

        def financials(self, year, codes):
            result = {}
            tables = [('indicator', 'adjusted_profit'), ('income', 'np_parent_company_owners'),
                      ('cash_flow', 'net_operate_cash_flow')]
            for offset in range(0, len(codes), self.batch_size):
                batch = codes[offset:offset + self.batch_size]
                values = {c: {} for c in batch}
                # Separate tables preserve a missing statement as missing, not an inner-join disappearance.
                for name, field in tables:
                    table = _apis[name]
                    q = self.query(table.code, table.statDate, table.pubDate, getattr(table, field)).filter(
                        table.code.in_([self.codes[c] for c in batch]))
                    frame = _apis['get_fundamentals'](q, statDate=str(year))
                    rows = _checked_records(frame, ('code', 'statDate', 'pubDate', field), name)
                    seen = set()
                    for row in rows:
                        code = str(row['code']).split('.')[0]
                        if code not in batch or code in seen:
                            raise ValueError('年度财报代码重复或越界。')
                        seen.add(code)
                        published = _optional_date(row['pubDate'])
                        if (_optional_date(row['statDate']) == datetime.date(year, 12, 31)
                                and published is not None and published <= self.day):
                            values[code][field] = _float_number(row[field])
                for code, row in values.items():
                    result[code] = dict(adjusted_profit=row.get('adjusted_profit'),
                        net_profit=row.get('np_parent_company_owners'), operating_cashflow=row.get('net_operate_cash_flow'))
            return result

        def capital_stable(self, code, start_year):
            if code not in self.capital:
                t = self.finance.STK_CAPITAL_CHANGE
                self.capital[code] = sorted(self.read(t,
                    ('id', 'change_date', 'pub_date', 'share_total', 'change_reason'),
                    [t.code == self.codes[code], t.change_date <= self.day, t.pub_date <= self.day]),
                    key=lambda row: (str(row['change_date']), int(row['id'])))
            baseline = None
            for row in self.capital[code]:
                day, shares = _optional_date(row['change_date']), _float_number(row['share_total'])
                if day is None or shares is None or shares <= 0:
                    return False
                if day <= datetime.date(start_year, 1, 1):
                    baseline = shares
                    continue
                reason = str(row['change_reason'] or '')
                ordinary = reason in ('增发A股上市', '增发上市', '超额配售', '回购注销',
                                      '股份回购注销', '股权激励股份回购注销')
                if (baseline is None or (shares != baseline and not ordinary)
                        or re.search(r'拆|合股|缩股|配股|送股|转增', reason)):
                    return False
                baseline = shares
            return baseline is not None

        def per_share_dividends(self, code, start_year, end_year):
            if code not in self.events:
                self.events[code] = list(self.dividend_rows([code]))
            annual, seen, invalid, action = {}, set(), set(), False
            for row in self.events[code]:
                # A corporate action after the fiscal window also invalidates comparison with today's price.
                if row['paid'].year >= start_year:
                    note = str(row['implementation_bonusnote'] or '')
                    if (any((_float_number(row[f]) or 0) > 0 for f in ('dividend_ratio', 'transfer_ratio'))
                            or re.search(r'送\s*[1-9]|转增?\s*[1-9]|配股|拆股|合股', note)):
                        action = True
                fiscal = _optional_date(row['report_date'])
                if fiscal is None or not start_year <= fiscal.year <= end_year:
                    continue
                year, key = fiscal.year, (fiscal, row['paid'])
                rate = _float_number(row['bonus_ratio_rmb'])
                note = str(row['implementation_bonusnote'] or '')
                if key in seen or rate is None or rate < 0 or re.search(r'差异|部分股东|公众股|不参与', note):
                    invalid.add(year)
                else:
                    annual[year] = annual.get(year, 0.0) + rate / 10  # 元/10股 -> 元/股
                seen.add(key)
            if action or not self.capital_stable(code, start_year):
                return {year: None for year in range(start_year, end_year + 1)}
            return {year: None if year in invalid else annual.get(year) for year in range(start_year, end_year + 1)}

        def per_share_dividends_by_period(self, code, end_year, periods):
            return {p: self.per_share_dividends(code, end_year - p + 1, end_year) for p in periods}

        def prices(self, code, start_year, end_year):
            if code not in self.daily:
                frame = get_price(self.codes[code], start_date=datetime.date(start_year, 1, 1),
                    end_date=self.day, frequency='daily', fields=['close'], fq=None, skip_paused=True, panel=False)
                if frame is None or 'close' not in frame.columns:
                    raise ValueError('历史日线缺失。')
                daily = [(as_date(day), _float_number(row['close'])) for day, row in frame.iterrows()]
                days = [day for day, _ in daily]
                if days != sorted(set(days)) or any(not datetime.date(start_year, 1, 1) <= d <= self.day for d in days):
                    raise ValueError('日线日期重复、乱序或越界。')
                self.daily[code] = daily
            output = {}
            for year in range(start_year, end_year + 1):
                values = [(d, v) for d, v in self.daily[code] if d.year == year]
                valid = (values and values[0][0].month == 1 and values[-1][0].month == 12
                         and all(v is not None and v > 0 for _, v in values))
                output[year] = dict(year_average_close=sum(v for _, v in values) / len(values) if valid else None,
                    price_date=values[-1][0].isoformat() if values else None)
            return output

        def current_dividend_yield(self, code):
            year = self.day.year - 1
            annual = self.per_share_dividends(code, year - 2, year)
            daily = self.daily.get(code, [])
            day, close = daily[-1] if daily else (None, None)
            fresh = day is not None and (self.day - day).days <= 10 and close is not None and close > 0
            history = [dict(fiscal_year=y, yield_pct=100 * annual[y] / close
                            if annual.get(y) is not None and fresh else None) for y in range(year, year - 3, -1)]
            return dict(current_yield=history[0]['yield_pct'], dividend_yield_history=history,
                latest_price=close, latest_price_date=day.isoformat() if day else None,
                dividend_annual_per_share=annual.get(year), dividend_fiscal_year=year)

    def calculate(day, source=None):
        source = source or JoinQuantSource(day, settings['batch_size'])
        def progress(stage, processed=None, total=None):
            suffix = ' · {}/{} 只'.format(processed, total) if total is not None else ''
            _progress_note(stage + suffix)
        items, outcomes, stocks = screen_cash_cows(source, day.year - 1, progress)
        updated = china_now().isoformat() + '+08:00'
        run_id, payload = uuid.uuid4().hex, {}
        for period in PERIODS:
            if len(outcomes[period]) != len(stocks):
                raise ValueError('奶牛股分类未完成，不替换数据库结果。')
            items[period].sort(key=lambda row: (-row['average_yield'], row['code']))
            excluded = sum(v == 'excluded' for v in outcomes[period].values())
            insufficient = sum(v == 'insufficient' for v in outcomes[period].values())
            snapshot = dict(as_of=day.isoformat(), updated_at=updated, run_id=run_id,
                years=period, start_year=day.year - period, end_year=day.year - 1,
                universe_count=len(stocks), evaluated_count=excluded + len(items[period]),
                excluded_count=excluded, insufficient_count=insufficient, matched_count=len(items[period]),
                source='聚宽', rule_version=RULE_VERSION)
            payload[str(period)] = dict(snapshot=snapshot, items=items[period])
        # Do not let an unavailable data source turn the whole market into a valid empty selection.
        if all(row['snapshot']['evaluated_count'] == 0 for row in payload.values()):
            raise ValueError('全市场均缺少可判定资料，保留已有结果。')
        json.dumps(payload, allow_nan=False)
        return payload

    def save(Session, day, payload):
        with session_scope(Session) as session:
            old = session.query(CashCowSnapshot).filter_by(id=1).with_for_update().first()
            if old is not None and old.as_of_date > day:
                raise ValueError('历史日期不能覆盖更新的奶牛股快照。')
            for period, result in payload.items():
                previous = (old.payload.get(period, {}).get('snapshot', {}) if old else {})
                result['snapshot'].update(previous_updated_at=previous.get('updated_at'),
                    previous_previous_updated_at=previous.get('previous_updated_at'))
            if old is None:
                old = CashCowSnapshot(id=1)
                session.add(old)
            old.as_of_date, old.updated_at, old.payload = day, china_now(), payload
            session.commit()

    def run(end_date=settings['end_date'], write_database=True, force_refresh=False):
        if not all(isinstance(value, bool) for value in (write_database, force_refresh)):
            raise ValueError('write_database、force_refresh 必须为布尔值。')
        moment = china_now()
        day = as_date(end_date) if end_date else moment.date()
        if day > moment.date():
            raise ValueError('采集日期不能晚于今天。')
        days = _apis['get_trade_days'](end_date=_closed_day(day, moment), count=1)
        if days is None or len(days) != 1:
            raise ValueError('交易日历不可用。')
        day = as_date(days[0])
        if day > _closed_day(as_date(end_date) if end_date else moment.date(), moment):
            raise ValueError('交易日历返回未收市或未来日期。')
        _progress_note('检查奶牛股最近结果')
        engine = connect_database(_config) if write_database else None
        try:
            if engine is not None:
                Base.metadata.create_all(engine)
                Session = sessionmaker(bind=engine)
                with session_scope(Session) as session:
                    old = session.query(CashCowSnapshot).filter_by(id=1).first()
                    if (not force_refresh and old is not None and old.as_of_date == day
                            and set(old.payload) == {'3', '5', '10'}
                            and all(r['snapshot'].get('rule_version') == RULE_VERSION for r in old.payload.values())):
                        _progress_note('奶牛股当日已采集，跳过重复计算')
                        return preview(old.payload)
            payload = calculate(day)
            if engine is not None:
                save(Session, day, payload)
            return preview(payload)
        finally:
            if engine is not None:
                engine.dispose()

    def preview(payload):
        return pd.DataFrame([dict(years=int(p), code=r['code'], name=r['name'],
            average_yield=r['average_yield'], current_yield=r.get('current_yield'))
            for p, result in payload.items() for r in result['items']],
            columns=['years', 'code', 'name', 'average_yield', 'current_yield'])

    return SimpleNamespace(**locals())


_API_NAMES = ('get_all_securities', 'get_trade_days', 'get_price', 'get_fundamentals',
              'query', 'indicator', 'income', 'cash_flow', 'finance')


def run(database_config=None, **options):
    apis = {key: globals().get(key, getattr(jq, key, getattr(builtins, key, None))) for key in _API_NAMES}
    missing = [key for key, value in apis.items() if value is None]
    if missing:
        raise RuntimeError('聚宽环境缺少接口：' + ', '.join(missing))
    with _progress_scope('奶牛股', 1, 1):
        return _cash_cow_strategy(apis, DB_CONFIG if database_config is None else dict(database_config)).run(**options)


if __name__ == '__main__':
    try:
        cash_cow_df = run()
        print('奶牛股采集完成：3/5/10年合计 {} 条；请同步网站奶牛股数据。'.format(len(cash_cow_df)))
        if len(cash_cow_df):
            print(cash_cow_df.to_string(index=False))
    except Exception as exc:
        raise RuntimeError(_safe_error(exc, DB_CONFIG)) from None
