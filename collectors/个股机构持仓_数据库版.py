# -*- coding: utf-8 -*-
"""聚宽研究环境：个股机构持仓、股东户数及社保新进。
独立单文件；首次近五年，之后刷新近期报告及新披露户数。
依赖和参数集中在开头；共用连接、会话及日期工具。用法见 docs/collector-batch.md。
"""
import builtins
import datetime
import uuid
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
from sqlalchemy import BigInteger, Column, Date, DateTime, Integer, JSON, String, or_, create_engine, event, tuple_, func
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


def _optional_date(value):
    return None if value is None or pd.isna(value) else as_date(value)

def _source_pages(fetch_page, key='id', start=-1, label='数据接口', progress=None, fields=(), page_size=None):
    """普通主键分页只保留当前页，读到空页；服务端可能返回少于请求数量。"""
    cursor, count = start, 0
    while True:
        if progress is not None:
            progress('{}：已读取 {} 条，正在请求下一页'.format(label, count))
        frame = fetch_page(cursor)
        if frame is None:
            raise ValueError('{} 未返回结果，取消本次入库。'.format(label))
        if not set(fields).issubset(frame.columns):
            raise ValueError('{} 返回字段不完整，取消本次入库。'.format(label))
        if page_size is not None and len(frame) > page_size:
            raise ValueError('{} 分页超限。'.format(label))
        if frame.empty:
            return
        if key not in frame.columns:
            raise ValueError('{} 缺少分页主键 {}。'.format(label, key))
        ids = [int(value) for value in frame[key]]
        if ids != sorted(set(ids)) or ids[0] <= cursor:
            raise ValueError('{} 分页主键重复、乱序或未前进。'.format(label))
        cursor, count = ids[-1], count + len(frame)
        yield frame
        del frame, ids


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
    _PROGRESS_LOCAL.output = sys.stdout
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
        _PROGRESS_LOCAL.output = None
        if progress is not None:
            progress.close(ending)


