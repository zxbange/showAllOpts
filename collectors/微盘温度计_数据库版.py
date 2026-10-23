# -*- coding: utf-8 -*-
from __future__ import print_function, division

# 聚宽研究环境：直接粘贴运行，或 %run -i 微盘温度计_数据库版.py
from jqdata import *

import bisect
import builtins
import datetime
import json
import time
from decimal import Decimal, InvalidOperation
from statistics import median

import pandas as pd
from sqlalchemy import Column, Date, DateTime, Integer, Numeric, String, Text, create_engine
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

START_DATE = '2021-01-01'
END_DATE = None  # 每次运行时取北京时间当天。
MICRO_NUM = 1000
SAMPLE_STEP = 5  # 从首个交易日起，每 5 个交易日采样；增量不改变采样网格。
BATCH_SIZE = 1000
RAW_FIELDS = ('micro_median', 'relative_ratio', 'crowding_ratio', 'amount_crowding_ratio')
PCT_FIELDS = ('cap_pct', 'relative_pct', 'crowding_pct', 'amount_crowding_pct')
Q = Decimal('0.0000000001')
Base = declarative_base()


def _decimal_number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None

def _closed_day(end, now):
    """日行情在北京时间16点后使用当天；交易日仍由各策略的日历校验。"""
    return end - datetime.timedelta(days=1) if end == now.date() and now.hour < 16 else end


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


class MysqlMicrocap:
    # GitHub 脱敏模板：在聚宽私有副本中填写连接信息，不要提交真实凭据。
    host = 'your_mysql_host'
    port = 3306
    dbname = 'your_database'
    dbuser = 'your_database_user'
    dbpassword = 'your_password'

    @classmethod
    def connect_to_mysql(cls):
        # URL.create 兼容新 SQLAlchemy；旧版聚宽环境使用 URL 构造函数。
        make_url = getattr(URL, 'create', URL)
        uri = make_url(
            drivername='mysql+mysqldb', host=cls.host, port=cls.port,
            username=cls.dbuser, password=cls.dbpassword, database=cls.dbname,
            query={'charset': 'utf8mb4'},
        )
        engine = create_engine(
            uri, echo=False, pool_size=1, max_overflow=0,
            pool_recycle=3600, pool_pre_ping=True,
            connect_args={'connect_timeout': 10},
        )
        for attempt in range(1, 6):
            try:
                with engine.connect():
                    pass
                print('成功连接到 MySQL 数据库。')
                return engine
            except Exception as exc:
                # 不打印连接串或异常全文，避免日志包含密码。
                print('MySQL 连接失败，第 {}/5 次，异常类型：{}'.format(
                    attempt, type(exc).__name__))
                engine.dispose()
                if attempt == 5:
                    raise RuntimeError('MySQL 连接失败，请检查网络、白名单和连接参数。') from None
                time.sleep(10)

def china_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.datetime.strptime(str(value)[:10], '%Y-%m-%d').date()


number = _decimal_number


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
    session = Session()
    try:
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
    finally:
        session.close()


def save_period(Session, record, skip_existing=False):
    required = RESULT_COLUMNS + ['member_codes']
    for field in required:
        if field not in record or (record[field] is None and field != 'avg_np_growth'):
            raise ValueError('快照字段 {} 不完整。'.format(field))
    for field in PCT_FIELDS + ('score',):
        value = number(record[field])
        if value is None or not 0 <= value <= 1:
            raise ValueError('分位或温度不在 0～1 范围内。')
    session = Session()
    try:
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
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run(start_date=START_DATE, end_date=END_DATE, micro_num=MICRO_NUM, sample_step=SAMPLE_STEP, skip_existing=True):
    start = as_date(start_date)
    now = china_now()
    today = now.date()
    end = today if end_date is None else as_date(end_date)
    if start > end or end > today:
        raise ValueError('开始日期不能晚于结束日期，结束日期不能晚于今天。')
    if skip_existing:
        end = _closed_day(end, now)
        print('只新增模式：16:00 前不写入当天未完成快照。')
    for value in (micro_num, sample_step):
        if isinstance(value, builtins.bool) or not isinstance(value, builtins.int) or value <= 0:
            raise ValueError('微盘数量和采样步长必须是正整数。')
    series_id = series_key(start, micro_num, sample_step)
    engine = MysqlMicrocap.connect_to_mysql()
    records = []
    try:
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        last, previous, histories = load_resume(Session, series_id, include_latest=skip_existing)
        if last is not None and last > today:
            raise ValueError('最新入库日期晚于今天，请检查已有数据。')
        begin = last or start
        if begin > end:
            print('最新日期晚于结束日期，无需增量更新。')
            return pd.DataFrame(columns=RESULT_COLUMNS)
        days = calendar(begin, end)
        if last is not None and (not days or days[0] != last):
            raise ValueError('最新入库日不在交易日历中，停止写入。')
        pending = days[sample_step::sample_step] if skip_existing and last is not None else days[::sample_step]
        print('微盘 {}；每 {} 个交易日采样；本次 {} 至 {}，待更新 {} 期。'.format(
            micro_num, sample_step, begin, end, len(pending)))
        for day in pending:
            try:
                record = calculate_period(day, previous, micro_num)
                if record['trade_date'] != day or record['micro_num'] != micro_num:
                    raise ValueError('计算结果日期或微盘数量不一致。')
                record.update(series_id=series_id, sample_step=sample_step)
                append_percentiles(record, histories)
                if not save_period(Session, record, skip_existing=skip_existing):
                    raise ValueError('该采样期已被其他任务写入，未覆盖；请重跑以加载已保存的指数和分位基准。')
            except Exception as exc:
                print('{} 计算或写入失败（{}）；已完成期数已提交，修复后重跑从最新一期继续。'.format(
                    day, type(exc).__name__))
                if isinstance(exc, ValueError):
                    raise
                raise RuntimeError('{} 采集失败，请检查聚宽接口、权限及数据库连接。'.format(day)) from None
            previous = record
            records.append({field: record[field] for field in RESULT_COLUMNS})
            print('{} 入库完成：综合温度 {:.1f}，等权指数 {:.2f}，累计 {} 期。'.format(
                day, float(record['score']) * 100, float(record['micro_index']), record['sample_count']))
    finally:
        engine.dispose()
    return pd.DataFrame(records, columns=RESULT_COLUMNS)


if __name__ == '__main__':
    temperature_df = run()
    print('本次完成 {} 期微盘温度计入库。'.format(len(temperature_df)))
    print(temperature_df.tail(10).to_string(index=False))
