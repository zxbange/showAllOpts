# -*- coding: utf-8 -*-
"""聚宽研究环境：三年沪深A股行情、历史筛选快照，独立 MySQL 研究表。

数据库配置与采集逻辑全部包含在本文件，可直接粘贴到聚宽Notebook运行。
只建新表和幂等更新，不运行策略、不清理历史；直接执行本文件即开始采集。
"""
import builtins
import datetime as dt
import hashlib
import importlib
import math
import ssl
import sys
import time
from html import escape
from urllib.parse import quote, quote_plus

import jqdata as jq
import pandas as pd
from sqlalchemy import (MetaData, Table, Column, String, Date, Float, Integer,
                        DateTime, create_engine, select, and_, or_, func)
from sqlalchemy.engine.url import URL
from sqlalchemy.dialects.mysql import insert as mysql_insert

# 数据库配置仅有占位值，请在私有副本填写；无需读取其他文件。
DB_CONFIG = {
    'host': 'your_mysql_host',
    'port': 3306,
    'dbname': 'your_database',
    'dbuser': 'your_database_user',
    'dbpassword': 'your_password',
}

DB_OPTIONS = {'connect_timeout': 10, 'retry_count': 5,
              'retry_interval': 10, 'pool_recycle': 3600}

DB_DRIVER = 'pymysql'  # auto / mysqldb / pymysql；旧MySQLdb出现TLS错误时可显式改用pymysql

DB_TLS_VERIFY = True
DB_TLS_CA_PEM = ''

SCRIPT_VERSION = '2026-10-25.7'  # 精简公共进度与续采逻辑，兼容已有表和断点
START_DATE = None  # 默认最近已收盘交易日向前推三个自然年，含起始日
END_DATE = None    # 北京时间16点前不用当日日线
WARMUP_DAYS = 120
BATCH_SIZE = 50
PROGRESS_ENABLED = True
MIN_MARKET_CAP = 30.0  # 聚宽 valuation.market_cap 单位：亿元，等于30亿保留
RULE_VERSION = 'shsz_a_nonst_cap30_v2'  # 扩充板块后重新获取历史快照，避免复用仅主板断点
INDEX_CODES = ['000001.XSHG', '000300.XSHG', '000905.XSHG']
SCHEMA = MetaData()
CALENDAR = Table('ultra_research_calendar', SCHEMA,
                 Column('trade_date', Date, primary_key=True))
UNIVERSE = Table('ultra_research_universe', SCHEMA,
    Column('trade_date', Date, primary_key=True),
    Column('code', String(16), primary_key=True),
    Column('display_name', String(64)), Column('listed_date', Date),
    Column('is_st', Integer), Column('market_cap', Float),
    Column('eligible', Integer, nullable=False),
    Column('rule_version', String(64), nullable=False))
BARS = Table('ultra_research_bar', SCHEMA,
    Column('trade_date', Date, primary_key=True),
    Column('code', String(16), primary_key=True),
    Column('asset_type', String(8), nullable=False),
    *[Column(name, Float) for name in (
        'open', 'high', 'low', 'close', 'volume', 'money', 'factor',
        'high_limit', 'low_limit', 'adj_open', 'adj_high', 'adj_low', 'adj_close')],
    Column('paused', Integer), Column('collected_at', DateTime, nullable=False))
TASKS = Table('ultra_research_task', SCHEMA,
    Column('task_key', String(64), primary_key=True),
    Column('row_count', Integer, nullable=False),
    Column('completed_at', DateTime, nullable=False))
RAW_FIELDS = ['open', 'high', 'low', 'close', 'volume', 'money', 'factor',
              'high_limit', 'low_limit', 'paused']
OHLC = ['open', 'high', 'low', 'close']
PRICE_FIELDS = OHLC + ['adj_' + field for field in OHLC]


