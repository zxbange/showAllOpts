# -*- coding: utf-8 -*-
"""聚宽研究：静态股息率按上一财年已实施分红计算；三组最新结果与独立历史档案。"""
import builtins
import gc
import os
import datetime
import json
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
from sqlalchemy import Column, Date, DateTime, Index, Integer, JSON, Numeric, String, and_, or_, create_engine, text as sql_text
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from mysql_connector import get_database_config, get_database_options, mysql_drivername, engine_options
from sqlalchemy import Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy import inspect


STRATEGY_CONFIG = {'高股息选股': {'end_date': None, 'batch_size': 300, 'page_size': 2000}}


DB_CONFIG = get_database_config()
DB_OPTIONS = get_database_options()


PROGRESS_ENABLED = True  # 控制台进度条；关闭后仍保留各策略结果和最终汇总。


PROGRESS_INTERVAL_SECONDS = 5.0  # 最多每隔多少秒刷新；等待接口/数据库时也刷新耗时。


_PROGRESS_LOCAL = threading.local()


# BEGIN SHARED SELECTION ARCHIVE
def _selection_archive(_apis, _config, Base, Snapshot, Stock, rule_version, table_name,
                       date_field, calculate, validate_records, save_latest):
    """共享选股归档；计算与校验由策略提供，最新表和档案表分别提交。"""
    get_trade_days = _apis.get('get_trade_days')
    ArchiveBase = declarative_base()

    class SelectionArchive(ArchiveBase):
        __tablename__ = table_name
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        as_of_date = Column(Date, primary_key=True)
        source_kind = Column(String(32), primary_key=True)
        rule_version = Column(String(48), primary_key=True)
        status = Column(String(16), nullable=False)
        captured_at = Column(DateTime, nullable=False)
        snapshot_json = Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
        stocks_json = Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
        provenance_json = Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False)
        error_code = Column(String(80), nullable=True)

    REVISION_LIMIT = ('聚宽披露差错修正只保留最新版本，pub_date 过滤不能恢复被覆盖版本；'
                      '不能可靠还原原始时点选股。仅可显式选择 revised_asof 保存修订数据重算档案。')
    SOURCE_URL = 'https://www.joinquant.com/community/post/detailMobile?postId=21247'

    def archive_json(value):
        def encode(item):
            if isinstance(item, (datetime.date, datetime.datetime, Decimal)):
                return str(item)
            raise TypeError('不支持的档案数据类型。')
        return json.dumps(value, default=encode, ensure_ascii=False, sort_keys=True, allow_nan=False)

    def validate_archive_schema(Session):
        with session_scope(Session) as session:
            engine = session.get_bind()
            schema = inspect(engine)
            columns = {col['name']: col for col in schema.get_columns(SelectionArchive.__tablename__)}
            expected = SelectionArchive.__table__.columns
            primary = schema.get_pk_constraint(SelectionArchive.__tablename__).get('constrained_columns', [])
            if set(columns) != set(expected.keys()) or set(primary) != {c.name for c in expected if c.primary_key}:
                raise ValueError('档案表字段或主键不兼容，未修改现有表。')
            for col in expected:
                actual = columns[col.name]
                if (actual['type']._type_affinity != col.type._type_affinity
                        or actual['nullable'] != col.nullable
                        or (isinstance(col.type, String) and col.type.length is not None
                            and (actual['type'].length is None or actual['type'].length < col.type.length))
                        or (engine.dialect.name == 'mysql' and isinstance(col.type, Text)
                            and not isinstance(actual['type'], LONGTEXT))):
                    raise ValueError('档案表类型、长度或空值约束不兼容，未修改现有表。')

    def save_archive(Session, day, source_kind, meta=None, records=None, error=None):
        validate_archive_schema(Session)
        if source_kind not in ('observed', 'revised_asof'):
            raise ValueError(REVISION_LIMIT)
        if error is None:
            if meta[date_field] != day or meta['rule_version'] != rule_version:
                raise ValueError('档案日期或规则不一致。')
            validate_records(meta, records)
        with session_scope(Session) as session:
            saved = session.query(SelectionArchive).filter_by(
                as_of_date=day, source_kind=source_kind, rule_version=rule_version).with_for_update().first()
            # Successful archives are immutable, including empty valid selections.
            if saved is not None and saved.status == 'complete':
                return False
            if saved is None:
                saved = SelectionArchive(as_of_date=day, source_kind=source_kind, rule_version=rule_version)
                session.add(saved)
            saved.status = 'gap' if error is not None else 'complete'
            saved.captured_at = china_now()
            saved.snapshot_json = archive_json(meta) if error is None else None
            saved.stocks_json = archive_json(records) if error is None else None
            saved.provenance_json = archive_json(dict(provider='JoinQuant', schema_version=1,
                rule_version=rule_version, source_kind=source_kind, original_point_in_time=False,
                limitation=REVISION_LIMIT, documentation=SOURCE_URL))
            # Never persist raw exceptions, which can contain credentials or SQL parameters.
            saved.error_code = type(error).__name__[:80] if error is not None else None
            session.commit()
            return True

    def archive_history(start_date=None, end_date=None, max_days=20, source_kind='revised_asof',
                        dry_run=False, allow_partial=False, retry_gaps=False):
        if source_kind != 'revised_asof':
            raise ValueError(REVISION_LIMIT)
        if max_days is not None and (isinstance(max_days, bool) or not isinstance(max_days, int) or not 1 <= max_days <= 250):
            raise ValueError('max_days 必须为 None 或 1 到 250 的整数。')
        if not builtins.all(isinstance(v, bool) for v in (dry_run, allow_partial, retry_gaps)):
            raise ValueError('dry_run、allow_partial、retry_gaps 必须为布尔值。')
        now = china_now()
        calendar_api = _apis.get('get_all_trade_days')
        if not callable(calendar_api):
            raise ValueError('历史恢复需要 get_all_trade_days 核验完整交易日历。')
        full_calendar = calendar_api()
        if full_calendar is None:
            raise ValueError('完整交易日历缺失。')
        calendar = [as_date(value) for value in full_calendar]
        if not calendar or calendar != sorted(set(calendar)):
            raise ValueError('完整交易日历为空、重复或乱序。')
        start = as_date(start_date) if start_date is not None else calendar[0]
        end = as_date(end_date) if end_date is not None else now.date()
        if start > end or end > now.date():
            raise ValueError('日期范围无效或晚于今天。')
        end = _closed_day(end, now)
        raw_days = get_trade_days(start_date=start, end_date=end)
        if raw_days is None:
            raise ValueError('交易日历缺失。')
        days = [as_date(value) for value in raw_days]
        if days != sorted(set(days)) or builtins.any(not start <= day <= end for day in days):
            raise ValueError('交易日历重复、乱序或越界。')
        if days != [day for day in calendar if start <= day <= end]:
            raise ValueError('区间交易日历与完整交易日历不一致，拒绝漏日完成。')
        if not days:
            raise ValueError('区间内无交易日，未建立完成标记。')
        summary = dict(source_kind=source_kind, original_point_in_time=False, limitation=REVISION_LIMIT,
                       dates=[str(day) for day in days], added=0, skipped=0, gaps=[], remaining=0, unresolved_gaps=None)
        if dry_run:
            summary['remaining'] = len(days)
            return summary
        with database_scope(ArchiveBase, _config) as Session:
            validate_archive_schema(Session)
            with session_scope(Session) as session:
                previous = session.query(SelectionArchive).filter(
                    SelectionArchive.as_of_date >= start, SelectionArchive.as_of_date <= end,
                    SelectionArchive.source_kind == source_kind, SelectionArchive.rule_version == rule_version).all()
                done = {row.as_of_date for row in previous if row.status == 'complete'}
                gaps = {row.as_of_date: row.error_code for row in previous if row.status == 'gap'}
                if builtins.any(row.status not in ('complete', 'gap') for row in previous):
                    raise ValueError('档案存在未知状态，拒绝跳过。')
            summary['previous_gaps'] = [dict(date=str(day), error_code=gaps[day]) for day in sorted(gaps)]
            pending = [day for day in days if day not in done and (retry_gaps or day not in gaps)]
            limit = len(pending) if max_days is None else max_days
            summary['skipped'], summary['remaining'] = len(done), builtins.max(0, len(pending)-limit)
            for day in _progress_items(pending[:limit], '修订数据历史档案'):
                try:
                    meta, records = calculate(day)
                    if save_archive(Session, day, source_kind, meta, records):
                        summary['added'] += 1
                except Exception as exc:
                    save_archive(Session, day, source_kind, error=exc)
                    summary['gaps'].append(dict(date=str(day), error_code=type(exc).__name__))
                    if not allow_partial:
                        raise RuntimeError('{} 档案采集失败；该日保留缺口，已完成日期可续跑。'.format(day)) from None
            with session_scope(Session) as session:
                summary['unresolved_gaps'] = session.query(SelectionArchive).filter(
                    SelectionArchive.as_of_date >= start, SelectionArchive.as_of_date <= end,
                    SelectionArchive.source_kind == source_kind, SelectionArchive.rule_version == rule_version,
                    SelectionArchive.status == 'gap').count()
        return summary

    def restore_observed(day):
        """Restore an actual captured run, never silently promote a historical reconstruction."""
        day = as_date(day)
        with database_scope(ArchiveBase, _config) as Session:
            validate_archive_schema(Session)
            with session_scope(Session) as session:
                saved = session.query(SelectionArchive).filter_by(as_of_date=day,
                    source_kind='observed', rule_version=rule_version, status='complete').first()
                if saved is None:
                    raise ValueError('没有该日实际采集档案；修订重算不能冒充原始最新名单。')
                meta, records = json.loads(saved.snapshot_json), json.loads(saved.stocks_json)
        def decode(values, table):
            result = dict(values)
            if set(result) != {c.name for c in table.columns if c.name not in ('id', 'updated_at')}:
                raise ValueError('档案字段与当前表结构不一致。')
            for col in table.columns:
                value = result.get(col.name)
                if value is not None and isinstance(col.type, Date):
                    result[col.name] = as_date(value)
                elif value is not None and isinstance(col.type, Numeric):
                    result[col.name] = Decimal(value)
            return result
        meta = decode(meta, Snapshot.__table__)
        records = [decode(record, Stock.__table__) for record in records]
        with database_scope(Base, _config) as Session:
            return save_latest(Session, meta, records)
    return SimpleNamespace(ArchiveBase=ArchiveBase, SelectionArchive=SelectionArchive,
        archive_json=archive_json, validate_archive_schema=validate_archive_schema,
        save_archive=save_archive, archive_history=archive_history, restore_observed=restore_observed,
        REVISION_LIMIT=REVISION_LIMIT, SOURCE_URL=SOURCE_URL)
