# -*- coding: utf-8 -*-
from __future__ import print_function, division

# 聚宽 Notebook：直接粘贴运行，或 %run -i 抱团拥挤度_数据库版.py
from jqdata import *

# 放在通配符导入之后，避免平台覆盖同名模块/内置函数。
import builtins
import datetime
import time
from decimal import Decimal, InvalidOperation

import pandas as pd
from sqlalchemy import Column, Date, DateTime, Integer, Numeric, create_engine
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

START_DATE = '2020-01-01'
END_DATE = None  # 每次调用 run 时取北京时间当天，不在导入文件时固定日期。
TOP_PCT = 0.05  # 沿用原程序的前 5%。
# 本模块保留完整历史，不使用行业动量的 180 个交易日清理规则。
Base = declarative_base()
RESULT_COLUMNS = ['trade_date', 'top_pct', 'ratio', 'top_money', 'total_money',
                  'stock_count', 'top_stock_count', 'updated_at']


class MarketCrowding(Base):
    __tablename__ = 'market_crowding'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}

    # 查询固定 TOP_PCT 后按日期范围读取，复合主键同时支持去重与查询。
    top_pct = Column(Numeric(8, 6), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    ratio = Column(Numeric(18, 10), nullable=False)
    top_money = Column(Numeric(24, 2), nullable=False)
    total_money = Column(Numeric(24, 2), nullable=False)
    stock_count = Column(Integer, nullable=False)
    top_stock_count = Column(Integer, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class MysqlCrowding:
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


def summarize_money(pairs, top_pct=TOP_PCT):
    """纯计算：零成交额股票计入总数量；无效/负数/重复数据拒绝入库。"""
    pct = Decimal(str(top_pct))
    if not pct.is_finite() or not Decimal('0') < pct <= Decimal('1'):
        raise ValueError('TOP_PCT 必须在 (0, 1] 内。')
    if pct != pct.quantize(Decimal('0.000001')):
        raise ValueError('TOP_PCT 最多支持六位小数。')
    values = []
    codes = set()
    for code, money in pairs:
        if code in codes:
            raise ValueError('单日行情存在重复股票，停止写入。')
        codes.add(code)
        try:
            amount = Decimal(str(money))
        except (InvalidOperation, ValueError):
            raise ValueError('成交额不是有效数值，停止写入。') from None
        if not amount.is_finite() or amount < 0:
            raise ValueError('成交额缺失、非有限或为负数，停止写入。')
        values.append(amount)
    if len(values) < 100:
        raise ValueError('有效股票不足 100 只，停止写入。')
    count = builtins.max(int(Decimal(len(values)) * pct), 1)
    total = builtins.sum(values, Decimal('0'))
    if total <= 0:
        raise ValueError('全市场成交额为零，停止写入。')
    top = builtins.sum(sorted(values, reverse=True)[:count], Decimal('0'))
    return dict(top_pct=pct, ratio=(top / total).quantize(Decimal('0.0000000001')),
                top_money=top.quantize(Decimal('0.01')),
                total_money=total.quantize(Decimal('0.01')),
                stock_count=len(values), top_stock_count=count)


def calculate_crowding(trade_date, top_pct=TOP_PCT):
    day = as_date(trade_date)
    securities = get_all_securities(types=['stock'], date=day.isoformat())
    if securities is None or securities.empty:
        raise ValueError('{} 股票列表为空。'.format(day))
    stocks = securities.index.tolist()
    prices = get_price(
        security=stocks, end_date=day.isoformat(), count=1, frequency='daily',
        fields=['money'], skip_paused=False, panel=False,
    )
    if prices is None or prices.empty:
        raise ValueError('{} 行情为空。'.format(day))
    if not {'code', 'time', 'money'}.issubset(prices.columns):
        raise ValueError('行情缺少 code、time 或 money 字段。')
    # 防止接口部分返回或返回上一交易日行情，被当成当日完整快照。
    if set(prices['code']) != set(stocks):
        raise ValueError('{} 行情股票与当日列表不一致，停止写入。'.format(day))
    for value in prices['time']:
        if as_date(value) != day:
            raise ValueError('{} 行情日期不一致，停止写入。'.format(day))
    record = summarize_money(zip(prices['code'], prices['money']), top_pct)
    record.update(trade_date=day, updated_at=china_now())
    return record


def save_day(Session, day, top_pct, record):
    if record['trade_date'] != as_date(day) or record['top_pct'] != Decimal(str(top_pct)):
        raise ValueError('结果日期或 TOP_PCT 不一致，拒绝入库。')
    session = Session()
    try:
        session.query(MarketCrowding).filter_by(
            trade_date=as_date(day), top_pct=Decimal(str(top_pct)),
        ).delete(synchronize_session=False)
        session.add(MarketCrowding(**record))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_saved_dates(Session, start, end, top_pct):
    session = Session()
    try:
        rows = session.query(MarketCrowding.trade_date).filter(
            MarketCrowding.top_pct == Decimal(str(top_pct)),
            MarketCrowding.trade_date >= start,
            MarketCrowding.trade_date <= end,
        ).all()
        return {as_date(row[0]) for row in rows}
    finally:
        session.close()


def run(start_date=START_DATE, end_date=END_DATE, top_pct=TOP_PCT):
    today = china_now().date()
    start = as_date(start_date)
    end = today if end_date is None else as_date(end_date)
    if start > end or end > today:
        raise ValueError('开始日期不能晚于结束日期，结束日期不能晚于运行当天。')
    # 参数及整个日历先验证，再进行数据库连接或写入。
    pct = Decimal(str(top_pct))
    if not pct.is_finite() or not Decimal('0') < pct <= Decimal('1'):
        raise ValueError('TOP_PCT 必须在 (0, 1] 内。')
    if pct != pct.quantize(Decimal('0.000001')):
        raise ValueError('TOP_PCT 最多支持六位小数。')
    raw_days = get_trade_days(start_date=start, end_date=end)
    if raw_days is None:
        raise ValueError('获取交易日历失败，未写入数据库。')
    days = [as_date(day) for day in raw_days]
    previous = None
    for day in days:
        if day < start or day > end or (previous is not None and day <= previous):
            raise ValueError('交易日历越界、重复或乱序，未写入数据库。')
        previous = day
    print('采集范围：{} 至 {}；TOP_PCT={}；完整保留历史。'.format(start, end, top_pct))
    if not days:
        print('日期范围没有交易日。')
        return pd.DataFrame(columns=RESULT_COLUMNS)
    engine = MysqlCrowding.connect_to_mysql()
    records = []
    try:
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        saved = get_saved_dates(Session, start, end, top_pct)
        last = builtins.max(saved) if saved else None
        # 补齐任意历史缺口，并重算最后一个已入库日，修正盘中快照。
        pending = [day for day in days if day not in saved or day == last]
        print('最后已入库日期：{}；待计算 {} 个交易日。'.format(last, len(pending)))
        for index, day in enumerate(pending, 1):
            try:
                record = calculate_crowding(day, top_pct)
                save_day(Session, day, top_pct, record)
            except Exception as exc:
                # 不输出数据库异常全文，避免日志泄露账号、地址或 SQL。
                print('{} 计算或写入失败（{}），本次停止；已完成日期已提交，修复后重跑可补齐。'.format(day, type(exc).__name__))
                if isinstance(exc, ValueError):
                    raise
                raise RuntimeError('{} 采集失败，请检查聚宽接口、数据权限和数据库连接。'.format(day)) from None
            records.append(record)
            print('[{}/{}] {} 入库完成：占比 {:.2%}，前 {} / {} 只股票。'.format(
                index, len(pending), day, float(record['ratio']),
                record['top_stock_count'], record['stock_count']))
    finally:
        engine.dispose()
    return pd.DataFrame(records, columns=RESULT_COLUMNS)


if __name__ == '__main__':
    crowding_df = run()
    print('本次完成 {} 个交易日的入库。'.format(len(crowding_df)))
    print(crowding_df.tail(10).to_string(index=False))
