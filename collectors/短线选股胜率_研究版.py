# -*- coding: utf-8 -*-
"""聚宽独立研究：短线三战法入选后第2/4/10个交易日的收盘表现。
直接显示胜率及涨跌幅门槛统计；不生成CSV、不连接业务数据库、不下单。
"""
import builtins
import datetime
import json
import math
import os
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import jqdata as jq
import numpy as np
import pandas as pd

START_DATE = '2026-07-01'  # 包含这一天
END_DATE = None  # 入选截止日；None取最近已收盘交易日
AS_OF_DATE = None  # 后续表现观察截止日；可以晚于END_DATE
PRICE_BATCH_SIZE = 50
FUNDAMENTAL_BATCH_SIZE = 1000
USE_CACHE = True  # 保存研究进度；重跑只补未完成部分
CACHE_DIR = 'short_term_winrate_cache'
HORIZONS = (2, 4, 10)
METHODS = {'bukou': '补票龙', 'redhover': '红悬停龙', 'xuli': '蓄力龙'}
RULE_VERSION = 'stock_three_patterns_v2_nonst_cap30'
CACHE_VERSION = 'short_term_returns_v1'
THRESHOLDS = (('涨幅>0%', 'up_0'), ('涨幅>5%', 'up_5'), ('涨幅>10%', 'up_10'),
              ('涨幅<0%', 'down_0'), ('涨幅<-5%', 'down_5'), ('涨幅<-10%', 'down_10'))
API_NAMES = ('get_all_securities', 'get_trade_days', 'get_price', 'get_extras',
             'get_fundamentals', 'query', 'valuation')


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


def collect_selections(apis, selector, day, price_batch_size):
    """只用目标日及之前数据筛选；先批量过滤市值和ST，再读取90日日线。"""
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
    for batch in chunks(codes, FUNDAMENTAL_BATCH_SIZE):
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
    selected, started = [], time.monotonic()
    batches = list(chunks(eligible, price_batch_size))
    for i, batch in enumerate(batches, 1):
        if i == 1 or i % 10 == 0 or i == len(batches):
            print('  {} 行情批次 {}/{}，已入选 {} 只，已用 {:.0f}s'.format(
                day, i, len(batches), len(selected), time.monotonic() - started), flush=True)
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


def measure_return(before, after):
    """用十进制交叉相乘判门槛，避免恰好5%因浮点误差计入>5%。"""
    base, target = Decimal(str(before)), Decimal(str(after))
    if not base.is_finite() or not target.is_finite() or base <= 0 or target <= 0:
        raise ValueError('收益计算价格必须为正且有限。')
    change = (target - base) * 100
    flags = {}
    for n in (0, 5, 10):
        flags['up_' + str(n)] = change > base * n
        flags['down_' + str(n)] = change < -base * n
    flags['flat'] = change == 0
    return builtins.float(change / base), flags


def valid_flag(value):
    return (not pd.isnull(value) and isinstance(value, (builtins.bool, np.bool_, builtins.int, builtins.float, np.integer, np.floating))
            and value in (0, 1))


def collect_outcomes(apis, record, targets, folder, batch_size):
    """入选日和观察日复权价一并查询，避免新旧价格复权基准不同。"""
    day, outcomes, needed = as_date(record['day']), record['outcomes'], {}
    for stock in record['selected']:
        for horizon, target in targets.items():
            old = outcomes.get('{}|{}'.format(stock['code'], horizon))
            if target is not None and not (old and old.get('status') == '有效'
                                          and old.get('target_date') == target.isoformat()):
                needed.setdefault(stock['code'], []).append((horizon, target))
    stocks = {stock['code']: stock for stock in record['selected']}
    for batch in chunks(sorted(needed), batch_size):
        last_day = builtins.max(target for code in batch for _, target in needed[code])
        raw = apis['get_price'](batch, start_date=day, end_date=last_day, frequency='daily',
                               fields=['close', 'paused'], fq='pre', skip_paused=False, panel=False)
        if raw is None or not {'code', 'time', 'close', 'paused'}.issubset(raw.columns):
            raise ValueError('后续行情接口返回不完整，保留入选名单，重跑可续算。')
        prices = {}
        for row in raw.to_dict('records'):
            code, date = row['code'], as_date(row['time'])
            key = (code, date)
            if code not in batch or not day <= date <= last_day or key in prices:
                raise ValueError('后续行情有重复记录或日期/股票越界。')
            prices[key] = row
        for code in batch:
            base = prices.get((code, day), {})
            before = finite(base.get('close'))
            for horizon, target in needed[code]:
                point = prices.get((code, target), {})
                after = finite(point.get('close'))
                result = dict(target_date=target.isoformat(), base_close=before, target_close=after,
                              base_paused=None, target_paused=None, return_pct=None, flags={},
                              status='数据缺失', reason='收盘价或停牌状态缺失')
                delisted = stocks[code].get('end_date')
                if delisted and target > as_date(delisted):
                    result['reason'] = '观察日已退市，无可比较收盘价'
                elif (before is not None and after is not None and before > 0 and after > 0
                      and valid_flag(base.get('paused')) and valid_flag(point.get('paused'))):
                    pct, flags = measure_return(before, after)
                    result.update(status='有效', reason='', base_paused=builtins.bool(base['paused']),
                                  target_paused=builtins.bool(point['paused']), return_pct=pct, flags=flags)
                outcomes['{}|{}'.format(code, horizon)] = result
        save_checkpoint(folder, record)