# END SHARED SELECTION ARCHIVE


def _replace_latest_snapshot(Session, Snapshot, Stock, meta, records,
                             result_columns, meta_columns, date_field, json_fields=()):
    """Replace a validated latest snapshot atomically; unchanged results keep their timestamp."""
    def signature(values):
        return sorted(tuple(json.dumps(r[c], sort_keys=True, ensure_ascii=False) if c in json_fields else r[c]
                            for c in result_columns) for r in values)

    with session_scope(Session) as session:
        head = session.query(Snapshot).filter_by(id=1).with_for_update().first()
        if head is not None and getattr(head, date_field) > meta[date_field]:
            raise ValueError('数据库已有更新日期，拒绝旧日期覆盖。')
        existing = session.query(Stock).all()
        old = [{c: getattr(r, c) for c in result_columns} for r in existing]
        if (head is not None and builtins.all(getattr(head, c) == meta[c] for c in meta_columns)
                and signature(old) == signature(records)):
            return False
        if head is None:
            head = Snapshot(id=1, updated_at=china_now(), **meta)
            session.add(head)
            session.flush()  # A concurrent first insertion must fail before deleting existing rows.
        else:
            for key, value in meta.items():
                setattr(head, key, value)
            head.updated_at = china_now()
        for previous in existing:
            session.expunge(previous)
        session.query(Stock).delete(synchronize_session=False)
        session.add_all([Stock(**record) for record in records])
        session.commit()
        return True


