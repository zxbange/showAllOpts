# -*- coding: utf-8 -*-

from __future__ import print_function, division

# 沿用已能运行的原程序导入方式，使用研究环境提供的 API。
# 不用 globals() 判断接口是否存在：函数也可能位于内置命名空间。
# 在 Notebook 中直接粘贴运行；运行文件请使用：
# %run -i 行业动量计算_数据库版.py
# -i 让文件沿用当前 Notebook 的命名空间，避免丢失环境预置的接口。
from jqdata import *

# 放在平台通配符导入之后，避免 datetime 等同名对象被覆盖。
import datetime
import math
import time
from decimal import Decimal

import pandas as pd
from sqlalchemy import Column, Date, DateTime, Integer, Numeric, String, create_engine, func
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker


START_DATE = '2025-01-01'  # 该行业分类首次入库时使用的起始日期。
END_DATE = None  # None 表示每次运行时取北京时间当天，也可指定 'YYYY-MM-DD'。
INDUSTRY_TYPE = 'sw_l2'
RETENTION_TRADE_DAYS = 180  # 按运行当天的交易日历保留，不是 180 个自然日。

Base = declarative_base()
RESULT_COLUMNS = [
    'trade_date', 'industry_type', 'industry_code', 'industry_name',
    'momentum', 'momentum_rank', 'front_stock_count', 'industry_stock_count',
]


class IndustryMomentum(Base):
    __tablename__ = 'industry_momentum'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}

    trade_date = Column(Date, primary_key=True)
    industry_type = Column(String(16), primary_key=True)
    industry_code = Column(String(32), primary_key=True)
    industry_name = Column(String(128), nullable=False)
    momentum = Column(Numeric(18, 2), nullable=False)
    momentum_rank = Column(Integer, nullable=False)
    front_stock_count = Column(Integer, nullable=False)
    industry_stock_count = Column(Integer, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.datetime.now)


class MysqlMomentum:
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


def as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.datetime.strptime(str(value)[:10], '%Y-%m-%d').date()


def calculate_momentum(trade_date, industry_type=INDUSTRY_TYPE):
    """计算一个交易日的全部行业；仅获取数据，不连接数据库。"""
    day = as_date(trade_date)
    date_text = day.isoformat()
    industries = get_industries(name=industry_type, date=date_text)
    if industries is None or industries.empty:
        raise ValueError('{} 未获取到行业列表，停止写入。'.format(date_text))

    # 同一行业只请求一次，计数和归属使用完全相同的成分股数据。
    members = {}
    for code in industries.index:
        members[code] = set(get_industry_stocks(code, date=date_text))

    securities = get_all_securities(types=['stock'], date=date_text)
    if securities is None or securities.empty:
        raise ValueError('{} 未获取到股票列表，停止写入。'.format(date_text))
    cutoff = day - datetime.timedelta(days=150)
    eligible = securities[
        (securities['end_date'].map(as_date) > day)
        & (securities['start_date'].map(as_date) < cutoff)
    ].index.tolist()
    if not eligible:
        raise ValueError('{} 没有符合上市期限条件的股票，停止写入。'.format(date_text))

    # 20 日涨幅需要 21 个交易日。用交易日定位端点，避免缺行时取错日期。
    window = [as_date(d) for d in get_trade_days(end_date=date_text, count=21)]
    if len(window) != 21 or window[-1] != day:
        raise ValueError('{} 不是交易日或历史交易日不足 21 天。'.format(date_text))
    prices = get_price(
        eligible, start_date=None, end_date=date_text, frequency='daily',
        fields=['close'], skip_paused=False, fq='pre', count=25, panel=False,
    )
    if prices is None or prices.empty:
        raise ValueError('{} 行情为空，停止写入，避免误存全零结果。'.format(date_text))
    prices = prices.copy()
    prices['price_date'] = pd.to_datetime(prices['time']).dt.date
    front_stocks = set()
    valid_count = 0
    for code, group in prices.groupby('code'):
        if group['price_date'].duplicated().any():
            raise ValueError('{} {} 行情日期重复。'.format(date_text, code))
        close = group.set_index('price_date')['close']
        if window[0] not in close.index or day not in close.index:
            continue
        start_close, end_close = close.loc[window[0]], close.loc[day]
        if pd.isnull(start_close) or pd.isnull(end_close):
            continue
        start_close, end_close = float(start_close), float(end_close)
        if not (math.isfinite(start_close) and math.isfinite(end_close)):
            continue
        if start_close <= 0 or end_close <= 0:
            continue
        valid_count += 1
        if (end_close - start_close) / start_close > 0.2:
            front_stocks.add(code)
    if not valid_count:
        raise ValueError('{} 没有可计算涨幅的股票，停止写入。'.format(date_text))
    if valid_count < len(eligible):
        print('{}：{} 只股票价格无效或缺失，未参与涨幅统计。'.format(
            date_text, len(eligible) - valid_count))

    records = []
    for code in industries.index:
        total = len(members[code])
        count = len(members[code] & front_stocks)
        score = count * count / total if total else 0.0
        records.append({
            'trade_date': day,
            'industry_type': industry_type,
            'industry_code': str(code),
            'industry_name': str(industries.loc[code, 'name']),
            'momentum': Decimal(format(score, '.2f')),
            'front_stock_count': count,
            'industry_stock_count': total,
        })
    # 与原程序相同：先按两位小数的得分降序，再按行业代码降序。
    records.sort(key=lambda r: (r['momentum'], r['industry_code']), reverse=True)
    for rank, record in enumerate(records, 1):
        record['momentum_rank'] = rank
    return records


