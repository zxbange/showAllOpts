# -*- coding: utf-8 -*-
"""聚宽研究独立入口，Python 3.6+。默认2018年起；不按证券上市日裁剪回溯行情。"""
from datetime import date, datetime, timedelta
import builtins
from builtins import any, type, int, bool, isinstance, float, str, callable, getattr, sorted, set, len, range, min
import gc
import math
import re
import sqlalchemy
from decimal import Decimal
from sqlalchemy import (MetaData, Table, Column, String, Date, DateTime, Float, inspect, select)

DEFAULT_START_DATE = '2018-01-01'
CODES = ('399006.XSHE', '000922.XSHG', '000688.XSHG')
SOURCE = 'JQDATA.GET_PRICE.INDEX_CLOSE.V1'
CALENDAR_SOURCE = 'JQDATA.GET_TRADE_DAYS'
metadata = MetaData()
PRICES = Table('growth_dividend_index_history', metadata,
    Column('index_code', String(16), primary_key=True), Column('trade_date', Date, primary_key=True),
    Column('close', Float(53), nullable=False), Column('source', String(48), nullable=False),
    Column('updated_at', DateTime, nullable=False), mysql_engine='InnoDB')
CALENDAR = Table('growth_dividend_trading_day', metadata,
    Column('trade_date', Date, primary_key=True), Column('source', String(48), nullable=False),
    Column('updated_at', DateTime, nullable=False), mysql_engine='InnoDB')


def check_schema(connection):
    inspector = inspect(connection)
    for table in (PRICES, CALENDAR):
        columns = {c['name']: c for c in inspector.get_columns(table.name)}
        if set(columns) != set(c.name for c in table.columns):
            raise ValueError('源表字段不兼容')
        if set(inspector.get_pk_constraint(table.name)['constrained_columns']) != set(c.name for c in table.primary_key):
            raise ValueError('源表主键不兼容')
        for col in table.columns:
            actual = columns[col.name]
            typ = actual['type']
            if getattr(typ, 'unsigned', False):
                raise ValueError('源表数值类型不得为无符号')
            if actual['nullable'] or typ._type_affinity is not col.type._type_affinity:
                raise ValueError('源表类型或非空约束不兼容')
            if isinstance(col.type, String) and typ.length != col.type.length:
                raise ValueError('源表字符串长度不兼容')
            if isinstance(col.type, Float) and connection.dialect.name == 'mysql' and type(typ).__name__.upper() not in ('DOUBLE', 'DOUBLE_PRECISION'):
                raise ValueError('收盘点位必须为双精度')
        if connection.dialect.name == 'mysql' and str(inspector.get_table_options(table.name).get('mysql_engine', '')).lower() != 'innodb':
            raise ValueError('源表必须为 InnoDB')


def positive(value):
    if isinstance(value, (bool, str)):
        raise ValueError('无效点位')
    number = float(value)
    if not math.isfinite(number) or number <= 0 or (isinstance(value, Decimal) and Decimal(str(number)) != value):
        raise ValueError('无效点位')
    return number


def as_day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), '%Y-%m-%d').date()


def china_now():
    return datetime.utcnow() + timedelta(hours=8)


def api(name, namespace=None):
    import jqdata
    for value in ((namespace or {}).get(name), globals().get(name),
                  getattr(jqdata, name, None), getattr(builtins, name, None)):
        if callable(value):
            return value
    raise ValueError('聚宽研究环境缺少接口：{}'.format(name))


def database():
    from mysql_connector import create_mysql_engine
    return create_mysql_engine()


def diagnostic(exc, stage):
    """Only report phase, type, numeric codes and function locations; never SQL or credentials."""
    original = getattr(exc, 'orig', exc)
    kind = type(original).__name__
    if not re.match(r'^[A-Za-z_][A-Za-z_0-9]{0,63}$', kind):
        kind = 'Exception'
    args = getattr(original, 'args', ())
    code = args[0] if args and type(args[0]) is int and 1000 <= args[0] <= 9999 else None
    number = getattr(original, 'errno', None)
    details = '；数据库错误码={}'.format(code) if code is not None else ''
    if isinstance(original, OSError) and type(number) is int:
        details += '；系统错误码={}'.format(number)
    frames, trace = [], exc.__traceback__
    while trace is not None:
        frames.append('{}:{}'.format(trace.tb_frame.f_code.co_name, trace.tb_lineno))
        trace = trace.tb_next
    return '阶段={}；异常={}{}；位置={}'.format(stage, kind, details, ' > '.join(frames[-6:]))