def _source_pages(fetch_page, key='id', start=-1, label='数据接口', progress=None, fields=(), page_size=None):
    """普通主键分页只保留当前页，读到空页；服务端可能返回少于请求数量。"""
    cursor, count = start, 0
    while True:
        if progress is not None:
            progress('{}：已读取 {} 条，正在请求下一页'.format(label, count))
        retry = False
        try:
            frame = fetch_page(cursor)
        except OSError as exc:
            if exc.errno != 24:
                raise
            retry = True
        if retry:
            # Retry outside except: the failed traceback must not retain SDK objects.
            # Never close arbitrary descriptors owned by the notebook or the platform SDK.
            gc.collect()
            try:
                handles = len(os.listdir('/proc/self/fd'))
            except OSError:
                handles = '未知'
            if progress is not None:
                progress('{}：句柄不足；回收后 {} 个，重试当前页 1 次'.format(label, handles))
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


def _decimal_number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None

def _round_decimal(value, places=6, precision=50):
    value = _decimal_number(value)
    if value is None:
        return None
    with localcontext() as ctx:
        ctx.prec = precision
        return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)

def _optional_date(value):
    return None if value is None or pd.isna(value) else as_date(value)

def _closed_day(end, now):
    """日行情在北京时间16点后使用当天；交易日仍由各策略的日历校验。"""
    return end - datetime.timedelta(days=1) if end == now.date() and now.hour < 16 else end