def save_day(Session, trade_date, industry_type, records, skip_existing=False):
    """默认事务替换日快照；只新增模式按主键过滤已存在记录。"""
    day = as_date(trade_date)
    if not records:
        raise ValueError('拒绝写入空结果。')
    # 不使用 any(生成器)：研究环境中的同名函数可能是 numpy.any，
    # 对生成器的处理与 Python 内置 any 不同，会把一致的结果误判为异常。
    for record in records:
        if record['trade_date'] != day or record['industry_type'] != industry_type:
            raise ValueError(
                '结果中的日期或行业分类不一致：行业代码={!r}，'
                '预期日期={!r}、分类={!r}；实际日期={!r}、分类={!r}。'.format(
                    record.get('industry_code'), day, industry_type,
                    record['trade_date'], record['industry_type']))
    session = Session()
    try:
        query = session.query(IndustryMomentum).filter_by(trade_date=day, industry_type=industry_type)
        if skip_existing:
            existing = {row[0] for row in query.with_entities(IndustryMomentum.industry_code).all()}
            records = [record for record in records if record['industry_code'] not in existing]
            if not records:
                return []
        else:
            query.delete(synchronize_session=False)
        session.add_all([IndustryMomentum(**record) for record in records])
        session.commit()
        return records
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_last_saved_date(Session, industry_type):
    """按行业分类查询最后已提交的交易日期；没有记录时返回 None。"""
    session = Session()
    try:
        return session.query(func.max(IndustryMomentum.trade_date)).filter(
            IndustryMomentum.industry_type == industry_type,
        ).scalar()
    finally:
        session.close()


def get_retention_start(reference_date, count=RETENTION_TRADE_DAYS):
    """先验证交易日历，再允许清理；避免接口异常导致误删。"""
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ValueError('保留交易日数量必须是正整数。')
    reference = as_date(reference_date)
    raw_days = get_trade_days(end_date=reference.isoformat(), count=count)
    if raw_days is None:
        raise ValueError('保留范围的交易日历为空，未清理数据库。')
    days = [as_date(day) for day in raw_days]
    if len(days) != count:
        raise ValueError('交易日历不足 {} 天，未清理数据库。'.format(count))
    previous = None
    for day in days:
        if day > reference or (previous is not None and day <= previous):
            raise ValueError('交易日历存在未来日期、重复或乱序，未清理数据库。')
        previous = day
    return days[0]