class CollectionProgress:
    """Notebook固定位置更新，终端用回车刷新同一行。"""
    def __init__(self, enabled=True, stream=None):
        self.enabled = enabled
        self.stream = stream if stream is not None else sys.stdout
        self.started = time.monotonic()
        self.last_render, self.width = -math.inf, 0
        self.display_handle, self.notebook = None, None
        self.state = ('准备', 0, 1, 0, 0)
        if enabled and stream is None:
            try:
                from IPython import get_ipython
                from IPython.display import HTML, display
                if getattr(get_ipython(), 'kernel', None) is not None:
                    self.notebook = (HTML, display)
            except ImportError:
                pass

    def __enter__(self):
        return self

    def update(self, label, done, total, written=0, skipped=0, force=False, status=''):
        self.state = (label, done, total, written, skipped)
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and now - self.last_render < 0.5:
            return
        self.last_render = now
        ratio = builtins.min(1.0, done / builtins.max(1, total))
        label = '{} | {}/{} {:.1f}% | 补采{}行 跳过{}行 | {:.0f}s {}'.format(
            label, done, total, ratio*100, written, skipped, now-self.started, status)
        if self.notebook is not None:
            HTML, display = self.notebook
            content = HTML('<div style="padding:8px 0"><progress value="{}" max="{}" '
                           'style="width:100%;height:18px"></progress><br>{}</div>'.format(
                               done, builtins.max(1, total), escape(label)))
            try:
                if self.display_handle is None:
                    self.display_handle = display(content, display_id=True)
                else:
                    self.display_handle.update(content)
                if self.display_handle is not None:
                    return
            except Exception:
                pass
            self.notebook = None
        filled = builtins.int(20 * ratio)
        line = '[' + '#' * filled + '-' * (20-filled) + '] ' + label
        self.stream.write('\r' + line + ' ' * builtins.max(0, self.width-len(line)))
        self.stream.flush()
        self.width = len(line)

    def __exit__(self, exc_type, exc_value, traceback):
        self.update(*self.state, force=True, status='完成' if exc_type is None else '已中断/失败，重跑可续采')
        if self.enabled and self.notebook is None:
            self.stream.write('\n')
            self.stream.flush()
        return False


def progress_update(progress, label, done, total, stats, force=False):
    if progress is not None:
        progress.update(label, done, total, stats['written'], stats['skipped'], force=force)


def api(name):
    value = globals().get(name) or getattr(jq, name, None) or getattr(builtins, name, None)
    if value is None:
        raise RuntimeError('缺少聚宽研究接口：' + name)
    return value


def day(value):
    return pd.Timestamp(value).date()


def security_code(value):
    if pd.isna(value):
        raise ValueError('行情证券代码为空')
    if isinstance(value, bytes):
        value = value.decode('ascii')
    code = str(value).strip().upper()
    if not code:
        raise ValueError('行情证券代码为空')
    return code


def price_index_key(date, code):
    """内部行情索引统一用ISO日期文本，避免旧pandas自动转换date引发哈希差异。"""
    if pd.isna(date):
        raise ValueError('行情日期为空')
    return day(date).isoformat(), security_code(code)


def price_keys(index):
    """与数据库预期主键比较时显式转换，绝不直接比较Timestamp与date的集合。"""
    return set((day(date), security_code(code)) for date, code in index)


def number(value):
    if pd.isna(value):
        return None
    result = builtins.float(value)
    if not math.isfinite(result):
        return None
    return result


def included_stock(code):
    """沪深主板、创业板、科创板；不含B股、北交所和存托凭证。"""
    return ((code.endswith('.XSHG') and code.startswith(('600', '601', '603', '605', '688')))
            or (code.endswith('.XSHE') and code.startswith(('000', '001', '002', '003', '300', '301'))))


def chunks(values, size):
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def retry(function, **kwargs):
    for attempt in range(3):
        try:
            return function(**kwargs)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def upsert(conn, table, rows):
    if not rows:
        return
    for batch in chunks(rows, 1000):
        statement = mysql_insert(table).values(batch)
        updates = {c.name: statement.inserted[c.name] for c in table.c if not c.primary_key}
        if not updates:
            updates = {c.name: statement.inserted[c.name] for c in table.primary_key}
        conn.execute(statement.on_duplicate_key_update(**updates))


def task_key(*parts):
    return hashlib.sha256('|'.join(str(x) for x in parts).encode('utf-8')).hexdigest()


def select_compat(*columns):
    try:
        return select(*columns)
    except Exception:
        return select(list(columns))


def read_records(engine, table, condition, columns=None):
    fields = list(table.c) if columns is None else list(columns)
    with engine.connect() as conn:
        records = conn.execute(select_compat(*fields).where(condition)).fetchall()
    return [dict(zip([c.name for c in fields], row)) for row in records]


def task_row_count(engine, key):
    rows = read_records(engine, TASKS, TASKS.c.task_key == key, [TASKS.c.row_count])
    return rows[0]['row_count'] if rows else None


