# -*- coding: utf-8 -*-
"""聚宽研究环境：个股机构持仓、股东户数及社保新进。
独立单文件；首次近五年，之后刷新近期报告及新披露户数。
依赖和参数集中在开头；共用连接、会话及日期工具。用法见 docs/collector-batch.md。
"""
import builtins
import datetime
import math
import re
import sys
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import quote, quote_plus

import jqdata as jq
import pandas as pd
from sqlalchemy import Column, Date, DateTime, JSON, String, or_, create_engine
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker


# ==================== 集中配置区 ====================
DB_CONFIG = {
    'host': 'your_mysql_host',
    'port': 3306,
    'dbname': 'your_database',
    'dbuser': 'your_database_user',
    'dbpassword': 'your_password',
}
# 连接参数只配置一次；重试仅针对连接，不重放写入事务。
DB_OPTIONS = {'connect_timeout': 10, 'retry_count': 5, 'retry_interval': 10, 'pool_recycle': 3600}

# end_date=None：每次运行取北京时间当天。下列参数按策略分组配置。
STRATEGY_CONFIG = {'个股机构持仓': {'end_date': None, 'history_years': 5, 'refresh_quarters': 4, 'page_size': 1000, 'stock_batch_size': 50}}
PROGRESS_ENABLED = True
PROGRESS_INTERVAL_SECONDS = 5.0
_API_NAMES = ('finance', 'query', 'get_all_securities', 'get_industry', 'get_price', 'get_trade_days')

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

