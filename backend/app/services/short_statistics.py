# -*- coding: utf-8 -*-
"""本地只读MySQL：七策略统一入口，自2024-01-01起统计后续1/2/3交易日表现。

在本地 Python 运行本文件；默认读取同项目“本地专用/本地统计”下的私有配置，
也可用 --config 指定配置。独立复制使用时仍可填写顶部 DB_CONFIG。
不导入聚宽或其他项目代码；结果写入本地CSV，数据库只执行SELECT。
依赖：pandas、numpy、SQLAlchemy、PyMySQL。参数是研究假设，未经收益优化。
"""
import argparse
import builtins
from types import SimpleNamespace
import datetime as dt
import json
import shutil
import ssl
import sys
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, quote_plus

import numpy as np
import pandas as pd
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import URL

DB_CONFIG = {
    'host': 'your_mysql_host',
    'port': 3306,
    'dbname': 'your_database',
    'dbuser': 'your_database_user',
    'dbpassword': 'your_password',
}
DB_TLS_VERIFY = True
DB_TLS_CA_PEM = ''
SCRIPT_VERSION = '2026-10-29.1-seven'
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / '脚本/本地专用/本地统计/超短线四策略胜率统计配置.json'
PROGRESS_ENABLED = True
RETURN_PRICE_FIELD = 'adj_close'  # 同口径后复权收盘价；改为close可统计未复权价格
START_DATE = '2024-01-01'
END_DATE = None                 # 信号截止日；None取观察截止日
AS_OF_DATE = None               # 观察截止日；None取数据库最新已收盘日
WARMUP_DAYS = 160
READ_BATCH_SIZE = 50
OUTPUT_DIR = '超短线统计结果'
STRATEGIES = ['强势股首次缩量回踩', '超跌后止跌反弹', '平台突破后的首次回踩', '首板后的分歧转强']
PARAMS = dict(
    min_market_cap=30., min_listing_days=365, min_money=50_000_000.,
    strength_quantile=.8, strong_advance_10=.08, pullback_min=.03, pullback_max=.08,
    pullback_days=3, pullback_volume=.8,
    oversold_3=-.08, rebound_volume=1.,
    platform_days=20, platform_width=.15, breakout_volume=1.5,
    retest_days=5, retest_tolerance=.02, retest_volume=.8,
    first_limit_lookback=20, limit_close_tolerance=.005, disagreement_max_drop=.06,
    disagreement_volume=.8, turn_volume=1.1, turn_recovery=.97,
)
RULES = {
    STRATEGIES[0]: '新20日收盘高点且10日涨幅>=8%、MA20上行；随后连续下跌1~3日，'
        '最低价回撤3%~8%，下跌阶段均量<=高点日前后截至高点的5日均量80%；'
        '首次收盘转涨且守住MA10/MA20。信号日10日涨幅位于当日合格股票池前20%。',
    STRATEGIES[1]: '信号前3日跌幅>=8%；当日收盘超过前日最高价、'
        '收阳，成交量>=此前5日均量。不能用本数据排除重大基本面利空。',
    STRATEGIES[2]: '此前20日高低区间<=15%，收盘突破此前20日最高价，量>=此前20日均量1.5倍；'
        '突破后1~5日首次触及突破价上下2%区域，当日低点不超跌2%、收盘守住突破价并收阳，'
        '量<=突破日80%。连续突破只认第一日。',
    STRATEGIES[3]: '实际涨停价收盘，且之前20日未收盘涨停；次日收盘分歧、回撤不超6%、'
        '成交量>=首板80%；首板后第2或第3日首次收盘超过前日最高价、收阳，'
        '量>=前日1.1倍、收复首板收盘价97%，分歧期间不下探首板收盘价92%以下。',
}