DETAIL_COLUMNS = ['策略', '入选日', '股票代码', '股票名称', '总市值（亿元）', '观察期限（交易日）',
                  '观察日', '状态', '入选日收盘价（前复权）', '观察日收盘价（前复权）', '涨跌幅（%）',
                  '入选日停牌', '观察日停牌', '说明'] + [label for label, _ in THRESHOLDS] + ['持平']


def detail_rows(record, targets):
    rows = []
    for stock in record['selected']:
        for horizon in HORIZONS:
            target = targets[horizon]
            result = record['outcomes'].get('{}|{}'.format(stock['code'], horizon), {}) if target else {}
            if target and result.get('target_date') != target.isoformat():
                raise ValueError('观察日期与断点不一致，不能使用旧结果。')
            for method in stock['methods']:
                row = {'策略': METHODS[method], '入选日': record['day'], '股票代码': stock['code'],
                       '股票名称': stock['name'], '总市值（亿元）': stock['market_cap'],
                       '观察期限（交易日）': horizon, '观察日': target.isoformat() if target else None,
                       '状态': result.get('status', '待观察'),
                       '入选日收盘价（前复权）': result.get('base_close'),
                       '观察日收盘价（前复权）': result.get('target_close'), '涨跌幅（%）': result.get('return_pct'),
                       '入选日停牌': result.get('base_paused'), '观察日停牌': result.get('target_paused'),
                       '说明': result.get('reason', '尚未走满{}个交易日'.format(horizon))}
                row.update({label: builtins.bool(result.get('flags', {}).get(key, False)) for label, key in THRESHOLDS})
                row['持平'] = builtins.bool(result.get('flags', {}).get('flat', False))
                rows.append(row)
    return rows


def summarize(details):
    summary, distribution = [], []
    for method in METHODS.values():
        for horizon in HORIZONS:
            group = details[(details['策略'] == method) & (details['观察期限（交易日）'] == horizon)]
            valid = group[group['状态'] == '有效']
            n, wins = len(valid), builtins.int(valid['涨幅>0%'].sum())
            row = {'策略': method, '观察期限（交易日）': horizon, '总入选次数': len(group), '有效样本': n,
                   '上涨次数': wins, '下跌次数': builtins.int(valid['涨幅<0%'].sum()), '持平次数': builtins.int(valid['持平'].sum()),
                   '胜率（%）': 100.0 * wins / n if n else None,
                   '超过50%': ('是' if 2 * wins > n else '否') if n else '无有效样本',
                   '待观察': builtins.int((group['状态'] == '待观察').sum()),
                   '数据缺失': builtins.int((group['状态'] == '数据缺失').sum()),
                   '停牌估值样本': builtins.int(((valid['入选日停牌'] == True) | (valid['观察日停牌'] == True)).sum()),
                   '平均涨跌幅（%）': builtins.float(valid['涨跌幅（%）'].mean()) if n else None,
                   '中位涨跌幅（%）': builtins.float(valid['涨跌幅（%）'].median()) if n else None}
            summary.append(row)
            dist = {'策略': method, '观察期限（交易日）': horizon, '有效样本': n}
            dist.update({label: builtins.int(valid[label].sum()) for label, _ in THRESHOLDS})
            distribution.append(dist)
    columns = ['策略', '观察期限（交易日）', '总入选次数', '有效样本', '上涨次数', '下跌次数', '持平次数',
               '胜率（%）', '超过50%', '待观察', '数据缺失', '停牌估值样本', '平均涨跌幅（%）', '中位涨跌幅（%）']
    dist_columns = ['策略', '观察期限（交易日）', '有效样本'] + [label for label, _ in THRESHOLDS]
    return pd.DataFrame(summary, columns=columns), pd.DataFrame(distribution, columns=dist_columns)


