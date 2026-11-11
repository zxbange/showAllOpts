# -*- coding: utf-8 -*-
"""聚宽研究 Notebook 独立历史补采。填写 mysql_connector.py 和顶部日期配置后，整段执行即采集并入库。

仅使用平台内置 jqdata，不使用外部 JQData SDK、账号认证或交易所 HTTP 接口。
默认自动创建缺少的数据表、只新增缺失记录；已有数据不覆盖。
只需预览时将 WRITE_DATABASE 改为 False；源缺失是否允许部分入库由 ALLOW_PARTIAL 控制。
完整说明见 docs/etf-joinquant-history.md；不替代后续交易所每日自动采集。
"""
from __future__ import print_function, division

# Notebook 的共享命名空间可能被 NumPy 通配导入覆盖，显式使用 Python 内置函数。
import builtins as _builtins
import datetime as dt
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from sqlalchemy import BigInteger, Column, Date, DateTime, Numeric, String
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

START_DATE = None  # 自动从所选跟踪基金最早上市日扫描；不猜测份额源起点。
END_DATE = None  # 北京时间昨日；当天无论是否收盘均留给交易所日更。
WINDOW_SIZE = 120  # 单只基金、每次最多 120 个交易日，远低于 finance 查询上限。
WRITE_DATABASE = True  # 整段执行即入库；False 为仅预览、不连接数据库。
CREATE_TABLES = True  # 自动创建缺少的三张表；数据库本身需提前创建，账号须有相应权限。
ALLOW_PARTIAL = None  # None：自动全历史允许部分有效记录并持久记录缺口；显式日期仍严格。
from mysql_connector import get_database_config, get_database_options, mysql_drivername, engine_options
DB_CONFIG = get_database_config()
DB_OPTIONS = get_database_options()
# 单文件不能依赖项目安装。此名单由离线测试与 backend/app/core/etf_catalog.py 校验一致。
ETF_GROUPS = {
    'hs300': [('510300.XSHG', '沪深300ETF华泰柏瑞'), ('510310.XSHG', '沪深300ETF易方达'),
              ('510330.XSHG', '沪深300ETF华夏'), ('159919.XSHE', '沪深300ETF嘉实'),
              ('515330.XSHG', '沪深300ETF天弘')],
    'zz500': [('510500.XSHG', '中证500ETF南方'), ('510510.XSHG', '中证500ETF广发'),
              ('510580.XSHG', '中证500ETF易方达'), ('512500.XSHG', '中证500ETF华夏'),
              ('159922.XSHE', '中证500ETF嘉实')],
    'cyb': [('159915.XSHE', '创业板ETF易方达')],
    'kc50': [('588000.XSHG', '科创50ETF华夏'), ('588080.XSHG', '科创50ETF易方达')],
    'zz1000': [('512100.XSHG', '中证1000ETF南方'), ('159845.XSHE', '中证1000ETF华夏'),
               ('560010.XSHG', '中证1000ETF广发')],
    'sz50': [('510050.XSHG', '上证50ETF华夏')],
    'sz180': [('510180.XSHG', '上证180ETF华安')],
}
SHARE_SOURCE = 'JQDATA.FUND_SHARE_DAILY'
PRICE_SOURCE = 'JQDATA.GET_PRICE_UNADJUSTED'
CALENDAR_SOURCE = 'JQDATA.GET_TRADE_DAYS'
Base = declarative_base()