def commit_task(engine, key, table, rows, expected_count=None):
    with engine.begin() as conn:
        upsert(conn, table, rows)
        upsert(conn, TASKS, [dict(task_key=key, row_count=(len(rows) if expected_count is None else expected_count),
                               completed_at=dt.datetime.utcnow())])


def stage_prefix(stage):
    """沿用旧版任务键，使已有数据库无须重建进度。"""
    if stage == 'snapshot':
        return 'snapshot-day-v1:' + task_key('snapshot_scope_v1', RULE_VERSION, MIN_MARKET_CAP)[:16] + ':'
    scope = task_key('complete_scope_v1', RULE_VERSION, MIN_MARKET_CAP, ','.join(sorted(INDEX_CODES)))[:16]
    return 'complete-day-v1:' + scope + ':'


def last_stage_day(engine, stage):
    prefix = stage_prefix(stage)
    with engine.connect() as conn:
        row = conn.execute(select_compat(TASKS.c.task_key).where(
            TASKS.c.task_key.like(prefix + '%')).order_by(TASKS.c.task_key.desc()).limit(1)).first()
    return day(row[0][len(prefix):]) if row else None


def mark_stage_day(engine, stage, date, row_count):
    with engine.begin() as conn:
        upsert(conn, TASKS, [dict(task_key=stage_prefix(stage) + date.isoformat(),
            row_count=row_count, completed_at=dt.datetime.utcnow())])


def resume_point(engine, stage='bars'):
    """两个阶段各自接续；无专用断点的旧库仅核对最新日。"""
    last = last_stage_day(engine, stage)
    if last is not None:
        return last, None
    table = UNIVERSE if stage == 'snapshot' else BARS
    statement = select_compat(func.max(table.c.trade_date))
    if stage == 'snapshot':
        statement = statement.where(UNIVERSE.c.rule_version == RULE_VERSION)
    with engine.connect() as conn:
        latest = conn.execute(statement).scalar()
    if latest is None:
        return None, None
    latest = day(latest)
    records = read_records(engine, UNIVERSE, UNIVERSE.c.trade_date == latest)
    universe_key = task_key('universe', RULE_VERSION, MIN_MARKET_CAP, latest)
    expected_count = task_row_count(engine, universe_key)
    valid = (records and expected_count == len(records)
             and builtins.all(row['rule_version'] == RULE_VERSION for row in records))
    count = len(records)
    if valid and stage == 'bars':
        expected = set((latest, row['code'], 'stock') for row in records)
        expected.update((latest, code, 'index') for code in INDEX_CODES)
        valid = expected.issubset(complete_bar_keys(engine, latest, latest))
        count = len(expected)
    if valid:
        mark_stage_day(engine, stage, latest, count)
        return latest, None
    return None, latest if stage == 'snapshot' else latest.replace(day=1)


def complete_universe_row(row):
    cap = number(row.get('market_cap'))
    status = row.get('is_st')
    return (row.get('rule_version') == RULE_VERSION and status in (0, 1)
            and cap is not None and cap >= 0 and row.get('listed_date') is not None
            and builtins.bool(row.get('display_name'))
            and row.get('eligible') == builtins.int(status == 0 and cap >= MIN_MARKET_CAP))


