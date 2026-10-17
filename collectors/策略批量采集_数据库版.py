# -*- coding: utf-8 -*-
"""聚宽单文件串行采集：直接粘贴到 Notebook，或 %run -i 本文件。
五个策略在本文件内独立实现；不读取外部策略、配置或日志文件。
只需填写下面的 DB_CONFIG；用法见 docs/collector-batch.md。
"""
import builtins as _b
import io as _io
import re as _re
import time as _time
from contextlib import redirect_stdout, redirect_stderr, contextmanager
import threading as _threading
from types import SimpleNamespace as _Namespace
from urllib.parse import quote, quote_plus

DB_CONFIG = {
    'host': 'your_mysql_host',
    'port': 3306,
    'dbname': 'your_database',
    'dbuser': 'your_database_user',
    'dbpassword': 'your_password',
}
STRATEGY_ORDER = ['行业动量', '抱团拥挤度', '微盘温度计', 'ETF份额', '净利润断层']
STRATEGY_OPTIONS = {}  # 按策略名称传入 run() 参数；固定 skip_existing=True。
PREVIEW_ROWS = 5  # 每个策略展示最后几条新增记录；0 表示仅展示条数。
# Notebook 重复粘贴时沿用当前内核的锁；不创建锁文件。
_RUN_LOCK = globals().get('_RUN_LOCK') or _threading.Lock()
_API_NAMES = ('get_all_securities', 'get_trade_days', 'get_price', 'get_extras',
              'get_industries', 'get_industry_stocks', 'get_industry', 'get_fundamentals',
              'query', 'valuation', 'indicator', 'income', 'finance', 'jy')
_PREVIEW_COLUMNS = {
    '行业动量': ('trade_date', 'industry_name', 'momentum', 'momentum_rank'),
    '抱团拥挤度': ('trade_date', 'top_pct', 'ratio', 'top_money', 'total_money'),
    '微盘温度计': ('trade_date', 'score', 'micro_index', 'sample_count'),
    'ETF份额': ('trade_date', 'fund_code', 'fund_name', 'shares'),
    '净利润断层': ('report_period', 'code', 'name', 'industry_name', 'signal_date', 'profit_yoy'),
}


class BatchRunError(RuntimeError):
    def __init__(self, summary):
        self.summary = summary
        failed = [r['strategy'] for r in summary if r['status'] == 'FAILED']
        super(BatchRunError, self).__init__('采集未全部完成：{}；请查看控制台结果。'.format('、'.join(failed)))


class _QuietStream(_io.TextIOBase):
    """丢弃策略过程输出，不在内存累积历史日志，也不创建日志文件。"""
    def write(self, text):
        return len(text)

    def flush(self):
        pass


@contextmanager
def _batch_guard():
    if not _RUN_LOCK.acquire(False):
        raise RuntimeError('当前 Notebook 内核已有采集任务运行，请等待完成。')
    try:
        yield
    finally:
        _RUN_LOCK.release()


def _load_strategy(name, config, api_namespace):
    import jqdata as _jq
    apis = {key: api_namespace.get(key, getattr(_jq, key, getattr(_b, key, None)))
            for key in _API_NAMES}
    return STRATEGIES[name](apis, config).run


def _safe_error(exc, config):
    message = _re.sub(r'(?i)[a-z][a-z0-9+.-]*://[^\s]+', '[连接地址已隐藏]', str(exc))
    message = _re.sub(r"(?i)(password|passwd|pwd|token|secret)\s*[:=]\s*('.*?'|\".*?\"|[^\s,;]+)",
                      r'\1=[已隐藏]', message)
    secrets = set()
    for key in ('host', 'dbname', 'dbuser', 'dbpassword'):
        value = str(config[key])
        if value:
            secrets.update((value, quote(value, safe=''), quote_plus(value)))
    for value in sorted(secrets, key=len, reverse=True):
        message = message.replace(value, '[已隐藏]')
    return '{}: {}'.format(type(exc).__name__, ' '.join(message.split())[:400])


