# -*- coding: utf-8 -*-
"""聚宽独立研究：短线三策略：按超短线四策略口径统计后续1/2/3日收盘表现。
进度条展示采集，保留每日选股结果及胜率统计；导出CSV，不连接业务数据库、不下单。
"""
import builtins
import datetime
import json
import math
import os
import sys
import time
import uuid
from html import escape
from pathlib import Path
from types import SimpleNamespace

import jqdata as jq
import numpy as np
import pandas as pd

START_DATE = '2024-01-01'  # 包含这一天
END_DATE = None  # 入选截止日；None取最近已收盘交易日
AS_OF_DATE = None  # 后续表现观察截止日；可以晚于END_DATE
PRICE_BATCH_SIZE = 50
FUNDAMENTAL_BATCH_SIZE = 1000
USE_CACHE = True  # 保存研究进度；重跑只补未完成部分
CACHE_DIR = 'short_term_three_validation_cache'
HORIZONS = (1, 2, 3)
METHODS = {'bukou': '补票龙', 'redhover': '红悬停龙', 'xuli': '蓄力龙'}
RULE_VERSION = 'stock_three_patterns_v2_nonst_cap30'
CACHE_VERSION = 'short_three_close_v1'
API_NAMES = ('get_all_securities', 'get_trade_days', 'get_price', 'get_extras',
             'get_fundamentals', 'query', 'valuation')