def cleanup_old_data(Session, retention_start):
    """一个事务清理整张表的过期数据，保留边界日及之后的快照。"""
    cutoff = as_date(retention_start)
    session = Session()
    try:
        deleted = session.query(IndustryMomentum).filter(
            IndustryMomentum.trade_date < cutoff,
        ).delete(synchronize_session=False)
        session.commit()
        return deleted
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run(start_date=START_DATE, end_date=END_DATE, industry_type=INDUSTRY_TYPE, skip_existing=False):
    """先清理过期数据；skip_existing=True 时从最新日期之后只新增。"""
    now_china = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    today_china = now_china.date()
    if end_date is None:
        end_date = today_china
    initial_start, end = as_date(start_date), as_date(end_date)
    if end > today_china:
        raise ValueError('结束日期不能晚于运行当天。')
    if skip_existing and end == today_china and now_china.hour < 16:
        end = today_china - datetime.timedelta(days=1)
        print('只新增模式：16:00 前不写入当天未完成快照。')
    # 即使手动指定历史 end_date，保留范围仍以实际运行当天为基准。
    # 日历验证在任何数据库修改之前完成，节假日自动使用最近交易日。
    retention_start = get_retention_start(today_china, RETENTION_TRADE_DAYS)
    engine = MysqlMomentum.connect_to_mysql()
    all_records = []
    try:
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        try:
            deleted = cleanup_old_data(Session, retention_start)
        except Exception:
            print('过期数据清理失败，事务已回滚，未开始计算或入库。')
            raise
        print('已清理 {} 条过期记录（所有行业分类）；按运行当天保留最近 {} 个交易日，'
              '保留起始日为 {}（含）。'.format(deleted, RETENTION_TRADE_DAYS, retention_start))
        if end < retention_start:
            print('结束日期 {} 早于保留起始日 {}，不再补录过期数据。'.format(end, retention_start))
            return pd.DataFrame(columns=RESULT_COLUMNS)
        last_date = get_last_saved_date(Session, industry_type)
        if last_date is None:
            start = initial_start
            if start < retention_start:
                start = retention_start
            if start > end:
                raise ValueError('首次入库的开始日期不能晚于结束日期。')
            print('{} 尚无保留范围内的记录，从 {} 开始入库。'.format(industry_type, start))
        else:
            start = as_date(last_date)
            if skip_existing:
                start += datetime.timedelta(days=1)
            if start < retention_start:
                start = retention_start
            if start > end:
                print('最后已入库日期 {} 晚于结束日期 {}，无需更新。'.format(start, end))
                return pd.DataFrame(columns=RESULT_COLUMNS)
            print('{} 最后已入库日期为 {}，从 {}（含）采集至 {}。'.format(
                industry_type, last_date, start, end))
        trade_days = [as_date(day) for day in get_trade_days(start_date=start, end_date=end)]
        # 先检查整个补录日历，避免异常接口将过期或越界日期写回。
        previous = None
        for day in trade_days:
            if day < start or day > end or (previous is not None and day <= previous):
                raise ValueError('补录交易日历越界、重复或乱序，停止入库。')
            previous = day
        if not trade_days:
            print('日期范围内没有交易日，无需写入。')
            return pd.DataFrame(columns=RESULT_COLUMNS)
        for day in trade_days:
            records = calculate_momentum(day, industry_type)
            try:
                records = save_day(Session, day, industry_type, records, skip_existing=skip_existing)
            except Exception:
                print('{} 校验或写入失败；修复后重跑会从最后已入库日期继续。'.format(day))
                raise
            all_records.extend(records)
            print('{} 计算并入库完成，共 {} 个行业（含零分行业）。'.format(day, len(records)))
    finally:
        engine.dispose()
    return pd.DataFrame(all_records, columns=RESULT_COLUMNS)


if __name__ == '__main__':
    momentum_df = run()
    print('全部完成，共写入 {} 条行业动量记录。'.format(len(momentum_df)))
    print(momentum_df.tail(15).to_string(index=False))