class StatisticsProgress:
    """后台定时刷新；数据库等待期间也显示阶段和耗时，不伪造完成数量。"""
    def __init__(self, enabled=True, stream=None, interval=.5):
        self.enabled, self.stream, self.interval = enabled, stream or sys.stdout, interval
        self.started = self.stage_started = time.monotonic()
        self.label, self.detail, self.done, self.total = '准备', '', 0, 0
        self.width, self.tick = 0, 0
        self.lock, self.stopped = threading.RLock(), threading.Event()
        self.thread = None

    def __enter__(self):
        if self.enabled:
            self.thread = threading.Thread(target=self.refresh, daemon=True)
            self.thread.start()
        return self

    def phase(self, label, total=0, detail=''):
        with self.lock:
            self.label, self.total, self.done, self.detail = label, total, 0, detail
            self.stage_started = time.monotonic()
            self.render()

    def update(self, done=None, detail=None):
        with self.lock:
            if done is not None:
                self.done = done
            if detail is not None:
                self.detail = detail

    def refresh(self):
        while not self.stopped.wait(self.interval):
            with self.lock:
                self.render()

    def render(self):
        if not self.enabled:
            return
        elapsed = time.monotonic()-self.started
        if self.total:
            ratio = min(1., self.done/self.total)
            filled = int(16*ratio)
            bar = '[' + '#'*filled + '-'*(16-filled) + ']'
            count = '{}/{} {:.1f}%'.format(self.done, self.total, ratio*100)
        else:
            self.tick += 1
            bar, count = '[' + ('|/-\\'[self.tick % 4]).center(16) + ']', '处理中'
        line = '{} {} | {:.0f}s | {} {}'.format(bar, count, elapsed, self.label, self.detail)
        limit = max(40, shutil.get_terminal_size((120, 24)).columns-1)
        shown, width = '', 0
        for char in line:
            size = 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
            if width+size > limit:
                break
            shown, width = shown+char, width+size
        self.stream.write('\r' + shown + ' '*max(0, self.width-width))
        self.stream.flush()
        self.width = width

    def write(self, message):
        with self.lock:
            if self.enabled and self.width:
                self.stream.write('\r' + ' '*self.width + '\r')
                self.width = 0
            self.stream.write(str(message)+'\n')
            self.stream.flush()

    def __exit__(self, exc_type, exc, traceback):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        with self.lock:
            if exc_type is None:
                self.label, self.total = '完成', max(1, self.total)
                self.done = self.total
            else:
                self.label = '失败/中断：' + self.label
            self.detail = ''
            self.render()
            if self.enabled:
                self.stream.write('\n')
                self.stream.flush()
        return False


# BEGIN SHARED OVERSOLD RULES
OVERSOLD_PARAMS = dict(min_market_cap=30., min_listing_days=365, min_money=50_000_000.,
                       oversold_3=-.08, rebound_volume=1.)


def oversold_pattern(frame, params):
    drop, multiplier = params['oversold_3'], params['rebound_volume']
    if not (-1 < drop < 0) or not (0 < multiplier < float('inf')):
        raise ValueError('超跌阈值须在(-1, 0)之间，量比须为有限正数。')
    c, o, h, v = [frame[n] for n in ('adj_close', 'adj_open', 'adj_high', 'volume')]
    return ((c.shift(1) / c.shift(4) - 1 <= drop)
            & (c > h.shift(1)) & (c > o)
            & (v >= v.shift(1).rolling(5).mean() * multiplier))


def signal_eligibility(bars, universe, params, return_price_field='adj_close'):
    for key in ('min_market_cap', 'min_listing_days', 'min_money'):
        if not (0 <= params[key] < float('inf')):
            raise ValueError(key + '须为有限非负数')
    c = bars['adj_close']
    listed = pd.to_datetime(universe['listed_date'], errors='coerce')
    age = (pd.Series(bars.index, index=bars.index) - listed).dt.days
    ready = c.where(c > 0).rolling(60, min_periods=60).count() == 60
    return ((universe['eligible'] == 1) & (universe['is_st'] == 0)
            & (universe['market_cap'] >= params['min_market_cap'])
            & (age >= params['min_listing_days']) & ready
            & (bars['paused'] == 0) & (bars['money'] >= params['min_money'])
            & (bars['volume'] > 0) & (c > 0) & (bars[return_price_field] > 0))
# END SHARED OVERSOLD RULES


def read_sql(engine, sql, params=None, codes=None):
    statement = text(sql)
    values = dict(params or {})
    if codes is not None:
        statement = statement.bindparams(bindparam('codes', expanding=True))
        values['codes'] = list(codes)
    with engine.connect() as connection:
        return pd.read_sql_query(statement, connection, params=values)