def show_results(result):
    print('\n统计区间：{} 至 {}；行情观察截至 {}。'.format(result.start_date, result.end_date, result.as_of_date))
    print('胜率=涨幅>0%的次数/有效样本数；持平计入分母；待观察及缺价样本不计入。')
    print('同股不同入选日分别计数，跨策略分别统计；门槛严格大于/小于，累计计数。')
    print('停牌按平台填充收盘价估值并单列数量，不顺延观察日；缺价不当零收益。')
    focused = result.summary[['策略', '观察期限（交易日）', '有效样本', '上涨次数', '下跌次数', '持平次数',
                              '胜率（%）', '超过50%', '待观察', '数据缺失']].copy()
    focused['胜率（%）'] = focused['胜率（%）'].map(lambda x: '--' if pd.isnull(x) else '{:.2f}'.format(x))
    print('\n各策略胜率（第0天为入选日）：')
    print(focused.to_string(index=False))
    print('\n涨跌幅门槛次数（例如上涨12%同时计入三个上涨门槛）：')
    print(result.distribution.to_string(index=False))
    print('\n完整汇总在 research_result.summary，逐笔明细在 research_result.details；不生成CSV。')
    print('这是收盘表现统计，不含交易成本；连续入选样本可能重叠，超过50%不代表未来胜率或扣费后盈利。')


def run(start_date=START_DATE, end_date=END_DATE, as_of_date=AS_OF_DATE,
        price_batch_size=PRICE_BATCH_SIZE, use_cache=USE_CACHE, cache_dir=CACHE_DIR,
        api_namespace=None, show=True):
    for value, cap, name in ((price_batch_size, 100, 'PRICE_BATCH_SIZE'),
                             (FUNDAMENTAL_BATCH_SIZE, 1000, 'FUNDAMENTAL_BATCH_SIZE')):
        if isinstance(value, builtins.bool) or not isinstance(value, builtins.int) or not 1 <= value <= cap:
            raise ValueError('{} 必须为1至{}的整数。'.format(name, cap))
    apis = resolve_apis(api_namespace)
    now = china_now()
    closed = now.date() - datetime.timedelta(days=1) if now.hour < 16 else now.date()
    start = as_date(start_date)
    observed = as_date(as_of_date) if as_of_date is not None else closed
    end = as_date(end_date) if end_date is not None else observed
    if not start <= end <= observed <= closed:
        raise ValueError('日期须满足：开始日≤入选截止日≤观察截止日≤最近可用收盘日。')
    days = checked_calendar(apis['get_trade_days'](start_date=start, end_date=observed), start, observed)
    selected_days = [day for day in days if day <= end]
    if not selected_days:
        raise ValueError('指定入选区间没有交易日。')
    folder = Path(cache_dir) / CACHE_VERSION if use_cache else None
    if folder is not None:
        folder.mkdir(parents=True, exist_ok=True)
    selector = make_selector(apis)
    rows, daily = [], []
    print('短线胜率研究：{} 至 {}，共{}个入选交易日；观察截至{}。'.format(
        start, selected_days[-1], len(selected_days), days[-1]), flush=True)
    for i, day in enumerate(selected_days):
        record = read_checkpoint(folder, day)
        if record is None:
            print('[{}/{}] {} 历史选股'.format(i + 1, len(selected_days), day), flush=True)
            record = collect_selections(apis, selector, day, price_batch_size)
            save_checkpoint(folder, record)  # 零入选也保存；后续行情失败不必重扫全市场。
        else:
            print('[{}/{}] {} 复用已完成名单，{}只'.format(i + 1, len(selected_days), day, len(record['selected'])), flush=True)
        targets = {n: days[i + n] if i + n < len(days) else None for n in HORIZONS}
        collect_outcomes(apis, record, targets, folder, price_batch_size)
        rows.extend(detail_rows(record, targets))
        daily.append(dict(入选日=day.isoformat(), 股票池=record['universe_count'], 扫描股票=record['evaluated_count'],
                          **{name: builtins.sum(key in stock['methods'] for stock in record['selected']) for key, name in METHODS.items()}))
        print('  完成：' + '，'.join('{} {}次'.format(name, daily[-1][name]) for name in METHODS.values()), flush=True)
    details = pd.DataFrame(rows, columns=DETAIL_COLUMNS)
    summary, distribution = summarize(details)
    result = SimpleNamespace(summary=summary, distribution=distribution, details=details,
                             daily_counts=pd.DataFrame(daily), start_date=start,
                             end_date=selected_days[-1], as_of_date=days[-1])
    if show:
        show_results(result)
    return result


if __name__ == '__main__':
    research_result = run()
    winrate_df = research_result.summary
    threshold_df = research_result.distribution
    detail_df = research_result.details