class ETFShare(Base):
    __tablename__ = 'etf_share_daily'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
    fund_code = Column(String(16), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    fund_name = Column(String(128), nullable=False)
    shares = Column(BigInteger, nullable=False)
    previous_trade_date = Column(Date, nullable=False)
    source = Column(String(48), nullable=False)
    updated_at = Column(DateTime, nullable=False)


class ETFPrice(Base):
    __tablename__ = 'etf_price_daily'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
    fund_code = Column(String(16), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    close_price = Column(Numeric(20, 8), nullable=False)
    source = Column(String(48), nullable=False)
    updated_at = Column(DateTime, nullable=False)


class ETFTradingDay(Base):
    __tablename__ = 'etf_trading_day'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
    trade_date = Column(Date, primary_key=True)
    previous_trade_date = Column(Date, nullable=True)
    source = Column(String(48), nullable=False)
    updated_at = Column(DateTime, nullable=False)


class ETFHistoryCoverage(Base):
    __tablename__ = 'etf_history_coverage'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
    fund_code = Column(String(16), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    share_status = Column(String(32), nullable=False)
    price_status = Column(String(32), nullable=False)
    checked_at = Column(DateTime, nullable=False)


MODELS = (ETFShare, ETFPrice, ETFTradingDay)


class HistoryError(RuntimeError):
    """Only fixed, credential-free diagnostic text may be included here."""


def china_now():
    return dt.datetime.utcnow() + dt.timedelta(hours=8)


def as_date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.datetime.strptime(str(value), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        raise HistoryError('日期必须是 YYYY-MM-DD 或 date/datetime 对象。') from None


def load_api():
    try:
        import jqdata as jq
    except ImportError:
        raise HistoryError('请在聚宽研究 Notebook 执行；不需要安装或登录 jqdatasdk。') from None
    # 聚宽部分研究接口注入 Notebook/builtins，未必是 jqdata 模块的属性。
    resolved, missing = {}, []
    for name in ('query', 'get_trade_days', 'get_price', 'get_security_info', 'finance'):
        for value in (globals().get(name), getattr(jq, name, None), getattr(_builtins, name, None)):
            valid = callable(getattr(value, 'run_query', None)) if name == 'finance' else callable(value)
            if valid:
                resolved[name] = value
                break
        else:
            missing.append(name)
    if missing:
        raise HistoryError('聚宽研究环境缺少必需 API：{}。请在研究 Notebook 中执行。'.format(', '.join(missing)))
    table = getattr(resolved['finance'], 'FUND_SHARE_DAILY', None)
    if table is None or _builtins.any(not hasattr(table, key) for key in ('code', 'date', 'shares')):
        raise HistoryError('缺少 finance.FUND_SHARE_DAILY 的 code/date/shares，请核对数据权限及字段。')
    return SimpleNamespace(**resolved)


def checked_days(raw, start, end, count=None):
    if raw is None:
        raise HistoryError('交易日历未返回有效数据。')
    days = [as_date(day) for day in raw]
    if (days != sorted(set(days)) or _builtins.any(day < start or day > end for day in days)
            or (count is not None and len(days) != count)):
        raise HistoryError('交易日历乱序、重复、越界或长度异常；不能用工作日猜测。')
    return days


def calendar(jq, start, end):
    days = checked_days(jq.get_trade_days(start_date=start, end_date=end), start, end)
    if not days:
        raise HistoryError('指定范围没有返回交易日，请核对日期或数据权限。')
    cross = checked_days(jq.get_trade_days(end_date=days[-1], count=len(days)),
                         start, days[-1], count=len(days))
    if cross != days:
        raise HistoryError('交易日历区间与倒序计数交叉校验不一致。')
    # 额外取首日前两日：一日用于基线数据，一日确定基线的真实前日。
    prior = checked_days(jq.get_trade_days(end_date=days[0], count=3),
                         dt.date(1900, 1, 1), days[0], count=3)
    latest = checked_days(jq.get_trade_days(end_date=end, count=1),
                          dt.date(1900, 1, 1), end, count=1)
    if prior[-1] != days[0] or latest[-1] != days[-1]:
        raise HistoryError('交易日历首尾交叉校验失败。')
    data_days = prior[-2:-1] + days
    before = dict(zip(data_days, prior[-3:-2] + data_days[:-1]))
    return data_days, before


def decimal_value(value, label):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise HistoryError('{}不是有效数值。'.format(label)) from None
    if not number.is_finite():
        raise HistoryError('{}缺失或非有限数值。'.format(label))
    return number


def normalize_shares(value):
    number = decimal_value(value, '份额')
    if number < 0 or number != number.to_integral_value() or number > 9223372036854775807:
        raise HistoryError('份额须为非负整数份且不超出 BIGINT；不得把万份或市值直接入库。')
    return _builtins.int(number)


def normalize_close(value):
    number = decimal_value(value, '未复权收盘价')
    if number <= 0 or number >= Decimal('1000000000000'):
        raise HistoryError('未复权收盘价须大于0且适合 NUMERIC(20,8)。')
    if number != number.quantize(Decimal('0.00000001')):
        raise HistoryError('未复权收盘价超过8位小数；停止而非静默舍入。')
    return number


def fetch_shares(jq, code, name, days, before, stamp):
    table = jq.finance.FUND_SHARE_DAILY
    frame = jq.finance.run_query(jq.query(table.code, table.date, table.shares).filter(
        table.code == code, table.date >= days[0], table.date <= days[-1],
    ).order_by(table.date).limit(len(days) + 1))
    if frame is None or not {'code', 'date', 'shares'}.issubset(frame.columns):
        raise HistoryError('份额查询返回格式异常或缺少 code/date/shares。')
    result, seen, allowed = [], set(), set(days)
    for row in frame.to_dict('records'):
        day = as_date(row['date'])
        if row['code'] != code or day not in allowed or day in seen:
            raise HistoryError('份额响应代码不符、日期重复或不在请求交易日内。')
        seen.add(day)
        result.append(dict(fund_code=code, fund_name=name, trade_date=day,
                           shares=normalize_shares(row['shares']), previous_trade_date=before[day],
                           source=SHARE_SOURCE, updated_at=stamp))
    return result


def fetch_prices(jq, code, days, stamp):
    # 单只证券返回带日期索引的 DataFrame；不依赖新版本 panel/fill_paused 参数。
    # skip_paused=True 禁止聚宽用停牌前价格填充；fq 必须是 Python None，不是字符串。
    frame = jq.get_price(code, start_date=days[0], end_date=days[-1], frequency='daily',
                         fields=['close', 'paused'], skip_paused=True, fq=None)
    if frame is None or not {'close', 'paused'}.issubset(frame.columns):
        raise HistoryError('行情查询返回格式异常或缺少 close/paused。')
    result, seen, allowed = [], set(), set(days)
    for index, row in frame.iterrows():
        day = as_date(index)
        if day not in allowed or day in seen or ('code' in frame.columns and row['code'] != code):
            raise HistoryError('行情响应代码不符、日期重复或不在请求交易日内。')
        seen.add(day)
        paused = decimal_value(_builtins.int(row['paused'])
                               if isinstance(row['paused'], _builtins.bool) else row['paused'], '停牌标记')
        if paused not in (0, 1):
            raise HistoryError('停牌标记不是0或1。')
        if paused == 1:
            continue
        result.append(dict(fund_code=code, trade_date=day,
                           close_price=normalize_close(row['close']), source=PRICE_SOURCE, updated_at=stamp))
    return result


def quality_warnings(shares, prices, before, days):
    """与网页保守拆并筛查同口径；仅提示，不猜测复权因子或改写原始数据。"""
    units = {(row['fund_code'], row['trade_date']): row['shares'] for row in shares}
    closes = {(row['fund_code'], row['trade_date']): row['close_price'] for row in prices}
    following = dict(zip(days, days[1:]))
    flags = set()
    for (code, day), current in units.items():
        previous = units.get((code, before[day]))
        if previous is not None and previous > 0 and (current * 2 >= previous * 3 or current * 3 <= previous * 2):
            for target in (day, following.get(day)):
                if target in before:
                    flags.add((code, target, 'possible_share_unit_change'))
        close, prior = closes.get((code, day)), closes.get((code, before[day]))
        if close is not None and prior is not None and (close * 4 >= prior * 5 or close * 5 <= prior * 4):
            for target in (day, before[day]):
                if target in before:
                    flags.add((code, target, 'possible_price_unit_change'))
    return sorted(flags)


def connect_database(config):
    if _builtins.any(not config.get(key) or str(config[key]).startswith('your_')
           for key in ('host', 'dbname', 'dbuser', 'dbpassword')):
        raise HistoryError('请在 mysql_connector.py 中填写 DB_CONFIG，不要把凭据发到聊天或提交到仓库。')
    driver = config.get('driver', DB_OPTIONS['driver'])
    if driver not in ('auto', 'mysqldb', 'pymysql'):
        raise HistoryError('driver 仅支持 auto、mysqldb 或 pymysql。')
    stage = '加载驱动'
    engine = None
    try:
        drivername = mysql_drivername(driver)
        stage = '构造连接地址'
        make_url = getattr(URL, 'create', URL)
        url = make_url(drivername=drivername, host=config['host'],
                       port=_builtins.int(config.get('port', DB_CONFIG['port'])), database=config['dbname'],
                       username=config['dbuser'], password=config['dbpassword'],
                       query={'charset': DB_OPTIONS['charset']})
        stage = '初始化连接引擎'
        # 沿用批量采集的连接参数；旧版 SQLAlchemy 不接受 hide_parameters 构造参数。
        engine = create_engine(url, **engine_options(DB_OPTIONS))
        if hasattr(engine, 'hide_parameters'):
            engine.hide_parameters = True
        stage = '建立数据库连接'
        with engine.connect():
            pass
        return engine
    except Exception as exc:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass
        hint = ('请检查 SQLAlchemy/驱动版本及连接参数类型。' if isinstance(exc, TypeError)
                else '请检查驱动、网络白名单、账号及数据库。')
        raise HistoryError('MySQL {}失败（{}）；{}'.format(stage, type(exc).__name__, hint)) from None


def check_schema(engine, create_tables=False, models=MODELS):
    inspector = inspect(engine)
    missing = []
    table_names = set(inspector.get_table_names())  # 兼容聚宽较旧的 SQLAlchemy 1.3。
    for model in models:
        table = model.__table__
        if table.name not in table_names:
            missing.append(table)
            continue
        existing = {column['name'] for column in inspector.get_columns(table.name)}
        expected = {column.name for column in table.columns}
        pk = set(inspector.get_pk_constraint(table.name).get('constrained_columns') or [])
        if not expected.issubset(existing) or pk != {column.name for column in table.primary_key}:
            raise HistoryError('{} 字段或主键不兼容；请先按项目表结构处理，不自动改表。'.format(table.name))
        if engine.dialect.name == 'mysql':
            options = inspector.get_table_options(table.name)
            if str(options.get('mysql_engine', '')).lower() != 'innodb':
                raise HistoryError('{} 不是 InnoDB，不能保证事务回滚。'.format(table.name))
    if missing and not create_tables:
        raise HistoryError('缺少表 {}；默认不建表，可让管理员建表或显式 create_tables=True。'.format(
            ', '.join(table.name for table in missing)))
    for table in missing:
        table.create(engine, checkfirst=True)


def save_records(engine, shares, prices, sessions, create_tables=False, coverage=None):
    """只 INSERT 缺失主键，无 UPDATE/DELETE/REPLACE；全批事务失败则回滚。

    并发采集抢先插入同一主键时，整个事务失败后由用户重新运行，不覆盖对方数据。
    """
    session = None
    try:
        check_schema(engine, create_tables=create_tables)
        if coverage is not None:
            check_schema(engine, create_tables=create_tables, models=(ETFHistoryCoverage,))
        session = sessionmaker(bind=engine)()
        counts = {}
        for model, records in zip(MODELS, (shares, prices, sessions)):
            name = model.__tablename__
            counts[name] = {'inserted': 0, 'preserved': 0, 'different_preserved': 0}
            if not records:
                continue
            def identity(row):
                return (row['fund_code'], row['trade_date']) if model is not ETFTradingDay else row['trade_date']
            keyed = {identity(row): row for row in records}
            if len(keyed) != len(records):
                raise HistoryError('待写入批次存在重复主键。')
            dates = [row['trade_date'] for row in records]
            query = session.query(model).filter(model.trade_date >= _builtins.min(dates),
                                                 model.trade_date <= _builtins.max(dates))
            if model is not ETFTradingDay:
                query = query.filter(model.fund_code.in_(sorted({row['fund_code'] for row in records})))
            existing = {}
            for row in query.all():
                key = (row.fund_code, row.trade_date) if model is not ETFTradingDay else row.trade_date
                existing[key] = row
            pending = []
            for key, record in keyed.items():
                old = existing.get(key)
                if old is None:
                    pending.append(record)
                    continue
                counts[name]['preserved'] += 1
                fields = ('shares', 'previous_trade_date') if model is ETFShare else (
                    ('close_price',) if model is ETFPrice else ('previous_trade_date',))
                if _builtins.any(getattr(old, field) != record[field] for field in fields):
                    if model is ETFTradingDay:
                        raise HistoryError('已有交易日历与聚宽前一交易日冲突；未覆盖，停止本次入库。')
                    counts[name]['different_preserved'] += 1
            for offset in range(0, len(pending), 200):
                session.execute(model.__table__.insert(), pending[offset:offset + 200])
            counts[name]['inserted'] = len(pending)
        for record in coverage or []:
            row = session.query(ETFHistoryCoverage).filter_by(
                fund_code=record['fund_code'], trade_date=record['trade_date']).first()
            if row is None:
                session.add(ETFHistoryCoverage(**record))
            else:
                row.share_status, row.price_status, row.checked_at = (
                    record['share_status'], record['price_status'], record['checked_at'])
        session.commit()
        return counts
    except Exception as exc:
        if session is not None:
            try:
                session.rollback()
            except Exception:
                pass  # 原始驱动错误可能含连接细节；仍返回下面的安全诊断。
        if isinstance(exc, HistoryError):
            raise
        raise HistoryError('数据库写入未确认成功，事务已请求回滚（{}）；请核对权限/表结构后重跑。'
                           '若提交时连接中断，请重跑幂等补缺确认结果。'.format(type(exc).__name__)) from None
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def run(start_date=START_DATE, end_date=END_DATE, groups=None, write=WRITE_DATABASE,
        allow_partial=ALLOW_PARTIAL, create_tables=CREATE_TABLES, db_config=None):
    """采集并按顶部配置入库，返回记录、源缺失、拆并风险及计数；write=False 仅预览。"""
    automatic_history = start_date is None
    if allow_partial is None:
        allow_partial = automatic_history
    if _builtins.any(not isinstance(value, _builtins.bool) for value in (write, allow_partial, create_tables)):
        raise HistoryError('write/allow_partial/create_tables 必须是布尔值。')
    if (isinstance(WINDOW_SIZE, _builtins.bool) or not isinstance(WINDOW_SIZE, _builtins.int)
            or not 1 <= WINDOW_SIZE <= 120):
        raise HistoryError('WINDOW_SIZE 必须是1至120的整数。')
    start = None if start_date is None else as_date(start_date)
    yesterday = china_now().date() - dt.timedelta(days=1)
    end = yesterday if end_date is None else as_date(end_date)
    if (start is not None and start > end) or end > yesterday:
        raise HistoryError('开始日期不能晚于结束日期；历史补采截止日期最多为北京时间昨日。')
    selected = list(ETF_GROUPS) if groups is None else list(groups)
    if (not selected or len(selected) != len(set(selected))
            or _builtins.any(group not in ETF_GROUPS for group in selected)):
        raise HistoryError('groups 必须是不重复的有效分组列表。')
    funds = dict(pair for group in selected for pair in ETF_GROUPS[group])
    shares, prices = [], []
    try:
        jq = load_api()
        infos = {code: jq.get_security_info(code) for code in funds}
        if _builtins.any(info is None or getattr(info, 'start_date', None) is None
                         or getattr(info, 'end_date', None) is None for info in infos.values()):
            raise HistoryError('无法核实基金上市/退市日期，停止采集。')
        if start is None:
            start = _builtins.min(as_date(info.start_date) for info in infos.values())
        if start > end:
            raise HistoryError('所选基金均未在结束日前上市。')
        days, before = calendar(jq, start, end)
        end = days[-1]  # 昨日为周末/节假日时，实际终点取已发生的最近交易日。
        stamp = china_now()
        expected, reference_expected, listing_ranges = set(), set(), {}
        print('范围 {} 至 {}；前日 {} 仅作内存参考、不入库；{} 只ETF。'.format(start, end, days[0], len(funds)))
        for number, (code, name) in enumerate(funds.items(), 1):
            info = infos[code]
            if info is None or getattr(info, 'start_date', None) is None or getattr(info, 'end_date', None) is None:
                raise HistoryError('无法核实基金上市/退市日期，停止采集。')
            if getattr(info, 'code', code) != code:
                raise HistoryError('基金基本信息代码不符。')
            listed, delisted = as_date(info.start_date), as_date(info.end_date)
            if listed > delisted:
                raise HistoryError('基金上市日期晚于退市日期。')
            listing_ranges[code] = (listed, delisted)
            active_days = [day for day in days if listed <= day <= delisted]
            expected.update((code, day) for day in active_days if day >= start)
            reference_expected.update((code, day) for day in active_days if day < start)
            own_shares, own_prices = [], []
            for offset in range(0, len(active_days), WINDOW_SIZE):
                window = active_days[offset:offset + WINDOW_SIZE]
                own_shares.extend(fetch_shares(jq, code, name, window, before, stamp))
                own_prices.extend(fetch_prices(jq, code, window, stamp))
            shares.extend(own_shares)
            prices.extend(own_prices)
            print('[{}/{}] {}：份额 {}，未复权收盘价 {}。'.format(
                number, len(funds), code, len(own_shares), len(own_prices)))
    except HistoryError:
        raise
    except Exception as exc:
        raise HistoryError('聚宽数据采集失败（{}），本次尚未连接或写入 MySQL；请检查权限、配额及环境。'.format(
            type(exc).__name__)) from None
    share_keys = {(row['fund_code'], row['trade_date']) for row in shares}
    price_keys = {(row['fund_code'], row['trade_date']) for row in prices}
    missing = {'shares': sorted(expected - share_keys), 'prices': sorted(expected - price_keys)}
    reference_missing = {'shares': sorted(reference_expected - share_keys),
                         'prices': sorted(reference_expected - price_keys)}
    warnings = quality_warnings(shares, prices, before, days)
    unit_values = {(row['fund_code'], row['trade_date']): row['shares'] for row in shares}
    close_values = {(row['fund_code'], row['trade_date']): row['close_price'] for row in prices}
    # 首日前一交易日不入库。若这一边界发生疑似拆并或参考缺失，网页无法重新推导风险，
    # 则隔离本次该日价格，宁可缺数，也不能呈现未经筛查的合计市值。
    quarantined = set()
    for code, day in expected:
        reference = (code, before[day])
        if before[day] < start and reference in reference_expected:
            current, previous = unit_values.get((code, day)), unit_values.get(reference)
            close, prior = close_values.get((code, day)), close_values.get(reference)
            share_risk = (current is not None and previous is not None and previous > 0
                          and (current * 2 >= previous * 3 or current * 3 <= previous * 2))
            price_risk = (close is not None and prior is not None
                          and (close * 4 >= prior * 5 or close * 5 <= prior * 4))
            if share_risk or price_risk or reference not in share_keys or reference not in price_keys:
                quarantined.add((code, day))
    shares = [row for row in shares if row['trade_date'] >= start]
    prices = [row for row in prices if row['trade_date'] >= start]
    write_prices = [row for row in prices if (row['fund_code'], row['trade_date']) not in quarantined]
    sessions = [dict(trade_date=day, previous_trade_date=before[day],
                     source=CALENDAR_SOURCE, updated_at=stamp) for day in days if day >= start]
    coverage = {}
    for code in funds:
        coverage[code] = {}
        for kind, keys in (('shares', share_keys), ('prices', price_keys)):
            observed = sorted(day for fund, day in keys if fund == code and day >= start)
            absent = [day for fund, day in missing[kind] if fund == code]
            coverage[code][kind] = dict(first_observed=observed[0] if observed else None,
                last_observed=observed[-1] if observed else None, missing_count=len(absent),
                coverage_status='complete_requested_listing_range' if not absent and observed else 'unknown_or_partial')
        print('{} 分源覆盖：{}'.format(code, coverage[code]))
    result = dict(source_coverage=coverage, start_date=start, end_date=end, reference_date=days[0], shares=shares, prices=prices,
                  trading_days=sessions, missing=missing, reference_missing=reference_missing,
                  listing_ranges=listing_ranges, quality_warnings=warnings,
                  quarantined_prices=sorted(quarantined),
                  source_complete=not _builtins.any(missing.values()) and not _builtins.any(reference_missing.values()),
                  written=False, counts={}, coverage_persisted=False,
                  completion_status='complete' if not _builtins.any(missing.values()) and not _builtins.any(reference_missing.values()) and not quarantined else 'partial_unknown')
    print('源数据缺失：份额 {}，价格 {}。份额按“份”，价格按“元/份”，不补零或前向填充。'.format(
        len(missing['shares']), len(missing['prices'])))
    for kind in ('shares', 'prices'):
        if missing[kind]:
            print('{} 缺失前20项：{}'.format(kind, missing[kind][:20]))
    if _builtins.any(reference_missing.values()):
        print('首日前日参考缺失：{}'.format(reference_missing))
    if quarantined:
        print('首日价格隔离（本次不新增，已有记录不动）：{}'.format(sorted(quarantined)))
    if warnings:
        print('发现 {} 项份额/价格单位变化风险，前20项：{}'.format(len(warnings), warnings[:20]))
        print('原始记录保留供网页拆并防护使用；不能将这些风险当作已确认拆并或自动复权。')
    if not write:
        print('仅预览：未连接 MySQL、未建表、未写库。需要入库时将顶部 WRITE_DATABASE 改为 True 后重新运行整段脚本。')
        return result
    if not result['source_complete'] and not allow_partial:
        print('存在源缺失，本次未入库。确认接受缺失后将顶部 ALLOW_PARTIAL 改为 True，重新运行整段脚本即可保存有效记录。')
        return result
    engine = connect_database(DB_CONFIG if db_config is None else db_config)
    try:
        if automatic_history:
            coverage_rows = [dict(fund_code=code, trade_date=day,
                share_status='observed' if (code, day) in share_keys else 'source_missing_unknown',
                price_status=('quarantined' if (code, day) in quarantined else
                    'observed' if (code, day) in price_keys else 'source_missing_unknown'),
                checked_at=stamp) for code, day in sorted(expected)]
            for offset in range(0, len(sessions), WINDOW_SIZE):
                batch = sessions[offset:offset + WINDOW_SIZE]
                dates = {row['trade_date'] for row in batch}
                counts = save_records(engine,
                    [row for row in shares if row['trade_date'] in dates],
                    [row for row in write_prices if row['trade_date'] in dates], batch,
                    create_tables=create_tables,
                    coverage=[row for row in coverage_rows if row['trade_date'] in dates])
                for table, values in counts.items():
                    total = result['counts'].setdefault(table, dict.fromkeys(values, 0))
                    for key, value in values.items():
                        total[key] += value
        else:
            result['counts'] = save_records(engine, shares, write_prices, sessions, create_tables=create_tables)
        result['written'] = True
        result['coverage_persisted'] = automatic_history
    finally:
        engine.dispose()
    for table, counts in result['counts'].items():
        print('{}：新增 {}，保留已有 {}，其中值不同但仍保留 {}。'.format(
            table, counts['inserted'], counts['preserved'], counts['different_preserved']))
    print('有效记录入库完成；覆盖状态：{}。未知缺口不等于零或全历史完成。'.format(result['completion_status']))
    print('已有记录、来源及更新时间均未覆盖；请在项目中手动同步 ETF 本地数据。')
    return result


if __name__ == '__main__':
    RESULT = run()