def _checked_records(frame, fields, label):
    if frame is None or not set(fields).issubset(frame.columns):
        raise ValueError('{} 返回缺失或缺少字段，取消本次入库。'.format(label))
    return frame.to_dict('records')


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
        drivername=mysql_drivername(DB_OPTIONS['driver']), host=config['host'], port=config['port'],
        username=config['dbuser'], password=config['dbpassword'], database=config['dbname'],
        query={'charset': DB_OPTIONS['charset']})
    engine = create_engine(url, **engine_options(DB_OPTIONS))
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
    locations, trace = [], exc.__traceback__
    while trace is not None:
        locations.append('{}:{}'.format(trace.tb_frame.f_code.co_name, trace.tb_lineno))
        trace = trace.tb_next
    if locations:
        message += '；位置=' + ' > '.join(locations[-6:])
    secrets = set()
    for key in ('host', 'dbname', 'dbuser', 'dbpassword'):
        value = str(config[key])
        if value:
            secrets.update((value, quote(value, safe=''), quote_plus(value)))
    for value in sorted(secrets, key=len, reverse=True):
        message = message.replace(value, '[已隐藏]')
    return '{}: {}'.format(type(exc).__name__, ' '.join(message.split())[:400])


def _dividend_strategy(_apis, _config):
    """高股息低估值：非银行25、银行10；事务替换最新快照。"""
    settings = strategy_settings('高股息选股')
    get_all_securities, get_trade_days = _apis.get('get_all_securities'), _apis.get('get_trade_days')
    get_price, get_extras = _apis.get('get_price'), _apis.get('get_extras')
    get_fundamentals, get_industry = _apis.get('get_fundamentals'), _apis.get('get_industry')
    query, valuation, finance = [_apis.get(k) for k in ('query', 'valuation', 'finance')]
    BATCH_SIZE, PAGE_SIZE = settings['batch_size'], settings['page_size']
    if BATCH_SIZE > 3000 or PAGE_SIZE > 3000:
        raise ValueError('batch_size 和 page_size 不得超过 3000。')
    END_DATE = settings['end_date']
    RULE_VERSION = 'dividend_value_pe12_v6'
    MIN_YIELD = Decimal('3')
    METHODS = ('value', 'bank')
    LIMITS = dict(value=25, bank=10)
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
        value_candidates = Column(Integer, nullable=False)
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
        dividend_cash_1y = Column(Numeric(28, 6), nullable=False)
        dividend_yield_history = Column(JSON, nullable=True)
        pe_ttm = Column(Numeric(24, 6), nullable=False)
        pb = Column(Numeric(24, 6), nullable=True)
        market_cap = Column(Numeric(24, 6), nullable=False)
        close = Column(Numeric(24, 6), nullable=False)

    RESULT_COLUMNS = [c.name for c in DividendStock.__table__.columns]
    META_COLUMNS = [c.name for c in DividendSnapshot.__table__.columns if c.name not in ('id', 'updated_at')]

    number = _decimal_number

    def rounded(value):
        return _round_decimal(value, precision=40)

    optional_date = _optional_date

    rows = _checked_records

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
        fiscal_start, fiscal_end = datetime.date(day.year-3, 1, 1), datetime.date(day.year-1, 12, 31)
        total = {}
        for offset in _progress_items(range(0, len(codes), BATCH_SIZE), '近三个财年分红批次'):
            batch = codes[offset:offset+BATCH_SIZE]
            fields = ('id', 'code', 'report_date', 'a_xr_date', 'bonus_amount_rmb')
            selection = query(*[getattr(table, f) for f in fields]).filter(
                table.code.in_(batch),
                table.report_date >= fiscal_start, table.report_date <= fiscal_end, table.a_xr_date <= day)
            fetch_page = lambda cursor: finance.run_query(selection.filter(table.id > cursor).order_by(table.id).limit(PAGE_SIZE))
            for frame in _source_pages(fetch_page, label='分红明细', progress=_progress_note, fields=fields, page_size=PAGE_SIZE):
                page = frame.to_dict('records')
                for row in page:
                    code = str(row['code'])
                    if code not in batch:
                        raise ValueError('分红代码越界。')
                    ex_date = optional_date(row['a_xr_date'])
                    period = optional_date(row['report_date'])
                    if period is None or period > day:
                        raise ValueError('{} 分红报告期缺失或晚于选股日，无法确认所属财年。'.format(code))
                    annual = fiscal_start <= period <= fiscal_end and ex_date is not None and ex_date <= day
                    if not annual:
                        raise ValueError('分红日期越界。')
                    # 按报告期分别合计最近三个完整财年；各年均以选股日市值计算股息率。
                    # 只计选股日已实施分红，不读取未来方案；cash_1y/数据库同名列保留以兼容旧表。
                    cash = Decimal(0) if pd.isna(row['bonus_amount_rmb']) else number(row['bonus_amount_rmb'])
                    if cash is None or cash < 0:
                        raise ValueError('派现金额无效。')
                    item = total.setdefault(code, dict(cash_1y=Decimal(0),
                                                       annual_cash={year: Decimal(0) for year in range(day.year-3, day.year)}))
                    item['annual_cash'][period.year] += cash
                    if period.year == day.year-1:
                        item['cash_1y'] += cash
        return total

    def candidates(caps, cash):
        # 使用未舍入的静态股息率比较；同值按代码排序。
        yields = {code: cash.get(code, {}).get('cash_1y', Decimal(0))/100/number(cap['market_cap'])
                  for code, cap in caps.items()}
        codes = sorted((code for code, cap in caps.items() if number(cap['pe_ratio']) is not None
                        and 0 < number(cap['pe_ratio']) < 12 and yields[code] > MIN_YIELD),
                       key=lambda code: (-yields[code], code))
        return codes, yields

    def select(codes, banks):
        return dict(value=[code for code in codes if code not in banks][:25],
                    bank=[code for code in codes if code in banks][:10])

    def industries_for(codes, day):
        industries, banks = {}, set()
        for offset in _progress_items(range(0, len(codes), BATCH_SIZE), '行业分类批次'):
            batch = codes[offset:offset+BATCH_SIZE]
            result = get_industry(batch, date=day)
            if not isinstance(result, dict) or set(result) != set(batch):
                raise ValueError('行业查询未完整返回，保留原快照。')
            for code in batch:
                info = result[code]
                first = info.get('sw_l1') if isinstance(info, dict) else None
                if not isinstance(first, dict) or not first.get('industry_code'):
                    raise ValueError('{} 申万一级行业缺失，无法判断银行分类。'.format(code))
                if str(first['industry_code']) == '801780':
                    banks.add(code)
            industries.update(result)
        return industries, banks

    def calculate(day):
        names, quotes, caps = universe(day)
        codes = sorted(caps)
        cash = dividends(codes, day)
        candidates_all, yields = candidates(caps, cash)
        # 所有候选先分类，再独立截取，避免混合排名导致非银行漏选。
        industries, banks = industries_for(candidates_all, day)
        selected = select(candidates_all, banks)
        _progress_note('生成非银行25只、银行10只低估值结果')
        records = []
        for method in METHODS:
            for rank, code in enumerate(selected[method], 1):
                info = industries.get(code) or {}
                level = next((k for k in ('sw_l3', 'sw_l2', 'sw_l1') if info.get(k)), None)
                industry = info.get(level, {})
                cap, payout = caps[code], cash.get(code, {})
                pe = number(cap['pe_ratio'])
                records.append(dict(method=method, code=code, trade_date=day, rank=rank, name=names[code],
                    industry_code=industry.get('industry_code'), industry_name=industry.get('industry_name'), industry_level=level,
                    dividend_yield_1y=rounded(yields[code]),
                    dividend_cash_1y=rounded(payout.get('cash_1y', 0)),
                    dividend_yield_history=[dict(fiscal_year=year, yield_pct=float(rounded(
                        payout.get('annual_cash', {}).get(year, Decimal(0))/100/number(cap['market_cap']))))
                        for year in range(day.year-1, day.year-4, -1)],
                    pe_ttm=rounded(pe), pb=rounded(cap['pb_ratio']), market_cap=rounded(cap['market_cap']), close=rounded(quotes[code])))
        meta = dict(trade_date=day, rule_version=RULE_VERSION, universe_count=len(names), eligible_count=len(caps),
                    value_candidates=len(candidates_all), value_count=len(records))
        return meta, records

    def validate_records(meta, records):
        if len({(r['method'], r['code']) for r in records}) != len(records):
            raise ValueError('同组股票重复。')
        for method in METHODS:
            group = [r for r in records if r['method'] == method]
            if len(group) > LIMITS[method] or sorted(r['rank'] for r in group) != list(range(1, len(group)+1)):
                raise ValueError('数量或排名与快照不一致。')
        if (len(records) != meta['value_count']
                or {r['code'] for r in records if r['method'] == 'value'} & {r['code'] for r in records if r['method'] == 'bank'}):
            raise ValueError('分组数量或银行分类与快照不一致。')
        if builtins.any(r['method'] not in METHODS or r['trade_date'] != meta['trade_date'] for r in records):
            raise ValueError('方法或日期不一致。')

    def save_latest(Session, meta, records):
        validate_records(meta, records)
        # 只新增可空字段，不改旧数据；DDL 独立于快照替换事务。
        with session_scope(Session) as session:
            engine = session.get_bind()
        with engine.begin() as connection:
            columns = {c['name'] for c in inspect(connection).get_columns(DividendStock.__tablename__)}
            if 'dividend_yield_history' not in columns:
                connection.execute(sql_text('ALTER TABLE dividend_selection_stock ADD COLUMN dividend_yield_history JSON NULL'))
        _progress_note('事务更新两组最新选股结果')
        return _replace_latest_snapshot(Session, DividendSnapshot, DividendStock, meta, records,
                                        RESULT_COLUMNS, META_COLUMNS, 'trade_date')

    # Archives deliberately do not share the website's latest-only tables.
    archive = _selection_archive(_apis, _config, Base, DividendSnapshot, DividendStock, RULE_VERSION,
        'dividend_selection_archive', 'trade_date', calculate, validate_records, save_latest)
    ArchiveBase, SelectionArchive = archive.ArchiveBase, archive.SelectionArchive
    archive_json, validate_archive_schema = archive.archive_json, archive.validate_archive_schema
    save_archive, archive_history = archive.save_archive, archive.archive_history
    restore_observed = archive.restore_observed
    REVISION_LIMIT, SOURCE_URL = archive.REVISION_LIMIT, archive.SOURCE_URL

    def run(end_date=END_DATE, mode='latest', **options):
        if mode == 'history':
            return archive_history(end_date=end_date, **options)
        if mode == 'restore':
            return restore_observed(**options)
        if mode != 'latest' or options:
            raise ValueError('mode 仅支持 latest、history、restore；latest 不接受额外参数。')
        now = china_now()
        end = as_date(end_date) if end_date is not None else now.date()
        if end > now.date():
            raise ValueError('结束日期不能晚于今天。')
        end = _closed_day(end, now)
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
        with database_scope(ArchiveBase, _config) as Session:
            save_archive(Session, meta['trade_date'], 'observed', meta, records)
        return pd.DataFrame(records, columns=RESULT_COLUMNS)
    return SimpleNamespace(**locals())