def collect_universe(engine, dates, force=False, progress=None):
    """核对实际记录；完整日跳过，仅对缺失/不完整股票查询历史ST和市值。"""
    stats = dict(written=0, skipped=0, incomplete=0)
    for pos, date in enumerate(dates, 1):
        key = task_key('universe', RULE_VERSION, MIN_MARKET_CAP, date)
        progress_update(progress, '筛选快照 {} 检查已有数据'.format(date), pos-1, len(dates), stats)
        existing = {row['code']: row for row in read_records(
            engine, UNIVERSE, UNIVERSE.c.trade_date == date)}
        complete = set(code for code, row in existing.items() if complete_universe_row(row))
        expected_count = task_row_count(engine, key)
        if not force and expected_count and len(existing) == expected_count and len(complete) == expected_count:
            stats['skipped'] += len(complete)
            mark_stage_day(engine, 'snapshot', date, len(existing))
            progress_update(progress, '筛选快照 {} 已完整'.format(date), pos, len(dates), stats)
            continue
        securities = retry(api('get_all_securities'), types=['stock'], date=date)
        codes = sorted(code for code in securities.index if included_stock(code))
        if not codes:
            raise ValueError('{} 沪深A股股票池为空'.format(date))
        pending = codes if force else [code for code in codes if code not in complete]
        stats['skipped'] += len(codes) - len(pending)
        if not pending:
            commit_task(engine, key, UNIVERSE, [], expected_count=len(codes))
            mark_stage_day(engine, 'snapshot', date, len(codes))
            progress_update(progress, '筛选快照 {} 已完整'.format(date), pos, len(dates), stats)
            continue
        progress_update(progress, '筛选快照 {} 补采{}只'.format(date, len(pending)), pos-1, len(dates), stats)
        st = retry(api('get_extras'), info='is_st', security_list=pending,
                   start_date=date, end_date=date, df=True)
        if len(st) != 1 or day(st.index[0]) != date:
            raise ValueError('{} ST快照缺失'.format(date))
        caps = {}
        valuation = api('valuation')
        query = api('query')
        for batch in chunks(pending, 1000):
            data = retry(api('get_fundamentals'), query_object=query(
                valuation.code, valuation.market_cap).filter(valuation.code.in_(batch)), date=date)
            caps.update(dict(zip(data['code'], data['market_cap'])))
        rows = []
        for code in pending:
            old = existing.get(code, {})
            old = old if old.get('rule_version') == RULE_VERSION else {}
            status = st[code].iloc[0] if code in st else None
            status = old.get('is_st') if pd.isna(status) else builtins.int(builtins.bool(status))
            cap = number(caps.get(code))
            cap = old.get('market_cap') if cap is None else cap
            rows.append(dict(trade_date=date, code=code,
                display_name=str(securities.loc[code, 'display_name']),
                listed_date=day(securities.loc[code, 'start_date']),
                is_st=status, market_cap=cap,
                eligible=builtins.int(status == 0 and cap is not None and cap >= MIN_MARKET_CAP),
                rule_version=RULE_VERSION))
        commit_task(engine, key, UNIVERSE, rows, expected_count=len(codes))
        mark_stage_day(engine, 'snapshot', date, len(codes))
        stats['written'] += len(rows)
        stats['incomplete'] += builtins.sum(not complete_universe_row(r) for r in rows)
        progress_update(progress, '筛选快照 {} 已处理'.format(date), pos, len(dates), stats)
    progress_update(progress, '筛选快照检查完成', len(dates), len(dates), stats, force=True)
    return stats


def normalize_prices(frame, expected, fields, allow_missing=False):
    if frame is not None and frame.empty and allow_missing:
        index = pd.MultiIndex.from_tuples([], names=['trade_date', 'code'])
        return pd.DataFrame(columns=fields, index=index)
    if frame is None or frame.empty:
        raise ValueError('行情为空；不提交断点')
    required = ['time', 'code'] + fields
    if not set(required).issubset(frame.columns):
        raise ValueError('行情字段缺失：' + str(sorted(set(required) - set(frame.columns))))
    result = frame.copy()
    keys = [price_index_key(date, code) for date, code in zip(result['time'], result['code'])]
    if len(keys) != len(set(keys)):
        raise ValueError('行情含重复日期/股票')
    # 不能只map(day)后set_index：聚宽旧版pandas会再次把date提升成Timestamp。
    result.index = pd.MultiIndex.from_tuples(keys, names=['trade_date', 'code'])
    expected_keys = set(price_index_key(date, code) for date, code in expected)
    missing = expected_keys - set(keys)
    if missing and not allow_missing:
        raise ValueError('行情缺少{}行，例如{}'.format(len(missing), sorted(missing)[:3]))
    return result.loc[sorted(expected_keys & set(keys))]


def complete_bar_keys(engine, first, last):
    """只读取完整行主键。停牌日允许价格为空。"""
    active_valid = and_(BARS.c.paused == 0,
        *[BARS.c[name] > 0 for name in PRICE_FIELDS], BARS.c.volume >= 0, BARS.c.money >= 0)
    rows = read_records(engine, BARS,
        and_(BARS.c.trade_date >= first, BARS.c.trade_date <= last,
             or_(BARS.c.paused == 1, active_valid)),
        [BARS.c.trade_date, BARS.c.code, BARS.c.asset_type])
    return set((day(row['trade_date']), row['code'], row['asset_type']) for row in rows)


