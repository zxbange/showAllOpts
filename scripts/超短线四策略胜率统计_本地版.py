# -*- coding: utf-8 -*-
"""本地只读MySQL：四种日线策略，自2026-07-01起统计后续1/2/3交易日表现。

单文件运行，不导入聚宽或项目文件；结果写入本地CSV，数据库只执行SELECT。
依赖：pandas、numpy、SQLAlchemy、PyMySQL。参数是研究假设，未经收益优化。
"""
import argparse
import datetime as dt
import json
import ssl
import sys
import time
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
SCRIPT_VERSION = '2026-10-26.1'
RETURN_PRICE_FIELD = 'adj_close'  # 同口径后复权收盘价；改为close可统计未复权价格
START_DATE = '2026-07-01'
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


def read_sql(engine, sql, params=None, codes=None):
    statement = text(sql)
    values = dict(params or {})
    if codes is not None:
        statement = statement.bindparams(bindparam('codes', expanding=True))
        values['codes'] = list(codes)
    with engine.connect() as connection:
        return pd.read_sql_query(statement, connection, params=values)


def make_engine(config):
    kwargs = dict(drivername='mysql+pymysql', username=config['dbuser'],
                  password=config['dbpassword'], host=config['host'],
                  port=int(config.get('port', 3306)), database=config['dbname'], query={'charset': 'utf8mb4'})
    url = URL.create(**kwargs) if hasattr(URL, 'create') else URL(**kwargs)
    context = ssl.create_default_context(cadata=DB_TLS_CA_PEM or None)
    if not DB_TLS_VERIFY:
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
    prior_return = c.shift(1) / c.shift(4) - 1
    result[STRATEGIES[1]] = ((prior_return <= p['oversold_3'])
        & (c > h.shift(1)) & (c > o)
        & (volume >= volume.shift(1).rolling(5).mean()*p['rebound_volume']))
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


def stock_samples(code, bars, universe, calendar, start, end, params):
    bars = dated_frame(bars, '股票行情').set_index('trade_date').reindex(calendar)
    universe = dated_frame(universe, '股票快照').set_index('trade_date').reindex(calendar)
    numeric = ['open', 'high', 'low', 'close', 'adj_open', 'adj_high', 'adj_low', 'adj_close',
               'volume', 'money', 'paused', 'high_limit', 'low_limit']
    for column in numeric:
        bars[column] = pd.to_numeric(bars[column], errors='coerce')
    c = bars['adj_close']
    listed = pd.to_datetime(universe['listed_date'], errors='coerce')
    listing_age = (pd.Series(calendar, index=calendar)-listed).dt.days
    ready = c.where(c > 0).rolling(60, min_periods=60).count() == 60
    base = ((universe['eligible'] == 1) & (universe['is_st'] == 0)
        & (universe['market_cap'] >= params['min_market_cap'])
        & (listing_age >= params['min_listing_days']) & ready
        & (bars['paused'] == 0) & (bars['money'] >= params['min_money'])
        & (bars['volume'] > 0) & (c > 0) & (bars[RETURN_PRICE_FIELD] > 0))
    candidates = pattern_candidates(bars, params)
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


def select_signals(samples, params):
    samples = samples.copy()
    samples['strength_rank'] = samples.groupby('signal_date')['ret10'].rank(pct=True, method='average')
    samples[STRATEGIES[0]] &= samples['strength_rank'] > params['strength_quantile']
    pieces = []
    for method in STRATEGIES:
        selected = samples.loc[samples[method]].copy()
        selected['strategy'] = method
        selected['anchor_date'] = selected[method + '_anchor']
        pieces.append(selected)
    result = pd.concat(pieces, ignore_index=True)
    return result.sort_values(['signal_date', 'strategy', 'code']).reset_index(drop=True)


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


def load_plan(engine, start, end=None, as_of=None):
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
    raw = read_sql(engine, 'SELECT trade_date FROM ultra_research_calendar WHERE trade_date <= :end ORDER BY trade_date', {'end': observed.date()})
    calendar = pd.DatetimeIndex(pd.to_datetime(raw['trade_date']).dt.normalize())
    if calendar.has_duplicates or not calendar.is_monotonic_increasing:
        raise ValueError('交易日历重复或乱序')
    first = calendar.searchsorted(start)
    if first < WARMUP_DAYS:
        raise ValueError('开始日期之前不足{}个预热交易日'.format(WARMUP_DAYS))
    calendar = calendar[first-WARMUP_DAYS:]
    if calendar.empty or calendar[-1] != observed:
        raise ValueError('行情最大日期不在已采集的交易日历中')
    return start, end, observed, calendar