def load_local_config(config_path=None):
    """Only the CLI loads private settings; importing this module does not read them."""
    settings = dict(DB_CONFIG=dict(DB_CONFIG), DB_TLS_VERIFY=DB_TLS_VERIFY, DB_TLS_CA_PEM=DB_TLS_CA_PEM)
    path = DEFAULT_CONFIG_PATH if config_path is None else Path(config_path).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[3] / '脚本/本地统计' / path
    try:
        content = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        if config_path is None:
            return settings
        raise ValueError('指定的本机配置文件不存在，请检查 --config 路径。') from None
    except (OSError, UnicodeError):
        raise ValueError('无法读取本机配置，请检查文件权限及 UTF-8 编码。') from None
    try:
        loaded = json.loads(content)
    except ValueError:
        raise ValueError('本机配置不是有效的 JSON，未加载任何连接设置。') from None
    if not isinstance(loaded, dict) or set(loaded) - set(settings):
        raise ValueError('本机配置须为 JSON 对象，且只含支持的连接设置。')
    config = loaded.get('DB_CONFIG')
    keys = {'host', 'port', 'dbname', 'dbuser', 'dbpassword'}
    if not isinstance(config, dict) or set(config) != keys:
        raise ValueError('本机配置 DB_CONFIG 须包含 host、port、dbname、dbuser、dbpassword。')
    if any(not isinstance(config[key], str) for key in keys - {'port'}):
        raise ValueError('本机配置的数据库地址、库名、用户名及密码须为字符串。')
    if not isinstance(config['port'], (int, str)) or isinstance(config['port'], bool):
        raise ValueError('本机配置的数据库端口须为 1 到 65535 的整数。')
    try:
        port = int(config['port'])
    except ValueError:
        raise ValueError('本机配置的数据库端口须为 1 到 65535 的整数。') from None
    if not 1 <= port <= 65535:
        raise ValueError('本机配置的数据库端口须为 1 到 65535 的整数。')
    if 'DB_TLS_VERIFY' in loaded and not isinstance(loaded['DB_TLS_VERIFY'], bool):
        raise ValueError('本机配置 DB_TLS_VERIFY 须为 true 或 false。')
    if 'DB_TLS_CA_PEM' in loaded and not isinstance(loaded['DB_TLS_CA_PEM'], str):
        raise ValueError('本机配置 DB_TLS_CA_PEM 须为字符串。')
    settings.update(loaded)
    settings['DB_CONFIG'] = dict(config)
    return settings


def make_engine(config, tls_verify=None, tls_ca_pem=None):
    kwargs = dict(drivername='mysql+pymysql', username=config['dbuser'],
                  password=config['dbpassword'], host=config['host'],
                  port=int(config.get('port', 3306)), database=config['dbname'], query={'charset': 'utf8mb4'})
    url = URL.create(**kwargs) if hasattr(URL, 'create') else URL(**kwargs)
    ca_pem = DB_TLS_CA_PEM if tls_ca_pem is None else tls_ca_pem
    context = ssl.create_default_context(cadata=ca_pem or None)
    verify = DB_TLS_VERIFY if tls_verify is None else tls_verify
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return create_engine(url, pool_pre_ping=True, pool_recycle=3600,
                         connect_args={'connect_timeout': 15, 'ssl': context})


def dated_frame(frame, label):
    frame = frame.copy()
    frame['trade_date'] = pd.to_datetime(frame['trade_date']).dt.normalize()
    if frame.duplicated(['trade_date', 'code']).any():
        raise ValueError(label + '有重复的日期/股票主键')
    return frame