class ResearchProgress:
    """Notebook updates one display area; console rewrites one line without clearing results."""
    def __init__(self, total, enabled=True):
        self.total, self.enabled = total, enabled
        self.completed, self.day, self.stage = 0, '', '准备采集'
        self.done, self.stage_total, self.note = 0, 0, ''
        self.started, self.last_render, self.width = time.monotonic(), -math.inf, 0
        self.stream = sys.stdout
        self.display_id = 'short-winrate-' + uuid.uuid4().hex
        self.notebook, self.displayed = None, False
        if enabled:
            try:
                from IPython import get_ipython
                from IPython.display import HTML, display, update_display
                if getattr(get_ipython(), 'kernel', None) is not None:
                    self.notebook = (HTML, display, update_display)
            except ImportError:
                pass

    def __enter__(self):
        self.render(force=True)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        status = ('完成' if self.completed == self.total else '未完成') if exc_type is None else (
            '已中断' if issubclass(exc_type, KeyboardInterrupt) else '未完成')
        self.render(force=True, status=status)
        return False

    def start_day(self, day):
        self.day, self.stage, self.note = str(day), '读取研究断点', ''
        self.done, self.stage_total = 0, 0
        self.render(force=True)

    def update(self, stage, done=0, total=0, note=''):
        force = stage != self.stage or (total > 0 and done == total)
        self.stage, self.done, self.stage_total, self.note = stage, done, total, note
        self.render(force=force)

    def finish_day(self):
        self.completed += 1
        self.stage, self.done, self.stage_total, self.note = '当日完成', 0, 0, ''
        self.render(force=True)

    def render(self, force=False, status=None):
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and now - self.last_render < 0.5:
            return
        self.last_render = now
        percent = 100 * self.completed // self.total
        filled = 20 * self.completed // self.total
        phase = self.stage
        if self.stage_total:
            phase += ' {}/{}（{}%）'.format(self.done, self.stage_total, 100*self.done//self.stage_total)
        detail = '{} | {}{} | 已用 {:.0f}s'.format(
            self.day, phase, ' · ' + self.note if self.note else '', now-self.started)
        title = '研究进度：{}/{} 天 {}%{}'.format(
            self.completed, self.total, percent, ' · ' + status if status else '')
        if self.notebook is not None:
            HTML, display, update_display = self.notebook
            content = HTML('<div style="max-width:1000px;padding:10px 0;font-size:14px;color:#333">'
                           '<strong>{}</strong><br><progress value="{}" max="{}" '
                           'style="width:100%;height:18px"></progress><br>{}</div>'.format(
                               escape(title), self.completed, self.total, escape(detail)))
            try:
                if self.displayed:
                    update_display(content, display_id=self.display_id)
                else:
                    display(content, display_id=self.display_id)
                    self.displayed = True
                return
            except Exception:
                self.notebook = None  # Older Notebook display support: keep collection running.
        line = title + ' [' + '#' * filled + '-' * (20-filled) + '] | ' + detail
        self.stream.write('\r' + line + ' ' * builtins.max(0, self.width-len(line)) + ('\n' if status else ''))
        self.stream.flush()
        self.width = len(line)

    def write_result(self, text):
        if not self.enabled:
            return
        if self.notebook is None and self.width:
            self.stream.write('\r' + ' ' * self.width + '\r')
            self.width = 0
        print(text, file=self.stream, flush=True)
        if self.notebook is None:
            self.render(force=True)


def resolve_apis(api_namespace=None):
    """研究环境可能把行情/财务接口注入builtins，而不是导出为jqdata属性。"""
    namespace = api_namespace if api_namespace is not None else globals()
    apis = {}
    for name in API_NAMES:
        value = namespace.get(name)
        if value is None:
            value = getattr(jq, name, None)
        if value is None:
            value = getattr(builtins, name, None)
        apis[name] = value
    missing = [name for name, value in apis.items() if value is None]
    if missing:
        raise RuntimeError('未找到聚宽研究接口：{}；请在聚宽研究环境执行完整脚本。'.format(', '.join(missing)))
    return apis


def china_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.datetime.strptime(str(value)[:10], '%Y-%m-%d').date()


def finite(value):
    try:
        result = builtins.float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def chunks(values, size):
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def checked_calendar(raw, start=None, end=None):
    if raw is None:
        raise ValueError('交易日历接口返回空对象。')
    days = [as_date(d) for d in raw]
    if days != sorted(set(days)):
        raise ValueError('交易日历重复或乱序：前3项={}，后3项={}。'.format(days[:3], days[-3:]))
    outside = [day for day in days if (start is not None and day < start) or (end is not None and day > end)]
    if outside:
        raise ValueError('交易日历越界：请求{}至{}，首个越界日期={}。'.format(start, end, outside[0]))
    return days


def make_selector(apis):
    """与业务采集脚本保持相同选股函数；不依赖外部策略文件。"""
    get_extras = apis['get_extras']
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
        longest = builtins.min(len(bbi), max_window or len(bbi))
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
                need_len = (builtins.max(self.n_short, self.n_long)
                            + self.bbi_min_window + self.m)
                hist = hist.tail(builtins.max(need_len, self.max_window))
                if self._passes_filters(hist):
                    picks.append(code)
            return picks

    def get_limit_rate(ts_code: str) -> builtins.float:
        """根据股票代码判断涨停阈值（10%/20%/30%）"""
        code = ts_code.upper()
        if code.startswith("300") or code.startswith("301") or code.startswith("688"):
            return 0.20
        if code.startswith("9"):
            return 0.30
        return 0.10

    def is_limit_up(ts_code: str, pct_chg: builtins.float) -> builtins.bool:
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
        t2_limit_price = builtins.round(row_t2["pre_close"] * (1 + rate_t2), 2)

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
        t2_pct   = builtins.float(day_t2['pct_chg'])
        t2_open  = builtins.float(day_t2['open'])
        t2_close = builtins.float(day_t2['close'])
        t2_vol   = builtins.float(day_t2['volume'])
        t3_vol   = builtins.float(day_t3['volume'])

        cond1_ok = (
            t2_close > t2_open and                          # 阳线
            t2_pct > RISE_THRESHOLD and                     # 涨幅>3%
            t2_vol > t3_vol * VOLUME_ENLARGE_T2            # 放量>1.2倍
        )

        # ---- 条件2: T-1日阴线 + 收盘>T-2开盘 + 跌幅<4% + 缩量 ----
        t1_pct   = builtins.float(day_t1['pct_chg'])
        t1_open  = builtins.float(day_t1['open'])
        t1_close = builtins.float(day_t1['close'])
        t1_vol   = builtins.float(day_t1['volume'])

        cond2_ok = (
            t1_close < t1_open and                           # 阴线（收<开）
            t1_close > t2_open and                            # 收盘>T-2开盘价
            -4.0 < t1_pct < 0 and                             # 跌幅低于4%（-4% < 跌幅 < 0）
            t1_vol < t2_vol * VOLUME_SHRINK_T1               # 缩量<0.8倍
        )

        # ---- 条件3: T日阳线 + 收盘>T-2收盘 + 涨幅>3% + 放量 ----
        t_pct   = builtins.float(day_t['pct_chg'])
        t_open  = builtins.float(day_t['open'])
        t_close = builtins.float(day_t['close'])
        t_vol   = builtins.float(day_t['volume'])

        cond3_ok = (
            t_close > t_open and                            # 阳线
            t_close > t2_close and                          # 收盘>T-2收盘价
            t_pct > RISE_THRESHOLD and                       # 涨幅>3%
            t_vol > t1_vol * VOLUME_ENLARGE_T               # 放量>1.2倍
        )

        # ---- 条件4: 均线多头（MA10>MA20>MA30>MA60） ----
        closes = df['close'].values.astype(builtins.float)
        ma10 = closes[-10:].mean() if len(closes) >= 10 else 0
        ma20 = closes[-20:].mean() if len(closes) >= 20 else 0
        ma30 = closes[-30:].mean() if len(closes) >= 30 else 0
        ma60 = closes[-60:].mean() if len(closes) >= 60 else 0

        cond4_ok = ma10 > ma20 > ma30 > ma60

        return cond1_ok and cond2_ok and cond3_ok and cond4_ok

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
                        if not pd.isnull(value) and isinstance(value, (builtins.bool, np.bool_, builtins.int, builtins.float, np.integer, np.floating)) and value in (0, 1):
                            flags[code] = builtins.bool(value)
        for code in codes:
            if code in flags:
                continue
            # Single-stock/single-day dict response avoids batch DataFrame omissions.
            extra = get_extras('is_st', [code], start_date=day, end_date=day, df=False)
            values = extra.get(code, []) if isinstance(extra, dict) else []
            if len(values) == 1:
                value = values[0]
                if not pd.isnull(value) and isinstance(value, (builtins.bool, np.bool_, builtins.int, builtins.float, np.integer, np.floating)) and value in (0, 1):
                    flags[code] = builtins.bool(value)
        missing = [code for code in codes if code not in flags]
        if missing:
            raise ValueError('短线股票池ST状态缺失：日期 {}，股票 {}；批量查询及逐只补查后仍无有效数据，该日未写入，请稍后重试。'.format(day, ', '.join(missing)))
        return flags

    bbi_selector = BBIShortLongSelector()
    return SimpleNamespace(evaluate=evaluate, clean_history=clean_history, read_st_flags=read_st_flags)


def collect_selections(apis, selector, day, price_batch_size, progress=None):
    """只用目标日及之前数据筛选；先批量过滤市值和ST，再读取90日日线。"""
    report = progress if progress is not None else lambda *args, **kwargs: None
    report('读取历史股票池')
    days = checked_calendar(apis['get_trade_days'](end_date=day, count=90), end=day)
    if len(days) != 90 or days[-1] != day:
        raise ValueError('{} 的90日指标预热日历不完整。'.format(day))
    securities = apis['get_all_securities'](types=['stock'], date=day)
    if securities is None or securities.empty or not {'start_date', 'display_name'}.issubset(securities.columns):
        raise ValueError('{} 的历史股票列表不完整。'.format(day))
    if securities.index.duplicated().any():
        raise ValueError('历史股票列表有重复代码。')
    codes = sorted(c for c in securities.index if c.startswith(('0', '3', '4', '6', '8', '9')))
    if not codes:
        raise ValueError('历史A股股票池为空。')
    caps, eligible = {}, []
    valuation, query = apis['valuation'], apis['query']
    for offset, batch in enumerate(chunks(codes, FUNDAMENTAL_BATCH_SIZE)):
        report('市值与ST筛选', offset*FUNDAMENTAL_BATCH_SIZE, len(codes))
        q = query(valuation.code, valuation.market_cap).filter(valuation.code.in_(batch))
        frame = apis['get_fundamentals'](q, date=day)
        if frame is None or not {'code', 'market_cap'}.issubset(frame.columns):
            raise ValueError('{} 总市值接口缺少字段。'.format(day))
        for row in frame.to_dict('records'):
            code, cap = row['code'], finite(row['market_cap'])
            if code not in batch or code in caps or cap is None or cap <= 0:
                raise ValueError('{} 总市值数据重复、越界或无效：{}'.format(day, code))
            caps[code] = cap
        missing = [code for code in batch if code not in caps]
        if missing:
            raise ValueError('{} 总市值缺失：{}'.format(day, ', '.join(missing[:10])))
        large = [code for code in batch if caps[code] >= 30]
        flags = selector.read_st_flags(large, day)
        eligible.extend(code for code in large if not flags[code])
        report('市值与ST筛选', builtins.min((offset+1)*FUNDAMENTAL_BATCH_SIZE, len(codes)), len(codes))
    selected = []
    batches = list(chunks(eligible, price_batch_size))
    for i, batch in enumerate(batches, 1):
        report('选股行情批次', i-1, len(batches), '已入选 {}只'.format(len(selected)))
        raw = apis['get_price'](batch, start_date=days[0], end_date=day, frequency='daily',
                               fields=['open', 'high', 'low', 'close', 'volume'],
                               fq='pre', skip_paused=False, panel=False)
        for code in batch:
            listing = as_date(securities.loc[code, 'start_date'])
            if listing > day:
                raise ValueError('股票上市日期晚于入选日期：{}'.format(code))
            history = selector.clean_history(raw, code, days, listing)
            methods = selector.evaluate(history, code)
            if methods:
                end_value = securities.loc[code, 'end_date'] if 'end_date' in securities.columns else None
                selected.append(dict(code=code, name=str(securities.loc[code, 'display_name']),
                                     methods=methods, market_cap=caps[code],
                                     selection_close=finite(history['close'].iloc[-1]),
                                     end_date=None if pd.isnull(end_value) else as_date(end_value).isoformat()))
        report('选股行情批次', i, len(batches), '已入选 {}只'.format(len(selected)))
    return dict(version=CACHE_VERSION, rule_version=RULE_VERSION, day=day.isoformat(),
                universe_count=len(codes), evaluated_count=len(eligible), selected=selected, outcomes={})


def read_checkpoint(folder, day):
    if folder is None:
        return None
    path = folder / (day.isoformat() + '.json')
    if not path.exists():
        return None
    with path.open('r', encoding='utf-8') as stream:
        result = json.load(stream)
    if (result.get('version') != CACHE_VERSION or result.get('rule_version') != RULE_VERSION
            or result.get('day') != day.isoformat() or not isinstance(result.get('selected'), list)
            or not isinstance(result.get('outcomes'), dict)):
        raise ValueError('研究断点版本或结构不匹配，请更换CACHE_DIR：{}'.format(path))
    seen = set()
    for stock in result['selected']:
        if stock['code'] in seen or not stock['methods'] or len(set(stock['methods'])) != len(stock['methods']):
            raise ValueError('研究断点包含重复样本。')
        if builtins.any(method not in METHODS for method in stock['methods']):
            raise ValueError('研究断点包含未知策略。')
        seen.add(stock['code'])
    return result


def save_checkpoint(folder, record):
    if folder is None:
        return
    path = folder / (record['day'] + '.json')
    temporary = path.with_suffix('.json.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(record, stream, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    os.replace(str(temporary), str(path))


RETURN_PRICE_FIELD = 'adj_close'
STRATEGIES = list(METHODS.values())
OUTPUT_DIR = '短线三策略验证结果'

def forward_outcomes(frame):
    """先按市场日历重建索引再shift；停牌和缺行绝不让观察日顺延。"""
    c = frame[RETURN_PRICE_FIELD]
    dates = pd.Series(frame.index, index=frame.index)
    present = frame['close'] > 0
    result = pd.DataFrame(index=frame.index)
    for horizon in (1, 2, 3):
        prefix = 'd' + str(horizon)
        target = dates.shift(-horizon)
        close = c.shift(-horizon)
        status = pd.Series('有效', index=frame.index)
        status.loc[target.isna()] = '待观察'
        status.loc[target.notna() & (~present.shift(-horizon, fill_value=False) | close.isna() | (close <= 0))] = '缺少行情'
        status.loc[target.notna() & frame['paused'].shift(-horizon).isna()] = '缺少行情'
        status.loc[frame['paused'].shift(-horizon) == 1] = '停牌'
        result[prefix + '_date'] = target
        result[prefix + '_close'] = close
        result[prefix + '_status'] = status
        result[prefix + '_return_pct'] = ((close/c-1)*100).where(status == '有效')
        result[prefix + '_win'] = np.where(status == '有效', (close > c).astype(int), np.nan)
    return result


def summary_table(details):
    rows = []
    for method in STRATEGIES:
        group = details[details['strategy'] == method]
        for horizon in (1, 2, 3):
            prefix = 'd' + str(horizon)
            status_col, return_col = prefix + '_status', prefix + '_return_pct'
            valid = group[status_col].eq('有效') & np.isfinite(group[return_col])
            returns = group.loc[valid, return_col]
            count = len(returns)
            row = {'策略': method, '后续交易日': horizon, '触发样本': len(group),
                '股票数': group['code'].nunique(), '有效样本': count,
                '上涨次数': int((returns > 0).sum()), '持平次数': int((returns == 0).sum()),
                '下跌次数': int((returns < 0).sum()),
                '胜率(%)': (returns > 0).mean()*100 if count else np.nan,
                '平均收益(%)': returns.mean(), '中位收益(%)': returns.median(),
                '最差收益(%)': returns.min(), '最好收益(%)': returns.max(),
                '待观察': int(group[status_col].eq('待观察').sum()),
                '其余不可统计': int((~valid & ~group[status_col].eq('待观察')).sum())}
            row['停牌样本'] = int(group[status_col].eq('停牌').sum())
            row['行情缺失'] = int(group[status_col].eq('缺少行情').sum())
            rows.append(row)
    return pd.DataFrame(rows)


def window_summary(details):
    rows = []
    for method in STRATEGIES:
        group = details[details['strategy'] == method]
        complete = pd.Series(True, index=group.index)
        for horizon in (1, 2, 3):
            complete &= group['d{}_status'.format(horizon)].eq('有效')
        full = group.loc[complete]
        closing = full[['d{}_return_pct'.format(h) for h in (1, 2, 3)]]
        rows.append({'策略': method, '触发样本': len(group), '完整三日样本': len(full),
            '三日内任一收盘上涨率(%)': (closing.gt(0).any(axis=1).mean()*100 if len(full) else np.nan),
            '三日内最好收盘收益均值(%)': closing.max(axis=1).mean(),
            '三日内最差收盘收益均值(%)': closing.min(axis=1).mean()})
    return pd.DataFrame(rows)


def collect_forward(apis, record, calendar, folder, batch_size, progress):
    """原统计函数原样复用；信号价与观察价同批查询后复权价。"""
    day = as_date(record['day'])
    needed = []
    for stock in record['selected']:
        saved = record['outcomes'].get(stock['code'])
        # 同一观察范围内完整结果可复用；缺行情重试、窗口延长重算。
        if (saved and saved.get('through') == calendar[-1].isoformat()
                and all(saved['row'].get('d{}_status'.format(h)) != '缺少行情' for h in HORIZONS)):
            continue
        needed.append(stock['code'])
    for offset, batch in enumerate(chunks(needed, batch_size)):
        progress.update('后续收盘表现', offset, len(needed))
        frames = []
        for fq in (None, 'post'):
            raw = apis['get_price'](batch, start_date=day, end_date=calendar[-1],
                frequency='daily', fields=['close', 'paused'], fq=fq, skip_paused=False, panel=False)
            if raw is None or not {'code', 'time', 'close', 'paused'}.issubset(raw.columns):
                raise ValueError('后续行情字段缺失；保留名单，下次续算。')
            raw = raw.copy()
            raw['date'] = pd.to_datetime(raw['time']).dt.normalize()
            if (raw.duplicated(['code', 'date']).any() or not raw['code'].isin(batch).all()
                    or not raw['date'].isin(pd.to_datetime(calendar)).all()):
                raise ValueError('后续行情有重复或越界记录。')
            frames.append(raw)
        for code in batch:
            index = pd.DatetimeIndex(calendar)
            raw, post = [f[f['code'] == code].set_index('date').reindex(index) for f in frames]
            bars = pd.DataFrame(index=index)
            bars['close'] = pd.to_numeric(raw['close'], errors='coerce')
            bars['adj_close'] = pd.to_numeric(post['close'], errors='coerce')
            bars['paused'] = pd.to_numeric(raw['paused'], errors='coerce')
            # 无效数值按缺失处理，避免产生无穷收益；原统计输入也要求有效基准。
            bars.replace([np.inf, -np.inf], np.nan, inplace=True)
            bars.loc[~bars['paused'].isin([0, 1]), 'paused'] = np.nan
            outcome = forward_outcomes(bars).iloc[0].to_dict()
            base = finite(bars['adj_close'].iloc[0])
            if base is None or base <= 0 or not (bars['close'].iloc[0] > 0):
                for h in HORIZONS:
                    if outcome['d{}_status'.format(h)] == '有效':
                        outcome['d{}_status'.format(h)] = '缺少行情'
                        outcome['d{}_return_pct'.format(h)] = np.nan
                        outcome['d{}_win'.format(h)] = np.nan
            outcome.update(signal_close=base, signal_adj_close=base,
                           signal_raw_close=finite(bars['close'].iloc[0]))
            clean = {}
            for key, value in outcome.items():
                if pd.isnull(value):
                    clean[key] = None
                elif isinstance(value, (datetime.date, datetime.datetime, pd.Timestamp)):
                    clean[key] = as_date(value).isoformat()
                elif isinstance(value, np.generic):
                    clean[key] = value.item()
                else:
                    clean[key] = value
            record['outcomes'][code] = dict(through=calendar[-1].isoformat(), row=clean)
        save_checkpoint(folder, record)
        progress.update('后续收盘表现', builtins.min(offset + batch_size, len(needed)), len(needed))


def run(start_date=START_DATE, end_date=END_DATE, as_of_date=AS_OF_DATE,
        price_batch_size=PRICE_BATCH_SIZE, use_cache=USE_CACHE, cache_dir=CACHE_DIR,
        output_dir=OUTPUT_DIR, api_namespace=None, show=True):
    if isinstance(price_batch_size, builtins.bool) or not isinstance(price_batch_size, builtins.int) or not 1 <= price_batch_size <= 100:
        raise ValueError('PRICE_BATCH_SIZE 必须为1至100的整数。')
    apis = resolve_apis(api_namespace)
    now = china_now()
    closed = now.date() if now.hour >= 16 else now.date() - datetime.timedelta(days=1)
    start = as_date(start_date)
    observed = builtins.min(as_date(as_of_date), closed) if as_of_date else closed
    end = as_date(end_date) if end_date else observed
    if not start <= end <= observed:
        raise ValueError('要求：开始日期 <= 信号截止日 <= 可用观察截止日。')
    days = checked_calendar(apis['get_trade_days'](start_date=start, end_date=observed), start, observed)
    selected_days = [d for d in days if d <= end]
    if not selected_days:
        raise ValueError('指定入选区间没有交易日。')
    folder = Path(cache_dir) / CACHE_VERSION if use_cache else None
    if folder is not None:
        folder.mkdir(parents=True, exist_ok=True)
    selector = make_selector(apis)
    rows, daily = [], []
    with ResearchProgress(len(selected_days), enabled=show) as progress:
        for i, day in enumerate(selected_days):
            progress.start_day(day)
            record = read_checkpoint(folder, day)
            if record is None:
                record = collect_selections(apis, selector, day, price_batch_size, progress.update)
                save_checkpoint(folder, record)
            collect_forward(apis, record, days[i:i+4], folder, price_batch_size, progress)
            for stock in record['selected']:
                outcome = record['outcomes'][stock['code']]['row']
                for method in stock['methods']:
                    row = dict(outcome, strategy=METHODS[method], code=stock['code'],
                               signal_date=pd.Timestamp(day), display_name=stock['name'], market_cap=stock['market_cap'])
                    rows.append(row)
            daily.append(dict(signal_date=day.isoformat(), universe_count=record['universe_count'],
                              evaluated_count=record['evaluated_count'], selected_count=len(record['selected'])))
            progress.finish_day()
    columns = ['strategy', 'code', 'signal_date', 'display_name', 'market_cap',
               'signal_close', 'signal_adj_close', 'signal_raw_close']
    columns += ['d{}_{}'.format(h, field) for h in HORIZONS for field in ('date', 'close', 'status', 'return_pct', 'win')]
    details = pd.DataFrame(rows, columns=columns)
    details['signal_date'] = pd.to_datetime(details['signal_date'])
    for h in HORIZONS:
        details['d{}_return_pct'.format(h)] = pd.to_numeric(details['d{}_return_pct'.format(h)], errors='coerce')
    details = details.sort_values(['signal_date', 'strategy', 'code']).reset_index(drop=True)
    if details.duplicated(['strategy', 'signal_date', 'code']).any():
        raise ValueError('同策略同日同股出现重复样本。')
    summary, windows = summary_table(details), window_summary(details)
    monthly = []
    for month in pd.period_range(start, selected_days[-1], freq='M'):
        item = summary_table(details[details['signal_date'].dt.to_period('M') == month])
        item.insert(0, '信号月份', str(month))
        monthly.append(item)
    destination = Path(output_dir) / china_now().strftime('%Y%m%d_%H%M%S_%f')
    destination.mkdir(parents=True, exist_ok=False)
    outputs = {'收盘胜率汇总.csv': summary, '三日窗口汇总.csv': windows,
               '策略信号明细.csv': details, '按月收盘胜率.csv': pd.concat(monthly, ignore_index=True),
               '数据覆盖.csv': pd.DataFrame(daily)}
    for filename, frame in outputs.items():
        frame.to_csv(str(destination / filename), index=False, encoding='utf-8-sig')
    metadata = dict(start_date=str(start), first_signal_day=str(selected_days[0]), end_date=str(selected_days[-1]),
                    as_of_date=str(days[-1]), rule_version=RULE_VERSION, cache_version=CACHE_VERSION,
                    signal_price='pre', return_price='post', horizons=list(HORIZONS), warmup_trade_days=90,
                    limitations=['信号日收盘为基准，不代表可成交买入；不含费用、滑点及涨跌停成交模拟',
                                 '停牌、缺少行情、待观察不进入有效分母；持平进入分母不计胜',
                                 '历史当日股票池、非ST、总市值至少30亿元；保持三策略原生产规则',
                                 '信号日停牌未额外排除，沿用生产策略；未添加四策略特有股票池限制',
                                 '有效缓存不自动修订，更换CACHE_DIR可重采；勿并发写入同目录'])
    (destination / '运行参数.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    result = SimpleNamespace(summary=summary, windows=windows, details=details,
                             monthly=pd.concat(monthly, ignore_index=True), daily_counts=pd.DataFrame(daily),
                             output_dir=destination)
    if show:
        print('\n信号区间 {} 至 {}；观察截至 {}。'.format(selected_days[0], selected_days[-1], days[-1]))
        print(summary.to_string(index=False, na_rep='--'))
        print(windows.to_string(index=False, na_rep='--'))
        print('结果目录：' + str(destination.resolve()))
        print('这是信号后收盘表现，不是可成交交易回测；未扣成本。')
    return result


if __name__ == '__main__':
    research_result = run()
    winrate_df = research_result.summary
    window_df = research_result.windows
    detail_df = research_result.details