def missing_price_requests(expected, complete, days):
    """相同缺失日期的股票合批，不跨过已有交易日下载。"""
    missing_by_code = {}
    for date, code in sorted(expected - complete):
        missing_by_code.setdefault(code, []).append(date)
    groups = {}
    for code, missing_dates in missing_by_code.items():
        groups.setdefault(tuple(missing_dates), []).append(code)
    positions = {date: pos for pos, date in enumerate(days)}
    requests = []
    for missing_dates, codes in sorted(groups.items()):
        runs = []
        for date in missing_dates:
            if not runs or positions[date] != positions[runs[-1][-1]] + 1:
                runs.append([])
            runs[-1].append(date)
        for batch in chunks(sorted(codes), BATCH_SIZE):
            for run_days in runs:
                requests.append((batch, run_days[0], run_days[-1],
                                 set((date, code) for date in run_days for code in batch)))
    return requests


def returned_price_summary(frame):
    if frame is None or frame.empty:
        return 'None' if frame is None else '0行'
    dates = sorted(set(day(value) for value in frame['time']))
    return '{}行，实际日期{}~{}，代码样例{}'.format(
        len(frame), dates[0], dates[-1], [repr(value) for value in frame['code'].iloc[:3]])


def fetch_prices(expected, fields, fq, days, on_missing=None):
    """批量返回不全时只补缺口，逐步拆小到单股单日，不虚构行情。"""
    expected = price_keys(expected)
    days = [day(date) for date in days]
    def fetch(target):
        codes = sorted(set(code for _, code in target))
        target_days = sorted(set(date for date, _ in target))
        kwargs = dict(security=codes, start_date=target_days[0], end_date=target_days[-1],
                      frequency='daily', skip_paused=False, panel=False, fields=fields, fq=fq)
        source = retry(api('get_price'), **kwargs)
        data = normalize_prices(source, target, fields, allow_missing=True)
        missing = target - price_keys(data.index)
        if not missing:
            return data
        if on_missing is not None:
            missing_days = sorted(set(date for date, _ in missing))
            on_missing(len(missing), len(target), missing_days[0], missing_days[-1])
        if len(target) == 1:
            kwargs.pop('start_date')
            kwargs['count'] = 1
            fallback = retry(api('get_price'), **kwargs)
            checked = normalize_prices(fallback, target, fields, allow_missing=True)
            if price_keys(checked.index) == target:
                return checked
            raise ValueError('行情单股单日补采仍缺少{}行，例如{}；fq={}，字段={}；'
                             '区间查询返回{}；count=1返回{}。'
                             '请检查历史数据权限、配额及源接口，本批数据和断点未提交。'.format(
                                 len(missing), sorted(missing)[:3], fq, ','.join(fields),
                                 returned_price_summary(source), returned_price_summary(fallback)))
        if missing == target:
            axis = 1 if len(codes) > 1 else 0
            values = codes if axis else target_days
            half = set(values[:len(values)//2])
            left = set(item for item in target if item[axis] in half)
            subsets = [left, target - left]
        else:
            subsets = [request[3] for request in missing_price_requests(missing, set(), days)]
        parts = [data] if not data.empty else []
        parts.extend(fetch(subset) for subset in subsets)
        result = pd.concat(parts).sort_index()
        if result.index.duplicated().any() or price_keys(result.index) != target:
            raise ValueError('补采合并后的行情主键不完整或重复；不提交断点')
        return result.loc[sorted(price_index_key(date, code) for date, code in target)]
    return fetch(expected)


def collect_bars(engine, dates, force=False, progress=None):
    """按月检查实际行情，仅下载缺失日期/股票，保留不合格日上下文。"""
    stats = dict(written=0, skipped=0)
    months = sorted(set((d.year, d.month) for d in dates))
    for month_pos, (year, month) in enumerate(months, 1):
        days = [d for d in dates if (d.year, d.month) == (year, month)]
        day_set = set(days)
        label = '行情 {}/{} {}-{:02d}'.format(month_pos, len(months), year, month)
        progress_update(progress, label + ' 检查已有数据', month_pos-1, len(months), stats, force=True)
        records = read_records(engine, UNIVERSE,
            and_(UNIVERSE.c.trade_date >= days[0], UNIVERSE.c.trade_date <= days[-1]),
            [UNIVERSE.c.trade_date, UNIVERSE.c.code])
        expected_stock = set((day(row['trade_date']), row['code']) for row in records
                             if day(row['trade_date']) in day_set)
        if set(date for date, _ in expected_stock) != day_set:
            raise ValueError('当月部分交易日缺少股票池快照，不能登记行情采集完成')
        complete = set() if force else complete_bar_keys(engine, days[0], days[-1])
        for kind, expected_all in [('stock', expected_stock),
                                  ('index', set((d, c) for d in days for c in INDEX_CODES))]:
            existing = set((d, c) for d, c, asset_type in complete if asset_type == kind)
            stats['skipped'] += len(expected_all & existing)
            requests = missing_price_requests(expected_all, existing, days)
            for request_pos, (batch, first, last, expected) in enumerate(requests, 1):
                progress_update(progress, '{} {} 补采批次{}/{} {}~{}'.format(
                    label, kind, request_pos, len(requests), first, last), month_pos-1, len(months), stats)
                key = task_key('bars_fill_v2', kind,
                               hashlib.sha256(str(sorted(expected)).encode('utf-8')).hexdigest())
                fields = RAW_FIELDS if kind == 'stock' else OHLC + ['volume', 'money']
                def report_missing(missing, requested, first_missing, last_missing):
                    progress_update(progress, '{} {} 接口少{}/{}行，缩小补采 {}~{}'.format(
                        label, kind, missing, requested, first_missing, last_missing),
                        month_pos-1, len(months), stats, force=True)
                raw = fetch_prices(expected, fields, None, days, on_missing=report_missing)
                # 直接保存后复权OHLC，避免假设factor的方向/基准，也不混用不同批次前复权价格。
                adjusted = (fetch_prices(expected, OHLC, 'post', days,
                                         on_missing=report_missing) if kind == 'stock' else raw)
                rows = []
                for (price_date, code), record in raw.iterrows():
                    date = day(price_date)
                    values = {name: number(record.get(name)) for name in RAW_FIELDS if name != 'paused'}
                    values.update({'adj_' + name: number(adjusted.loc[(price_date, code), name]) for name in OHLC})
                    paused = number(record.get('paused')) if kind == 'stock' else 0
                    if paused not in (0, 1):
                        raise ValueError('{} {} 停牌状态缺失'.format(date, code))
                    invalid = [name for name in PRICE_FIELDS if values[name] is None or values[name] <= 0]
                    invalid += [name for name in ['volume', 'money'] if values[name] is None or values[name] < 0]
                    if not paused and invalid:
                        raise ValueError('{} {} 行情字段缺失/无效：{}'.format(date, code, ','.join(invalid)))
                    rows.append(dict(values, trade_date=date, code=code, asset_type=kind,
                                     paused=builtins.int(paused), collected_at=dt.datetime.utcnow()))
                commit_task(engine, key, BARS, rows)
                stats['written'] += len(rows)
        mark_stage_day(engine, 'bars', days[-1], len(expected_stock) + len(days)*len(INDEX_CODES))
        progress_update(progress, label + ' 已处理', month_pos, len(months), stats, force=True)
    return stats


def make_engine(config):
    # 项目现有聚宽脚本使用MySQLdb，优先复用环境已有驱动。
    driver = None
    candidates = [('MySQLdb', 'mysql+mysqldb'), ('pymysql', 'mysql+pymysql')]
    if DB_DRIVER not in ('auto', 'mysqldb', 'pymysql'):
        raise ValueError('DB_DRIVER必须是auto、mysqldb或pymysql')
    if DB_DRIVER != 'auto':
        candidates = [item for item in candidates if item[1] == 'mysql+' + DB_DRIVER]
    for module_name, dialect in candidates:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            print('驱动不可用 {}：{}'.format(module_name, connection_error(exc, config)), flush=True)
            continue
        driver = dialect
        print('使用数据库驱动：{}，版本：{}'.format(
            dialect, getattr(module, '__version__', '未知')), flush=True)
        break
    if driver is None:
        raise RuntimeError('未找到所需MySQL驱动（{}）：需要环境提供MySQLdb或PyMySQL。'.format(DB_DRIVER))
    values = dict(drivername=driver, username=config['dbuser'],
                  password=config['dbpassword'], host=config['host'],
                  port=builtins.int(config.get('port', 3306)), database=config['dbname'],
                  query={'charset': 'utf8mb4'})
    url = URL.create(**values) if hasattr(URL, 'create') else URL(**values)
    connect_args = {'connect_timeout': DB_OPTIONS['connect_timeout']}
    if driver == 'mysql+pymysql':
        context = ssl.create_default_context(cadata=DB_TLS_CA_PEM or None)
        if DB_TLS_VERIFY:
            print('PyMySQL TLS：启用{}CA及服务器身份验证'.format(
                '内置' if DB_TLS_CA_PEM else '系统'), flush=True)
        else:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            print('PyMySQL TLS：临时跳过证书验证，无法确认服务器身份；请勿作为长期配置', flush=True)
        connect_args['ssl'] = context
    return create_engine(url, echo=False, pool_size=1, max_overflow=0,
                         pool_recycle=DB_OPTIONS['pool_recycle'], pool_pre_ping=True,
                         connect_args=connect_args)


def connection_error(exc, config):
    """只输出异常和底层DBAPI错误，不包含SQL参数；隐藏可能出现在错误中的密码。"""
    original = getattr(exc, 'orig', None)
    parts = ['{}: {}'.format(type(exc).__name__, str(exc))]
    if original is not None:
        parts.append('底层错误 {}，参数 {}'.format(type(original).__name__, repr(original.args)))
    result = '；'.join(parts)
    password = str(config.get('dbpassword') or '')
    if password:
        variants = {password, quote(password, safe=''), quote_plus(password, safe=''),
                    repr(password)[1:-1]}
        for value in sorted(variants, key=len, reverse=True):
            result = result.replace(value, '***')
    return result


def connect_database(config):
    """沿用批量采集脚本的连接检测、有限重试和异常释放方式。"""
    opts = DB_OPTIONS
    if (not isinstance(opts['retry_count'], builtins.int) or opts['retry_count'] < 1
            or opts['retry_interval'] < 0 or opts['connect_timeout'] <= 0
            or opts['pool_recycle'] <= 0):
        raise ValueError('DB_OPTIONS 的次数、超时须为正，重试间隔不可为负。')
    print('数据库目标：host={}，port={}，database={}，user={}，password=***；'
          'charset=utf8mb4，connect_timeout={}秒'.format(
              config.get('host'), config.get('port', 3306), config.get('dbname'),
              config.get('dbuser'), opts['connect_timeout']), flush=True)
    try:
        engine = make_engine(config)
    except Exception as exc:
        detail = connection_error(exc, config)
        print('创建数据库引擎失败：' + detail, flush=True)
        raise RuntimeError('创建数据库引擎失败：' + detail) from None
    try:
        for attempt in range(opts['retry_count']):
            print('连接数据库（第 {} / {} 次）'.format(attempt + 1, opts['retry_count']), flush=True)
            started = time.monotonic()
            try:
                with engine.connect():
                    pass
                print('数据库连接成功，耗时 {:.2f} 秒'.format(time.monotonic() - started), flush=True)
                return engine
            except Exception as exc:
                detail = connection_error(exc, config)
                print('连接失败，耗时 {:.2f} 秒：{}'.format(time.monotonic() - started, detail), flush=True)
                if 'CERTIFICATE_VERIFY_FAILED' in detail:
                    print('证书验证失败：请检查CA信任链、有效期以及host是否匹配证书；'
                          '可信CA可填入DB_TLS_CA_PEM。本类错误停止自动重试。', flush=True)
                    raise RuntimeError('MySQL TLS证书验证失败：' + detail) from None
                if attempt + 1 == opts['retry_count']:
                    raise RuntimeError('MySQL 连接失败：' + detail) from None
                print('连接暂未成功，等待重试', flush=True)
                time.sleep(opts['retry_interval'])
    except BaseException:
        engine.dispose()
        raise


def run(db_config=None, start_date=START_DATE, end_date=END_DATE, force=False):
    print('超短线历史行情采集 v{} | 快照/行情分别续采 + 进度条 + 缺行拆分补采'.format(SCRIPT_VERSION), flush=True)
    print('运行环境：Python {}，pandas {}'.format(sys.version.split()[0], pd.__version__), flush=True)
    config = db_config if db_config is not None else DB_CONFIG
    if not config:
        raise ValueError('请填写本脚本顶部的DB_CONFIG')
    now = dt.datetime.utcnow() + dt.timedelta(hours=8)
    cutoff = now.date() if now.hour >= 16 else now.date() - dt.timedelta(days=1)
    requested_end = day(end_date) if end_date else cutoff
    if requested_end > cutoff:
        raise ValueError('结束日期尚未收盘，禁止保存未完成日线')
    trading = api('get_trade_days')
    end = day(trading(end_date=requested_end, count=1)[0])
    start = day(start_date) if start_date else (pd.Timestamp(end) - pd.DateOffset(years=3)).date()
    if start > end:
        raise ValueError('开始日期晚于结束日期')
    engine = connect_database(config)
    try:
        for table in SCHEMA.tables.values():
            table.dialect_options['mysql']['engine'] = 'InnoDB'
            table.dialect_options['mysql']['charset'] = 'utf8mb4'
        SCHEMA.create_all(engine)
        last, unfinished_start = (None, None) if force else resume_point(engine)
        warmup_start = None
        if last is not None:
            collect_from = last + dt.timedelta(days=1)
            print('上次完整采集至 {}，本次仅采集其后的交易日。'.format(last), flush=True)
        elif unfinished_start is not None:
            collect_from = unfinished_start
        else:
            before = [day(d) for d in trading(end_date=start - dt.timedelta(days=1), count=WARMUP_DAYS)]
            if len(before) != WARMUP_DAYS:
                raise ValueError('预热交易日不足')
            warmup_start = collect_from = before[0]
        dates = ([day(d) for d in trading(start_date=collect_from, end_date=end)]
                 if collect_from <= end else [])
        if len(set(dates)) != len(dates) or dates != sorted(dates):
            raise ValueError('交易日历重复或乱序')
        if not dates:
            print('截至 {} 没有新增交易日，无需采集。'.format(end), flush=True)
            return dict(start_date=start, end_date=end, warmup_start=warmup_start,
                collected_from=None, last_complete_date=last, calendar_added=0,
                universe=dict(written=0, skipped=0, incomplete=0), bars=dict(written=0, skipped=0))
        print('本次采集区间 {} ~ {}{}。'.format(dates[0], dates[-1],
            '，包含首次预热' if warmup_start is not None else ''), flush=True)
        existing_days = set(day(row['trade_date']) for row in read_records(engine, CALENDAR,
            and_(CALENDAR.c.trade_date >= dates[0], CALENDAR.c.trade_date <= dates[-1])))
        missing_calendar = [dict(trade_date=d) for d in dates if d not in existing_days]
        if missing_calendar:
            with engine.begin() as conn:
                upsert(conn, CALENDAR, missing_calendar)
        snapshot_last, snapshot_partial = (None, None) if force else resume_point(engine, 'snapshot')
        snapshot_dates = dates
        if snapshot_last is not None:
            snapshot_dates = [date for date in dates if date > snapshot_last]
        elif snapshot_partial is not None:
            snapshot_dates = [date for date in dates if date >= snapshot_partial]
        if snapshot_dates:
            print('筛选快照仅需采集 {} ~ {}，共{}个交易日。'.format(
                snapshot_dates[0], snapshot_dates[-1], len(snapshot_dates)), flush=True)
        else:
            print('筛选快照已采集至 {}，本次整段跳过，不逐日检查。'.format(snapshot_last), flush=True)
        with CollectionProgress(enabled=PROGRESS_ENABLED) as progress:
            universe_stats = (collect_universe(engine, snapshot_dates, force=force, progress=progress)
                              if snapshot_dates else dict(written=0, skipped=0, incomplete=0))
            bar_stats = collect_bars(engine, dates, force=force, progress=progress)
        print('采集汇总：日历新增{}行；筛选快照补采{}行、跳过{}行；行情补采{}行、跳过{}行。'.format(
            len(missing_calendar), universe_stats['written'], universe_stats['skipped'],
            bar_stats['written'], bar_stats['skipped']), flush=True)
        if universe_stats['incomplete']:
            print('有{}行快照的源字段缺失，按不合格保留；默认追加模式不回扫历史，'
                  '需复核时请指定历史区间并设置force=True。'.format(universe_stats['incomplete']), flush=True)
        print('完成。仅 eligible=1 且日期在研究区间内的记录可作为入选样本；末端不足3日需待观察。')
        return dict(start_date=start, end_date=end, warmup_start=warmup_start,
                    collected_from=dates[0], last_complete_date=last_stage_day(engine, 'bars'),
                    calendar_added=len(missing_calendar), universe=universe_stats, bars=bar_stats)
    finally:
        engine.dispose()


if __name__ == '__main__':
    collection_result = run()