def pattern_candidates(frame, params):
    """仅使用当日及更早数据产生信号；未来收益在另一个函数中计算。"""
    p = params
    c, o, h, low, volume = [frame[name] for name in ['adj_close', 'adj_open', 'adj_high', 'adj_low', 'volume']]
    ma10, ma20 = c.rolling(10).mean(), c.rolling(20).mean()
    vol5 = volume.rolling(5).mean()
    ret10 = c / c.shift(10) - 1
    prior_high20 = h.shift(1).rolling(20).max()
    peak = ((c > c.shift(1).rolling(20).max()) & (ret10 >= p['strong_advance_10'])
            & (c > ma20) & (ma20 > ma20.shift(1)))
    result = pd.DataFrame(index=frame.index)
    result['ret10'] = ret10
    result['ma20'] = ma20
    for method in STRATEGIES:
        result[method] = False
        result[method + '_anchor'] = pd.NaT
    dates = pd.Series(frame.index, index=frame.index)
    # 首次回踩：高点之后必须连续下跌，首次转涨后不会重复使用同一高点。
    for lag in range(2, int(p['pullback_days']) + 2):
        declining = pd.Series(True, index=c.index)
        for j in range(1, lag):
            declining &= c.shift(j) < c.shift(j+1)
        depth = 1 - low.rolling(lag).min() / c.shift(lag)
        shrinking = volume.shift(1).rolling(lag-1).mean() <= vol5.shift(lag)*p['pullback_volume']
        signal = (peak.shift(lag, fill_value=False) & declining & shrinking
                  & depth.between(p['pullback_min'], p['pullback_max'])
                  & (c > c.shift(1)) & (c >= ma10) & (c >= ma20))
        result.loc[signal, STRATEGIES[0]] = True
        result.loc[signal, STRATEGIES[0] + '_anchor'] = dates.shift(lag)[signal]
    result[STRATEGIES[1]] = oversold_pattern(frame, p)
    result.loc[result[STRATEGIES[1]], STRATEGIES[1] + '_anchor'] = dates.shift(1)
    n = int(p['platform_days'])
    support = h.shift(1).rolling(n).max()
    width = support / low.shift(1).rolling(n).min() - 1
    breakout = ((c > support) & (width <= p['platform_width']) & (c > ma20)
                & (volume >= volume.shift(1).rolling(n).mean()*p['breakout_volume']))
    breakout = breakout & ~breakout.shift(1, fill_value=False)
    for lag in range(1, int(p['retest_days'])+1):
        level = support.shift(lag)
        untouched = pd.Series(True, index=c.index)
        for j in range(1, lag):
            untouched &= low.shift(j) > level*(1+p['retest_tolerance'])
        signal = (breakout.shift(lag, fill_value=False) & untouched
            & (low <= level*(1+p['retest_tolerance'])) & (low >= level*(1-p['retest_tolerance']))
            & (c >= level) & (c >= o) & (volume <= volume.shift(lag)*p['retest_volume']))
        result.loc[signal, STRATEGIES[2]] = True
        result.loc[signal, STRATEGIES[2] + '_anchor'] = dates.shift(lag)[signal]
    limit = ((frame['high_limit'] > 0)
             & ((frame['close']-frame['high_limit']).abs() <= p['limit_close_tolerance'])
             & (frame['paused'] == 0) & (volume > 0))
    lookback = int(p['first_limit_lookback'])
    known = (frame['close'] > 0) & (frame['high_limit'] > 0) & frame['paused'].notna()
    first_limit = (limit & (limit.shift(1).rolling(lookback).sum() == 0)
                   & (known.shift(1).rolling(lookback).sum() == lookback))
    for lag in (2, 3):
        anchor = c.shift(lag)
        disagreement = ((c.shift(lag-1) < anchor)
            & (c.shift(lag-1) >= anchor*(1-p['disagreement_max_drop']))
            & ~limit.shift(lag-1, fill_value=False)
            & (volume.shift(lag-1) >= volume.shift(lag)*p['disagreement_volume']))
        first_turn = (c > h.shift(1))
        if lag == 3:
            first_turn &= c.shift(1) <= h.shift(2)
        signal = (first_limit.shift(lag, fill_value=False) & disagreement & first_turn
            & (c > o) & (volume >= volume.shift(1)*p['turn_volume'])
            & (c >= anchor*p['turn_recovery']) & (low.rolling(lag).min() >= anchor*.92))
        result.loc[signal, STRATEGIES[3]] = True
        result.loc[signal, STRATEGIES[3] + '_anchor'] = dates.shift(lag)[signal]
    return result


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


def stock_samples(code, bars, universe, calendar, start, end, params, selected=None):
    bars = dated_frame(bars, '股票行情').set_index('trade_date').reindex(calendar)
    universe = dated_frame(universe, '股票快照').set_index('trade_date').reindex(calendar)
    numeric = ['open', 'high', 'low', 'close', 'adj_open', 'adj_high', 'adj_low', 'adj_close',
               'volume', 'money', 'paused', 'high_limit', 'low_limit']
    for column in numeric:
        bars[column] = pd.to_numeric(bars[column], errors='coerce')
    c = bars['adj_close']
    base = signal_eligibility(bars, universe, params, RETURN_PRICE_FIELD)
    if selected is None:
        candidates = pattern_candidates(bars, params)
    else:
        candidates = pd.DataFrame(index=bars.index)
        if '超跌后止跌反弹' in selected:
            candidates['超跌后止跌反弹'] = oversold_pattern(bars, params).fillna(False)
            candidates['超跌后止跌反弹_anchor'] = pd.NaT
    outcomes = forward_outcomes(bars)
    result = pd.concat([candidates, outcomes], axis=1)
    result['code'], result['signal_date'], result['signal_close'] = code, result.index, bars[RETURN_PRICE_FIELD]
    result['signal_raw_close'], result['signal_adj_close'] = bars['close'], c
    result['display_name'] = universe['display_name']
    result['market_cap'] = universe['market_cap']
    in_window = (result.index >= start) & (result.index <= end)
    coverage = dict(code=code, bar_rows=int(bars['close'].notna().sum()),
        snapshot_rows=int(universe['eligible'].notna().sum()),
        bars_missing_on_snapshot_days=int((universe['eligible'].notna() & bars['close'].isna()).sum()),
        base_samples=int((base & in_window).sum()))
    return result.loc[base & in_window].copy(), coverage


