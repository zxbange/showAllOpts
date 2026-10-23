# -*- coding: utf-8 -*-
"""聚宽研究：短线三战法；近三个交易日快照及30日K线，不下单。"""
import builtins
import datetime
import json
import math
import re
import sys
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from types import SimpleNamespace
from urllib.parse import quote, quote_plus
import jqdata as jq
import pandas as pd
import numpy as np
from sqlalchemy import Column, Date, DateTime, Index, Integer, JSON, Numeric, String, and_, or_, create_engine
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

STRATEGY_CONFIG = {'短线选股': {'end_date': None, 'batch_size': 50}}

DB_CONFIG = {
    'host': 'your_mysql_host',
    'port': 3306,
    'dbname': 'your_database',
    'dbuser': 'your_database_user',
    'dbpassword': 'your_password',
}


DB_OPTIONS = {'connect_timeout': 10, 'retry_count': 5, 'retry_interval': 10, 'pool_recycle': 3600}


PROGRESS_ENABLED = True  # 控制台进度条；关闭后仍保留各策略结果和最终汇总。


PROGRESS_INTERVAL_SECONDS = 5.0  # 最多每隔多少秒刷新；等待接口/数据库时也刷新耗时。


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

    ST_STOCKS = set()
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

    def get_limit_rate(ts_code: str, pct_chg: float) -> float:
        """根据股票代码判断涨停阈值（10%/20%/30%）"""
        code = ts_code.upper()
        if code.startswith("300") or code.startswith("301") or code.startswith("688"):
            return 0.20
        if code.startswith("9"):
            return 0.30
        return 0.10

    def is_limit_up(ts_code: str, pct_chg: float) -> bool:
        """判断是否涨停（根据板块不同阈值）"""
        rate = get_limit_rate(ts_code, pct_chg)
        threshold = 9.5 if rate == 0.10 else (19.5 if rate == 0.20 else 29.5)
        return pct_chg >= threshold

    def analyze_stock(df, ts_code):
        if len(df) < 3:
            return None

        row_t2 = df.iloc[-3]
        row_t1 = df.iloc[-2]
        row_t = df.iloc[-1]

        # 0. 过滤ST股票
        if ts_code in ST_STOCKS:
            return None

        # 1. T-2日必须涨停
        if not is_limit_up(ts_code, row_t2["pct_chg"]):
            return None

        # 计算T-2涨停价
        rate_t2 = get_limit_rate(ts_code, row_t2["pct_chg"])
        t2_limit_price = round(row_t2["pre_close"] * (1 + rate_t2), 2)

        # 2. T-1日条件（不限是否涨停，只看低价和放量）
        if row_t1["low"] < t2_limit_price:
            return None

        # 3. T日条件（未涨停 + 低价过涨停价）
        if is_limit_up(ts_code, row_t["pct_chg"]):
            return None
        if row_t["low"] < t2_limit_price:
            return None

        return {
            "ts_code": ts_code,
            "date_t2": str(row_t2["date"]),
            "date_t1": str(row_t1["date"]),
            "date_t": str(row_t["date"]),
            "t2_pct": round(row_t2["pct_chg"], 2),
            "t2_limit_price": t2_limit_price,
            "t1_pct": round(row_t1["pct_chg"], 2),
            "t1_vol_ratio": round(row_t1["volume"] / row_t2["volume"], 2),
            "t1_low": row_t1["low"],
            "t_pct": round(row_t["pct_chg"], 2),
            "t_low": row_t["low"],
        }

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

    def finite(value):
        try:
            value = float(value)
            return value if math.isfinite(value) else None
        except (TypeError, ValueError):
            return None

    def evaluate(history, code, is_st=False):
        # Keep original strategy windows: BBI tail 60, xuli tail 65.
        matched = []
        if BBIShortLongSelector().select(history['date'].iloc[-1], {code: history}):
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
        stamp = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
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
        latest = clock.date() if clock.hour >= 16 else clock.date() - datetime.timedelta(days=1)
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

_API_NAMES = ('get_all_securities', 'get_trade_days', 'get_price', 'get_extras', 'get_industry', 'get_fundamentals', 'query', 'valuation')

def run(**options):
    apis = {key: globals().get(key, getattr(jq, key, getattr(builtins, key, None))) for key in _API_NAMES}
    with _progress_scope('短线选股', 1, 1):
        return _short_term_strategy(apis, DB_CONFIG).run(**options)

if __name__ == '__main__':
    try:
        short_term_df = run()
        print('短线选股：新增 {} 条股票快照，保留最近三个完整交易日。'.format(len(short_term_df)))
        if len(short_term_df):
            print(short_term_df[['trade_date', 'code', 'name', 'methods', 'close']].to_string(index=False))
    except Exception as exc:
        raise RuntimeError(_safe_error(exc, DB_CONFIG)) from None
