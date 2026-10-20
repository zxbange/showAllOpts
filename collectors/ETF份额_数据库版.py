# -*- coding: utf-8 -*-
from __future__ import print_function, division

# 聚宽研究 Notebook：直接粘贴运行，或 %run -i ETF份额_数据库版.py
from jqdata import *
import builtins
import datetime
import time
from decimal import Decimal, InvalidOperation

import pandas as pd
from sqlalchemy import BigInteger, Column, Date, DateTime, String, create_engine, func
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

START_DATE = None  # 首次采集：截至运行当天，最近 300 个交易日。
INITIAL_TRADE_DAYS = 300
END_DATE = None  # 每次运行取北京时间当天；不保证当天源数据已经发布。
LOOKBACK_DAYS = 5  # 含最后入库日，回查最近 5 个交易日。
WINDOW_SIZE = 120  # 每只基金每次最多请求 120 个交易日，避免接口行数上限截断。
ETF_GROUPS = {
    # 按 2026-10-16 规模排名选定的前五只，固定跟踪，不在运行时自动替换名单。
    'hs300': [('510300.XSHG', '沪深300ETF华泰柏瑞'), ('510310.XSHG', '沪深300ETF易方达'),
              ('510330.XSHG', '沪深300ETF华夏'), ('159919.XSHE', '沪深300ETF嘉实'),
              ('515330.XSHG', '沪深300ETF天弘')],
    'zz500': [('510500.XSHG', '中证500ETF南方'), ('510510.XSHG', '中证500ETF广发'),
              ('510580.XSHG', '中证500ETF易方达'), ('512500.XSHG', '中证500ETF华夏'),
              ('159922.XSHE', '中证500ETF嘉实')],
}
SOURCE = 'jqdata.FUND_SHARE_DAILY'
Base = declarative_base()