def _ownership_strategy(_apis, _config):
    """披露持仓、股东户数与社保新进；仅公开披露口径，无交易操作。"""
    settings = strategy_settings('个股机构持仓')
    finance, query = _apis.get('finance'), _apis.get('query')
    Base = declarative_base()

    class OwnershipStock(Base):
        __tablename__ = 'ownership_stock'
        code = Column(String(16), primary_key=True)
        name = Column(String(128), nullable=False, index=True)
        industry = Column(String(128))
        updated_at = Column(DateTime, nullable=False)

    class OwnershipReport(Base):
        __tablename__ = 'ownership_report'
        code = Column(String(16), primary_key=True)
        report_date = Column(Date, primary_key=True, index=True)
        payload = Column(JSON, nullable=False)
        updated_at = Column(DateTime, nullable=False)

    class OwnershipHolders(Base):
        __tablename__ = 'ownership_holders'
        code = Column(String(16), primary_key=True)
        end_date = Column(Date, primary_key=True)
        pub_date = Column(Date)
        payload = Column(JSON, nullable=False)
        updated_at = Column(DateTime, nullable=False)

    class Checkpoint(Base):
        __tablename__ = 'ownership_checkpoint'
        key = Column(String(32), primary_key=True)
        as_of = Column(Date, nullable=False)

    CATEGORY_IDS = {'qfii': (307007,), 'social': (307004,),
                    'broker': (307002, 307015, 307022), 'insurance': (307006,307014,307023),
                    'trust': (307009,307016,307025), 'private': (307028,)}

    def number(value, integer=False):
        if value is None or pd.isnull(value):
            return None
        n = float(value)
        if not math.isfinite(n) or n < 0:
            raise ValueError('持仓数据出现无效负值或非有限数')
        return int(n) if integer else n

    def text_value(value):
        return '' if value is None or pd.isnull(value) else str(value).strip()

    def day(value):
        return None if value is None or pd.isnull(value) else as_date(value)

    def previous_quarter(period):
        return datetime.date(period.year, ((period.month-1)//3)*3+1, 1)-datetime.timedelta(days=1)

    def pages(table, *conditions, columns=None):
        """Yield one row at a time; only the current source page is retained."""
        cursor, count = 0, 0
        size = builtins.min(settings['page_size'], 2000)
        while True:
            _progress_note('读取 {}，已读取 {} 条'.format(table.__name__, count))
            selection = query(*columns) if columns is not None else query(table)
            frame = finance.run_query(selection.filter(table.id > cursor, *conditions).order_by(table.id).limit(size))
            if frame is None:
                raise ValueError('数据接口未返回结果')
            if frame.empty:
                break
            rows = frame.to_dict('records')
            del frame
            ids = [int(r['id']) for r in rows]
            if ids != sorted(set(ids)) or ids[0] <= cursor:
                raise ValueError('数据接口分页未按 ID 递增')
            cursor = ids[-1]
            count += len(rows)
            yield from rows
            del rows, ids
            # Query until empty even when upstream caps below requested page size.

    def batches(items):
        for offset in range(0, len(items), settings['stock_batch_size']):
            yield items[offset:offset+settings['stock_batch_size']]

    def fund_document(row, fund_map):
        fund = text_value(row['code']).split('.')[0]
        canonical = fund_map.get(fund, fund)
        full = text_value(row.get('report_type')) in ('年度','年度报告','年报','半年度','半年度报告')
        priority = (int(full), day(row['pub_date']), int(fund == canonical), fund)
        document = (fund, day(row['pub_date']), int(row['report_type_id']))
        return canonical, priority, document

    def select_documents(period, as_of, fund_map):
        # First pass retains only the winning document identity, not its holdings.
        t = finance.FUND_PORTFOLIO_STOCK
        selected = {}
        columns = [t.id, t.code, t.pub_date, t.report_type_id, t.report_type]
        for row in pages(t, t.period_end == period, t.pub_date <= as_of, columns=columns):
            canonical, priority, document = fund_document(row, fund_map)
            if canonical not in selected or priority > selected[canonical][0]:
                selected[canonical] = (priority, document)
        return selected

    def batch_funds(codes, period, as_of, fund_map, selected):
        # Select documents globally before stock filtering, otherwise an absent holding
        # in a newer/A-class portfolio could resurrect an older/C-class position.
        t = finance.FUND_PORTFOLIO_STOCK
        code_map = {c.split('.')[0]:c for c in codes}
        candidates = {}
        columns = [t.id,t.code,t.symbol,t.pub_date,t.report_type_id,t.report_type,t.shares,t.market_cap]
        for row in pages(t, t.period_end == period, t.pub_date <= as_of,
                         t.symbol.in_(list(code_map)+list(codes)), columns=columns):
            canonical, _, document = fund_document(row, fund_map)
            if canonical not in selected or document != selected[canonical][1]:
                continue
            code = code_map.get(text_value(row['symbol']).split('.')[0])
            if code:
                # IDs are ascending, so the last occurrence wins within a document.
                candidates[(code,canonical)] = (number(row['shares']),number(row['market_cap']))
        totals = {}
        for (code,_),(shares,value) in candidates.items():
            if code not in totals:
                totals[code] = {'shares':0.,'market_value':0.,'holder_count':0,
                                'scope':'fund_disclosed','value_method':'reported'}
            total = totals[code]
            total['holder_count'] += 1
            total['shares'] = None if shares is None or total['shares'] is None else total['shares']+shares
            total['market_value'] = None if value is None or total['market_value'] is None else total['market_value']+value
        return totals

    def batch_floats(codes, period, as_of):
        t = finance.STK_SHAREHOLDER_FLOATING_TOP10
        latest = {}
        for row in pages(t, t.end_date == period, t.pub_date <= as_of, t.code.in_(codes),
                         t.change_reason_id == 306019, t.sharesnature_id == 308007):
            code, pub = row['code'], day(row['pub_date'])
            if code not in latest or pub > latest[code][0]:
                latest[code] = (pub,{})
            if pub == latest[code][0]:
                latest[code][1][int(row['shareholder_rank'])] = row
        return {code:list(ranks.values()) for code,(_,ranks) in latest.items()}

    def save_report_batch(session, codes, period, as_of, fund_map, selected, changed):
        fund_groups = batch_funds(codes,period,as_of,fund_map,selected)
        by_stock = batch_floats(codes,period,as_of)
        closes = prices(sorted(by_stock),period)
        old_rows = {r.code:r for r in session.query(OwnershipReport).filter(
            OwnershipReport.report_date == period,OwnershipReport.code.in_(codes)).all()}
        previous_rows = {r.code:r for r in session.query(OwnershipReport).filter(
            OwnershipReport.report_date == previous_quarter(period),OwnershipReport.code.in_(codes)).all()}
        changed_float_codes = set()
        for code in sorted(set(by_stock) | set(fund_groups) | set(old_rows)):
            members, complete, pub = float_snapshot(by_stock.get(code,[]),closes.get(code))
            categories = {'public_fund':fund_groups.get(code)}
            for category, ids in CATEGORY_IDS.items():
                categories[category] = aggregate([r for r in members if r['class_id'] in ids],'top10_float','estimated')
            payload = {'schema_version':1, 'categories':categories, 'float_members':members,
                       'float_complete':complete, 'float_pub_date':pub,
                       'public_fund_scope':'full_disclosure' if period.month in (6,12) else 'quarterly_top_holdings',
                       'fund_identity_rule':'statistics_main_code_else_product_code'}
            previous = previous_rows.get(code)
            payload['social_entry'] = social_entry(payload,previous.payload if previous else None)
            old_payload = old_rows[code].payload if code in old_rows else {}
            if old_payload.get('float_members') != members or old_payload.get('float_complete') != complete:
                changed_float_codes.add(code)
            if upsert(session,OwnershipReport,{'code':code,'report_date':period},{'payload':payload},
                      existing=old_rows.get(code),loaded=True):
                changed.add(('持仓',code,period))
        next_quarter = period+datetime.timedelta(days=95)
        next_period = previous_quarter(datetime.date(next_quarter.year,next_quarter.month,1))
        if changed_float_codes:
            followers = session.query(OwnershipReport).filter(OwnershipReport.report_date == next_period,
                OwnershipReport.code.in_(changed_float_codes)).all()
            for follower in followers:
                follower.payload = dict(follower.payload,social_entry={'status':'unknown'})
                follower.updated_at = china_now()
        # Flush, not commit: release ORM state while retaining quarter-level rollback.
        session.flush()
        session.expunge_all()

    def repair_social_batch(session, codes, changed):
        records = session.query(OwnershipReport).filter(OwnershipReport.code.in_(codes)).all()
        lookup = {(r.code,r.report_date):r for r in records}
        for row in records:
            previous = lookup.get((row.code,previous_quarter(row.report_date)))
            social = social_entry(row.payload,previous.payload if previous else None)
            if row.payload.get('social_entry') != social:
                row.payload = dict(row.payload,social_entry=social)
                row.updated_at = china_now()
                changed.add(('持仓',row.code,row.report_date))
        session.flush()
        session.expunge_all()

    def save_holder_batch(session, codes, conditions, start, changed):
        t = finance.STK_HOLDER_NUM
        latest = {}
        for row in pages(t,t.code.in_(codes),*conditions):
            key = (row['code'],day(row['end_date']))
            priority = (day(row['pub_date']) or datetime.date.min,int(row['id']))
            if key not in latest or priority > latest[key][0]:
                latest[key] = (priority,row)
        if not latest:
            return
        existing = {(r.code,r.end_date):r for r in session.query(OwnershipHolders).filter(
            OwnershipHolders.end_date >= start,OwnershipHolders.code.in_(codes)).all()}
        for (code,end),(_,row) in latest.items():
            payload = {key:number(row[key],True) for key in
                       ('share_holders','a_share_holders','b_share_holders','h_share_holders')}
            payload['schema_version'] = 1
            if upsert(session,OwnershipHolders,{'code':code,'end_date':end},
                      {'pub_date':day(row['pub_date']),'payload':payload},existing=existing.get((code,end)),loaded=True):
                changed.add(('户数',code,end))
        session.flush()
        session.expunge_all()

    def aggregate(items, scope, method, close=None):
        if not items:
            return None  # absence of disclosure is not zero ownership
        shares = [r.get('shares') for r in items]
        values = [r.get('market_value') for r in items]
        return {'shares': builtins.sum(shares) if builtins.all(v is not None for v in shares) else None,
                'market_value': builtins.sum(values) if builtins.all(v is not None for v in values) else None,
                'holder_count': len(items), 'scope': scope, 'value_method': method}

    def float_snapshot(rows, close):
        if not rows:
            return [], False, None
        latest = builtins.max(day(r['pub_date']) for r in rows)
        rows = [r for r in rows if day(r['pub_date']) == latest]
        by_rank = {}
        for r in sorted(rows, key=lambda r: r['id']):
            by_rank[int(r['shareholder_rank'])] = r
        complete = set(by_rank) == set(range(1,11))
        result = []
        for rank, r in sorted(by_rank.items()):
            shares = number(r['share_number'])
            identity = text_value(r.get('shareholder_id'))
            if identity.endswith('.0'):
                identity = identity[:-2]
            name = text_value(r['shareholder_name'])
            category = number(r.get('shareholder_class_id'), True)
            result.append({'id': identity or None, 'name': name, 'rank': rank,
                           'class_id': category, 'shares': shares,
                           'ratio': number(r.get('share_ratio')),
                           'market_value': shares*close if shares is not None and close is not None else None})
        # Unknown classifications or identities make absence/newness unreliable.
        complete = complete and builtins.all(r['class_id'] is not None and r['name'] for r in result)
        return result, bool(complete), latest.isoformat()

    def social_entry(current, previous):
        empty = {'status': 'unknown', 'first_appearance': False, 'new_combinations': [],
                 'new_shares': None, 'new_market_value': None}
        if not previous or not current.get('float_complete') or not previous.get('float_complete'):
            return empty
        current_members = [r for r in current['float_members'] if r['class_id'] == 307004]
        previous_members = previous['float_members']
        def same(a, b):
            return (a.get('id') and b.get('id') and a['id'] == b['id']) or re.sub(r'\s+', '', a['name']) == re.sub(r'\s+', '', b['name'])
        # Compare all prior shareholders: reclassification is not a new holding.
        new = [r for r in current_members if not builtins.any(same(r,p) for p in previous_members)]
        old_social = [r for r in previous_members if r['class_id'] == 307004]
        totals = aggregate(new, 'top10_float', 'estimated') or {}
        return {'status': 'comparable', 'first_appearance': bool(new) and not old_social,
                'new_combinations': new, 'new_shares': totals.get('shares'),
                'new_market_value': totals.get('market_value')}

    def upsert(session, model, keys, values, existing=None, loaded=False):
        row = existing if loaded else session.query(model).filter_by(**keys).first()
        if row is not None and builtins.all(getattr(row,k) == v for k,v in values.items()):
            return False
        if row is None:
            session.add(model(updated_at=china_now(), **dict(keys, **values)))
        else:
            for k,v in values.items():
                setattr(row,k,v)
            row.updated_at = china_now()
        return True

    def prices(codes, period):
        if not codes:
            return {}
        trade_days = _apis['get_trade_days'](end_date=period, count=1)
        if trade_days is None or len(trade_days) != 1:
            raise ValueError('报告期末交易日不可用')
        price_date = as_date(trade_days[0])
        result = {}
        for offset in range(0,len(codes),300):
            frame = _apis['get_price'](codes[offset:offset+300], start_date=price_date,
                end_date=price_date, frequency='daily', fields=['close'], fq=None, panel=False)
            if frame is None:
                raise ValueError('报告期末行情获取失败')
            if not frame.empty:
                for r in frame.to_dict('records'):
                    v = number(r['close'])
                    if v and v > 0:
                        result[r['code']] = v
        return result

    def run(end_date=None, full_refresh=False, skip_existing=True):
        as_of = as_date(end_date or settings.get('end_date') or china_now().date())
        if as_of > china_now().date():
            raise ValueError('结束日期不能晚于今天')
        for option in ('refresh_quarters','stock_batch_size'):
            if isinstance(settings[option],bool) or not isinstance(settings[option],int) or settings[option] < 1:
                raise ValueError(option+' 必须为正整数')
        if settings['stock_batch_size'] > 300:
            raise ValueError('stock_batch_size 最大为 300，防止研究环境内存溢出')
        years = settings['history_years']
        if not isinstance(years,int) or years < 1 or years > 20:
            raise ValueError('history_years 必须在 1 到 20 之间')
        start = as_of.replace(year=as_of.year-years, day=builtins.min(as_of.day,28))
        periods = [datetime.date(y,m,d) for y in range(start.year,as_of.year+1)
                   for m,d in ((3,31),(6,30),(9,30),(12,31))
                   if start <= datetime.date(y,m,d) <= as_of]
        periods.insert(0, previous_quarter(periods[0]))
        securities = _apis['get_all_securities'](types=['stock'],date=as_of)
        if securities is None or securities.empty:
            raise ValueError('股票清单为空，停止写入')
        codes = sorted(str(code) for code in securities.index)
        fund_map = {}
        main = finance.FUND_MAIN_INFO
        for row in pages(main,columns=[main.id,main.main_code,main.statistics_main_code]):
            fund = text_value(row.get('main_code')).split('.')[0]
            canonical = text_value(row.get('statistics_main_code')).split('.')[0] or fund
            if fund:
                fund_map[fund] = canonical
        # Compact keys only: no raw rows or JSON payloads are accumulated for the preview.
        changed = set()
        with database_scope(Base,_config) as Session:
            with session_scope(Session) as session:
                for batch in _progress_items(list(batches(codes)),'股票资料',lambda b:b[0]):
                    industries = _apis['get_industry'](batch,date=as_of)
                    if industries is None:
                        raise ValueError('行业查询失败')
                    existing = {r.code:r for r in session.query(OwnershipStock).filter(OwnershipStock.code.in_(batch)).all()}
                    for code in batch:
                        industry = industries.get(code,{}).get('sw_l2',{}).get('industry_name')
                        upsert(session,OwnershipStock,{'code':code},
                               {'name':str(securities.loc[code,'display_name']),'industry':industry},
                               existing=existing.get(code),loaded=True)
                    session.flush()
                    session.expunge_all()
                    del existing, industries
                session.commit()
            for period in _progress_items(periods,'报告期',str):
                with session_scope(Session) as session:
                    checkpoint = session.query(Checkpoint).get('report:'+str(period))
                    if checkpoint and checkpoint.as_of > as_of:
                        raise ValueError('已有较新采集结果，拒绝历史运行回退')
                    if checkpoint and not full_refresh and period not in periods[-settings['refresh_quarters']:]:
                        continue
                    # Only scalar codes; JSON payloads are loaded by bounded stock batch below.
                    old_codes = [r[0] for r in session.query(OwnershipReport.code).filter_by(report_date=period).all()]
                selected = select_documents(period,as_of,fund_map)
                with session_scope(Session) as session:
                    for batch in _progress_items(list(batches(sorted(set(codes)|set(old_codes)))), '持仓分批',lambda b:b[0]):
                        save_report_batch(session,batch,period,as_of,fund_map,selected,changed)
                    head = session.query(Checkpoint).get('report:'+str(period))
                    if head is None:
                        session.add(Checkpoint(key='report:'+str(period),as_of=as_of))
                    else:
                        head.as_of = as_of
                    session.commit()
                del selected, old_codes
            # Include historical/delisted stock codes, but never load every historical JSON at once.
            with session_scope(Session) as session:
                social_codes = [r[0] for r in session.query(OwnershipReport.code).distinct().all()]
                for batch in _progress_items(list(batches(social_codes)),'社保比较',lambda b:b[0]):
                    repair_social_batch(session,batch,changed)
                session.commit()
            t = finance.STK_HOLDER_NUM
            with session_scope(Session) as session:
                head = session.query(Checkpoint).get('holders')
                if head and head.as_of > as_of:
                    raise ValueError('股东户数已有较新结果，拒绝回退')
                since = None if full_refresh or head is None else head.as_of-datetime.timedelta(days=30)
            conditions = [t.end_date >= start,t.end_date <= as_of,or_(t.pub_date == None,t.pub_date <= as_of)]
            if since is not None:
                conditions.append(or_(t.pub_date >= since,t.end_date >= since))
            with session_scope(Session) as session:
                for batch in _progress_items(list(batches(codes)),'股东户数',lambda b:b[0]):
                    save_holder_batch(session,batch,conditions,start,changed)
                head = session.query(Checkpoint).get('holders')
                if head is None:
                    session.add(Checkpoint(key='holders',as_of=as_of))
                else:
                    head.as_of = as_of
                session.commit()
        return pd.DataFrame(sorted(changed),columns=['kind','code','date'])
    return SimpleNamespace(**locals())


def run(**options):
    apis = {key: globals().get(key, getattr(jq, key, getattr(builtins, key, None))) for key in _API_NAMES}
    with _progress_scope('个股机构持仓', 1, 1):
        return _ownership_strategy(apis, DB_CONFIG).run(**options)

if __name__ == '__main__':
    try:
        result = run()
        print('个股机构持仓：新增或更新 {} 条。'.format(len(result)))
        print(result.tail(10).to_string(index=False))
    except Exception as exc:
        raise RuntimeError(_safe_error(exc, DB_CONFIG)) from None