def select_signals(samples, params, strategies=None):
    samples = samples.copy()
    samples['strength_rank'] = samples.groupby('signal_date')['ret10'].rank(pct=True, method='average')
    samples[STRATEGIES[0]] &= samples['strength_rank'] > params['strength_quantile']
    pieces = []
    for method in (STRATEGIES if strategies is None else strategies):
        selected = samples.loc[samples[method]].copy()
        selected['strategy'] = method
        selected['anchor_date'] = selected[method + '_anchor']
        pieces.append(selected)
    result = pd.concat(pieces, ignore_index=True)
    return result.sort_values(['signal_date', 'strategy', 'code']).reset_index(drop=True)


def summary_table(details, strategies=None):
    rows = []
    for method in (STRATEGIES if strategies is None else strategies):
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


def window_summary(details, strategies=None):
    rows = []
    for method in (STRATEGIES if strategies is None else strategies):
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


def load_plan(engine, start, end=None, as_of=None, warmup_days=WARMUP_DAYS):
    now = dt.datetime.utcnow() + dt.timedelta(hours=8)
    cutoff = now.date() if now.hour >= 16 else now.date()-dt.timedelta(days=1)
    latest = read_sql(engine, "SELECT MAX(trade_date) AS latest FROM ultra_research_bar WHERE asset_type='stock'")['latest'].iloc[0]
    if pd.isna(latest):
        raise ValueError('股票行情表为空，请先完成采集')
    observed = min(pd.Timestamp(latest).normalize(), pd.Timestamp(as_of or cutoff), pd.Timestamp(cutoff))
    start = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end).normalize() if end else observed
    if start > end or end > observed:
        raise ValueError('要求：开始日期 <= 信号截止日 <= 可用观察截止日')
    raw = read_sql(engine, 'SELECT trade_date FROM ultra_research_calendar WHERE trade_date <= :end ORDER BY trade_date LIMIT 20001', {'end': observed.date()})
    if len(raw) > 20000:
        raise ValueError('交易日历超过20000行，请检查重复或异常历史数据')
    calendar = pd.DatetimeIndex(pd.to_datetime(raw['trade_date']).dt.normalize())
    if calendar.has_duplicates or not calendar.is_monotonic_increasing:
        raise ValueError('交易日历重复或乱序')
    first = calendar.searchsorted(start)
    if first < warmup_days:
        raise ValueError('开始日期之前不足{}个预热交易日'.format(warmup_days))
    calendar = calendar[first-warmup_days:]
    if calendar.empty or calendar[-1] != observed:
        raise ValueError('行情最大日期不在已采集的交易日历中')
    return start, end, observed, calendar


def make_three_selector(selected=None):
    selected = set(selected or ('bukou', 'redhover', 'xuli'))
    """与业务采集脚本保持相同选股函数；不依赖外部策略文件。"""
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
        if 'bukou' in selected and bbi_selector.select(history['date'].iloc[-1], {code: history}):
            matched.append('bukou')
        if 'redhover' in selected and not is_st and analyze_stock(history, code):
            matched.append('redhover')
        if 'xuli' in selected and analyze_one(code, history):
            matched.append('xuli')
        return matched

    bbi_selector = BBIShortLongSelector()
    return SimpleNamespace(evaluate=evaluate)


THREE_METHODS = {'redhover': '红悬停龙', 'xuli': '蓄力龙', 'bukou': '补票龙'}
ALL_STRATEGIES = STRATEGIES + list(THREE_METHODS.values())
VALIDATION_NOTICE = ('统一四策略数据口径的验证：七策略共用四策略沪深股票池、基础过滤、后复权价格及收盘表现口径。'
    '三个短线形态保留原条件和阈值，但复权基准、红悬停价格取整及股票池改变可能使信号不同于原生产选股；'
    '本结果不是原三策略生产信号的精确复现。')


def four_stock(code):
    """Match the original ultra-research collector universe even if the DB is expanded."""
    return ((code.endswith('.XSHG') and code.startswith(('600', '601', '603', '605', '688')))
            or (code.endswith('.XSHE') and code.startswith(('000', '001', '002', '003', '300', '301'))))


def resolve_strategies(values=None):
    if values is None:
        return list(STRATEGIES)  # compatible four-strategy library API
    if isinstance(values, str):
        values = [values]
    aliases = {'four': STRATEGIES, 'three': list(THREE_METHODS.values()), 'all': ALL_STRATEGIES,
               '红悬停': ['红悬停龙']}
    selected = []
    for value in values:
        for token in value.replace('，', ',').split(','):
            names = aliases.get(token, [token])
            for name in names:
                if name not in ALL_STRATEGIES:
                    raise ValueError('未知策略：{}；可用 --list-strategies 查看。'.format(name))
                if name not in selected:
                    selected.append(name)
    if not selected:
        raise ValueError('至少选择一个策略。')
    return selected