def run_all(strategy_names=None, options=None, db_config=None, preview_rows=None,
            raise_on_failure=True, api_namespace=None):
    """依次调用文件内策略，只新增；控制台展示结果，失败后继续，最后统一报告。"""
    names = list(STRATEGY_ORDER if strategy_names is None else strategy_names)
    config = dict(DB_CONFIG if db_config is None else db_config)
    options = dict(STRATEGY_OPTIONS if options is None else options)
    preview_rows = PREVIEW_ROWS if preview_rows is None else preview_rows
    if not names or len(names) != len(set(names)) or _b.any(n not in STRATEGIES for n in names):
        raise ValueError('策略列表不能为空、重复或包含未知名称。')
    if set(config) != {'host', 'port', 'dbname', 'dbuser', 'dbpassword'}:
        raise ValueError('DB_CONFIG 必须包含 host、port、dbname、dbuser、dbpassword。')
    if _b.any(n not in STRATEGIES or not isinstance(v, dict) for n, v in options.items()):
        raise ValueError('策略参数必须为按策略名称组织的字典。')
    if _b.any(v.get('skip_existing', True) is not True for v in options.values()):
        raise ValueError('不允许关闭 skip_existing；已有股票及其他明细保持原值。')
    if not isinstance(preview_rows, int) or preview_rows < 0:
        raise ValueError('preview_rows 必须是非负整数。')
    apis = globals() if api_namespace is None else api_namespace
    summary = []
    with _batch_guard():
        for name in names:
            started = _time.monotonic()
            row = dict(strategy=name, status='FAILED', rows=None, error_type=None, error=None)
            result = None
            try:
                with _QuietStream() as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
                    run = _load_strategy(name, config, apis)
                    kwargs = dict(options.get(name, {}), skip_existing=True)
                    result = run(**kwargs)
                    row.update(status='SUCCESS', rows=len(result))
            except KeyboardInterrupt:
                print('【{}】已中断；已提交数据保留，后续策略不再执行。'.format(name))
                raise
            except Exception as exc:
                row.update(error_type=type(exc).__name__, error=_safe_error(exc, config))
            row['elapsed_seconds'] = _b.round(_time.monotonic() - started, 3)
            summary.append(row)
            if row['status'] == 'SUCCESS':
                print('\n【{}】成功：新增 {} 条，耗时 {:.2f} 秒。'.format(name, row['rows'], row['elapsed_seconds']))
                if not row['rows']:
                    print('无新增数据；已有记录跳过，或数据源尚无可入库数据。')
                elif preview_rows:
                    try:
                        columns = [c for c in _PREVIEW_COLUMNS[name] if c in result.columns]
                        print('本次新增结果（最后 {} 条，字段单位沿用策略）：'.format(_b.min(preview_rows, row['rows'])))
                        print(result[columns].tail(preview_rows).to_string(index=False))
                    except Exception:
                        print('结果预览不可用，入库已完成。')
            else:
                print('\n【{}】失败：{}；已提交部分保留，继续后续策略。'.format(name, row['error']))
            del result
        print('\n采集结果汇总\n策略 | 结果 | 新增条数 | 耗时（秒）')
        for row in summary:
            print('{} | {} | {} | {:.2f}'.format(row['strategy'], row['status'],
                  row['rows'] if row['rows'] is not None else '未完成', row['elapsed_seconds']))
        failed = _b.sum(r['status'] == 'FAILED' for r in summary)
        print('成功 {}，失败 {}。'.format(len(summary) - failed, failed))
        if failed and raise_on_failure:
            raise BatchRunError(summary)
    return summary



# ---------- 行业动量 ----------
def _momentum_strategy(_apis, _config):
    """独立作用域隔离模型、参数和函数，返回本策略的接口。"""
    get_all_securities = _apis.get('get_all_securities')
    get_trade_days = _apis.get('get_trade_days')
    get_price = _apis.get('get_price')
    get_industries = _apis.get('get_industries')
    get_industry_stocks = _apis.get('get_industry_stocks')
    query = _apis.get('query')
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
        host = _config['host']
        port = _config['port']
        dbname = _config['dbname']
        dbuser = _config['dbuser']
        dbpassword = _config['dbpassword']

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
    return _Namespace(**locals())