class ETFShare(Base):
    __tablename__ = 'etf_share_daily'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
    fund_code = Column(String(16), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    fund_name = Column(String(128), nullable=False)
    shares = Column(BigInteger, nullable=False)  # 原始单位：份；不用亿元或成交量替代。
    previous_trade_date = Column(Date, nullable=False)
    source = Column(String(48), nullable=False)
    updated_at = Column(DateTime, nullable=False)


RESULT_COLUMNS = [column.name for column in ETFShare.__table__.columns]


class MysqlETF:
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


def share_table():
    try:
        apis = (finance, query, get_trade_days)
    except NameError:
        apis = (None,)
    if builtins.any(api is None for api in apis):
        raise ValueError('缺少聚宽研究 API，请在聚宽 Notebook 直接粘贴运行，或使用 %run -i。')
    table = getattr(finance, 'FUND_SHARE_DAILY', None)
    if table is None or builtins.any(not hasattr(table, field) for field in ('code', 'date', 'shares')):
        raise ValueError('当前环境未提供 finance.FUND_SHARE_DAILY 或所需字段，请核对聚宽数据权限。')
    return table


def validate_days(raw, start, end):
    if raw is None:
        raise ValueError('交易日历接口返回空对象。')
    days = [as_date(value) for value in raw]
    if days != sorted(set(days)) or builtins.any(day < start or day > end for day in days):
        raise ValueError('交易日历重复、越界或乱序。')
    return days


def trading_days(start, end):
    return validate_days(get_trade_days(start_date=start, end_date=end), start, end)


def previous_days(start, count):
    raw = get_trade_days(end_date=start, count=count)
    if raw is None:
        raise ValueError('交易日历获取失败。')
    return validate_days(raw, datetime.date(1900, 1, 1), start)


def normalize_shares(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('基金份额不是有效数字。') from None
    if not number.is_finite() or number < 0 or number != number.to_integral_value() or number > 9223372036854775807:
        raise ValueError('基金份额缺失、非整数、负数或超出 BIGINT 范围。')
    return int(number)


def fetch_window(code, name, days, before, require_start=False):
    table = share_table()
    frame = finance.run_query(query(table).filter(
        table.code == code, table.date >= days[0], table.date <= days[-1],
    ).order_by(table.date).limit(len(days) + 1))
    if frame is None or not {'code', 'date', 'shares'}.issubset(frame.columns):
        raise ValueError('ETF 份额接口未返回有效表格或缺少必要字段。')
    records = []
    allowed = set(days)
    seen = set()
    for item in frame.to_dict('records'):
        day = as_date(item['date'])
        if item['code'] != code or day not in allowed or day in seen:
            raise ValueError('ETF 份额返回代码、日期越界或重复记录。')
        seen.add(day)
        records.append(dict(fund_code=code, fund_name=name, trade_date=day,
                            shares=normalize_shares(item['shares']), previous_trade_date=before[day],
                            source=SOURCE, updated_at=china_now()))
    records.sort(key=lambda record: record['trade_date'])
    if records:
        first, last = records[0]['trade_date'], records[-1]['trade_date']
        expected = {day for day in days if first <= day <= last}
        if seen != expected or (require_start and first != days[0]):
            raise ValueError('{} 份额历史中间缺失，停止该基金，不能跳过缺口更新进度。'.format(code))
    return records


def last_dates(Session, codes):
    session = Session()
    try:
        return {code: (first, last) for code, first, last in session.query(
            ETFShare.fund_code, func.min(ETFShare.trade_date), func.max(ETFShare.trade_date),
        ).filter(ETFShare.fund_code.in_(codes)).group_by(ETFShare.fund_code).all()}
    finally:
        session.close()


def save_window(Session, code, records, skip_existing=False):
    if not records:
        return []
    dates = [record['trade_date'] for record in records]
    if len(set(dates)) != len(dates) or builtins.any(r['fund_code'] != code or r['previous_trade_date'] >= r['trade_date'] for r in records):
        raise ValueError('待写入份额快照不一致。')
    session = Session()
    try:
        query = session.query(ETFShare).filter(ETFShare.fund_code == code, ETFShare.trade_date.in_(dates))
        if skip_existing:
            existing = {row[0] for row in query.with_entities(ETFShare.trade_date).all()}
            records = [record for record in records if record['trade_date'] not in existing]
            if not records:
                return []
        else:
            # 旧模式只替换源实际返回的日期；不补零。
            query.delete(synchronize_session=False)
        session.add_all([ETFShare(**record) for record in records])
        session.commit()
        return records
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def probe(code='510300.XSHG', end_date=None):
    """只测试聚宽最近份额接口，不连接 MySQL；用于确认权限、日期和份额单位。"""
    end = china_now().date() if end_date is None else as_date(end_date)
    days = previous_days(end, 10)
    if len(days) < 2:
        raise ValueError('交易日历不足，无法验证。')
    records = fetch_window(code, code, days[1:], dict(zip(days[1:], days[:-1])))
    print('源字段 shares 按份读取；返回 {} 条，请核对最新日期及基金官方份额。'.format(len(records)))
    return pd.DataFrame(records, columns=RESULT_COLUMNS)


def run(start_date=START_DATE, end_date=END_DATE, incremental=True, lookback_days=LOOKBACK_DAYS, groups=None, skip_existing=True):
    today = china_now().date()
    end = today if end_date is None else as_date(end_date)
    start = None if start_date is None else as_date(start_date)
    if end > today or (start is not None and start > end):
        raise ValueError('开始日期不能晚于结束日期，结束日期不能晚于今天。')
    if not isinstance(incremental, builtins.bool) or isinstance(lookback_days, builtins.bool) or not isinstance(lookback_days, builtins.int) or lookback_days < 1:
        raise ValueError('incremental 必须为布尔值，回查交易日数必须为正整数。')
    selected = list(ETF_GROUPS) if groups is None else list(groups)
    if not selected or builtins.any(group not in ETF_GROUPS for group in selected):
        raise ValueError('分组仅支持 hs300、zz500。')
    funds = dict(pair for group in selected for pair in ETF_GROUPS[group])
    share_table()  # 在连接数据库前检查环境。
    if start is None:
        initial_days = previous_days(end, INITIAL_TRADE_DAYS)
        if not initial_days:
            raise ValueError('无法取得首次采集范围的交易日历。')
        start = initial_days[0]
    engine = MysqlETF.connect_to_mysql()
    output, failures = [], []
    try:
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        latest = last_dates(Session, list(funds)) if incremental else {}
        for code, name in funds.items():
            try:
                first, last = latest.get(code, (None, None))
                if last and as_date(last) > today:
                    raise ValueError('最新入库日期晚于今天。')
                if last and as_date(last) > end:
                    print('{} 最新记录晚于请求终点，跳过；历史修订请指定 incremental=False。'.format(code))
                    continue
                begin = start
                if last:
                    recent = previous_days(as_date(last), 1 if skip_existing else lookback_days)
                    if not recent or recent[-1] != as_date(last):
                        raise ValueError('最新记录不在交易日历中。')
                    # 已有数据以水位为准；即使一年以上没运行，也不能跳过中间缺口。
                    if skip_existing:
                        begin = as_date(last) + datetime.timedelta(days=1)
                        if start_date is not None:
                            begin = builtins.max(start, begin)
                    else:
                        begin = builtins.max(as_date(first), recent[0]) if start_date is None else builtins.max(start, recent[0])
                if begin > end:
                    print('{} 已有数据，无新增日期，跳过写入。'.format(code))
                    continue
                days = trading_days(begin, end)
                if not days:
                    continue
                prior = previous_days(days[0] - datetime.timedelta(days=1), 1)
                if not prior:
                    raise ValueError('无法确定首日的前一交易日。')
                before = dict(zip(days, prior + days[:-1]))
                # 回查可能早于基金首条有效数据；已有记录本身作为连续性锚点。
                seen_data = builtins.bool(last and first and days[0] >= as_date(first))
                print('{} {}：最新入库 {}；查询 {} 至 {}。'.format(code, name, last, begin, end))
                for offset in range(0, len(days), WINDOW_SIZE):
                    window = days[offset:offset + WINDOW_SIZE]
                    records = fetch_window(code, name, window, before, require_start=seen_data)
                    if records:
                        # 已知最新日期处于此窗口时，接口应仍包含它，防止覆盖不完整快照。
                        if last and window[0] <= as_date(last) <= window[-1] and as_date(last) not in {r['trade_date'] for r in records}:
                            raise ValueError('源数据未包含已入库的最新日期，停止该基金。')
                        written = save_window(Session, code, records, skip_existing=skip_existing)
                        output.extend(written)
                        seen_data = True
                        print('{} {} 至 {}：写入 {} 条，最新份额 {:.4f} 亿份。'.format(
                            code, records[0]['trade_date'], records[-1]['trade_date'], len(written), records[-1]['shares'] / 1e8))
                        if records[-1]['trade_date'] < window[-1]:
                            print('{} 后续日期尚无数据，保留待下次获取，不补零。'.format(code))
                            break
                    elif seen_data or last:
                        print('{} 当前窗口无数据，保留已有记录，下次重试。'.format(code))
                        break
                if not seen_data:
                    print('{} 本次未取得份额；请检查数据覆盖、权限及发布时间。'.format(code))
            except Exception as exc:
                # 每只基金独立恢复；不打印数据库连接或原始驱动异常。
                print('{} 采集失败（{}）；该窗口未写入或已回滚，其他基金继续。'.format(code, type(exc).__name__))
                if isinstance(exc, ValueError):
                    print(str(exc))
                failures.append(code)
        if failures:
            raise RuntimeError('以下 ETF 未完成：{}。成功窗口已提交，修复后重跑可续传。'.format(', '.join(failures)))
    finally:
        engine.dispose()
    return pd.DataFrame(output, columns=RESULT_COLUMNS)


if __name__ == '__main__':
    etf_df = run()
    print('本次共写入 {} 条 ETF 份额记录。'.format(len(etf_df)))
    print(etf_df.tail(10).to_string(index=False))