def _progress_message(message):
    """Keep brief collection-scope messages visible through the batch quiet stream."""
    progress = getattr(_PROGRESS_LOCAL, 'current', None)
    stream = getattr(_PROGRESS_LOCAL, 'output', None) or sys.stdout
    if progress is not None:
        with progress.lock:
            stream.write('\r' + ' ' * progress.width + '\r' + message + '\n')
            stream.flush()
            progress.width = 0
            progress.render(force=True)
    else:
        print(message, file=stream, flush=True)


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
    price_universes = {}  # Report-date stock membership, reused across stock batches.
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

    class DisclosureProbe(Base):
        __tablename__ = 'ownership_disclosure_probe'
        key = Column(String(16), primary_key=True)
        payload = Column(JSON, nullable=False)
        as_of = Column(Date, nullable=False)

    def disclosure_signature(as_of):
        # Six one-row queries, no holdings payload or all-stock history scan.
        result = {'history_years': settings['history_years'], 'refresh_quarters': settings['refresh_quarters']}
        for name, table, period in (
                ('fund', finance.FUND_PORTFOLIO_STOCK, finance.FUND_PORTFOLIO_STOCK.period_end),
                ('float', finance.STK_SHAREHOLDER_FLOATING_TOP10, finance.STK_SHAREHOLDER_FLOATING_TOP10.end_date),
                ('holders', finance.STK_HOLDER_NUM, finance.STK_HOLDER_NUM.end_date)):
            filters = [period <= as_of, or_(table.pub_date == None, table.pub_date <= as_of)]
            markers = []
            for order in ((table.id.desc(),), (table.pub_date.desc(), table.id.desc())):
                df = finance.run_query(query(table.id, table.pub_date).filter(*filters).order_by(*order).limit(1))
                if df is None:
                    raise ValueError('新增披露检查失败：' + name)
                if df.empty:
                    markers.append(None)
                else:
                    row = df.iloc[0]
                    pub = day(row['pub_date'])
                    markers.append([int(row['id']), pub.isoformat() if pub else None])
            result[name] = markers
        return result

    class SyncHead(Base):
        __tablename__ = 'ownership_sync_head'
        id = Column(String(16), primary_key=True)
        generation = Column(String(32), nullable=False)
        revision = Column(BigInteger, nullable=False)

    class Change(Base):
        __tablename__ = 'ownership_change'
        table_name = Column(String(32), primary_key=True)
        code = Column(String(16), primary_key=True)
        record_date = Column(Date, primary_key=True)
        revision = Column(BigInteger, nullable=False, index=True)
        deleted = Column(Integer, nullable=False)

    def install_change_tracking(Session):
        """Coalesced change keys, committed atomically with the business rows.

        The locked head serializes writers before any tracked DML. Unlike an
        auto-increment event ID, its committed order cannot skip a slow writer.
        """
        with session_scope(Session) as session:
            if session.query(SyncHead).filter_by(id='main').first() is None:
                session.add(SyncHead(id='main',generation=uuid.uuid4().hex,revision=0))
                session.commit()
        tracked = (OwnershipStock,OwnershipReport,OwnershipHolders)
        def before_flush(session, flush_context, instances):
            pending = [r for r in session.new if isinstance(r,tracked)]
            pending.extend(r for r in session.dirty if isinstance(r,tracked) and session.is_modified(r,include_collections=False))
            pending.extend(r for r in session.deleted if isinstance(r,tracked))
            if not pending:
                return
            revision = session.info.get('ownership_revision')
            if revision is None:
                head = session.query(SyncHead).filter_by(id='main').populate_existing().with_for_update().one()
                head.revision += 1
                revision = head.revision
                session.info['ownership_revision'] = revision
            keys = {}
            for row in pending:
                date = getattr(row,'report_date',getattr(row,'end_date',datetime.date(1970,1,1)))
                keys[(row.__tablename__,row.code,date)] = int(row in session.deleted)
            # Bound SQL parameter counts even if a caller flushes a large batch.
            key_list = list(keys)
            for offset in range(0,len(key_list),200):
                batch = key_list[offset:offset+200]
                existing = {(r.table_name,r.code,r.record_date):r for r in session.query(Change).filter(
                    tuple_(Change.table_name,Change.code,Change.record_date).in_(batch)).all()}
                for key in batch:
                    row = existing.get(key)
                    if row is None:
                        session.add(Change(table_name=key[0],code=key[1],record_date=key[2],revision=revision,deleted=keys[key]))
                    else:
                        row.revision, row.deleted = revision, keys[key]
        def reset(session):
            session.info.pop('ownership_revision',None)
        event.listen(Session,'before_flush',before_flush)
        event.listen(Session,'after_commit',reset)
        event.listen(Session,'after_rollback',reset)

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

    day = _optional_date

    def previous_quarter(period):
        return datetime.date(period.year, ((period.month-1)//3)*3+1, 1)-datetime.timedelta(days=1)

    def pages(table, *conditions, columns=None):
        selection = query(*columns) if columns is not None else query(table)
        size = builtins.min(settings['page_size'], 2000)
        fetch_page = lambda cursor: finance.run_query(selection.filter(table.id > cursor, *conditions)
                                                     .order_by(table.id).limit(size))
        for frame in _source_pages(fetch_page, start=0, label=table.__name__, progress=_progress_note):
            yield from frame.to_dict('records')
            # Query until empty even when upstream caps below requested page size.

    def grouped_pages(table, columns, *conditions, label='报告索引'):
        """Group at source; paginate by unique minimum ID without splitting a group."""
        cursor, count = 0, 0
        size = builtins.min(settings['page_size'], 2000)
        group_id = func.min(table.id)
        while True:
            _progress_note('{}：已读取 {} 条去重索引'.format(label, count))
            q = (query(group_id.label('id'), *columns).filter(*conditions)
                 .group_by(*columns).having(group_id > cursor).order_by(group_id).limit(size))
            frame = finance.run_query(q)
            if frame is None:
                raise ValueError('去重索引接口未返回结果：' + label)
            if frame.empty:
                break
            rows = frame.to_dict('records')
            ids = [int(r['id']) for r in rows]
            if ids != sorted(set(ids)) or ids[0] <= cursor:
                raise ValueError('去重索引分页未按组 ID 递增：' + label)
            cursor = ids[-1]
            count += len(rows)
            yield from rows
            # Continue to empty, even when the provider caps below requested size.

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

    def select_documents(period, as_of, fund_map, codes):
        # Discover only funds that have held the requested stocks in this quarter.
        # Then inspect all share classes of those funds, including documents where
        # the requested stock disappeared. Never scan the whole market's documents.
        t = finance.FUND_PORTFOLIO_STOCK
        canonical_funds, observed_funds = set(), set()
        for batch in batches(sorted(set(codes))):
            symbols = sorted(set(batch) | {c.split('.')[0] for c in batch})
            for row in grouped_pages(t, [t.code], t.period_end == period, t.pub_date <= as_of,
                                     t.symbol.in_(symbols), label='关联基金'):
                fund = text_value(row['code']).split('.')[0]
                observed_funds.add(fund)
                canonical_funds.add(fund_map.get(fund, fund))
        related = sorted(observed_funds | canonical_funds |
                         {f for f, c in fund_map.items() if c in canonical_funds})
        _progress_message('{}：发现 {} 个相关基金代码（含关联份额），仅查询去重报告版本。'.format(period, len(related)))
        selected = {}
        columns = [t.code, t.pub_date, t.report_type_id, t.report_type]
        for batch in _progress_items(list(batches(related)), '相关基金报告版本', lambda b: b[0]):
            for row in grouped_pages(t, columns, t.period_end == period, t.pub_date <= as_of,
                                     t.code.in_(batch), label='相关基金报告版本'):
                canonical, priority, document = fund_document(row, fund_map)
                if canonical not in selected or priority > selected[canonical][0]:
                    selected[canonical] = (priority, document)
        return selected

    def load_fund_map():
        result = {}
        main = finance.FUND_MAIN_INFO
        for row in pages(main, columns=[main.id, main.main_code, main.statistics_main_code]):
            fund = text_value(row.get('main_code')).split('.')[0]
            canonical = text_value(row.get('statistics_main_code')).split('.')[0] or fund
            if fund:
                result[fund] = canonical
        return result

    def affected_stocks(previous, previous_day, current, as_of, fund_map, known_codes, periods):
        """Find affected stocks from newly inserted/published source rows, not all A shares."""
        affected = {name: set() for name in ('fund', 'float', 'holders')}
        row_counts = {name: 0 for name in affected}
        changed_funds, ignored = set(), set()
        known_codes = set(known_codes)
        code_map = {c.split('.')[0]: c for c in known_codes}
        def stock_code(value):
            value = text_value(value)
            # Resolve bare fund symbols, but never replace an explicit exchange.
            code = value if '.' in value else code_map.get(value)
            return code if code in known_codes else None
        specs = (('fund', finance.FUND_PORTFOLIO_STOCK, 'period_end'),
                 ('float', finance.STK_SHAREHOLDER_FLOATING_TOP10, 'end_date'),
                 ('holders', finance.STK_HOLDER_NUM, 'end_date'))
        for name, table, period_name in specs:
            if previous.get(name) == current.get(name):
                continue
            old_marker = previous.get(name, [None])[0]
            old_id = old_marker[0] if old_marker else 0
            new_marker = current.get(name, [None])[0]
            if new_marker is None:
                raise ValueError('披露来源记录消失，请使用完整刷新复核：' + name)
            filters = [table.id <= new_marker[0], getattr(table, period_name) <= as_of,
                       or_(table.pub_date == None, table.pub_date <= as_of),
                       or_(table.id > old_id, table.pub_date > previous_day)]
            if name == 'float':
                filters += [table.change_reason_id == 306019, table.sharesnature_id == 308007]
            columns = [table.id, table.code]
            if name == 'fund':
                columns.append(table.symbol)
            for row in pages(table, *filters, columns=columns):
                row_counts[name] += 1
                if name == 'fund':
                    fund = text_value(row['code']).split('.')[0]
                    changed_funds.add(fund_map.get(fund, fund))
                    code = stock_code(row['symbol'])
                else:
                    code = stock_code(row['code'])
                    if code is None:
                        ignored.add(text_value(row['code']))
                if code:
                    affected[name].add(code)
        # Include old constituents: a new fund report may remove a stock entirely.
        if changed_funds:
            table = finance.FUND_PORTFOLIO_STOCK
            funds = sorted(set(changed_funds) | {f for f, c in fund_map.items() if c in changed_funds})
            for batch in batches(funds):
                for row in grouped_pages(table, [table.symbol], table.code.in_(batch), table.period_end.in_(periods),
                                         table.pub_date <= as_of, label='新旧报告关联股票'):
                    code = stock_code(row['symbol'])
                    if code:
                        affected['fund'].add(code)
        if ignored:
            _progress_message('个股机构持仓：忽略 {} 个不在股票范围内的披露代码（如 {}）。'.format(
                len(ignored), '、'.join(sorted(ignored)[:5])))
        for name, label in (('fund', '公募持仓'), ('float', '十大流通股东'), ('holders', '股东户数')):
            if previous.get(name) != current.get(name):
                _progress_message('个股机构持仓：{}增量源记录 {} 条，影响 {} 只股票。'.format(
                    label, row_counts[name], len(affected[name])))
        return {name: sorted(codes) for name, codes in affected.items()}

    def batch_funds(codes, period, as_of, fund_map, selected):
        # Compare all documents of related funds, including other share classes;
        # otherwise stock filtering could resurrect an older position.
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
        if price_date not in price_universes:
            _progress_note('核对 {} 报告期末股票清单'.format(price_date))
            securities = _apis['get_all_securities'](types=['stock'], date=price_date)
            if securities is None or len(securities.index) == 0:
                raise ValueError('报告期末股票清单为空，停止估值与写入')
            price_universes[price_date] = set(str(code) for code in securities.index)
        available = [code for code in codes if code in price_universes[price_date]]
        if len(available) != len(codes):
            _progress_message('{}：{} 只股票无当期上市行情，持仓数量保留，估算市值留空。'.format(
                price_date, len(codes)-len(available)))
        result = {}
        for offset in range(0,len(available),300):
            batch = available[offset:offset+300]
            _progress_note('获取 {} 报告期末行情（{} 只）'.format(price_date, len(batch)))
            frame = _apis['get_price'](batch, start_date=price_date,
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
        price_universes.clear()
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
        _progress_message('个股机构持仓 [分来源增量版 v6]：开始检查新增披露。')
        signature = disclosure_signature(as_of)
        previous_probe, previous_day = None, None
        with database_scope(Base, _config) as Session:
            with session_scope(Session) as session:
                probe = session.query(DisclosureProbe).filter_by(key='main').first()
                if probe is not None:
                    previous_probe, previous_day = dict(probe.payload), probe.as_of
                if probe is not None and probe.as_of > as_of:
                    raise ValueError('已有较新采集结果，拒绝历史运行回退')
                if not full_refresh and probe is not None and probe.payload == signature:
                    _progress_message('个股机构持仓：未检测到新增披露，跳过持仓复查、户数采集和社保比较。')
                    return pd.DataFrame(columns=['kind', 'code', 'date'])

            securities = _apis['get_all_securities'](types=['stock'],date=as_of)
            if securities is None or securities.empty:
                raise ValueError('股票清单为空，停止写入')
            codes = sorted(str(code) for code in securities.index)
            targeted = (not full_refresh and previous_probe is not None
                        and previous_probe.get('history_years') == settings['history_years']
                        and previous_probe.get('refresh_quarters') == settings['refresh_quarters'])
            fund_map, review_codes, holder_codes = None, None, None
            if targeted:
                labels = [('fund', '公募持仓'), ('float', '十大流通股东'), ('holders', '股东户数')]
                sources = [label for name, label in labels if previous_probe.get(name) != signature.get(name)]
                _progress_message('个股机构持仓：触发来源 {}；上次已处理至 {}，本次检查至 {}。'.format(
                    '、'.join(sources), previous_day, as_of))
                # Holder-only changes need neither fund metadata nor quarterly holdings.
                if previous_probe.get('fund') != signature.get('fund'):
                    fund_map = load_fund_map()
                with session_scope(Session) as session:
                    stored_codes = {r[0] for r in session.query(OwnershipStock.code).all()}
                affected = affected_stocks(previous_probe, previous_day, signature, as_of,
                    fund_map or {}, set(codes) | stored_codes, periods[-settings['refresh_quarters']:])
                review_codes = sorted(set(affected['fund']) | set(affected['float']))
                holder_codes = affected['holders']
                # Keep existing stock metadata on holder-only updates; add new stocks normally.
                metadata_codes = set(review_codes) | (set(holder_codes) - stored_codes)
                codes = [c for c in codes if c in metadata_codes]
                periods = periods[-settings['refresh_quarters']:] if review_codes else []
                if review_codes:
                    _progress_message('个股机构持仓：持仓披露影响 {} 只股票，仅复查这些股票最近 {} 个季度。'.format(
                        len(review_codes), settings['refresh_quarters']))
                else:
                    _progress_message('个股机构持仓：无持仓新增披露，跳过报告期、持仓明细和社保比较。')
                _progress_message('个股机构持仓：股东户数更新范围 {} 只股票。'.format(len(holder_codes)))
            else:
                reason = '手动完整刷新' if full_refresh else ('首次建立基线' if previous_probe is None else '采集范围参数变化')
                _progress_message('个股机构持仓：{}，按原有断点采集。'.format(reason))
            if fund_map is None and (not targeted or review_codes):
                fund_map = load_fund_map()
            # Compact keys only: no raw rows or JSON payloads are accumulated for the preview.
            changed = set()
            install_change_tracking(Session)
            if codes:
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
                    old_codes = [] if targeted else [r[0] for r in session.query(OwnershipReport.code).filter_by(report_date=period).all()]
                scope_codes = review_codes if targeted else sorted(set(codes)|set(old_codes))
                _progress_message('{}：复查 {} 只股票，按去重索引核对相关基金报告。'.format(period, len(scope_codes)))
                selected = select_documents(period,as_of,fund_map,scope_codes)
                with session_scope(Session) as session:
                    for batch in _progress_items(list(batches(scope_codes)), '持仓分批',lambda b:b[0]):
                        save_report_batch(session,batch,period,as_of,fund_map,selected,changed)
                    head = session.query(Checkpoint).get('report:'+str(period))
                    if head is None:
                        session.add(Checkpoint(key='report:'+str(period),as_of=as_of))
                    else:
                        head.as_of = as_of
                    session.commit()
                del selected, old_codes
            # Include historical/delisted stock codes, but never load every historical JSON at once.
            if not targeted or review_codes:
                with session_scope(Session) as session:
                    social_codes = review_codes if targeted else [r[0] for r in session.query(OwnershipReport.code).distinct().all()]
                    for batch in _progress_items(list(batches(social_codes)),'社保比较',lambda b:b[0]):
                        repair_social_batch(session,batch,changed)
                    session.commit()
            if not targeted or holder_codes:
                t = finance.STK_HOLDER_NUM
                with session_scope(Session) as session:
                    head = session.query(Checkpoint).get('holders')
                    if head and head.as_of > as_of:
                        raise ValueError('股东户数已有较新结果，拒绝回退')
                    since = None if full_refresh or head is None else head.as_of-datetime.timedelta(days=30)
                conditions = [t.end_date >= start,t.end_date <= as_of,or_(t.pub_date == None,t.pub_date <= as_of)]
                if since is not None and not targeted:
                    conditions.append(or_(t.pub_date >= since,t.end_date >= since))
                with session_scope(Session) as session:
                    for batch in _progress_items(list(batches(holder_codes if targeted else codes)),'股东户数',lambda b:b[0]):
                        save_holder_batch(session,batch,conditions,start,changed)
                    head = session.query(Checkpoint).get('holders')
                    if head is None:
                        session.add(Checkpoint(key='holders',as_of=as_of))
                    else:
                        head.as_of = as_of
                    session.commit()
            # Advance only after every phase succeeds; failed runs must retry.
            with session_scope(Session) as session:
                probe = session.query(DisclosureProbe).filter_by(key='main').first()
                if probe is None:
                    session.add(DisclosureProbe(key='main', payload=signature, as_of=as_of))
                else:
                    probe.payload, probe.as_of = signature, as_of
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
        _progress_message('个股机构持仓：新增或更新 {} 条。'.format(len(result)))
        print(result.tail(10).to_string(index=False))
    except Exception as exc:
        raise RuntimeError(_safe_error(exc, DB_CONFIG)) from None