def run(start_date=DEFAULT_START_DATE, end_date=None, preview=True, max_days=20000,
        max_windows=1000, window_days=250, check_cancel=None,
        api_namespace=None, engine_factory=None, progress=None, retry_earlier_missing=True):
    """先预览。写库 run(preview=False)，取消回调抛异常；分批提交，只补缺口。"""
    get_trade_days, get_price = api('get_trade_days', api_namespace), api('get_price', api_namespace)
    check_cancel = check_cancel or (lambda: None)
    notify = progress or (lambda message: None)
    stage = '初始化'
    def update(message):
        nonlocal stage
        stage = message
        notify(message)
    if any(type(n) is not int or n <= 0 for n in (max_days, max_windows, window_days)):
        raise ValueError('运行上限必须为正整数')
    if not isinstance(retry_earlier_missing, bool):
        raise ValueError('retry_earlier_missing 必须是布尔值')
    moment = china_now()
    closed = moment.date() if moment.hour >= 18 else moment.date() - timedelta(days=1)
    end = as_day(end_date) if end_date is not None else closed
    if end > closed:
        raise ValueError('结束日期尚未在北京时间18:00收盘确认')
    start = as_day(DEFAULT_START_DATE if start_date is None else start_date)
    if start > end:
        raise ValueError('开始日期不能晚于结束日期')
    update('创红比：读取 {} 至 {} 交易日历'.format(start, end))
    raw_days = get_trade_days(start_date=start, end_date=end)
    if raw_days is None or any(d is None for d in raw_days):
        raise ValueError('创红比交易日历返回空值，未开始写入；请重试聚宽日历接口')
    all_days = [as_day(d) for d in raw_days]
    if not all_days or all_days != sorted(set(all_days)) or all_days[-1] > end:
        raise ValueError('平台交易日历无效')
    days = [d for d in all_days if start <= d <= end]
    if not days or len(days) > max_days:
        raise ValueError('区间为空或超过交易日上限')
    report = dict(start=str(days[0]), end=str(days[-1]), preview=preview,
                  added=0, skipped=0, remaining=0, gaps=[], unit='条',
                  source=SOURCE, warning='当前平台可见历史，可能含历史修订；更早缺失原因未知，不保证最早完整覆盖。', indices={})
    engine = None
    try:
        if not preview:
            update('连接数据库并核对指数历史')
            engine = (engine_factory or database)()
            engine.echo = False
            engine.hide_parameters = True
            with engine.connect() as conn:
                update('数据库已连接：检查创红比表结构')
                metadata.create_all(conn)
                check_schema(conn)
        stored = {code: set() for code in CODES}
        if engine:
            update('创红比：读取已有指数日期')
            with engine.connect() as conn:
                keys = select([PRICES.c.index_code, PRICES.c.trade_date]) if sqlalchemy.__version__.startswith('1.') else select(PRICES.c.index_code, PRICES.c.trade_date)
                for code, day in conn.execute(keys.where((PRICES.c.trade_date >= days[0]) & (PRICES.c.trade_date <= days[-1]))):
                    if code in stored:
                        stored[code].add(as_day(day))
        if engine:
            update('创红比：读取已保存交易日历')
            calendar_keys = CALENDAR.select().with_only_columns(
                [CALENDAR.c.trade_date] if sqlalchemy.__version__.startswith('1.') else CALENDAR.c.trade_date)
            with engine.connect() as conn:
                saved_days = {row[0] for row in conn.execute(calendar_keys.where(
                    (CALENDAR.c.trade_date >= days[0]) & (CALENDAR.c.trade_date <= days[-1])))}
            pending_days = [day for day in days if day not in saved_days]
            for offset in range(0, len(pending_days), 250):
                check_cancel()
                target_days = pending_days[offset:offset + 250]
                update('创红比：补写交易日历 {}/{} 日'.format(offset, len(pending_days)))
                with engine.begin() as conn:
                    # 重读本批键，保留其他任务已写入的日历；每批一起提交。
                    existing = {row[0] for row in conn.execute(calendar_keys.where(CALENDAR.c.trade_date.in_(target_days)))}
                    now = datetime.utcnow()
                    rows = [dict(trade_date=day, source=CALENDAR_SOURCE, updated_at=now)
                            for day in target_days if day not in existing]
                    if rows:
                        conn.execute(CALENDAR.insert(), rows)
                update('创红比：补写交易日历 {}/{} 日'.format(offset + len(target_days), len(pending_days)))
        windows = 0
        for code in CODES:
            observed = []
            first_saved = min(stored[code]) if stored[code] else None
            missing = [d for d in days if d not in stored[code]
                       and (retry_earlier_missing or first_saved is None or d >= first_saved)]
            report['skipped'] += len(stored[code])
            update('{}：已有 {} 日，待查询 {} 日'.format(code, len(stored[code]), len(missing)))
            for offset in range(0, len(missing), window_days):
                check_cancel()
                windows += 1
                if windows > max_windows:
                    raise ValueError('超过查询窗口上限，已保存数据保留')
                target = missing[offset:offset + window_days]
                update('{}：读取 {} 至 {}'.format(code, target[0], target[-1]))
                for attempt in range(2):
                    try:
                        frame = get_price(code, start_date=target[0], end_date=target[-1], frequency='daily', fields=['close'], skip_paused=False, fq=None)
                    except OSError as exc:
                        if exc.errno != 24 or attempt:
                            raise
                    else:
                        break
                    gc.collect()
                    update('{}：读取 {} 至 {}（句柄回收后重试一次）'.format(code, target[0], target[-1]))
                update('{}：校验 {} 至 {} 行情'.format(code, target[0], target[-1]))
                quotes = {}
                if frame is not None and not frame.empty:
                    for stamp, row in frame.iterrows():
                        day = as_day(stamp)
                        if day not in target:
                            continue
                        if day in quotes:
                            raise ValueError('重复行情日期')
                        # NaN is an unavailable observation; never fabricate a close.
                        if row['close'] is None or math.isnan(float(row['close'])):
                            continue
                        quotes[day] = positive(row['close'])
                observed.extend(quotes)
                if engine:
                    update('{}：核对待保存行情 {} 至 {}（{} 条）'.format(code, target[0], target[-1], len(quotes)))
                    check_cancel()
                    rows = []
                    if quotes:
                        with engine.begin() as conn:
                            dates_query = select([PRICES.c.trade_date]) if sqlalchemy.__version__.startswith('1.') else select(PRICES.c.trade_date)
                            existing = {as_day(row[0]) for row in conn.execute(dates_query.where(
                                (PRICES.c.index_code == code) & PRICES.c.trade_date.in_(list(quotes))))}
                            now = datetime.utcnow()
                            rows = [dict(index_code=code, trade_date=day, close=quotes[day], source=SOURCE, updated_at=now)
                                    for day in target if day in quotes and day not in existing]
                            if rows:
                                conn.execute(PRICES.insert(), rows)
                            check_cancel()
                    report['added'] += len(rows)
                    report['skipped'] += len(quotes) - len(rows)
                    update('{}：本批新增 {} 条，跳过已有 {} 条'.format(code, len(rows), len(quotes) - len(rows)))
            known = set(observed) | (stored[code] & set(days))
            report['indices'][code] = dict(observed_first_day=str(min(known)) if known else None,
                available=len(known), missing=len(set(days) - known), earlier_missing='unknown')
            # Do not turn an unknown prehistory prefix into fabricated missing prices.
            # Gaps inside/after observed history (or an entirely empty index) remain failures.
            report['gaps'].extend((code, str(day)) for day in days
                                  if day not in known and (not known or day >= min(known)))
        return report
    except Exception as exc:
        raise RuntimeError('创红比采集中止；{}；已提交数据保留。'.format(diagnostic(exc, stage))) from None
    finally:
        if engine:
            engine.dispose()
