# -*- coding: utf-8 -*-
"""聚宽研究：高股息两组选股 Top25；完整单文件，只保留最新结果，不下单。"""
import builtins
import datetime
import math
import re
import sys
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from types import SimpleNamespace
from urllib.parse import quote, quote_plus
import jqdata as jq
import pandas as pd
from sqlalchemy import Column, Date, DateTime, Index, Integer, Numeric, String, and_, or_, create_engine
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

STRATEGY_CONFIG = {'高股息选股': {'end_date': None, 'batch_size': 300, 'page_size': 2000}}

DB_CONFIG = {
    'host': 'your_mysql_host',
    'port': 3306,
    'dbname': 'your_database',
    'dbuser': 'your_database_user',
    'dbpassword': 'your_password',
}


DB_OPTIONS = {'connect_timeout': 10, 'retry_count': 5, 'retry_interval': 10, 'pool_recycle': 3600}


PROGRESS_ENABLED = True  # 控制台进度条；关闭后仍保留各策略结果和最终汇总。


PROGRESS_INTERVAL_SECONDS = 5.0  # 最多每隔多少秒刷新；等待接口/数据库时也刷新耗时。


_PROGRESS_LOCAL = threading.local()


class _ConsoleProgress:
    """仅后台刷新文字；采集与数据库操作始终在主线程串行执行。"""
    def __init__(self, name, index, total, stream, interval):
        self.title = '【{}/{} {}】'.format(index, total, name)
        self.stream, self.interval = stream, interval
        self.started = time.monotonic()
        self.last_refresh, self.width = 0.0, 0
        self.frames = []
        self.note = '初始化策略'
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.thread = None
        self.writable = True

    def render(self, force=False, ending=None):
        with self.lock:
            now = time.monotonic()
            if not self.writable or (not force and now - self.last_refresh < self.interval):
                return
            parts = []
            for frame in self.frames:
                total, done = frame['total'], frame['done']
                filled = int(20 * done / total) if total else 0
                bar = '[' + '#' * filled + '-' * (20 - filled) + ']'
                value = '{}/{} {}%'.format(done, total, 100*done//total) if total else '0/0 无待处理项'
                parts.append('{} {} {} {}'.format(frame['label'], bar, value, frame['item']))
            line = '{} {} | {} | 已用 {:.0f}s'.format(
                self.title, ' > '.join(parts) or ('[结束]' if ending else '[处理中]'), self.note, now-self.started)
            if ending:
                line += ' | ' + ending
            try:
                self.stream.write('\r' + line + ' ' * builtins.max(0, self.width-len(line)) + ('\n' if ending else ''))
                self.stream.flush()
            except Exception:
                # 显示失败不改变采集结果，也不打印可能含连接信息的异常。
                self.writable = False
            self.width, self.last_refresh = len(line), now

    def heartbeat(self):
        while not self.stopped.wait(self.interval):
            self.render(force=True)

    def start(self):
        self.render(force=True)
        self.thread = threading.Thread(target=self.heartbeat, name='collector-progress')
        self.thread.daemon = True
        try:
            self.thread.start()
        except Exception:
            # 某些研究容器限制线程：退化为任务推进时刷新，不阻断采集。
            self.thread = None
            self.note = '自动定时刷新不可用，按任务推进刷新'
            self.render(force=True)

    def close(self, ending):
        self.stopped.set()
        if self.thread is not None and self.thread.ident is not None:
            self.thread.join()
        self.render(force=True, ending=ending)


@contextmanager
def _progress_scope(name, index, total):
    progress = None
    ending = '完成'
    try:
        if PROGRESS_ENABLED:
            progress = _ConsoleProgress(name, index, total, sys.stdout, PROGRESS_INTERVAL_SECONDS)
            _PROGRESS_LOCAL.current = progress
            progress.start()
        yield
    except BaseException as exc:
        ending = '已中断' if isinstance(exc, KeyboardInterrupt) else '未完成'
        raise
    finally:
        _PROGRESS_LOCAL.current = None
        if progress is not None:
            progress.close(ending)


def _progress_note(message):
    progress = getattr(_PROGRESS_LOCAL, 'current', None)
    if progress is not None:
        with progress.lock:
            progress.note = message
            progress.render()


def _progress_items(items, label, describe=str):
    """仅循环体正常返回后计为已处理；不是新增入库条数。"""
    progress = getattr(_PROGRESS_LOCAL, 'current', None)
    if progress is None:
        yield from items
        return
    frame = dict(label=label, total=len(items), done=0, item='')
    with progress.lock:
        progress.frames.append(frame)
        progress.note = '准备处理'
        progress.render(force=True)
    try:
        for item in items:
            with progress.lock:
                frame['item'] = describe(item)
                progress.note = '采集中'
                progress.render(force=frame['done'] == 0)
            yield item
            with progress.lock:
                frame['done'] += 1
                progress.note = '本项已处理'
                progress.render(force=frame['done'] == frame['total'])
    finally:
        with progress.lock:
            progress.frames.remove(frame)


def strategy_settings(name):
    """每次运行取得配置快照，先检查控制分页/窗口的参数以免出现空循环。"""
    settings = deepcopy(STRATEGY_CONFIG[name])
    for key in ('retention_trade_days', 'batch_size', 'initial_trade_days', 'window_size', 'page_size', 'initial_days', 'price_batch_size'):
        value = settings.get(key)
        if key in settings and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise ValueError('{} 的 {} 必须为正整数。'.format(name, key))
    return settings


def china_now():
    """所有策略统一按北京时间确定运行日期。"""
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.datetime.strptime(str(value)[:10], '%Y-%m-%d').date()


def connect_database(config):
    """统一建立连接、有限重试；调用策略负责最终释放 engine。"""
    opts = DB_OPTIONS
    if (not isinstance(opts['retry_count'], int) or opts['retry_count'] < 1
            or opts['retry_interval'] < 0 or opts['connect_timeout'] <= 0
            or opts['pool_recycle'] <= 0):
        raise ValueError('DB_OPTIONS 的次数、超时须为正，重试间隔不可为负。')
    url = getattr(URL, 'create', URL)(
        drivername='mysql+mysqldb', host=config['host'], port=config['port'],
        username=config['dbuser'], password=config['dbpassword'], database=config['dbname'],
        query={'charset': 'utf8mb4'})
    engine = create_engine(url, echo=False, pool_size=1, max_overflow=0,
                           pool_pre_ping=True, pool_recycle=opts['pool_recycle'],
                           connect_args={'connect_timeout': opts['connect_timeout']})
    try:
        for attempt in range(opts['retry_count']):
            _progress_note('连接数据库（第 {} / {} 次）'.format(attempt + 1, opts['retry_count']))
            try:
                with engine.connect():
                    pass
                return engine
            except Exception:
                if attempt + 1 == opts['retry_count']:
                    raise RuntimeError('MySQL 连接失败，请检查配置、网络和白名单。') from None
                _progress_note('连接暂未成功，等待重试')
                time.sleep(opts['retry_interval'])
    except BaseException:
        engine.dispose()
        raise


@contextmanager
def database_scope(base, config):
    """各策略独立建表及持有连接，正常结束、异常、中断时均释放。"""
    engine = connect_database(config)
    try:
        for table in base.metadata.tables.values():
            table.dialect_options['mysql']['engine'] = 'InnoDB'
            table.dialect_options['mysql']['charset'] = 'utf8mb4'
        _progress_note('检查数据库表结构')
        base.metadata.create_all(engine)
        _progress_note('读取已有数据与增量范围')
        yield sessionmaker(bind=engine)
    finally:
        engine.dispose()


@contextmanager
def session_scope(Session):
    """统一回滚和关闭；提交仍由各策略显式决定，查询不触发写入。"""
    session = Session()
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def _safe_error(exc, config):
    message = re.sub(r'(?i)[a-z][a-z0-9+.-]*://[^\s]+', '[连接地址已隐藏]', str(exc))
    message = re.sub(r"(?i)(password|passwd|pwd|token|secret)\s*[:=]\s*('.*?'|\".*?\"|[^\s,;]+)",
                      r'\1=[已隐藏]', message)
    secrets = set()
    for key in ('host', 'dbname', 'dbuser', 'dbpassword'):
        value = str(config[key])
        if value:
            secrets.update((value, quote(value, safe=''), quote_plus(value)))
    for value in sorted(secrets, key=len, reverse=True):
        message = message.replace(value, '[已隐藏]')
    return '{}: {}'.format(type(exc).__name__, ' '.join(message.split())[:400])


def _dividend_strategy(_apis, _config):
    """两组高股息 Top25；每次重算，事务替换最新快照，不下单。"""
    settings = strategy_settings('高股息选股')
    get_all_securities, get_trade_days = _apis.get('get_all_securities'), _apis.get('get_trade_days')
    get_price, get_extras = _apis.get('get_price'), _apis.get('get_extras')
    get_fundamentals, get_industry = _apis.get('get_fundamentals'), _apis.get('get_industry')
    query, valuation, indicator, finance = [_apis.get(k) for k in ('query', 'valuation', 'indicator', 'finance')]
    BATCH_SIZE, PAGE_SIZE = settings['batch_size'], settings['page_size']
    if BATCH_SIZE > 3000 or PAGE_SIZE > 3000:
        raise ValueError('batch_size 和 page_size 不得超过 3000。')
    END_DATE = settings['end_date']
    RULE_VERSION = 'dividend_top25_v2'
    METHODS = ('classic', 'value')
    Base = declarative_base()

    class DividendSnapshot(Base):
        __tablename__ = 'dividend_selection_snapshot'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        id = Column(Integer, primary_key=True)
        trade_date = Column(Date, nullable=False)
        updated_at = Column(DateTime, nullable=False)
        rule_version = Column(String(32), nullable=False)
        universe_count = Column(Integer, nullable=False)
        eligible_count = Column(Integer, nullable=False)
        classic_candidates = Column(Integer, nullable=False)
        value_candidates = Column(Integer, nullable=False)
        classic_count = Column(Integer, nullable=False)
        value_count = Column(Integer, nullable=False)

    class DividendStock(Base):
        __tablename__ = 'dividend_selection_stock'
        __table_args__ = (Index('ux_dividend_method_rank', 'method', 'rank', unique=True),
                          {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'})
        method = Column(String(16), primary_key=True)
        code = Column(String(16), primary_key=True)
        trade_date = Column(Date, nullable=False)
        rank = Column(Integer, nullable=False)
        name = Column(String(128), nullable=False)
        industry_code = Column(String(32), nullable=True)
        industry_name = Column(String(128), nullable=True)
        industry_level = Column(String(16), nullable=True)
        dividend_yield_1y = Column(Numeric(24, 6), nullable=False)
        dividend_ratio_3y = Column(Numeric(24, 6), nullable=False)
        dividend_cash_1y = Column(Numeric(28, 6), nullable=False)
        dividend_cash_3y = Column(Numeric(28, 6), nullable=False)
        pe_ttm = Column(Numeric(24, 6), nullable=False)
        pb = Column(Numeric(24, 6), nullable=True)
        market_cap = Column(Numeric(24, 6), nullable=False)
        close = Column(Numeric(24, 6), nullable=False)
        revenue_growth = Column(Numeric(24, 6), nullable=True)
        profit_growth = Column(Numeric(24, 6), nullable=True)
        adjusted_roe = Column(Numeric(24, 6), nullable=True)
        peg = Column(Numeric(24, 6), nullable=True)
        report_period = Column(Date, nullable=True)
        publication_date = Column(Date, nullable=True)

    RESULT_COLUMNS = [c.name for c in DividendStock.__table__.columns]
    META_COLUMNS = [c.name for c in DividendSnapshot.__table__.columns if c.name not in ('id', 'updated_at')]

    def number(value):
        try:
            result = Decimal(str(value))
            return result if result.is_finite() else None
        except (InvalidOperation, ValueError, TypeError):
            return None

    def rounded(value):
        value = number(value)
        if value is None:
            return None
        with localcontext() as ctx:
            ctx.prec = 40
            return value.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)

    def optional_date(value):
        return None if value is None or pd.isna(value) else as_date(value)

    def rows(frame, required, label):
        if frame is None or not set(required).issubset(frame.columns):
            raise ValueError('{} 返回空对象或缺少字段。'.format(label))
        return frame.to_dict('records')

    def by_code(frame, batch, required, label, complete=True):
        values = rows(frame, ['code']+list(required), label)
        result = {}
        for row in values:
            code = str(row['code'])
            if code not in batch or code in result:
                raise ValueError('{} 包含重复或范围外代码。'.format(label))
            result[code] = row
        if complete and set(result) != set(batch):
            raise ValueError('{} 未完整返回，保留数据库原快照。'.format(label))
        return result

    def universe(day):
        _progress_note('读取历史股票池')
        frame = get_all_securities(types=['stock'], date=day)
        if frame is None or frame.empty or 'display_name' not in frame.columns:
            raise ValueError('股票列表为空或缺少名称。')
        names = {str(code): str(row['display_name']) for code, row in frame.iterrows()}
        if len(names) != len(frame):
            raise ValueError('股票列表代码重复。')
        # 无真实持仓，不使用原交易策略的持仓豁免；两个筛选共用可选池。
        codes = sorted(code for code, name in names.items() if not code.startswith(('4', '8'))
                       and not builtins.any(tag in name for tag in ('ST', '*', '退')))
        quotes, caps = {}, {}
        for offset in _progress_items(range(0, len(codes), BATCH_SIZE), '股票池批次'):
            batch = codes[offset:offset+BATCH_SIZE]
            st = get_extras('is_st', batch, start_date=day, end_date=day, df=True)
            if (st is None or len(st.index) != 1 or as_date(st.index[0]) != day
                    or set(st.columns) != set(batch) or len(st.columns) != len(batch)):
                raise ValueError('ST 状态不完整。')
            # 价格仅用于展示；不按停牌、开盘价或涨跌停状态筛选。
            # skip_paused=False 保留停牌股票，由数据源提供填充后的收盘价。
            prices = by_code(get_price(batch, start_date=day, end_date=day, frequency='daily',
                                      fields=['close'], fq=None, skip_paused=False, panel=False),
                             batch, ('time', 'close'), '行情')
            eligible = []
            for code in batch:
                flag = st.iloc[0][code]
                bar = prices[code]
                if pd.isna(flag) or flag not in (True, False) or as_date(bar['time']) != day:
                    raise ValueError('ST 状态或行情日期无效。')
                if flag:
                    continue
                closing = number(bar['close'])
                if closing is None or closing <= 0:
                    raise ValueError('收盘价缺失或无效。')
                eligible.append(code)
                quotes[code] = closing
            if eligible:
                q = query(valuation.code, valuation.market_cap, valuation.pe_ratio, valuation.pb_ratio).filter(valuation.code.in_(eligible))
                caps.update(by_code(get_fundamentals(q, date=day), eligible,
                                    ('market_cap', 'pe_ratio', 'pb_ratio'), '估值'))
        if not caps:
            raise ValueError('可选股票池为空，拒绝用异常空池覆盖结果。')
        for row in caps.values():
            if number(row['market_cap']) is None or number(row['market_cap']) <= 0:
                raise ValueError('总市值缺失或无效。')
        return names, quotes, caps

    def dividends(codes, day):
        table = finance.STK_XR_XD
        start3, start1 = day-datetime.timedelta(days=1095), day-datetime.timedelta(days=365)
        total = {}
        for offset in _progress_items(range(0, len(codes), BATCH_SIZE), '分红批次'):
            batch = codes[offset:offset+BATCH_SIZE]
            after = -1
            while True:
                _progress_note('读取分红明细（主键分页）')
                fields = ('id', 'code', 'a_registration_date', 'a_xr_date', 'bonus_amount_rmb')
                q = query(*[getattr(table, f) for f in fields]).filter(
                    table.code.in_(batch), table.id > after,
                    or_(and_(table.a_registration_date >= start3, table.a_registration_date <= day),
                        and_(table.a_xr_date > start1, table.a_xr_date <= day)),
                ).order_by(table.id).limit(PAGE_SIZE)
                page = rows(finance.run_query(q), fields, '分红明细')
                if not page:
                    break
                ids = [int(r['id']) for r in page]
                if ids != sorted(set(ids)) or ids[0] <= after or len(page) > PAGE_SIZE:
                    raise ValueError('分红分页重复、乱序或超限。')
                for row in page:
                    code = str(row['code'])
                    if code not in batch:
                        raise ValueError('分红代码越界。')
                    registration = optional_date(row['a_registration_date'])
                    ex_date = optional_date(row['a_xr_date'])
                    in3 = registration is not None and start3 <= registration <= day
                    in1 = ex_date is not None and start1 < ex_date <= day
                    if not (in3 or in1):
                        raise ValueError('分红日期越界。')
                    # 只有登记/除息日期已发生的记录；空派现金额沿用原程序按 0 处理。
                    cash = Decimal(0) if pd.isna(row['bonus_amount_rmb']) else number(row['bonus_amount_rmb'])
                    if cash is None or cash < 0:
                        raise ValueError('派现金额无效。')
                    item = total.setdefault(code, dict(cash_1y=Decimal(0), cash_3y=Decimal(0), has_3y=False))
                    if in3:
                        item['cash_3y'] += cash
                        item['has_3y'] = True
                    if in1:
                        item['cash_1y'] += cash
                after = ids[-1]
        return total

    def financials(codes, day):
        result = {}
        fields = ('inc_total_revenue_year_on_year', 'inc_net_profit_year_on_year', 'inc_return', 'statDate', 'pubDate')
        for offset in _progress_items(range(0, len(codes), BATCH_SIZE), '财务指标批次'):
            batch = codes[offset:offset+BATCH_SIZE]
            q = query(indicator.code, *[getattr(indicator, f) for f in fields]).filter(indicator.code.in_(batch))
            result.update(by_code(get_fundamentals(q, date=day), batch, fields, '财务指标', complete=False))
        if codes and not result:
            raise ValueError('全股票池财务指标为空，保留原快照。')
        for row in result.values():
            pub = optional_date(row['pubDate'])
            period = optional_date(row['statDate'])
            if (pub and pub > day) or (period and period > day):
                raise ValueError('财务指标包含未来日期。')
        return result

    def select(caps, cash, fundamentals):
        # 比较使用未舍入值；相同分红比例按代码排序，结果稳定。
        ratios = {}
        for code, cap in caps.items():
            item = cash.get(code, {})
            market_cap = number(cap['market_cap'])
            if market_cap is None or market_cap <= 0:
                raise ValueError('市值无效。')
            ratios[code] = (item.get('cash_1y', Decimal(0))/100/market_cap,
                            item.get('cash_3y', Decimal(0))/100/market_cap)
        dividend_codes = sorted((c for c in caps if cash.get(c, {}).get('has_3y')),
                                key=lambda c: (-ratios[c][1], c))
        classic = []
        for code in dividend_codes[:len(dividend_codes)//10]:
            pe = number(caps[code]['pe_ratio'])
            metrics = fundamentals.get(code, {})
            revenue, profit, roe = [number(metrics.get(f)) for f in
                                   ('inc_total_revenue_year_on_year', 'inc_net_profit_year_on_year', 'inc_return')]
            if None not in (pe, revenue, profit, roe) and revenue > 3 and profit > 8 and roe > Decimal('4.5'):
                if Decimal('.1') < pe/profit < 2:
                    classic.append(code)
        value = sorted((c for c in caps if number(caps[c]['pe_ratio']) is not None
                        and 0 < number(caps[c]['pe_ratio']) < 8 and ratios[c][0] > 4),
                       key=lambda c: (-ratios[c][0], c))
        return dict(classic=classic[:25], value=value[:25]), ratios, dict(classic=len(classic), value=len(value))

    def calculate(day):
        names, quotes, caps = universe(day)
        codes = sorted(caps)
        cash = dividends(codes, day)
        fundamentals = financials(codes, day)
        selected, ratios, counts = select(caps, cash, fundamentals)
        chosen = sorted(set(selected['classic']+selected['value']))
        _progress_note('补充细分行业并生成两组 Top25')
        industries = get_industry(chosen, date=day) if chosen else {}
        if not isinstance(industries, dict) or set(chosen)-set(industries):
            raise ValueError('入选股票行业查询未完整返回。')
        records = []
        for method in METHODS:
            for rank, code in enumerate(selected[method], 1):
                info = industries.get(code) or {}
                level = next((k for k in ('sw_l3', 'sw_l2', 'sw_l1') if info.get(k)), None)
                industry = info.get(level, {})
                cap, fin, payout = caps[code], fundamentals.get(code, {}), cash.get(code, {})
                profit = number(fin.get('inc_net_profit_year_on_year'))
                pe = number(cap['pe_ratio'])
                records.append(dict(method=method, code=code, trade_date=day, rank=rank, name=names[code],
                    industry_code=industry.get('industry_code'), industry_name=industry.get('industry_name'), industry_level=level,
                    dividend_yield_1y=rounded(ratios[code][0]), dividend_ratio_3y=rounded(ratios[code][1]),
                    dividend_cash_1y=rounded(payout.get('cash_1y', 0)), dividend_cash_3y=rounded(payout.get('cash_3y', 0)),
                    pe_ttm=rounded(pe), pb=rounded(cap['pb_ratio']), market_cap=rounded(cap['market_cap']), close=rounded(quotes[code]),
                    revenue_growth=rounded(fin.get('inc_total_revenue_year_on_year')), profit_growth=rounded(profit),
                    adjusted_roe=rounded(fin.get('inc_return')), peg=rounded(pe/profit) if profit and profit > 0 else None,
                    report_period=optional_date(fin.get('statDate')), publication_date=optional_date(fin.get('pubDate'))))
        meta = dict(trade_date=day, rule_version=RULE_VERSION, universe_count=len(names), eligible_count=len(caps),
                    classic_candidates=counts['classic'], value_candidates=counts['value'],
                    classic_count=len(selected['classic']), value_count=len(selected['value']))
        return meta, records

    def save_latest(Session, meta, records):
        if len({(r['method'], r['code']) for r in records}) != len(records):
            raise ValueError('同组股票重复。')
        for method in METHODS:
            group = [r for r in records if r['method'] == method]
            if len(group) != meta[method+'_count'] or len(group) > 25 or sorted(r['rank'] for r in group) != list(range(1, len(group)+1)):
                raise ValueError('数量或排名与快照不一致。')
        if builtins.any(r['method'] not in METHODS or r['trade_date'] != meta['trade_date'] for r in records):
            raise ValueError('方法或日期不一致。')
        _progress_note('事务更新两组最新选股结果')
        with session_scope(Session) as session:
            head = session.query(DividendSnapshot).filter_by(id=1).with_for_update().first()
            if head is not None and head.trade_date > meta['trade_date']:
                raise ValueError('数据库已有更新日期，拒绝旧日期覆盖。')
            existing = session.query(DividendStock).all()
            def signature(values):
                return sorted(tuple(r[c] for c in RESULT_COLUMNS) for r in values)
            old_records = [{c: getattr(r, c) for c in RESULT_COLUMNS} for r in existing]
            if head is not None and all(getattr(head, c) == meta[c] for c in META_COLUMNS) and signature(old_records) == signature(records):
                return False
            if head is None:
                head = DividendSnapshot(id=1, updated_at=china_now(), **meta)
                session.add(head)
                session.flush()  # 首次并发创建冲突时整个事务回滚，不覆盖他人结果。
            else:
                for key, value in meta.items():
                    setattr(head, key, value)
                head.updated_at = china_now()
            for previous in existing:
                session.expunge(previous)
            session.query(DividendStock).delete(synchronize_session=False)
            session.add_all([DividendStock(**r) for r in records])
            session.commit()
            return True

    def run(end_date=END_DATE):
        now = china_now()
        end = as_date(end_date) if end_date is not None else now.date()
        if end > now.date():
            raise ValueError('结束日期不能晚于今天。')
        if end == now.date() and now.hour < 16:
            end -= datetime.timedelta(days=1)
        _progress_note('确定最近完整交易日')
        days = get_trade_days(end_date=end, count=1)
        if days is None or len(days) != 1 or as_date(days[0]) > end:
            raise ValueError('交易日历无效。')
        day = as_date(days[0])
        with database_scope(Base, _config) as Session:
            for current in _progress_items([day], '最新选股日'):
                meta, records = calculate(current)
                if not save_latest(Session, meta, records):
                    _progress_note('选股结果未变化，未重复写入')
        return pd.DataFrame(records, columns=RESULT_COLUMNS)
    return SimpleNamespace(**locals())

_API_NAMES = ('get_all_securities', 'get_trade_days', 'get_price', 'get_extras', 'get_industry',
              'get_fundamentals', 'query', 'valuation', 'indicator', 'finance')


def run(**options):
    if (not isinstance(PROGRESS_ENABLED, bool) or isinstance(PROGRESS_INTERVAL_SECONDS, bool)
            or not isinstance(PROGRESS_INTERVAL_SECONDS, (int, float))
            or not math.isfinite(PROGRESS_INTERVAL_SECONDS) or PROGRESS_INTERVAL_SECONDS <= 0):
        raise ValueError('进度开关须为布尔值，刷新间隔须为有限正数。')
    apis = {key: globals().get(key, getattr(jq, key, getattr(builtins, key, None))) for key in _API_NAMES}
    with _progress_scope('高股息选股', 1, 1):
        return _dividend_strategy(apis, DB_CONFIG).run(**options)


if __name__ == '__main__':
    try:
        dividend_df = run()
        print('高股息选股：当前两组结果共 {} 条，数据库仅保留最新快照。'.format(len(dividend_df)))
        if len(dividend_df):
            print(dividend_df.to_string(index=False))
    except Exception as exc:
        raise RuntimeError(_safe_error(exc, DB_CONFIG)) from None