_API_NAMES = ('get_all_trade_days', 'get_all_securities', 'get_trade_days', 'get_price', 'get_extras', 'get_industry',
              'get_fundamentals', 'query', 'valuation', 'finance')


def run(database_config=None, **options):
    if (not isinstance(PROGRESS_ENABLED, bool) or isinstance(PROGRESS_INTERVAL_SECONDS, bool)
            or not isinstance(PROGRESS_INTERVAL_SECONDS, (int, float))
            or not math.isfinite(PROGRESS_INTERVAL_SECONDS) or PROGRESS_INTERVAL_SECONDS <= 0):
        raise ValueError('进度开关须为布尔值，刷新间隔须为有限正数。')
    apis = {key: globals().get(key, getattr(jq, key, getattr(builtins, key, None))) for key in _API_NAMES}
    with _progress_scope('高股息选股', 1, 1):
        return _dividend_strategy(apis, DB_CONFIG if database_config is None else dict(database_config)).run(**options)


if __name__ == '__main__':
    try:
        dividend_df = run()
        print('高股息选股：当前两组结果共 {} 条，数据库仅保留最新快照。'.format(len(dividend_df)))
        if len(dividend_df):
            print(dividend_df.to_string(index=False))
    except Exception as exc:
        raise RuntimeError(_safe_error(exc, DB_CONFIG)) from None