# ---------- 抱团拥挤度 ----------
def _crowding_strategy(_apis, _config):
    """独立作用域隔离模型、参数和函数，返回本策略的接口。"""
    get_all_securities = _apis.get('get_all_securities')
    get_trade_days = _apis.get('get_trade_days')
    get_price = _apis.get('get_price')
    query = _apis.get('query')
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
        host = _config['host']
        port = _config['port']
        dbname = _config['dbname']
        dbuser = _config['dbuser']
        dbpassword = _config['dbpassword']

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

    def save_day(Session, day, top_pct, record, skip_existing=False):
        if record['trade_date'] != as_date(day) or record['top_pct'] != Decimal(str(top_pct)):
            raise ValueError('结果日期或 TOP_PCT 不一致，拒绝入库。')
        session = Session()
        try:
            query = session.query(MarketCrowding).filter_by(
                trade_date=as_date(day), top_pct=Decimal(str(top_pct)))
            if skip_existing:
                if query.first() is not None:
                    return False
            else:
                query.delete(synchronize_session=False)
            session.add(MarketCrowding(**record))
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_last_saved_date(Session, top_pct):
        """利用 (top_pct, trade_date) 主键，仅查询当前口径的最新日期。"""
        session = Session()
        try:
            row = session.query(MarketCrowding.trade_date).filter(
                MarketCrowding.top_pct == Decimal(str(top_pct)),
            ).order_by(MarketCrowding.trade_date.desc()).first()
            return as_date(row[0]) if row is not None else None
        finally:
            session.close()

    def run(start_date=START_DATE, end_date=END_DATE, top_pct=TOP_PCT, incremental=True, skip_existing=False):
        """默认从最后已入库日（含）增量更新；手动补历史时传 incremental=False。"""
        now = china_now()
        today = now.date()
        start = as_date(start_date)
        end = today if end_date is None else as_date(end_date)
        if start > end or end > today:
            raise ValueError('开始日期不能晚于结束日期，结束日期不能晚于运行当天。')
        if skip_existing and end == today and now.hour < 16:
            end = today - datetime.timedelta(days=1)
            print('只新增模式：16:00 前不写入当天未完成快照。')
        pct = Decimal(str(top_pct))
        if not pct.is_finite() or not Decimal('0') < pct <= Decimal('1'):
            raise ValueError('TOP_PCT 必须在 (0, 1] 内。')
        if pct != pct.quantize(Decimal('0.000001')):
            raise ValueError('TOP_PCT 最多支持六位小数。')
        if not isinstance(incremental, bool):
            raise ValueError('incremental 必须是 True 或 False。')
        engine = MysqlCrowding.connect_to_mysql()
        records = []
        try:
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine)
            if incremental:
                last = get_last_saved_date(Session, top_pct)
                if last is not None:
                    if last > today:
                        raise ValueError('数据库最新日期晚于运行当天，请检查已有数据。')
                    start = builtins.max(start, last + datetime.timedelta(days=1) if skip_existing else last)
                print('最后已入库日期：{}；按最新日期增量更新。'.format(last))
            else:
                print('手动补录模式：重算指定区间，不查询历史日期列表。')
            if start > end:
                print('最后已入库日期晚于结束日期，无需增量更新。')
                return pd.DataFrame(columns=RESULT_COLUMNS)
            # 仅请求实际增量区间的日历；全部验证通过后才计算、写入日记录。
            raw_days = get_trade_days(start_date=start, end_date=end)
            if raw_days is None:
                raise ValueError('获取交易日历失败，未写入数据。')
            days = [as_date(day) for day in raw_days]
            previous = None
            for day in days:
                if day < start or day > end or (previous is not None and day <= previous):
                    raise ValueError('交易日历越界、重复或乱序，未写入数据。')
                previous = day
            print('实际采集范围：{} 至 {}；TOP_PCT={}；待计算 {} 个交易日。'.format(
                start, end, top_pct, len(days)))
            if not days:
                print('日期范围没有交易日。')
                return pd.DataFrame(columns=RESULT_COLUMNS)
            # 默认模式刷新末日；skip_existing=True 仅新增，更早缺口不自动扫描。
            for index, day in enumerate(days, 1):
                try:
                    record = calculate_crowding(day, top_pct)
                    written = save_day(Session, day, top_pct, record, skip_existing=skip_existing)
                    if not written:
                        print('{} 已有数据，跳过写入。'.format(day))
                        continue
                except Exception as exc:
                    # 不输出数据库异常全文，避免日志泄露账号、地址或 SQL。
                    print('{} 计算或写入失败（{}），本次停止；已完成日期已提交，修复后重跑可继续。'.format(day, type(exc).__name__))
                    if isinstance(exc, ValueError):
                        raise
                    raise RuntimeError('{} 采集失败，请检查聚宽接口、数据权限和数据库连接。'.format(day)) from None
                records.append(record)
                print('[{}/{}] {} 入库完成：占比 {:.2%}，前 {} / {} 只股票。'.format(
                    index, len(days), day, float(record['ratio']),
                    record['top_stock_count'], record['stock_count']))
        finally:
            engine.dispose()
        return pd.DataFrame(records, columns=RESULT_COLUMNS)
    return _Namespace(**locals())


# ---------- 微盘温度计 ----------
def _microcap_strategy(_apis, _config):
    """独立作用域隔离模型、参数和函数，返回本策略的接口。"""
    get_all_securities = _apis.get('get_all_securities')
    get_trade_days = _apis.get('get_trade_days')
    get_price = _apis.get('get_price')
    get_extras = _apis.get('get_extras')
    get_fundamentals = _apis.get('get_fundamentals')
    query = _apis.get('query')
    valuation = _apis.get('valuation')
    indicator = _apis.get('indicator')
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
        host = _config['host']
        port = _config['port']
        dbname = _config['dbname']
        dbuser = _config['dbuser']
        dbpassword = _config['dbpassword']

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

    def number(value):
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return result if result.is_finite() else None

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

    def run(start_date=START_DATE, end_date=END_DATE, micro_num=MICRO_NUM, sample_step=SAMPLE_STEP, skip_existing=False):
        start = as_date(start_date)
        now = china_now()
        today = now.date()
        end = today if end_date is None else as_date(end_date)
        if start > end or end > today:
            raise ValueError('开始日期不能晚于结束日期，结束日期不能晚于今天。')
        if skip_existing and end == today and now.hour < 16:
            end = today - datetime.timedelta(days=1)
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
    return _Namespace(**locals())