def require_columns(engine, table, columns):
    # Identifiers below are hardcoded, never supplied by user input; no DDL/PRAGMA probes.
    try:
        read_sql(engine, 'SELECT {} FROM {} WHERE 1=0'.format(', '.join(columns), table))
    except Exception as exc:
        from sqlalchemy.exc import SQLAlchemyError
        if not isinstance(exc, SQLAlchemyError):
            raise
        raise ValueError('已有数据库缺少表/字段或没有读取权限：{} [{}]；本程序不会建表或采集。'.format(
            table, ', '.join(columns))) from None


def validate_schema(engine):
    require_columns(engine, 'ultra_research_calendar', ['trade_date'])
    require_columns(engine, 'ultra_research_universe',
                    ['trade_date', 'code', 'display_name', 'listed_date', 'eligible', 'is_st', 'market_cap'])
    require_columns(engine, 'ultra_research_bar',
                    ['trade_date', 'code', 'asset_type', 'open', 'high', 'low', 'close',
                     'adj_open', 'adj_high', 'adj_low', 'adj_close', 'volume', 'money',
                     'paused', 'high_limit', 'low_limit'])


def add_three_candidates(code, samples, bars, universe, calendar, start, end, params, selector, activity=None):
    """Evaluate unchanged short-term formulas on the same post-adjusted data and base pool."""
    result = samples.copy()
    history = dated_frame(bars, '股票行情').set_index('trade_date').reindex(calendar)
    snapshots = dated_frame(universe, '股票快照').set_index('trade_date').reindex(calendar)
    for name in THREE_METHODS.values():
        result[name] = False
        result[name + '_anchor'] = pd.NaT
    # Missing data must not masquerade as an untriggered pattern, including gaps
    # inside the original 60-day readiness filter. Validate the historical pool first.
    ages = (pd.Series(calendar, index=calendar) - pd.to_datetime(snapshots['listed_date'], errors='coerce')).dt.days
    potential = ((snapshots['eligible'] == 1) & (snapshots['is_st'] == 0)
                 & (snapshots['market_cap'] >= params['min_market_cap'])
                 & (ages >= params['min_listing_days']) & (calendar >= start) & (calendar <= end))
    for position_in_task, day in enumerate(calendar[potential]):
        if activity is not None and position_in_task % 20 == 0:
            activity(day)
        position = calendar.get_loc(day)
        window = history.iloc[max(0, position-89):position+1].copy()
        listed = pd.to_datetime(snapshots.loc[day, 'listed_date'])
        window = window[window.index >= listed]
        expected = calendar[max(0, position-89):position+1]
        expected = expected[expected >= listed]
        if len(expected) < 90 or len(window) != len(expected):
            raise ValueError('{} {} 缺少三策略90交易日预热窗口。'.format(day.date(), code))
        frame = pd.DataFrame(index=window.index)
        for name in ('open', 'high', 'low', 'close'):
            frame[name] = pd.to_numeric(window['adj_' + name], errors='coerce')
            if not (np.isfinite(frame[name]) & (frame[name] > 0)).all():
                raise ValueError('{} {} 三策略预热行情缺失或无效：adj_{}；不输出部分统计。'.format(
                    day.date(), code, name))
        frame['volume'] = pd.to_numeric(window['volume'], errors='coerce')
        if not (np.isfinite(frame['volume']) & (frame['volume'] >= 0)).all():
            raise ValueError('{} 三策略预热成交量缺失或无效。'.format(day.date()))
        if day not in result.index:
            continue  # The remaining four-strategy base filters rejected this signal day.
        frame['date'] = frame.index.date
        frame['pre_close'] = frame['close'].shift(1)
        frame['pct_chg'] = (frame['close']/frame['pre_close']-1)*100
        for key in selector.evaluate(frame, code):
            result.loc[day, THREE_METHODS[key]] = True
    return result