def run(start_date=START_DATE, end_date=END_DATE, as_of=AS_OF_DATE, output_dir=OUTPUT_DIR, engine=None, params=None):
    p = dict(PARAMS, **(params or {}))
    owned = engine is None
    engine = make_engine(DB_CONFIG) if owned else engine
    started = time.monotonic()
    try:
        start, end, observed, calendar = load_plan(engine, start_date, end_date, as_of)
        bounds = dict(first=calendar[0].date(), last=observed.date())
        codes = read_sql(engine, '''SELECT DISTINCT code FROM ultra_research_universe
            WHERE trade_date BETWEEN :first AND :last AND eligible=1 AND is_st=0
            AND market_cap>=:cap ORDER BY code''',
            dict(first=start.date(), last=end.date(), cap=p['min_market_cap']))['code'].tolist()
        if not codes:
            raise ValueError('研究区间没有合格股票快照，请检查采集进度和日期')
        frames, coverage = [], []
        print('信号区间 {} ~ {}；观察截至 {}；股票{}只。'.format(start.date(), end.date(), observed.date(), len(codes)))
        for offset in range(0, len(codes), READ_BATCH_SIZE):
            batch = codes[offset:offset+READ_BATCH_SIZE]
            bars = dated_frame(read_sql(engine, '''SELECT * FROM ultra_research_bar
                WHERE asset_type='stock' AND code IN :codes AND trade_date BETWEEN :first AND :last
                ORDER BY code, trade_date''', bounds, codes=batch), '股票行情')
            universe = dated_frame(read_sql(engine, '''SELECT * FROM ultra_research_universe
                WHERE code IN :codes AND trade_date BETWEEN :first AND :last
                ORDER BY code, trade_date''', bounds, codes=batch), '股票快照')
            bar_groups = dict(tuple(bars.groupby('code')))
            universe_groups = dict(tuple(universe.groupby('code')))
            for code in batch:
                frame, quality = stock_samples(code, bar_groups.get(code, bars.iloc[:0]),
                    universe_groups.get(code, universe.iloc[:0]), calendar, start, end, p)
                frames.append(frame)
                coverage.append(quality)
            done = min(offset+READ_BATCH_SIZE, len(codes))
            filled = int(20*done/len(codes))
            print('\r[{}{}] {}/{} {:.1f}% | {:.0f}s'.format('#'*filled, '-'*(20-filled), done,
                len(codes), 100*done/len(codes), time.monotonic()-started), end='', flush=True)
        print()
        if not sum(item['base_samples'] for item in coverage):
            raise ValueError('没有可用基础样本，请检查历史快照、行情完整性、预热长度及筛选参数')
        samples = pd.concat(frames, ignore_index=True)
        details = select_signals(samples, p)
        summary = summary_table(details)
        windows = window_summary(details)
        monthly = []
        for month in pd.period_range(start, end, freq='M'):
            group = details[details['signal_date'].dt.to_period('M') == month]
            item = summary_table(group)
            item.insert(0, '信号月份', str(month))
            monthly.append(item)
        destination = Path(output_dir) / (dt.datetime.utcnow()+dt.timedelta(hours=8)).strftime('%Y%m%d_%H%M%S_%f')
        destination.mkdir(parents=True, exist_ok=False)
        outputs = {'收盘胜率汇总.csv': summary, '三日窗口汇总.csv': windows,
            '策略信号明细.csv': details, '按月收盘胜率.csv': pd.concat(monthly, ignore_index=True),
            '数据覆盖.csv': pd.DataFrame(coverage)}
        for filename, frame in outputs.items():
            frame.to_csv(destination / filename, index=False, encoding='utf-8-sig')
        metadata = dict(version=SCRIPT_VERSION, signal_start=str(start.date()), signal_end=str(end.date()),
            observed_through=str(observed.date()), warmup_start=str(calendar[0].date()),
            params=p, default_rules=RULES, return_price_field=RETURN_PRICE_FIELD,
            main_metric='信号日收盘为基准；严格正收益计胜，持平计入分母；各期限独立有效样本',
            limitations=['日线触发近似，不能判断盘中先后或成交', '同股不同日和同日不同股可能相关，非独立交易',
                         '不含行业强弱与公告利空过滤', '缺失/停牌单列且不纳入有效胜率分母，需同时审查数量'])
        (destination / '运行参数.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        visible = ['策略', '后续交易日', '触发样本', '有效样本', '胜率(%)', '平均收益(%)', '待观察', '停牌样本', '行情缺失']
        print(summary[visible].to_string(index=False, na_rep='--', float_format=lambda value: '{:.2f}'.format(value)))
        print('\n三日窗口统计：\n' + windows.to_string(index=False, na_rep='--', float_format=lambda value: '{:.2f}'.format(value)))
        print('\n结果目录：' + str(destination.resolve()))
        return dict(summary=summary, windows=windows, details=details, output_dir=destination)
    finally:
        if owned:
            engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', default=START_DATE, help='信号开始日期，默认2026-07-01')
    parser.add_argument('--end', default=END_DATE, help='信号截止日，默认数据库最新已收盘日')
    parser.add_argument('--as-of', default=AS_OF_DATE, help='观察截止日，可晚于信号截止日')
    parser.add_argument('--output', default=OUTPUT_DIR, help='本地结果目录')
    args = parser.parse_args()
    try:
        run(args.start, args.end, args.as_of, args.output)
    except Exception as exc:
        message = '{}: {}'.format(type(exc).__name__, exc)
        password = str(DB_CONFIG.get('dbpassword') or '')
        if password:
            for value in {password, quote(password, safe=''), quote_plus(password, safe='')}:
                message = message.replace(value, '***')
        print('\n统计失败：' + message, file=sys.stderr)
        sys.exit(1)