# ---------- ETF份额 ----------
def _etf_strategy(_apis, _config):
    """独立作用域隔离模型、参数和函数，返回本策略的接口。"""
    get_trade_days = _apis.get('get_trade_days')
    query = _apis.get('query')
    finance = _apis.get('finance')
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
        host = _config['host']
        port = _config['port']
        dbname = _config['dbname']
        dbuser = _config['dbuser']
        dbpassword = _config['dbpassword']

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

    def run(start_date=START_DATE, end_date=END_DATE, incremental=True, lookback_days=LOOKBACK_DAYS, groups=None, skip_existing=False):
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
    return _Namespace(**locals())


# ---------- 净利润断层 ----------
def _earnings_strategy(_apis, _config):
    """独立作用域隔离模型、参数和函数，返回本策略的接口。"""
    get_all_securities = _apis.get('get_all_securities')
    get_trade_days = _apis.get('get_trade_days')
    get_price = _apis.get('get_price')
    get_industry = _apis.get('get_industry')
    get_fundamentals = _apis.get('get_fundamentals')
    query = _apis.get('query')
    valuation = _apis.get('valuation')
    indicator = _apis.get('indicator')
    income = _apis.get('income')
    finance = _apis.get('finance')
    jy = _apis.get('jy')
    import builtins

    import datetime as dt

    import json

    import math

    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

    import pandas as pd

    from sqlalchemy import Column, Date, DateTime, Index, Integer, JSON, Numeric, String, Text, create_engine

    from sqlalchemy.engine.url import URL

    from sqlalchemy.ext.declarative import declarative_base

    from sqlalchemy.orm import sessionmaker

    END_DATE = None  # 北京时间运行当天；16:00 前仅使用上一日及以前的完整行情。

    RULE_VERSION = 'announcement-open-v1'

    PAGE_SIZE = 1000

    Base = declarative_base()

    class EarningsRun(Base):
        __tablename__ = 'earnings_gap_run'
        report_period = Column(Date, primary_key=True)
        as_of_date = Column(Date, nullable=False)
        updated_at = Column(DateTime, nullable=False)
        universe_count = Column(Integer, nullable=False)
        matched_count = Column(Integer, nullable=False)
        rule_version = Column(String(32), nullable=False)

    class EarningsStock(Base):
        __tablename__ = 'earnings_gap_stock'
        __table_args__ = (
            Index('ix_earnings_period_industry', 'report_period', 'industry_code'),
            Index('ix_earnings_period_signal', 'report_period', 'signal_date'),
        )
        report_period = Column(Date, primary_key=True)
        code = Column(String(16), primary_key=True)
        name = Column(String(128), nullable=False)
        industry_code = Column(String(32), nullable=True)
        industry_name = Column(String(128), nullable=True)
        industry_level = Column(String(16), nullable=True)
        signal_date = Column(Date, nullable=False)
        announcement_date = Column(Date, nullable=False)
        signal_source = Column(String(16), nullable=False)
        profit_basis = Column(String(32), nullable=False)
        profit_yoy = Column(Numeric(24, 6), nullable=False)
        net_profit = Column(Numeric(28, 4), nullable=True)
        open_gap_pct = Column(Numeric(20, 6), nullable=False)
        intraday_pct = Column(Numeric(20, 6), nullable=False)
        market_cap = Column(Numeric(24, 6), nullable=True)
        circulating_market_cap = Column(Numeric(24, 6), nullable=True)
        pe_ttm = Column(Numeric(24, 6), nullable=True)
        pb = Column(Numeric(24, 6), nullable=True)
        valuation_date = Column(Date, nullable=False)
        # 兼容旧表的非空列；不再生成分析内容，新记录写入空字符串。
        analysis = Column(Text, nullable=False, default='')
        analysis_basis = Column(String(32), nullable=False, default='')
        events = Column(JSON, nullable=False)
        prices = Column(JSON, nullable=False)

    class MysqlEarnings:
        # GitHub 脱敏模板；仅在聚宽私有副本填写真实凭据。
        host = _config['host']
        port = _config['port']
        dbname = _config['dbname']
        dbuser = _config['dbuser']
        dbpassword = _config['dbpassword']

        @classmethod
        def connect_to_mysql(cls):
            url = getattr(URL, 'create', URL)(
                drivername='mysql+mysqldb', host=cls.host, port=cls.port,
                username=cls.dbuser, password=cls.dbpassword, database=cls.dbname,
                query={'charset': 'utf8mb4'})
            engine = create_engine(url, echo=False, pool_size=1, max_overflow=0,
                                  pool_pre_ping=True, pool_recycle=3600,
                                  connect_args={'connect_timeout': 10})
            try:
                with engine.connect():
                    pass
            except Exception:
                engine.dispose()
                raise RuntimeError('MySQL 连接失败，请检查私有配置、网络和白名单。') from None
            return engine

    def china_now():
        return dt.datetime.utcnow() + dt.timedelta(hours=8)

    def as_date(value):
        if isinstance(value, dt.datetime):
            return value.date()
        if isinstance(value, dt.date):
            return value
        return dt.datetime.strptime(str(value)[:10], '%Y-%m-%d').date()

    def latest_period(day):
        # 当前季度开始前一天；10 月运行统一取 9 月 30 日。
        return dt.date(day.year, ((day.month - 1) // 3) * 3 + 1, 1) - dt.timedelta(days=1)

    def number(value):
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except (TypeError, ValueError):
            return None

    def clean(value):
        # 原始字段用于审计；JSON 不允许 NaN/Infinity。
        if isinstance(value, dict):
            return {str(k): clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value]
        if value is None or pd.isna(value):
            return None
        if isinstance(value, (dt.datetime, dt.date)):
            return value.isoformat()
        if isinstance(value, (str, bool)):
            return value
        return number(value)

    def checked_rows(frame, fields, label):
        if frame is None or not set(fields).issubset(frame.columns):
            raise ValueError('{} 返回缺失或字段不完整，取消本次入库。'.format(label))
        return frame.to_dict('records')

    def paged(table, filters, fetch):
        # 主键分页，直到空页；避免平台行数限制造成漏数。
        key = getattr(table, 'id', None)
        if key is None:
            key = getattr(table, 'ID', None)
        if key is None:
            raise ValueError('数据表缺少分页主键，不能保证完整采集。')
        after, rows = -1, []
        while True:
            frame = fetch(query(table).filter(*(filters + [key > after])).order_by(key).limit(PAGE_SIZE))
            page = checked_rows(frame, [key.key], '公告接口')
            if not page:
                return rows
            ids = [int(r[key.key]) for r in page]
            if ids != sorted(set(ids)) or ids[0] <= after:
                raise ValueError('公告分页顺序异常，取消本次入库。')
            rows.extend(page)
            after = ids[-1]

    SOURCE_NAMES = {'express': '业绩快报', 'forecast': '业绩预告', 'formal': '正式财报'}

    def event(code, kind, pub, period, profit, growth, raw, growth_max=None):
        profit, growth = number(profit), number(growth)
        eligible = growth is not None and growth >= 20 and (
            kind == 'forecast' or (profit is not None and profit > 0))
        # 仅明确原因字段作为经营归因，绝不把任意摘要当成原因。
        reason_field = next((f for f in ('change_reason', 'profit_change_reason', 'reason')
                             if isinstance(raw.get(f), str) and raw[f].strip()), None)
        return dict(code=code, kind=kind, publication_date=str(as_date(pub)),
                    report_period=str(period),
                    source_table={'express': 'jy.LC_FSPerformedLetters', 'forecast': 'finance.STK_FIN_FORCAST',
                                  'formal': 'income + indicator'}[kind],
                    net_profit=profit, profit_yoy=growth,
                    profit_yoy_max=number(growth_max), eligible=eligible,
                    profit_basis='单季度' if kind == 'formal' else '报告期累计',
                    reason=raw[reason_field].strip() if reason_field else None,
                    reason_field=reason_field, raw=clean(raw), signal=None)

    def fetch_events(codes, period, as_of):
        events = []
        code_map = {code[:6]: code for code in codes}
        code_set = set(codes)
        t = jy.SecuMain
        mappings = paged(t, [t.SecuCategory == 1], jy.run_query)
        companies = {}
        for row in mappings:
            code = code_map.get(str(row['SecuCode']))
            if code:
                companies.setdefault(row['CompanyCode'], set()).add(code)
        if not companies:
            raise ValueError('A 股公司代码映射为空，不能覆盖数据库。')
        cutoff = as_of + dt.timedelta(days=1)
        t = jy.LC_FSPerformedLetters
        for row in paged(t, [t.EndDate == period, t.InfoPublDate < cutoff], jy.run_query):
            for code in sorted(companies.get(row['CompanyCode'], [])):
                events.append(event(code, 'express', row['InfoPublDate'], period,
                                    row['NPParentCompanyOwners'], row['NPCOwnersYOY'], row))
        print('业绩快报读取完成：{} 条。'.format(len(events)))
        t = finance.STK_FIN_FORCAST
        for row in paged(t, [t.end_date == period, t.pub_date <= as_of], finance.run_query):
            if row['code'] in code_set:
                # 不猜测可选预告金额字段的单位；原始字段完整保存在 raw。
                events.append(event(row['code'], 'forecast', row['pub_date'], period, None,
                                    row['profit_ratio_min'], row, row.get('profit_ratio_max')))
        print('业绩预告读取完成。')
        for start in range(0, len(codes), 300):
            q = query(income.code, income.pubDate.label('publication_date'),
                      income.statDate.label('report_period'),
                      income.np_parent_company_owners.label('net_profit'),
                      indicator.inc_net_profit_to_shareholders_year_on_year.label('profit_yoy'),
                      indicator.pubDate.label('indicator_publication_date'),
                      indicator.statDate.label('indicator_period')).filter(
                          income.code.in_(codes[start:start+300]), income.statDate == period,
                          indicator.statDate == period)
            frame = get_fundamentals(q, date=as_of)
            for row in checked_rows(frame, ['code', 'publication_date', 'report_period', 'net_profit',
                                            'profit_yoy', 'indicator_publication_date', 'indicator_period'], '正式财报'):
                if pd.isna(row['publication_date']):
                    continue
                if as_date(row['report_period']) != period or as_date(row['indicator_period']) != period:
                    raise ValueError('财报与指标的报告期不一致。')
                pub = builtins.max(as_date(row['publication_date']), as_date(row['indicator_publication_date']))
                if pub <= as_of:
                    events.append(event(row['code'], 'formal', pub, period, row['net_profit'], row['profit_yoy'], row))
        print('三类公告合计 {} 条。'.format(len(events)))
        unique = {json.dumps(e, ensure_ascii=False, sort_keys=True): e for e in events}
        return sorted(unique.values(), key=lambda e: (e['code'], e['publication_date'], e['kind']))

    def price_signal(previous_close, open_price, close_price):
        values = [number(v) for v in (previous_close, open_price, close_price)]
        if builtins.any(v is None or v <= 0 for v in values):
            return None
        previous, opening, closing = [Decimal(str(v)) for v in values]
        # 精确等于 3% 不通过，避免二进制浮点边界误差。
        if opening <= previous * Decimal('1.03') or closing < opening:
            return None
        return dict(open_gap_pct=float((opening / previous - 1) * 100),
                    intraday_pct=float((closing / opening - 1) * 100))

    def screen(codes, names, events, period, as_of):
        grouped = {}
        for e in events:
            if e['report_period'] != str(period) or as_date(e['publication_date']) > as_of or e['code'] not in names:
                raise ValueError('公告报告期、股票代码或截止日期异常。')
            grouped.setdefault(e['code'], []).append(e)
        results = []
        for index, (code, disclosures) in enumerate(sorted(grouped.items()), 1):
            qualifying = [e for e in disclosures if e['eligible']]
            if not qualifying:
                continue
            first = builtins.min(as_date(e['publication_date']) for e in qualifying)
            calendar = [as_date(d) for d in get_trade_days(start_date=first-dt.timedelta(days=40), end_date=as_of)]
            if not calendar or calendar != sorted(set(calendar)):
                raise ValueError('交易日历为空或顺序异常。')
            frame = get_price(code, start_date=calendar[0], end_date=as_of, frequency='daily',
                              fields=['open', 'close', 'high', 'low', 'paused', 'volume'],
                              skip_paused=False, fq='pre')
            checked_rows(frame, ['open', 'close', 'high', 'low', 'paused', 'volume'], '股票行情')
            bars = {as_date(day): row.to_dict() for day, row in frame.iterrows()}
            hits = []
            for e in qualifying:
                pub = as_date(e['publication_date'])
                # 原策略的公告日行情；非交易日公告对应下一个交易日，不倒取公告前行情。
                day = next((d for d in calendar if d >= pub), None)
                if day is None:
                    continue
                position = calendar.index(day)
                if position == 0:
                    continue
                previous = calendar[position-1]
                current, prior = bars.get(day), bars.get(previous)
                if current is None or prior is None:
                    raise ValueError('{} 行情缺失，取消本次入库。'.format(code))
                if number(current['paused']) is None:
                    raise ValueError('{} 停牌状态缺失。'.format(code))
                if number(current['volume']) is None:
                    raise ValueError('{} 成交量缺失。'.format(code))
                if current['paused'] or number(current['volume']) == 0:
                    continue
                if builtins.any(number(current[k]) is None or number(current[k]) <= 0 for k in ('open', 'close', 'high', 'low')):
                    raise ValueError('{} 已成交日的价格不完整。'.format(code))
                # 上市前缺失前收盘，不构造跳空信号；不延迟到复牌日追认。
                signal = price_signal(prior['close'], current['open'], current['close'])
                if signal:
                    signal['date'] = str(day)
                    e['signal'] = signal
                    hits.append(e)
            if not hits:
                continue
            selected = sorted(hits, key=lambda e: (e['signal']['date'], e['signal']['open_gap_pct'], e['kind']))[-1]
            industry_data = get_industry([code], date=as_of)
            if not isinstance(industry_data, dict) or code not in industry_data:
                raise ValueError('{} 行业接口返回异常。'.format(code))
            industries = industry_data[code]
            level = next((k for k in ('sw_l3', 'sw_l2', 'sw_l1') if industries.get(k)), None)
            industry = industries.get(level, {})
            signal_day = as_date(selected['signal']['date'])
            offset = calendar.index(signal_day)
            chart_days = calendar[builtins.max(0, offset-10):offset+21]
            prices = [dict(date=str(d), **{k: number(bars[d][k]) for k in ('open', 'close', 'high', 'low')})
                      for d in chart_days if d in bars]
            record = dict(report_period=period, code=code, name=names[code],
                          industry_code=industry.get('industry_code'), industry_name=industry.get('industry_name'),
                          industry_level=level, signal_date=signal_day,
                          announcement_date=as_date(selected['publication_date']), signal_source=selected['kind'],
                          profit_basis=selected['profit_basis'], profit_yoy=selected['profit_yoy'],
                          net_profit=selected['net_profit'], open_gap_pct=selected['signal']['open_gap_pct'],
                          intraday_pct=selected['signal']['intraday_pct'], valuation_date=as_of,
                          events=disclosures, prices=prices)
            results.append(record)
            if index % 25 == 0:
                print('已检查 {} 只披露股票，命中 {} 只。'.format(index, len(results)))
        selected_codes = [r['code'] for r in results]
        valuations = {}
        for start in range(0, len(selected_codes), 300):
            q = query(valuation.code, valuation.market_cap, valuation.circulating_market_cap,
                      valuation.pe_ratio, valuation.pb_ratio).filter(valuation.code.in_(selected_codes[start:start+300]))
            frame = get_fundamentals(q, date=as_of)
            valuations.update({r['code']: r for r in checked_rows(frame,
                ['code', 'market_cap', 'circulating_market_cap', 'pe_ratio', 'pb_ratio'], '估值')})
        for r in results:
            v = valuations.get(r['code'], {})
            for target, source in [('market_cap', 'market_cap'), ('circulating_market_cap', 'circulating_market_cap'),
                                   ('pe_ttm', 'pe_ratio'), ('pb', 'pb_ratio')]:
                r[target] = number(v.get(source))
        return results

    def database_record(record):
        """按表定义量化全部 DECIMAL 列，保留 Decimal 交给 MySQL 驱动。"""
        result = dict(record)
        for column in EarningsStock.__table__.columns:
            if not isinstance(column.type, Numeric):
                continue
            value = record.get(column.name)
            if value is None:
                if not column.nullable:
                    raise ValueError('{} 不允许为空。'.format(column.name))
                result[column.name] = None
                continue
            precision, scale = column.type.precision, column.type.scale
            try:
                decimal_value = Decimal(str(value))
                limit = Decimal(10) ** (precision - scale)
                if not decimal_value.is_finite() or decimal_value.copy_abs() >= limit:
                    raise ValueError('{} 不是有限值或超出字段范围。'.format(column.name))
                with localcontext() as context:
                    context.prec = precision + 2
                    rounded = decimal_value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
                if rounded.copy_abs() >= limit:
                    raise ValueError('{} 四舍五入后超出字段范围。'.format(column.name))
            except (InvalidOperation, TypeError):
                raise ValueError('{} 无法转换为数据库定点数。'.format(column.name)) from None
            result[column.name] = rounded
        return result

    def save_results(Session, period, as_of, universe_count, records, skip_existing=False):
        if len({r['code'] for r in records}) != len(records):
            raise ValueError('筛选结果包含重复股票。')
        for r in records:
            if (r['report_period'] != period or r['valuation_date'] != as_of
                    or r['signal_date'] > as_of or r['announcement_date'] > r['signal_date']
                    or r['open_gap_pct'] <= 3 or r['intraday_pct'] < 0 or r['profit_yoy'] < 20):
                raise ValueError('筛选结果校验失败，拒绝覆盖数据库。')
        # 先验证原始筛选阈值，再量化写入值；任何转换失败均发生在删除旧快照之前。
        # 不修改原始 records 和公告 JSON，也不把 Decimal 转回 float。
        db_records = [database_record(r) for r in records]
        session = Session()
        try:
            if skip_existing:
                # 股票快照只插入首次命中的结果；不覆盖、不删除已入库股票。
                existing = {row[0] for row in session.query(EarningsStock.code).filter(
                    EarningsStock.report_period == period).all()}
                fresh = [record for record in records if record['code'] not in existing]
                fresh_db = [record for record in db_records if record['code'] not in existing]
                run = session.query(EarningsRun).filter(EarningsRun.report_period == period).first()
                total = len(existing) + len(fresh_db)
                # 元数据用于网页展示累计命中数量和扫描截止日；仅值有变化时更新。
                metadata_changed = False
                if run is None:
                    session.add(EarningsRun(report_period=period, as_of_date=as_of, updated_at=china_now(),
                        universe_count=universe_count, matched_count=total, rule_version=RULE_VERSION))
                    metadata_changed = True
                else:
                    values = {'matched_count': total}
                    if as_of >= run.as_of_date:
                        values.update(as_of_date=as_of, universe_count=universe_count, rule_version=RULE_VERSION)
                    for key, value in values.items():
                        if getattr(run, key) != value:
                            setattr(run, key, value)
                            metadata_changed = True
                    if metadata_changed:
                        run.updated_at = china_now()
                if not fresh_db and not metadata_changed:
                    return []
                session.add_all([EarningsStock(**record) for record in fresh_db])
                session.commit()
                return fresh
            # 清空与重建只针对当前季度，并在同一个事务提交，合法零命中也有元数据。
            session.query(EarningsStock).filter(EarningsStock.report_period == period).delete(synchronize_session=False)
            session.query(EarningsRun).filter(EarningsRun.report_period == period).delete(synchronize_session=False)
            session.add_all([EarningsStock(**r) for r in db_records])
            session.add(EarningsRun(report_period=period, as_of_date=as_of, updated_at=china_now(),
                                   universe_count=universe_count, matched_count=len(records), rule_version=RULE_VERSION))
            session.commit()
            return records
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def run(end_date=END_DATE, skip_existing=False):
        now = china_now()
        requested = as_date(end_date) if end_date is not None else now.date()
        if requested > now.date():
            raise ValueError('结束日期不能晚于今天。')
        period = latest_period(requested)
        cutoff = requested - dt.timedelta(days=1) if requested == now.date() and now.hour < 16 else requested
        days = get_trade_days(end_date=cutoff, count=1)
        if days is None or len(days) != 1:
            raise ValueError('无法获取最近完整交易日。')
        as_of = as_date(days[0])
        universe = get_all_securities(types=['stock'], date=as_of)
        if universe is None or universe.empty or 'display_name' not in universe.columns:
            raise ValueError('A 股股票池获取失败。')
        # 平台提供的全部 A 股，不使用原行业白名单、黑名单、ST/持仓或数量限制。
        names = {str(code): str(row['display_name']) for code, row in universe.iterrows()}
        codes = sorted(names)
        print('报告期 {}，行情/披露截止 {}，扫描 {} 只股票。'.format(period, as_of, len(codes)))
        if not skip_existing:
            events = fetch_events(codes, period, as_of)
            records = screen(codes, names, events, period, as_of)
        engine = MysqlEarnings.connect_to_mysql()
        try:
            # 显式指定 InnoDB，确保旧数据库默认引擎不会破坏整批事务。
            for table in Base.metadata.tables.values():
                table.dialect_options['mysql']['engine'] = 'InnoDB'
                table.dialect_options['mysql']['charset'] = 'utf8mb4'
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine)
            if skip_existing:
                session = Session()
                try:
                    existing = {row[0] for row in session.query(EarningsStock.code).filter(
                        EarningsStock.report_period == period).all()}
                finally:
                    session.close()
                pending = [code for code in codes if code not in existing]
                print('本季度已入库 {} 只股票，保留原始快照；仅筛选其余 {} 只。'.format(len(existing), len(pending)))
                events = fetch_events(pending, period, as_of) if pending else []
                records = screen(pending, names, events, period, as_of) if pending else []
            records = save_results(Session, period, as_of, len(codes), records, skip_existing=skip_existing)
        finally:
            engine.dispose()
        print('完成：{} 只股票{}入库；其他季度数据保留。'.format(len(records), '新增' if skip_existing else '已'))
        return pd.DataFrame(records)
    return _Namespace(**locals())


STRATEGIES = {
    '行业动量': _momentum_strategy,
    '抱团拥挤度': _crowding_strategy,
    '微盘温度计': _microcap_strategy,
    'ETF份额': _etf_strategy,
    '净利润断层': _earnings_strategy,
}

if __name__ == '__main__':
    batch_summary = run_all()