def run(start_date=START_DATE, end_date=END_DATE, as_of=AS_OF_DATE, output_dir=OUTPUT_DIR,
        engine=None, params=None, tls_verify=None, show_progress=PROGRESS_ENABLED,
        db_config=None, tls_ca_pem=None, strategies=None):
    chosen = resolve_strategies(strategies)
    p = dict(PARAMS, **(params or {}))
    owned = engine is None
    print('七策略收盘胜率统计 v' + SCRIPT_VERSION)
    print(VALIDATION_NOTICE, flush=True)
    try:
        with StatisticsProgress(enabled=show_progress) as progress:
            progress.phase('连接数据库/读取日历')
            if owned:
                verify = DB_TLS_VERIFY if tls_verify is None else tls_verify
                if not verify:
                    progress.write('TLS：请求加密连接，但不验证服务器身份；仅用于可信目标的临时排查。')
                engine = make_engine(DB_CONFIG if db_config is None else db_config,
                                     tls_verify=tls_verify, tls_ca_pem=tls_ca_pem)
            validate_schema(engine)
            start, end, observed, calendar = load_plan(engine, start_date, end_date, as_of)
            bounds = dict(first=calendar[0].date(), last=observed.date())
            progress.phase('读取股票清单')
            codes = read_sql(engine, '''SELECT DISTINCT code FROM ultra_research_universe
                WHERE trade_date BETWEEN :first AND :last AND eligible=1 AND is_st=0
                AND market_cap>=:cap ORDER BY code''',
                dict(first=start.date(), last=end.date(), cap=p['min_market_cap']))['code'].tolist()
            codes = [code for code in codes if four_stock(code)]
            if not codes:
                raise ValueError('研究区间没有合格股票快照，请检查采集进度和日期')
            frames, coverage = [], []
            need_three = any(name in THREE_METHODS.values() for name in chosen)
            selector = make_three_selector() if need_three else None
            progress.write('信号区间 {} ~ {}；观察截至 {}；股票{}只。'.format(
                start.date(), end.date(), observed.date(), len(codes)))
            progress.phase('读取行情/计算策略', total=len(codes))
            for offset in range(0, len(codes), READ_BATCH_SIZE):
                batch = codes[offset:offset+READ_BATCH_SIZE]
                progress.update(detail='读取行情 批次{}/{}'.format(
                    offset//READ_BATCH_SIZE+1, (len(codes)+READ_BATCH_SIZE-1)//READ_BATCH_SIZE))
                bars = dated_frame(read_sql(engine, '''SELECT * FROM ultra_research_bar
                    WHERE asset_type='stock' AND code IN :codes AND trade_date BETWEEN :first AND :last
                    ORDER BY code, trade_date''', bounds, codes=batch), '股票行情')
                progress.update(detail='读取历史快照')
                universe = dated_frame(read_sql(engine, '''SELECT * FROM ultra_research_universe
                    WHERE code IN :codes AND trade_date BETWEEN :first AND :last
                    ORDER BY code, trade_date''', bounds, codes=batch), '股票快照')
                bar_groups, universe_groups = dict(tuple(bars.groupby('code'))), dict(tuple(universe.groupby('code')))
                for stock_pos, code in enumerate(batch, 1):
                    progress.update(detail='计算 ' + code)
                    frame, quality = stock_samples(code, bar_groups.get(code, bars.iloc[:0]),
                        universe_groups.get(code, universe.iloc[:0]), calendar, start, end, p)
                    if need_three:
                        frame = add_three_candidates(code, frame, bar_groups.get(code, bars.iloc[:0]),
                            universe_groups.get(code, universe.iloc[:0]), calendar, start, end, p, selector)
                    frames.append(frame)
                    coverage.append(quality)
                    progress.update(done=offset+stock_pos)
            if not sum(item['base_samples'] for item in coverage):
                raise ValueError('没有可用基础样本，请检查历史快照、行情完整性、预热长度及筛选参数')
            progress.phase('筛选信号/汇总胜率')
            details = select_signals(pd.concat(frames, ignore_index=True), p, chosen)
            details = details[details['strategy'].isin(chosen)]
            if details.duplicated(["signal_date", "strategy", "code"]).any():
                raise ValueError("同策略同日同股出现重复样本")
            summary, windows = summary_table(details, chosen), window_summary(details, chosen)
            monthly = []
            for month in pd.period_range(start, end, freq='M'):
                group = details[details['signal_date'].dt.to_period('M') == month]
                item = summary_table(group, chosen)
                item.insert(0, '信号月份', str(month))
                monthly.append(item)
            destination = Path(output_dir) / (dt.datetime.utcnow()+dt.timedelta(hours=8)).strftime('%Y%m%d_%H%M%S_%f')
            destination.mkdir(parents=True, exist_ok=False)
            outputs = {'收盘胜率汇总.csv': summary, '三日窗口汇总.csv': windows,
                '策略信号明细.csv': details, '按月收盘胜率.csv': pd.concat(monthly, ignore_index=True),
                '数据覆盖.csv': pd.DataFrame(coverage)}
            progress.phase('导出本地结果', total=len(outputs)+1)
            for written, (filename, frame) in enumerate(outputs.items(), 1):
                progress.update(detail=filename)
                frame.to_csv(destination / filename, index=False, encoding='utf-8-sig')
                progress.update(done=written)
            metadata = dict(version=SCRIPT_VERSION, signal_start=str(start.date()), signal_end=str(end.date()),
                observed_through=str(observed.date()), warmup_start=str(calendar[0].date()),
                params=p, default_rules=RULES, return_price_field=RETURN_PRICE_FIELD, strategies=chosen,
                shared_base_samples=sum(item["base_samples"] for item in coverage),
                shape_history_rows={"红悬停龙": 4, "蓄力龙": 65, "补票龙": 60},
                validation_notice=VALIDATION_NOTICE,
                signal_source="ultra_research_*已有行情重算；非历史已存候选信号",
                short_pattern_price="adj_open/adj_high/adj_low/adj_close（后复权）",
                short_pattern_volume="volume（未复权查询）",
                short_window_days=90, universe_scope="原四策略沪深池及signal_eligibility过滤",
                main_metric='信号日收盘为基准；严格正收益计胜，持平计入分母；各期限独立有效样本',
                limitations=['日线触发近似，不能判断盘中先后或成交', '同股不同日和同日不同股可能相关，非独立交易',
                             '不含行业强弱与公告利空过滤', '缺失/停牌单列且不纳入有效胜率分母，需同时审查数量'])
            (destination / '运行参数.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
            progress.update(done=len(outputs)+1, detail='运行参数.json')
        visible = ['策略', '后续交易日', '触发样本', '有效样本', '胜率(%)', '平均收益(%)', '待观察', '停牌样本', '行情缺失']
        print(summary[visible].to_string(index=False, na_rep='--', float_format=lambda value: '{:.2f}'.format(value)))
        print('\n三日窗口统计：\n' + windows.to_string(index=False, na_rep='--', float_format=lambda value: '{:.2f}'.format(value)))
        print('\n结果目录：' + str(destination.resolve()))
        return dict(summary=summary, windows=windows, details=details, output_dir=destination)
    finally:
        if owned and engine is not None:
            engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strategies', nargs='+', default=['four'], help='中文策略名、four、three或all；可空格分隔多个，默认four兼容旧入口')
    parser.add_argument('--list-strategies', action='store_true', help='列出七个策略后退出，不连接数据库')
    parser.add_argument('--start', default=START_DATE, help='信号开始日期，默认2024-01-01')
    parser.add_argument('--end', default=END_DATE, help='信号截止日，默认数据库最新已收盘日')
    parser.add_argument('--as-of', default=AS_OF_DATE, help='观察截止日，可晚于信号截止日')
    parser.add_argument('--output', default=OUTPUT_DIR, help='本地结果目录')
    parser.add_argument('--config', help='私有 JSON 配置；相对路径以本脚本所在目录为基准')
    parser.add_argument('--no-progress', action='store_true', help='关闭动态进度显示')
    tls = parser.add_mutually_exclusive_group()
    tls.add_argument('--tls-verify', dest='tls_verify', action='store_true', help='验证服务器证书及身份')
    tls.add_argument('--no-tls-verify', dest='tls_verify', action='store_false',
                     help='临时跳过证书验证：无法确认服务器身份，仅用于可信目标排查')
    parser.set_defaults(tls_verify=None)
    args = parser.parse_args(argv)
    if args.list_strategies:
        print('\n'.join(ALL_STRATEGIES))
        return 0
    settings = None
    try:
        settings = load_local_config(args.config)
        verify = settings['DB_TLS_VERIFY'] if args.tls_verify is None else args.tls_verify
        run(args.start, args.end, args.as_of, args.output, tls_verify=verify,
            show_progress=PROGRESS_ENABLED and not args.no_progress,
            db_config=settings['DB_CONFIG'], tls_ca_pem=settings['DB_TLS_CA_PEM'], strategies=args.strategies)
        return 0
    except Exception as exc:
        message = '{}: {}'.format(type(exc).__name__, exc)
        password = str((settings['DB_CONFIG'] if settings else DB_CONFIG).get('dbpassword') or '')
        if password:
            for value in {password, quote(password, safe=''), quote_plus(password, safe='')}:
                message = message.replace(value, '***')
        if 'certificate verify failed' in message.lower() or 'CERTIFICATE_VERIFY_FAILED' in message:
            message += ('\n服务器证书验证未通过。请检查证书有效期、host与证书域名是否匹配，'
                        '私有CA证书可填入DB_TLS_CA_PEM。若仅作临时排查且信任目标，'
                        '可添加 --no-tls-verify；该选项不验证服务器身份。')
        print('\n统计失败：' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
