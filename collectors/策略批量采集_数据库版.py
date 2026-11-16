# -*- coding: utf-8 -*-
"""聚宽单文件串行采集：直接粘贴到 Notebook，或 %run -i 本文件。
九个策略在本文件内独立实现；仅依赖 mysql_connector 获取统一数据库配置。
策略参数集中在开头；共用连接、会话及日期工具。用法见 docs/data/collector-batch.md。
"""
import bisect
import builtins
import datetime
import gc
import hashlib
import io
import json
import math
import os
import re
import sys
import threading
import time
import unicodedata
import uuid
import warnings
from builtins import (
    all, any, bool, callable, float, getattr, int, isinstance, len, max, min, range, set, sorted, str, sum, type,
)
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, ROUND_HALF_UP, localcontext
from statistics import median
from types import SimpleNamespace
from urllib.parse import quote, quote_plus

import jqdata as jq
import numpy as np
import pandas as pd
import sqlalchemy
from sqlalchemy import (
    BigInteger, Column, Date, DateTime, Float, Index, Integer, JSON, MetaData, Numeric, String,
    Table, Text, and_, create_engine, event, event as sql_event, func, inspect, or_, select,
    text as sql_text, tuple_,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.engine.url import URL
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from mysql_connector import (
    engine_options, get_database_config, get_database_options, mysql_drivername,
)



dt = datetime  # 估值历史采集共用日期模块。

# ==================== 集中配置区 ====================

DB_CONFIG = get_database_config()
DB_OPTIONS = get_database_options()
# 连接参数只配置一次；估值写入仅对已回滚的锁冲突重试。

# end_date=None：每次运行取北京时间当天。下列参数按策略分组配置。
STRATEGY_CONFIG = {
    '行业动量': {
        'start_date': '2025-01-01', 'end_date': None, 'industry_type': 'sw_l2',
        'retention_trade_days': 180, 'history_mode': False,
    },
    '抱团拥挤度': {
        'start_date': '2020-01-01', 'end_date': None, 'top_pct': 0.05,
    },
    '市场情绪跟踪': {
        'end_date': None, 'batch_size': 300, 'window_size': 40,
    },
    '微盘温度计': {
        'start_date': '2021-01-01', 'end_date': None, 'micro_num': 1000, 'batch_size': 1000,
        'archive_history': False,
    },
    '微盘新高新低': {
        'start_date': None, 'end_date': None, 'micro_num': 400, 'initial_days': 180,
        'batch_size': 500, 'price_batch_size': 100, 'history_mode': False,
    },
    '高股息选股': {
        'end_date': None, 'batch_size': 300, 'page_size': 2000,
    },
    'ETF动量热度': {
        'end_date': None, 'initial_trade_days': 180, 'retention_trade_days': 180,
        'min_size_yi': 30, 'history_mode': False, 'start_date': None,
        'from_platform_earliest': False, 'include_lof': True,
    },
    '个股机构持仓': {
        'end_date': None, 'history_mode': 'recent', 'history_years': 5, 'refresh_quarters': 4,
        'page_size': 1000, 'stock_batch_size': 50,
    },
    '估值标尺': {
        'start_date': '2010-01-01', 'end_date': None, 'max_days': None,
        'indices': ('000300.XSHG', '000016.XSHG', '000852.XSHG'),
    },
}

# ETF份额、两融担保比例、创红比及奶牛股由项目外部接口采集，不在此注册。
STRATEGY_ORDER = ['行业动量', '抱团拥挤度', '市场情绪跟踪', '微盘温度计', '微盘新高新低', '高股息选股', 'ETF动量热度', '个股机构持仓', '估值标尺']
STRATEGY_OPTIONS = {}  # 历史策略固定只新增；高股息选股每次重算并替换最新快照。
PREVIEW_ROWS = 5  # 每个策略展示最后几条新增记录；0 表示仅展示条数。
PROGRESS_ENABLED = True  # 控制台进度条；关闭后仍保留各策略结果和最终汇总。
PROGRESS_INTERVAL_SECONDS = 5.0  # 最多每隔多少秒刷新；等待接口/数据库时也刷新耗时。
# ==================== 公共能力 ====================
# Notebook 重复粘贴时沿用当前内核的锁；不创建锁文件。
_RUN_LOCK = globals().get('_RUN_LOCK') or threading.Lock()
_API_NAMES = ('get_all_trade_days', 'get_all_securities', 'get_trade_days', 'get_price', 'get_extras', 'get_industries', 'get_industry_stocks', 'get_index_stocks', 'get_industry', 'get_fundamentals', 'query', 'valuation', 'indicator', 'income', 'cash_flow', 'finance', 'jy')


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


# BEGIN SHARED MOMENTUM THRESHOLD


def _momentum_front_status(start_close, end_close):
    """Return strict >20% membership, or None for an invalid price pair.

    Use the decimal values supplied by the data source, without float arithmetic
    or rounding. Integer cross-products implement 5 * end > 6 * start exactly,
    including decimal inputs longer than the active Decimal context precision.
    """
    try:
        start = Decimal(str(start_close))
        end = Decimal(str(end_close))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not start.is_finite() or not end.is_finite() or start <= 0 or end <= 0:
        return None
    start_numerator, start_denominator = start.as_integer_ratio()
    end_numerator, end_denominator = end.as_integer_ratio()
    return (5 * end_numerator * start_denominator
            > 6 * start_numerator * end_denominator)
# END SHARED MOMENTUM THRESHOLD

# BEGIN SHARED RESULT JOURNAL

def _result_journal_tables():
    meta = MetaData()
    head = Table('result_sync_head', meta,
                 Column('module', String(48), primary_key=True),
                 Column('protocol', Integer, nullable=False),
                 Column('generation', String(32), nullable=False),
                 Column('revision', BigInteger, nullable=False))
    changes = Table('result_sync_change', meta,
                    Column('module', String(48), primary_key=True),
                    Column('revision', BigInteger, primary_key=True),
                    Column('payload', Text().with_variant(LONGTEXT(), 'mysql'), nullable=False))
    for table in meta.tables.values():
        table.dialect_options['mysql']['engine'] = 'InnoDB'
        table.dialect_options['mysql']['charset'] = 'utf8mb4'
    return meta, head, changes


def _install_result_journal(Session, models, module):
    """Journal every ORM write and explicitly wrapped bulk DML in the same transaction."""
    if getattr(Session, '_result_journal_module', None) == module:
        return
    meta, head, changes = _result_journal_tables()
    engine = Session.kw['bind']
    # Compile explicitly for SQLAlchemy versions predating CreateTable(if_not_exists=True).
    # Database-side IF NOT EXISTS avoids the check-then-create race across collectors.
    with engine.begin() as connection, warnings.catch_warnings():
        warnings.filterwarnings('ignore', message=r'''^\(1050, ["']Table 'result_sync_(?:change|head)' already exists["']\)$''')
        for table in meta.sorted_tables:
            ddl = str(CreateTable(table).compile(dialect=engine.dialect))
            connection.execute(sql_text(ddl.replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS', 1)))
    # Initialization races are harmless; never reset an existing generation/revision.
    with engine.begin() as connection:
        exists = connection.execute(head.select().where(head.c.module == module)).first()
    if exists is None:
        try:
            with engine.begin() as connection:
                connection.execute(head.insert().values(module=module, protocol=1,
                                   generation=uuid.uuid4().hex, revision=0))
        except IntegrityError:
            with engine.begin() as connection:
                if connection.execute(head.select().where(head.c.module == module)).first() is None:
                    raise
    tracked = tuple(models)
    info_key = 'result_journal:' + module

    def lock(session):
        info = session.info.get(info_key)
        if info is None:
            with session.no_autoflush:
                row = session.execute(head.select().where(head.c.module == module).with_for_update()).first()
            if row is None or row.protocol != 1:
                raise ValueError('结果同步日志版本无效，请检查采集版本。')
            info = {'revision': row.revision, 'records': {}, 'written': False}
            session.info[info_key] = info
        return info

    def record(session, table, keys, deleted):
        if not keys:
            return
        info = lock(session)
        for key in keys:
            values = [value.isoformat() if hasattr(value, 'isoformat') else value for value in key]
            identity = (table.name, json.dumps(values, ensure_ascii=True, separators=(',', ':')))
            info['records'][identity] = {'table': table.name, 'key': values, 'deleted': bool(deleted)}
        payload = json.dumps(list(info['records'].values()), ensure_ascii=True, separators=(',', ':'))
        with session.no_autoflush:
            if not info['written']:
                info['revision'] += 1
                session.execute(head.update().where(head.c.module == module).values(revision=info['revision']))
                session.execute(changes.insert().values(module=module, revision=info['revision'], payload=payload))
                info['written'] = True
            else:
                session.execute(changes.update().where((changes.c.module == module) &
                                (changes.c.revision == info['revision'])).values(payload=payload))

    def before_flush(session, context, instances):
        rows = [row for row in session.new if isinstance(row, tracked)]
        rows += [row for row in session.dirty if isinstance(row, tracked)
                 and session.is_modified(row, include_collections=False)]
        rows += [row for row in session.deleted if isinstance(row, tracked)]
        groups = {}
        for row in rows:
            state = inspect(row)
            keys = list(row.__table__.primary_key.columns)
            if state.persistent and state.identity != tuple(getattr(row, column.name) for column in keys):
                raise ValueError('结果同步不允许修改主键；请删除旧记录再插入新记录。')
            deleted = row in session.deleted
            group = (row.__table__, deleted)
            groups.setdefault(group, []).append(tuple(getattr(row, column.name) for column in keys))
        for (table, deleted), keys in groups.items():
            record(session, table, keys, deleted)

    def reset(session):
        session.info.pop(info_key, None)

    sql_event.listen(Session, 'before_flush', before_flush)
    sql_event.listen(Session, 'after_commit', reset)
    sql_event.listen(Session, 'after_rollback', reset)
    # sessionmaker copies this info dictionary into each new Session.
    Session.configure(info=dict(Session.kw.get('info', {}), _result_journal_callbacks=(tracked, lock, record)))
    Session._result_journal_module = module


def _result_bulk(query, values=None):
    """Serialize before selecting affected keys, then journal the exact bulk write."""
    session = query.session
    model = query.column_descriptions[0]['entity']
    registry = session.info.get('_result_journal_callbacks')
    if registry is None or model not in registry[0]:
        raise ValueError('批量写入缺少结果同步日志，拒绝无日志修改。')
    tracked, lock, record = registry
    lock(session)
    session.flush()
    table = model.__table__
    keys = query.with_entities(*table.primary_key.columns).with_for_update().all()
    if not keys:
        return 0
    record(session, table, keys, values is None)
    if values is None:
        return query.delete(synchronize_session=False)
    return query.update(values, synchronize_session=False)

# END SHARED RESULT JOURNAL


def _require_append(skip_existing):
    if skip_existing is not True:
        raise ValueError('此采集仅支持只新增，不允许关闭 skip_existing。')


def _save_record_set(Session, model, records, filters, key_fields, skip_existing=True):
    """只新增调用方限定范围内尚不存在的主键；已有记录保持原值。"""
    _require_append(skip_existing)
    if not records:
        return []
    with session_scope(Session) as session:
        rows = session.query(model).filter(*filters)
        keys = {tuple(row) for row in rows.with_entities(*(getattr(model, key) for key in key_fields)).all()}
        records = [r for r in records if tuple(r[key] for key in key_fields) not in keys]
        if not records:
            return []
        session.add_all([model(**record) for record in records])
        session.commit()
        return records

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

def _checked_trade_days(raw, start=None, end=None, count=None,
                        error='交易日历缺失、重复、乱序或越界。', missing_error=None):
    """Normalize and validate an exchange calendar before calculating or writing."""
    if raw is None:
        raise ValueError(missing_error or error)
    days = [as_date(value) for value in raw]
    if (days != sorted(set(days)) or (count is not None and len(days) != count)
            or builtins.any((start is not None and day < start) or
                            (end is not None and day > end) for day in days)):
        raise ValueError(error)
    return days


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


# ==================== 策略实现 ====================
def _momentum_strategy(_apis, _config):
    """Industry daily append and full-history reconstruction in an isolated scope."""
    settings = strategy_settings('行业动量')
    get_all_securities = _apis.get('get_all_securities')
    get_all_trade_days = _apis.get('get_all_trade_days')
    get_trade_days = _apis.get('get_trade_days')
    get_price = _apis.get('get_price')
    get_industries = _apis.get('get_industries')
    get_industry_stocks = _apis.get('get_industry_stocks')
    get_industry = _apis.get('get_industry')
    START_DATE = settings['start_date']
    END_DATE = settings['end_date']
    INDUSTRY_TYPE = settings['industry_type']
    RETENTION_TRADE_DAYS = settings['retention_trade_days']
    HISTORY_MODE = settings.get('history_mode', False)
    HISTORY_SOURCE_BASIS = 'joinquant_visible_revised_not_original_pit_v1'
    Base = declarative_base()
    RESULT_COLUMNS = ['trade_date', 'industry_type', 'industry_code', 'industry_name',
                      'momentum', 'momentum_rank', 'front_stock_count', 'industry_stock_count']

    class MysqlMomentum:
        @classmethod
        def connect_to_mysql(cls):
            return connect_database(_config)

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
            front_status = _momentum_front_status(start_close, end_close)
            if front_status is None:
                continue
            valid_count += 1
            if front_status:
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

    def save_day(Session, trade_date, industry_type, records, skip_existing=True):
        """默认事务替换日快照；只新增模式按主键过滤已存在记录。"""
        _require_append(skip_existing)
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
        installer = globals().get('_install_result_journal')
        if installer is not None:
            installer(Session, (IndustryMomentum,), 'industry-momentum')
        session = Session()
        try:
            query = session.query(IndustryMomentum).filter_by(trade_date=day, industry_type=industry_type)
            if skip_existing:
                existing = {row[0] for row in query.with_entities(IndustryMomentum.industry_code).all()}
                records = [record for record in records if record['industry_code'] not in existing]
                if not records:
                    return []
            else:
                if session.info.get('_result_journal_callbacks'):
                    _result_bulk(query)
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
        return _checked_trade_days(days, end=reference,
            error='交易日历存在未来日期、重复或乱序，未清理数据库。')[0]

    def cleanup_old_data(Session, retention_start, history_model=None):
        """一个事务清理整张表的过期数据，保留边界日及之后的快照。"""
        cutoff = as_date(retention_start)
        installer = globals().get('_install_result_journal')
        if installer is not None:
            installer(Session, (IndustryMomentum,), 'industry-momentum')
        session = Session()
        try:
            query = session.query(IndustryMomentum).filter(IndustryMomentum.trade_date < cutoff)
            if history_model is not None:
                protected = session.query(history_model).filter(
                    history_model.trade_date == IndustryMomentum.trade_date,
                    history_model.industry_type == IndustryMomentum.industry_type,
                    history_model.status == 'complete').exists()
                query = query.filter(~protected)
            deleted = (_result_bulk(query) if session.info.get('_result_journal_callbacks')
                       else query.delete(synchronize_session=False))
            session.commit()
            return deleted
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def run(start_date=START_DATE, end_date=END_DATE, industry_type=INDUSTRY_TYPE, skip_existing=True,
            history_mode=HISTORY_MODE, max_days=None):
        """普通模式使用近期采集窗口但不删历史；历史模式修复全部日期缺口。"""
        _require_append(skip_existing)
        if history_mode:
            return run_history(start_date=start_date, end_date=end_date, industry_type=industry_type,
                               skip_existing=skip_existing, max_days=max_days)
        now_china = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        today_china = now_china.date()
        if end_date is None:
            end_date = today_china
        initial_start, end = as_date(START_DATE if start_date is None else start_date), as_date(end_date)
        if end > today_china:
            raise ValueError('结束日期不能晚于运行当天。')
        if skip_existing:
            end = _closed_day(end, now_china)
            print('只新增模式：16:00 前不写入当天未完成快照。')
        # 即使手动指定历史 end_date，保留范围仍以实际运行当天为基准。
        # 日历验证在任何数据库修改之前完成，节假日自动使用最近交易日。
        retention_start = get_retention_start(today_china, RETENTION_TRADE_DAYS)
        engine = MysqlMomentum.connect_to_mysql()
        all_records = []
        try:
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine)
            # Even an up-to-date source needs a journal head when upgrading collectors.
            _install_result_journal(Session, (IndustryMomentum,), 'industry-momentum')
            print('普通模式从最近 {} 个交易日窗口续采，窗口起始日 {}；不自动删除任何历史记录。'.format(
                RETENTION_TRADE_DAYS, retention_start))
            if end < retention_start:
                print('结束日期 {} 早于普通采集窗口 {}；旧数据保留，补采请使用 run_history()。'.format(end, retention_start))
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

    class MomentumHistoryStatus(Base):
        __tablename__ = 'industry_momentum_history_status'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        trade_date = Column(Date, primary_key=True)
        industry_type = Column(String(16), primary_key=True)
        status = Column(String(32), nullable=False)
        reason = Column(String(128), nullable=False)
        source_basis = Column(String(64), nullable=False)
        payload_digest = Column(String(64), nullable=False)
        industry_codes = Column(Text, nullable=False)
        updated_at = Column(DateTime, nullable=False)

    class MomentumHistoryUnavailable(ValueError):
        """Only verified history-prefix limitations, never an API exception."""

    def momentum_history_calendar(start, end):
        # get_all_trade_days is independent of the requested window. A short range
        # response must never masquerade as an early history boundary.
        all_days = _checked_trade_days(get_all_trade_days())
        if not all_days:
            raise ValueError('完整交易日历为空，不能证明历史边界。')
        available = [d for d in all_days if d <= end]
        if not available:
            raise ValueError('结束日期前没有可验证的交易日。')
        first = available[0] if start is None else as_date(start)
        expected = [d for d in available if first <= d]
        actual = _checked_trade_days(get_trade_days(start_date=first, end_date=end), first, end)
        if actual != expected:
            raise ValueError('范围交易日历与完整交易日历不一致，停止历史恢复。')
        return available, expected

    def momentum_history_industries(day, industry_type, frame):
        if frame is None or frame.empty:
            raise ValueError('{} 行业分类为空，不能把源缺失当成零行业。'.format(day))
        codes = [str(code) for code in frame.index]
        if (len(codes) != len(set(codes)) or 'name' not in frame.columns
                or builtins.any(not code or pd.isnull(frame.loc[code, 'name']) for code in codes)):
            raise ValueError('行业列表包含重复代码或缺少名称。')
        return frame

    def calculate_momentum_history(day, industry_type, full_calendar, industry_inception=None):
        """Validate every dated universe/member/price before calculating any result."""
        day = as_date(day)
        known = [d for d in full_calendar if d <= day]
        if not known or known[-1] != day:
            raise ValueError('待恢复日期不在完整交易日历内。')
        if len(known) < 21:
            raise MomentumHistoryUnavailable('verified_calendar_warmup_21')
        window = known[-21:]
        actual = _checked_trade_days(get_trade_days(end_date=day.isoformat(), count=21), end=day, count=21)
        if actual != window:
            raise ValueError('21 日交易窗口与完整交易日历不一致。')
        raw_industries = get_industries(name=industry_type, date=day.isoformat())
        if raw_industries is not None and raw_industries.empty and industry_inception is not None and day < industry_inception:
            raise MomentumHistoryUnavailable('verified_classification_inception_' + industry_inception.isoformat())
        # None, exceptions, and empty results at/after inception remain source failures.
        industries = momentum_history_industries(day, industry_type, frame=raw_industries)
        securities = get_all_securities(types=['stock'], date=day.isoformat())
        if (securities is None or securities.empty
                or not {'start_date', 'end_date'}.issubset(securities.columns)):
            raise ValueError('历史股票列表为空或字段缺失。')
        codes = [str(code) for code in securities.index]
        if len(codes) != len(set(codes)):
            raise ValueError('历史股票列表包含重复代码。')
        cutoff = day - datetime.timedelta(days=150)
        eligible = []
        for code, row in securities.iterrows():
            start, end = as_date(row['start_date']), as_date(row['end_date'])
            if start > day or end < day or start > end:
                raise ValueError('历史股票列表含未上市或已退市股票。')
            if start < cutoff and end > day:
                eligible.append(str(code))
        if not eligible:
            raise MomentumHistoryUnavailable('verified_listing_warmup_150_calendar_days')
        members = {}
        for code in industries.index:
            raw = get_industry_stocks(code, date=day.isoformat())
            if raw is None:
                raise ValueError('行业成分返回空对象。')
            values = [str(value) for value in raw]
            if len(values) != len(set(values)) or not set(values).issubset(set(codes)):
                raise ValueError('行业成分重复或不在当日股票列表内。')
            members[str(code)] = set(values)
        # Cross-check the same dated membership from the per-security endpoint.
        # An empty industry's legitimate zero is allowed only after this check.
        assigned = {str(code): set() for code in industries.index}
        for offset in range(0, len(codes), 100):
            batch = codes[offset:offset + 100]
            info = get_industry(batch, date=day.isoformat())
            if not isinstance(info, dict) or set(info) != set(batch):
                raise ValueError('逐股行业归属未完整返回。')
            for code in batch:
                if not isinstance(info[code], dict):
                    raise ValueError('逐股行业归属格式无效。')
                industry = info[code].get(industry_type)
                if industry is not None:
                    if not isinstance(industry, dict) or industry.get('industry_code') not in assigned:
                        raise ValueError('逐股行业归属不在当日行业列表内。')
                    assigned[industry['industry_code']].add(code)
        if assigned != members:
            raise ValueError('行业成分与逐股归属不一致，不能认证成分完整性。')
        front = set()
        for offset in range(0, len(eligible), 100):
            batch = eligible[offset:offset + 100]
            frame = get_price(batch, start_date=window[0], end_date=day, frequency='daily',
                              fields=['close'], skip_paused=False, fq='pre', panel=False)
            if frame is None or not {'code', 'time', 'close'}.issubset(frame.columns):
                raise ValueError('历史行情为空或字段缺失。')
            rows = frame.to_dict('records')
            if len(rows) != len(batch) * 21:
                raise ValueError('历史行情日期/成员不完整，不能按零填补。')
            prices = {code: {} for code in batch}
            for row in rows:
                code, date = str(row['code']), as_date(row['time'])
                if code not in prices or date not in window or date in prices[code]:
                    raise ValueError('历史行情含重复、越界日期或股票。')
                if _momentum_front_status(row['close'], row['close']) is None:
                    raise ValueError('历史收盘价非正或无效。')
                prices[code][date] = row['close']
            for code in batch:
                if set(prices[code]) != set(window):
                    raise ValueError('个股历史行情缺日。')
                if _momentum_front_status(prices[code][window[0]], prices[code][day]):
                    front.add(code)
        result = []
        for code in industries.index:
            total = len(members[str(code)])
            count = len(members[str(code)] & front)
            score = Decimal(format(count * count / total if total else 0.0, '.2f'))
            result.append(dict(trade_date=day, industry_type=industry_type, industry_code=str(code),
                industry_name=str(industries.loc[code, 'name']), momentum=score,
                front_stock_count=count, industry_stock_count=total))
        result.sort(key=lambda row: (row['momentum'], row['industry_code']), reverse=True)
        for rank, row in enumerate(result, 1):
            row['momentum_rank'] = rank
        return result

    def momentum_history_digest(records):
        values = []
        for row in sorted(records, key=lambda item: item['industry_code']):
            values.append([row['industry_code'], row['industry_name'], format(Decimal(str(row['momentum'])), '.2f'),
                           row['momentum_rank'], row['front_stock_count'], row['industry_stock_count']])
        return hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(',', ':')).encode('utf-8')).hexdigest()

    def momentum_history_existing(Session, day, industry_type):
        session = Session()
        try:
            rows = session.query(IndustryMomentum).filter_by(trade_date=day, industry_type=industry_type).all()
            records = [{key: getattr(row, key) for key in RESULT_COLUMNS} for row in rows]
            status = session.query(MomentumHistoryStatus).filter_by(trade_date=day, industry_type=industry_type).first()
            if status is None or status.status != 'complete':
                return False
            expected = json.loads(status.industry_codes)
            return (status.source_basis == HISTORY_SOURCE_BASIS and bool(records)
                    and sorted(row['industry_code'] for row in records) == sorted(expected)
                    and momentum_history_digest(records) == status.payload_digest)
        finally:
            session.close()

    def save_momentum_history(Session, day, industry_type, records=None, reason=None):
        """Append missing industry keys and its completeness receipt in one transaction."""
        if records is not None:
            if not records:
                raise ValueError('历史行业结果为空。')
            for row in records:
                if row['trade_date'] != day or row['industry_type'] != industry_type:
                    raise ValueError('历史行业结果日期或分类不一致。')
                total, count, rank = row['industry_stock_count'], row['front_stock_count'], row['momentum_rank']
                if (builtins.any(isinstance(value, bool) or not isinstance(value, int) for value in (total, count, rank))
                        or total < 0 or not 0 <= count <= total or rank < 1):
                    raise ValueError('历史行业计数或排名无效。')
                score = Decimal(format(count * count / total if total else 0.0, '.2f'))
                if Decimal(str(row['momentum'])) != score:
                    raise ValueError('历史行业动量与成分统计不一致。')
            ordered = sorted(records, key=lambda row: (row['momentum'], row['industry_code']), reverse=True)
            if [row['momentum_rank'] for row in ordered] != list(range(1, len(records) + 1)):
                raise ValueError('历史行业排名不完整。')
        _install_result_journal(Session, (IndustryMomentum,), 'industry-momentum')
        session = Session()
        try:
            existing = session.query(IndustryMomentum).filter_by(trade_date=day, industry_type=industry_type).all()
            previous = {row.industry_code: {key: getattr(row, key) for key in RESULT_COLUMNS} for row in existing}
            added = []
            if records is not None:
                expected = {row['industry_code']: row for row in records}
                if not expected or len(expected) != len(records):
                    raise ValueError('历史行业结果为空或重复。')
                if not set(previous).issubset(expected):
                    raise ValueError('已存行业与历史源行业列表不一致，保留原值并停止。')
                for code, record in previous.items():
                    if momentum_history_digest([record]) != momentum_history_digest([expected[code]]):
                        raise ValueError('已存行业与重建值冲突，可能来自不同修订版本；保留原值并停止。')
                added = [row for code, row in expected.items() if code not in previous]
                session.add_all([IndustryMomentum(**row) for row in added])
            elif previous:
                raise ValueError('已有行业记录的日期不能标记为历史不可用。')
            status = session.query(MomentumHistoryStatus).filter_by(trade_date=day, industry_type=industry_type).first()
            if status is None:
                status = MomentumHistoryStatus(trade_date=day, industry_type=industry_type)
                session.add(status)
            status.status = 'complete' if records is not None else 'unavailable_prefix'
            status.reason = '' if records is not None else reason
            status.source_basis = HISTORY_SOURCE_BASIS
            status.payload_digest = momentum_history_digest(records) if records is not None else ''
            status.industry_codes = json.dumps(sorted(row['industry_code'] for row in records)) if records is not None else '[]'
            status.updated_at = datetime.datetime.utcnow()
            session.commit()
            return added
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def run_history(start_date=None, end_date=None, industry_type=INDUSTRY_TYPE,
                    skip_existing=True, max_days=None, retry_unavailable=False):
        """Full history by default; never retain-only, skip MAX or overwrite old results."""
        if skip_existing is not True:
            raise ValueError('历史恢复仅补缺，不允许覆盖已存行业结果。')
        if max_days is not None and (isinstance(max_days, bool) or not isinstance(max_days, int) or max_days <= 0):
            raise ValueError('max_days 必须为正整数或 None。')
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        end = as_date(end_date) if end_date is not None else now.date()
        if end > now.date() or (start_date is not None and as_date(start_date) > end):
            raise ValueError('历史恢复日期范围无效。')
        end = _closed_day(end, now)
        if start_date is not None and as_date(start_date) > end:
            return pd.DataFrame(columns=RESULT_COLUMNS)
        full, days = momentum_history_calendar(start_date, end)
        # Classification metadata provides positive evidence for its start date.
        # Never infer prehistory merely from a failed/empty API response.
        metadata = get_industries(name=industry_type, date=end.isoformat())
        inception = None
        if metadata is not None and not metadata.empty and 'start_date' in metadata.columns:
            inception = builtins.min(as_date(value) for value in metadata['start_date'])
        engine = MysqlMomentum.connect_to_mysql()
        output, attempted, available_started = [], 0, False
        try:
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine)
            _install_result_journal(Session, (IndustryMomentum,), 'industry-momentum')
            session = Session()
            try:
                first_saved = session.query(func.min(IndustryMomentum.trade_date)).filter_by(industry_type=industry_type).scalar()
            finally:
                session.close()
            for day in days:
                if not retry_unavailable:
                    session = Session()
                    try:
                        receipt = session.query(MomentumHistoryStatus).filter_by(trade_date=day, industry_type=industry_type).first()
                        unavailable = (receipt is not None and receipt.status == 'unavailable_prefix'
                                       and receipt.source_basis == HISTORY_SOURCE_BASIS)
                        present = session.query(IndustryMomentum).filter_by(trade_date=day, industry_type=industry_type).first()
                        if unavailable and present is None:
                            if first_saved is not None and day >= first_saved:
                                raise ValueError('历史不可用标记位于已有成功日期之后，须核查或重试。')
                            continue
                    finally:
                        session.close()
                if momentum_history_existing(Session, day, industry_type):
                    available_started = True
                    continue
                # A bound applies to actual calculations, not skipped complete days.
                if max_days is not None and attempted >= max_days:
                    break
                attempted += 1
                try:
                    records = calculate_momentum_history(day, industry_type, full, industry_inception=inception)
                except MomentumHistoryUnavailable as exc:
                    if available_started or (first_saved is not None and day >= first_saved):
                        raise ValueError('{} 成功历史之后出现不足样本，停止而不跳过。'.format(day)) from None
                    save_momentum_history(Session, day, industry_type, reason=str(exc))
                    print('{} 历史前缀不可计算：{}；未写入零值。'.format(day, str(exc)))
                    continue
                output.extend(save_momentum_history(Session, day, industry_type, records=records))
                available_started = True
                print('{} 历史行业恢复已验证；来源为当前可见修订数据，非原始时点快照。'.format(day))
            session = Session()
            try:
                unavailable_count = session.query(MomentumHistoryStatus).filter(
                    MomentumHistoryStatus.industry_type == industry_type,
                    MomentumHistoryStatus.status == 'unavailable_prefix',
                    MomentumHistoryStatus.trade_date.in_(days)).count()
            finally:
                session.close()
            print('本次检查 {} 个待计算日；新增 {} 条；范围内 {} 个历史前缀日期不可计算，未当作完成或零值。'.format(
                attempted, len(output), unavailable_count))
            if max_days is not None and attempted >= max_days:
                print('已达到 max_days；按相同参数重跑会继续其余未认证日期。')
        finally:
            engine.dispose()
        return pd.DataFrame(output, columns=RESULT_COLUMNS)
    return SimpleNamespace(**locals())


# ---------- 市场情绪跟踪 ----------

def _sentiment_strategy(_apis, _config):
    """全市场 MA20 广度；分窗口复用行情，只保存每日汇总。"""
    settings = strategy_settings('市场情绪跟踪')
    get_all_securities = _apis.get('get_all_securities')
    get_trade_days = _apis.get('get_trade_days')
    get_price = _apis.get('get_price')
    Base = declarative_base()
    RESULT_COLUMNS = ['trade_date', 'ratio', 'above_count', 'eligible_count', 'universe_count', 'updated_at']

    class MarketSentiment(Base):
        __tablename__ = 'market_sentiment'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        trade_date = Column(Date, primary_key=True)
        ratio = Column(Numeric(18, 10), nullable=False)
        above_count = Column(Integer, nullable=False)
        eligible_count = Column(Integer, nullable=False)
        universe_count = Column(Integer, nullable=False)
        updated_at = Column(DateTime, nullable=False)

    def three_years_ago(day):
        try:
            return day.replace(year=day.year - 3)
        except ValueError:  # 2 月 29 日对应三年前的 2 月 28 日。
            return day.replace(year=day.year - 3, day=28)

    def checked_days(values, start, end):
        return _checked_trade_days(values, start, end,
            error='交易日历越界、重复或乱序，取消采集。', missing_error='交易日历未返回，取消采集。')

    def security_dates():
        # date=None 获取含已退市股票的完整资料，再按各历史日期筛选，避免幸存者偏差。
        frame = get_all_securities(types=['stock'], date=None)
        if frame is None or frame.empty or not {'start_date', 'end_date'}.issubset(frame.columns):
            raise ValueError('股票上市/退市资料缺失，取消采集。')
        if frame.index.has_duplicates:
            raise ValueError('股票资料代码重复，取消采集。')
        dates = {}
        for code, row in frame.iterrows():
            if not isinstance(code, str):
                raise ValueError('股票代码无效，取消采集。')
            # 若接口提供 B 股，排除其六位代码；不排除 ST 或任何 A 股板块。
            if code.split('.')[0].startswith(('200', '900')):
                continue
            if pd.isna(row['start_date']) or pd.isna(row['end_date']):
                raise ValueError('股票上市/退市日期缺失，取消采集。')
            start, end = as_date(row['start_date']), as_date(row['end_date'])
            if start > end:
                raise ValueError('股票上市/退市日期无效，取消采集。')
            dates[code] = (start, end)
        if not dates:
            raise ValueError('未取得 A 股股票池，取消采集。')
        return dates

    def batch_counts(frame, codes, calendar, metadata):
        """校验上市期间数据完整，再计算当日收盘严格高于含当日的 MA20。"""
        if frame is None or frame.empty or not {'code', 'time', 'close', 'paused'}.issubset(frame.columns):
            raise ValueError('行情为空或缺少 close/paused/code/time，取消当前窗口入库。')
        frame = frame[['code', 'time', 'close', 'paused']].copy()
        frame['day'] = [as_date(value) for value in frame['time']]
        if (frame.duplicated(['day', 'code']).any()
                or not set(frame['code']).issubset(codes)
                or not set(frame['day']).issubset(calendar)):
            raise ValueError('行情存在重复或越界数据，取消当前窗口入库。')
        close = frame.pivot(index='day', columns='code', values='close').reindex(index=calendar, columns=codes).astype(float)
        paused = frame.pivot(index='day', columns='code', values='paused').reindex(index=calendar, columns=codes).astype(float)
        listed = np.array([[metadata[code][0] <= day <= metadata[code][1] for code in codes] for day in calendar])
        values, pauses = close.values, paused.values
        valid_price = np.isfinite(values) & (values > 0)
        valid_status = (pauses == 0) | (pauses == 1)
        if np.any(listed & ~(valid_price & valid_status)):
            raise ValueError('上市期间行情或停牌状态缺失，拒绝缩小分母写入。')
        # 历史停牌日沿用平台填充收盘价；当日停牌不计入当日分母。
        close = close.where(listed)
        mean = close.rolling(window=20, min_periods=20).mean().values
        eligible = listed & valid_price & np.isfinite(mean) & (pauses == 0)
        # 防止相等价格因浮点求均值的尾差被当作站上均线。
        above = eligible & ((values - mean) > np.maximum(np.abs(values), np.abs(mean)) * 1e-12)
        return np.column_stack((listed.sum(axis=1), eligible.sum(axis=1), above.sum(axis=1)))

    def collect_window(days, pending, metadata, batch_size):
        warmup = checked_days(get_trade_days(end_date=days[0], count=20), datetime.date(1990, 1, 1), days[0])
        if len(warmup) != 20 or warmup[-1] != days[0]:
            raise ValueError('MA20 预热交易日不足或错位，取消当前窗口入库。')
        calendar = warmup[:-1] + days
        codes = sorted(code for code, (start, end) in metadata.items() if start <= days[-1] and end >= days[0])
        if not codes:
            raise ValueError('历史股票池为空，取消当前窗口入库。')
        counts = np.zeros((len(calendar), 3), dtype=np.int64)
        offsets = list(range(0, len(codes), batch_size))
        for offset in _progress_items(offsets, '行情分批', lambda i: '{} / {} 只'.format(i, len(codes))):
            batch = codes[offset:offset + batch_size]
            _progress_note('读取 {} 至 {} 的前复权收盘价和停牌状态'.format(calendar[0], calendar[-1]))
            frame = get_price(security=batch, start_date=calendar[0], end_date=calendar[-1], frequency='daily',
                              fields=['close', 'paused'], fq='pre', skip_paused=False, panel=False)
            counts += batch_counts(frame, batch, calendar, metadata)
            del frame
        records = []
        stamp = china_now()
        for day, (universe, eligible, above) in zip(calendar, counts):
            if day not in pending:
                continue
            if eligible <= 0:
                raise ValueError('{} 没有有效 MA20 样本，拒绝写入零值。'.format(day))
            records.append(dict(trade_date=day, ratio=(Decimal(int(above)) / Decimal(int(eligible))).quantize(Decimal('0.0000000001')),
                                above_count=int(above), eligible_count=int(eligible), universe_count=int(universe), updated_at=stamp))
        return records

    def save_records(Session, records):
        _install_result_journal(Session, (MarketSentiment,), 'market-sentiment')
        dates = [record['trade_date'] for record in records]
        return _save_record_set(Session, MarketSentiment, records,
            (MarketSentiment.trade_date.in_(dates),), ('trade_date',))

    def run(end_date=settings['end_date'], start_date=None, batch_size=settings['batch_size'],
            window_size=settings['window_size'], skip_existing=True):
        if skip_existing is not True:
            raise ValueError('市场情绪仅新增，已有日期不覆盖。')
        for name, value in (('batch_size', batch_size), ('window_size', window_size)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError('{} 必须是正整数。'.format(name))
        now = china_now()
        end = now.date() if end_date is None else as_date(end_date)
        if end > now.date():
            raise ValueError('结束日期不能晚于今天。')
        end = _closed_day(end, now)
        cutoff = three_years_ago(now.date())
        start = cutoff if start_date is None else builtins.max(cutoff, as_date(start_date))
        if start > end:
            raise ValueError('采集范围须在最近三年且开始日期不晚于结束日期。')
        days = checked_days(get_trade_days(start_date=start, end_date=end), start, end)
        if not days:
            raise ValueError('指定区间未返回交易日，未清理或写入数据。')
        results = []
        with database_scope(Base, _config) as Session:
            _install_result_journal(Session, (MarketSentiment,), 'market-sentiment')
            with session_scope(Session) as session:
                # 历史补采结果永久保留；日更不得自动删除。
                removed = 0
                existing = {as_date(row[0]) for row in session.query(MarketSentiment.trade_date).filter(
                    MarketSentiment.trade_date >= start, MarketSentiment.trade_date <= end).all()}
            pending = set(days) - existing
            _progress_message('市场情绪：日常窗口缺失 {} 个交易日；历史记录保留（删除 {} 行）。'.format(len(pending), removed))
            if not pending:
                _progress_note('已有日期完整，跳过股票资料和行情查询')
                return pd.DataFrame(columns=RESULT_COLUMNS)
            metadata = security_dates()
            # 沿完整日历分窗，避免少数跨年缺口被拼成一个巨大的行情请求。
            windows = [days[i:i + window_size] for i in range(0, len(days), window_size)]
            windows = [window for window in windows if pending.intersection(window)]
            for window in _progress_items(windows, '历史窗口', lambda ds: '{}—{}'.format(ds[0], ds[-1])):
                wanted = sorted(pending.intersection(window))
                window = window[window.index(wanted[0]):window.index(wanted[-1]) + 1]
                records = collect_window(window, set(wanted), metadata, batch_size)
                _progress_note('保存 {} 个交易日的情绪汇总'.format(len(records)))
                results.extend(save_records(Session, records))
        return pd.DataFrame(results, columns=RESULT_COLUMNS)

    return SimpleNamespace(**locals())


# ---------- 抱团拥挤度 ----------
def _crowding_strategy(_apis, _config):
    """独立作用域隔离模型、参数和函数，返回本策略的接口。"""
    settings = strategy_settings('抱团拥挤度')
    get_all_securities = _apis.get('get_all_securities')
    get_trade_days = _apis.get('get_trade_days')
    get_price = _apis.get('get_price')

    START_DATE = settings['start_date']
    END_DATE = settings['end_date']
    TOP_PCT = settings['top_pct']
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
        _progress_note('获取全市场成交额，计算拥挤度')
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

    def save_day(Session, day, top_pct, record, skip_existing=True):
        _progress_note('写入当日拥挤度')
        if record['trade_date'] != as_date(day) or record['top_pct'] != Decimal(str(top_pct)):
            raise ValueError('结果日期或 TOP_PCT 不一致，拒绝入库。')
        return bool(_save_record_set(Session, MarketCrowding, [record],
            (MarketCrowding.trade_date == as_date(day), MarketCrowding.top_pct == Decimal(str(top_pct))),
            ('trade_date', 'top_pct'), skip_existing))

    def get_last_saved_date(Session, top_pct):
        """利用 (top_pct, trade_date) 主键，仅查询当前口径的最新日期。"""
        with session_scope(Session) as session:
            row = session.query(MarketCrowding.trade_date).filter(
                MarketCrowding.top_pct == Decimal(str(top_pct)),
            ).order_by(MarketCrowding.trade_date.desc()).first()
            return as_date(row[0]) if row is not None else None

    def run(start_date=START_DATE, end_date=END_DATE, top_pct=TOP_PCT, incremental=True, skip_existing=True):
        """默认从最后已入库日之后续传；incremental=False 扫描历史范围但不覆盖已有行。"""
        _require_append(skip_existing)
        now = china_now()
        today = now.date()
        start = as_date(start_date)
        end = today if end_date is None else as_date(end_date)
        if start > end or end > today:
            raise ValueError('开始日期不能晚于结束日期，结束日期不能晚于运行当天。')
        end = _closed_day(end, now)
        pct = Decimal(str(top_pct))
        if not pct.is_finite() or not Decimal('0') < pct <= Decimal('1'):
            raise ValueError('TOP_PCT 必须在 (0, 1] 内。')
        if pct != pct.quantize(Decimal('0.000001')):
            raise ValueError('TOP_PCT 最多支持六位小数。')
        if not isinstance(incremental, bool):
            raise ValueError('incremental 必须是 True 或 False。')
        records = []
        with database_scope(Base, _config) as Session:
            if incremental:
                last = get_last_saved_date(Session, top_pct)
                if last is not None:
                    if last > today:
                        raise ValueError('数据库最新日期晚于运行当天，请检查已有数据。')
                    start = builtins.max(start, last + datetime.timedelta(days=1))
            if start > end:
                return pd.DataFrame(columns=RESULT_COLUMNS)
            # 仅请求实际增量区间的日历；全部验证通过后才计算、写入日记录。
            days = _checked_trade_days(get_trade_days(start_date=start, end_date=end), start, end,
                error='交易日历越界、重复或乱序，未写入数据。', missing_error='获取交易日历失败，未写入数据。')
            if not days:
                return pd.DataFrame(columns=RESULT_COLUMNS)
            # 仅新增；默认增量模式不扫描最新入库日之前的缺口。
            for index, day in enumerate(_progress_items(days, '交易日'), 1):
                try:
                    record = calculate_crowding(day, top_pct)
                    written = save_day(Session, day, top_pct, record, skip_existing=skip_existing)
                    if not written:
                        continue
                except Exception as exc:
                    # 不输出数据库异常全文，避免日志泄露账号、地址或 SQL。
                    if isinstance(exc, ValueError):
                        raise
                    raise RuntimeError('{} 采集失败，请检查聚宽接口、数据权限和数据库连接。'.format(day)) from None
                records.append(record)
        return pd.DataFrame(records, columns=RESULT_COLUMNS)
    return SimpleNamespace(**locals())


# ---------- 微盘温度计 ----------
def _microcap_strategy(_apis, _config):
    """独立作用域隔离模型、参数和函数，返回本策略的接口。"""
    settings = strategy_settings('微盘温度计')
    if 'sample_step' in settings:
        raise ValueError('批量版固定每日采集，请移除 sample_step 配置。')
    get_all_securities = _apis.get('get_all_securities')
    get_trade_days = _apis.get('get_trade_days')
    get_all_trade_days = _apis.get('get_all_trade_days')
    get_price = _apis.get('get_price')
    get_extras = _apis.get('get_extras')
    get_fundamentals = _apis.get('get_fundamentals')
    query = _apis.get('query')
    valuation = _apis.get('valuation')
    indicator = _apis.get('indicator')

    START_DATE = settings['start_date']
    END_DATE = settings['end_date']
    MICRO_NUM = settings['micro_num']
    BATCH_SIZE = settings['batch_size']
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

    number = _decimal_number

    def rounded(value):
        return Decimal(value).quantize(Q)

    def series_key(start, micro_num, sample_step):
        return 'micro{}_{}d_{}_v1'.format(micro_num, sample_step, as_date(start).strftime('%Y%m%d'))

    def chunks(codes):
        for offset in range(0, len(codes), BATCH_SIZE):
            yield codes[offset:offset + BATCH_SIZE]

    def calendar(start, end):
        start, end = as_date(start), as_date(end)
        return _checked_trade_days(get_trade_days(start_date=start, end_date=end), start, end,
            error='交易日历越界、重复或乱序。', missing_error='交易日历获取失败。')

    def fundamental_rows(codes, table, fields, day):
        _progress_note('获取估值和财务指标')
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
        _progress_note('获取历史行情')
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

    def eligible_codes(day, minimum=0):
        _progress_note('获取股票池及 ST 状态')
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
        if len(codes) < minimum:
            return codes  # 上市期过滤后的上界已不足，无需请求更早且可能未覆盖的ST。
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

    def save_period(Session, record, skip_existing=True, invalidate_next=None):
        _require_append(skip_existing)
        _install_result_journal(Session, (MicrocapTemperature,), 'microcap-temperature')
        _progress_note('写入当前采样期温度数据')
        required = RESULT_COLUMNS + ['member_codes']
        for field in required:
            if field not in record or (record[field] is None and field != 'avg_np_growth'):
                raise ValueError('快照字段 {} 不完整。'.format(field))
        for field in PCT_FIELDS + ('score',):
            value = number(record[field])
            if value is None or not 0 <= value <= 1:
                raise ValueError('分位或温度不在 0～1 范围内。')
        with session_scope(Session) as session:
            query = session.query(MicrocapTemperature).filter_by(
                series_id=record['series_id'], trade_date=record['trade_date'])
            if query.first() is not None:
                return False
            session.add(MicrocapTemperature(**record))
            mark_daily_pending(session, record['series_id'], invalidate_next)
            session.commit()
            return True

    def mark_daily_pending(session, series_id, day):
        if day is not None:
            # 与当前日期一起提交：即使下一行原计数恰好正确，也能识别尚未完成的派生修复。
            changed = _result_bulk(session.query(MicrocapTemperature).filter_by(series_id=series_id, trade_date=day),
                {'sample_count': 0, 'updated_at': china_now()})
            if changed != 1:
                raise ValueError('待衔接日期被其他任务修改，请停止并重跑。')


    def load_daily_history(Session, series_id, end):
        """只读日期、计数及四个分位基准；无缺口时不下载成分股名单。"""
        with session_scope(Session) as session:
            fields = ('trade_date', 'sample_count', 'sample_step') + RAW_FIELDS
            rows = session.query(*[getattr(MicrocapTemperature, field) for field in fields]).filter(
                MicrocapTemperature.series_id == series_id, MicrocapTemperature.trade_date <= end,
            ).order_by(MicrocapTemperature.trade_date).all()
            return {as_date(row[0]): dict(zip(fields, row)) for row in rows}


    def stored_period(Session, series_id, day):
        with session_scope(Session) as session:
            row = session.query(MicrocapTemperature).filter_by(series_id=series_id, trade_date=day).first()
            return None if row is None else {
                column.name: getattr(row, column.name) for column in MicrocapTemperature.__table__.columns}


    def repair_daily_derived(Session, record, old, invalidate_next=None):
        _install_result_journal(Session, (MicrocapTemperature,), 'microcap-temperature')
        # 补齐更早日期后，仅修正依赖历史序列的列，不重新采集或覆盖原始指标、成分股。
        fields = ('sample_step', 'sample_count') + PCT_FIELDS + ('score', 'micro_index', 'period_return')
        updates = {field: record[field] for field in fields if record[field] != old[field]}
        if not updates:
            return
        updates['updated_at'] = china_now()
        with session_scope(Session) as session:
            changed = _result_bulk(session.query(MicrocapTemperature).filter_by(
                series_id=record['series_id'], trade_date=record['trade_date']), updates)
            if changed != 1:
                raise ValueError('每日序列被其他任务修改，请停止并重跑。')
            mark_daily_pending(session, record['series_id'], invalidate_next)
            session.commit()


    def collect_daily(Session, start, end, micro_num, archive=False):
        days = calendar(start, end)
        if not days:
            return pd.DataFrame(columns=RESULT_COLUMNS)
        series_id = series_key(start, micro_num, 1) + ('_archive' if archive else '')
        legacy_id = series_key(start, micro_num, 5)
        all_saved = load_daily_history(Session, series_id, china_now().date())
        saved = {day: row for day, row in all_saved.items() if day <= end}
        saved_dates = sorted(all_saved)
        if set(saved) - set(days):
            raise ValueError('已有每日记录不在完整交易日历中，停止写入。')
        # sample_count 也是连续性检查点。补缺后若中断，后续行计数仍旧，下次继续修正。
        first = next((i for i, day in enumerate(days)
                      if day not in saved or saved[day]['sample_count'] != i + 1 or saved[day]['sample_step'] != 1), len(days))
        missing = len(set(days) - set(saved))
        _progress_message('微盘温度计：检查 {} 至 {}，缺失 {} 个交易日。'.format(start, end, missing))
        if first == len(days):
            _progress_note('历史交易日完整，跳过行情采集和写入。')
            return pd.DataFrame(columns=RESULT_COLUMNS)
        histories = {field: sorted(Decimal(saved[day][field]) for day in days[:first]) for field in RAW_FIELDS}
        previous = stored_period(Session, series_id, days[first-1]) if first else None
        legacy = {} if archive else load_daily_history(Session, legacy_id, end)
        records = []
        reused = 0
        for day in _progress_items(days[first:], '补齐交易日'):
            _progress_note('检查已有指标并补采缺失日期 {}'.format(day))
            old = stored_period(Session, series_id, day) if day in saved else None
            record = dict(old) if old is not None else (stored_period(Session, legacy_id, day) if day in legacy else None)
            if record is None:
                record = calculate_period(day, previous, micro_num)
            else:
                # 五日采样的原始指标仍有效；指数必须按前一交易日的成分股衔接。
                record['micro_index'], record['period_return'] = index_return(previous, day, micro_num)
                reused += int(old is None)
            if record['trade_date'] != day or record['micro_num'] != micro_num:
                raise ValueError('计算结果日期或微盘数量不一致。')
            record.update(series_id=series_id, sample_step=1)
            append_percentiles(record, histories)
            following = bisect.bisect_right(saved_dates, day)
            next_day = saved_dates[following] if following < len(saved_dates) else None
            if old is not None:
                repair_daily_derived(Session, record, old, invalidate_next=next_day)
            else:
                record['updated_at'] = china_now()
                if not save_period(Session, record, skip_existing=True, invalidate_next=next_day):
                    raise ValueError('该日期已被其他任务写入，请重跑加载最新基准。')
                records.append({field: record[field] for field in RESULT_COLUMNS})
            previous = record
        _progress_message('每日序列新增 {} 行，其中复用旧采样日原始指标 {} 行。'.format(len(records), reused))
        return pd.DataFrame(records, columns=RESULT_COLUMNS)


    class MicrocapArchiveCoverage(Base):
        __tablename__ = 'microcap_archive_coverage'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        micro_num = Column(Integer, primary_key=True)
        trade_date = Column(Date, primary_key=True)
        status = Column(String(40), nullable=False)
        eligible_count = Column(Integer, nullable=True)
        updated_at = Column(DateTime, nullable=False)


    def collect_archive(Session, end, micro_num):
        """Current-visible reconstruction, not an unrevised point-in-time archive.

        Persist the tested prefix separately. Unknown source coverage is never a
        zero observation or a claim that the indicator could not exist that day.
        Once an archive has an anchor, never prepend/rebase it silently.
        """
        raw = get_all_trade_days()
        full = _checked_trade_days(raw)
        if not full:
            raise ValueError('无法验证完整历史交易日历。')
        days = [day for day in full if day <= end]
        if not days:
            return pd.DataFrame(columns=RESULT_COLUMNS)
        if calendar(days[0], days[-1]) != days:
            raise ValueError('完整历史日历与区间日历不一致。')
        session = Session()
        try:
            anchors = session.query(MicrocapTemperature.series_id).filter(
                MicrocapTemperature.series_id.like('micro{}_1d_%_v1_archive'.format(micro_num))).distinct().all()
            if len(anchors) > 1:
                raise ValueError('存在多个历史归档基点，必须明确选择，不能拼接。')
        finally:
            session.close()
        # Release the anchor query before collect_daily opens its own sessions.
        # A return inside try evaluates the nested call before finally can close it.
        if anchors:
            key = anchors[0][0]
            first = datetime.datetime.strptime(key.split('_')[2], '%Y%m%d').date()
            return collect_daily(Session, first, end, micro_num, archive=True)
        first = None
        unknown = 0
        for index, day in enumerate(days):
            count = None
            if index < 4:
                status = 'insufficient_calendar_warmup'
            else:
                codes = eligible_codes(day, minimum=micro_num)
                count = len(codes)
                if count < micro_num:
                    status = 'insufficient_eligible_securities'
                else:
                    rows = fundamental_rows(codes, valuation,
                        ('market_cap', 'turnover_ratio', 'pe_ratio', 'pb_ratio'), day)
                    if not rows:
                        status = 'unknown_valuation_coverage'
                        unknown += 1
                    else:
                        # Validation errors, missing endpoint prices and permission
                        # errors must stop, never convert them into non-computable days.
                        calculate_period(day, None, micro_num)
                        status = 'verified_anchor_current_visible'
                        first = day
            session = Session()
            try:
                old = session.query(MicrocapArchiveCoverage).filter_by(micro_num=micro_num, trade_date=day).first()
                if old is None:
                    session.add(MicrocapArchiveCoverage(micro_num=micro_num, trade_date=day,
                        status=status, eligible_count=count, updated_at=china_now()))
                else:
                    old.status, old.eligible_count, old.updated_at = status, count, china_now()
                session.commit()
            except BaseException:
                session.rollback()
                raise
            finally:
                session.close()
            if first is not None:
                break
        print('历史归档：首个已验证基点 {}；此前估值覆盖未知 {} 日。不是未修订PIT，也不承诺平台最早完整覆盖。'.format(first, unknown))
        if first is None:
            return pd.DataFrame(columns=RESULT_COLUMNS)
        return collect_daily(Session, first, end, micro_num, archive=True)


    def run(start_date=START_DATE, end_date=END_DATE, micro_num=MICRO_NUM, skip_existing=True, archive_history=None):
        _require_append(skip_existing)
        start = as_date(start_date)
        now = china_now()
        today = now.date()
        end = today if end_date is None else as_date(end_date)
        if start > end or end > today:
            raise ValueError('开始日期不能晚于结束日期，结束日期不能晚于今天。')
        end = _closed_day(end, now)
        if isinstance(micro_num, builtins.bool) or not isinstance(micro_num, builtins.int) or micro_num <= 0:
            raise ValueError('微盘数量必须是正整数。')
        with database_scope(Base, _config) as Session:
            _install_result_journal(Session, (MicrocapTemperature,), 'microcap-temperature')
            current = collect_daily(Session, start, end, micro_num)
            if archive_history is None:
                archive_history = settings.get('archive_history', False)
            if not isinstance(archive_history, builtins.bool):
                raise ValueError('archive_history 必须是布尔值。')
            if archive_history:
                if not callable(get_all_trade_days):
                    raise ValueError('历史归档需要 get_all_trade_days API。')
                collect_archive(Session, end, micro_num)
            return current
    return SimpleNamespace(**locals())


# ---------- 微盘新高新低 ----------
def _breadth_strategy(_apis, _config):
    """微盘 400 新高新低：每日动态选股，一次历史行情计算四个周期。"""
    settings = strategy_settings('微盘新高新低')
    get_all_securities = _apis.get('get_all_securities')
    get_trade_days = _apis.get('get_trade_days')
    get_all_trade_days = _apis.get('get_all_trade_days')
    get_extras = _apis.get('get_extras')
    get_fundamentals = _apis.get('get_fundamentals')
    get_price = _apis.get('get_price')
    query = _apis.get('query')
    valuation = _apis.get('valuation')
    PERIODS = (5, 10, 20, 30)
    START_DATE = settings['start_date']
    END_DATE = settings['end_date']
    MICRO_NUM = settings['micro_num']
    INITIAL_DAYS = settings['initial_days']
    BATCH_SIZE = settings['batch_size']
    PRICE_BATCH_SIZE = settings['price_batch_size']
    Base = declarative_base()

    class MicrocapBreadth(Base):
        __tablename__ = 'microcap_breadth'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        series_id = Column(String(64), primary_key=True)
        trade_date = Column(Date, primary_key=True)
        sample_count = Column(Integer, nullable=False)
        high_5 = Column(Integer, nullable=False)
        low_5 = Column(Integer, nullable=False)
        high_10 = Column(Integer, nullable=False)
        low_10 = Column(Integer, nullable=False)
        high_20 = Column(Integer, nullable=False)
        low_20 = Column(Integer, nullable=False)
        high_30 = Column(Integer, nullable=False)
        low_30 = Column(Integer, nullable=False)
        member_codes = Column(Text, nullable=False)
        updated_at = Column(DateTime, nullable=False)

    RESULT_COLUMNS = [c.name for c in MicrocapBreadth.__table__.columns if c.name != 'member_codes']

    def series_key(micro_num):
        return 'micro{}_hl_v1'.format(micro_num)

    def trade_calendar(start=None, end=None, count=None):
        args = {'end_date': end}
        if count is None:
            args['start_date'] = start
        else:
            args['count'] = count
        return _checked_trade_days(get_trade_days(**args), start, end, count,
            error='交易日历缺失、重复或越界。', missing_error='交易日历返回空对象。')

    def choose_stocks(day, micro_num, history_mode=False):
        securities = get_all_securities(types=['stock'], date=day)
        if securities is None or securities.empty or 'start_date' not in securities.columns:
            raise ValueError('股票列表为空或缺少上市日期。')
        if history_mode:
            if 'end_date' not in securities.columns:
                raise ValueError('历史股票列表缺少退市日期，不能验证当日成员。')
            for code, row in securities.iterrows():
                if as_date(row['start_date']) > day or as_date(row['end_date']) < day:
                    raise ValueError('历史股票列表包含未上市或已退市的成员。')
            if len(securities.index) != len(set(str(code) for code in securities.index)):
                raise ValueError('历史股票列表包含重复代码。')
        cutoff = day - datetime.timedelta(days=240)
        codes = sorted(str(code) for code, row in securities.iterrows()
                       if not str(code).startswith(('68', '4', '8')) and as_date(row['start_date']) < cutoff)
        if len(codes) != len(set(codes)):
            raise ValueError('股票列表包含重复代码。')
        eligible = []
        for offset in range(0, len(codes), BATCH_SIZE):
            batch = codes[offset:offset+BATCH_SIZE]
            st = get_extras('is_st', batch, start_date=day, end_date=day, df=True)
            if (st is None or len(st.index) != 1 or as_date(st.index[0]) != day
                    or set(st.columns) != set(batch) or len(st.columns) != len(batch)):
                raise ValueError('ST 状态未完整返回，停止该日采集。')
            for code in batch:
                flag = st.iloc[0][code]
                if getattr(pd, 'isnull', getattr(pd, 'isna', None))(flag) or flag not in (True, False):
                    raise ValueError('ST 状态无效。')
                if not flag:
                    eligible.append(code)
        caps = {}
        for offset in range(0, len(eligible), BATCH_SIZE):
            batch = eligible[offset:offset+BATCH_SIZE]
            frame = get_fundamentals(query(valuation.code, valuation.market_cap).filter(
                valuation.code.in_(batch)), date=day)
            if frame is None or not {'code', 'market_cap'}.issubset(frame.columns):
                raise ValueError('市值数据字段缺失。')
            rows = frame.to_dict('records')
            if len(rows) != len(batch) or {str(r['code']) for r in rows} != set(batch):
                raise ValueError('市值数据不完整或重复，不能确定真实的微盘股名单。')
            for row in rows:
                try:
                    cap = float(row['market_cap'])
                except (TypeError, ValueError):
                    raise ValueError('市值无效。') from None
                if not math.isfinite(cap) or cap <= 0:
                    raise ValueError('市值缺失或非正数。')
                caps[str(row['code'])] = cap
        selected = sorted(caps, key=lambda code: (caps[code], code))[:micro_num]
        if len(selected) != micro_num:
            if history_mode:
                raise BreadthHistoryUnavailable('verified_insufficient_pool_{}_of_{}'.format(len(selected), micro_num))
            raise ValueError('可选股票不足 {} 只。'.format(micro_num))
        return selected

    def high_low_counts(closes):
        if len(closes) != 31 or builtins.any(not math.isfinite(float(p)) or p <= 0 for p in closes):
            raise ValueError('新高新低需要连续 31 个有效交易日收盘价。')
        current = closes[-1]
        return {key: int(hit) for period in PERIODS for key, hit in (
            ('high_{}'.format(period), current >= builtins.max(closes[-period-1:-1])),
            ('low_{}'.format(period), current <= builtins.min(closes[-period-1:-1])),
        )}

    def count_prices(frame, codes, calendar):
        if frame is None or not {'code', 'time', 'close'}.issubset(frame.columns):
            raise ValueError('历史行情字段缺失。')
        rows = frame.to_dict('records')
        if len(rows) != len(codes)*len(calendar):
            raise ValueError('历史行情未完整返回，拒绝将缺失记录按零统计。')
        prices = {code: {} for code in codes}
        for row in rows:
            code, day = str(row['code']), as_date(row['time'])
            if code not in prices or day not in calendar or day in prices[code]:
                raise ValueError('行情包含重复、越界日期或股票。')
            try:
                price = float(row['close'])
            except (TypeError, ValueError):
                raise ValueError('收盘价无效。') from None
            if not math.isfinite(price) or price <= 0:
                raise ValueError('收盘价缺失或非正数。')
            prices[code][day] = price
        counts = {'{}_{}'.format(side, period): 0 for period in PERIODS for side in ('high', 'low')}
        for code in codes:
            if set(prices[code]) != set(calendar):
                raise ValueError('个股历史行情缺失。')
            for key, value in high_low_counts([prices[code][day] for day in calendar]).items():
                counts[key] += value
        return counts

    def calculate_day(day, micro_num=MICRO_NUM, history_window=None):
        codes = choose_stocks(day, micro_num, history_mode=history_window is not None)
        calendar = trade_calendar(end=day, count=31)
        if history_window is not None and calendar != history_window:
            raise ValueError('31 日窗口与完整交易日历不一致，不能视为历史预热不足。')
        if calendar[-1] != day:
            raise ValueError('采集日期不是交易日。')
        counts = {'{}_{}'.format(side, period): 0 for period in PERIODS for side in ('high', 'low')}
        for offset in range(0, len(codes), PRICE_BATCH_SIZE):
            batch = codes[offset:offset+PRICE_BATCH_SIZE]
            frame = get_price(batch, start_date=calendar[0], end_date=day, frequency='daily',
                              fields=['close'], fq='pre', skip_paused=False, panel=False)
            for key, value in count_prices(frame, batch, calendar).items():
                counts[key] += value
        return dict(counts, series_id=series_key(micro_num), trade_date=day, sample_count=micro_num,
                    member_codes=json.dumps(codes), updated_at=china_now())

    def last_date(Session, series_id):
        with session_scope(Session) as session:
            return session.query(func.max(MicrocapBreadth.trade_date)).filter_by(series_id=series_id).scalar()

    def validate_record(record):
        size = record['sample_count']
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ValueError('样本数量须为正整数。')
        if record['series_id'] != series_key(size):
            raise ValueError('序列与样本数量不一致。')
        members = json.loads(record['member_codes'])
        if not isinstance(members, list) or len(members) != size or len(set(members)) != size:
            raise ValueError('成分股名单与样本数量不一致。')
        for period in PERIODS:
            for side in ('high', 'low'):
                value = record['{}_{}'.format(side, period)]
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= size:
                    raise ValueError('新高新低数量超出范围。')
                if period > 5 and value > record['{}_5'.format(side)]:
                    raise ValueError('长期新高新低数量不能超过 5 日数量。')
    def save_day(Session, record):
        validate_record(record)
        _install_result_journal(Session, (MicrocapBreadth,), 'microcap-breadth')
        return bool(_save_record_set(Session, MicrocapBreadth, [record],
            (MicrocapBreadth.series_id == record['series_id'], MicrocapBreadth.trade_date == record['trade_date']),
            ('series_id', 'trade_date')))

    def run(start_date=START_DATE, end_date=END_DATE, micro_num=MICRO_NUM, skip_existing=True,
            history_mode=False, max_days=None, retry_unavailable=False):
        if history_mode:
            return run_history(start_date, end_date, micro_num, skip_existing, max_days, retry_unavailable)
        if skip_existing is not True:
            raise ValueError('微盘新高新低仅支持只新增，不覆盖已入库快照。')
        if isinstance(micro_num, bool) or not isinstance(micro_num, int) or micro_num <= 0:
            raise ValueError('micro_num 必须为正整数。')
        now = china_now()
        end = as_date(end_date) if end_date is not None else now.date()
        initial = as_date(start_date) if start_date is not None else end-datetime.timedelta(days=INITIAL_DAYS)
        if end > now.date() or initial > end:
            raise ValueError('日期范围无效或结束日期晚于今天。')
        end = _closed_day(end, now)
        output = []
        with database_scope(Base, _config) as Session:
            _install_result_journal(Session, (MicrocapBreadth,), 'microcap-breadth')
            latest = last_date(Session, series_key(micro_num))
            if latest and latest > now.date():
                raise ValueError('数据库最新日期晚于今天。')
            start = latest+datetime.timedelta(days=1) if latest else initial
            if start > end:
                return pd.DataFrame(columns=RESULT_COLUMNS)
            for day in trade_calendar(start=start, end=end):
                record = calculate_day(day, micro_num)
                # 每日一个事务；任何缺失或失败立即停止，不越过缺失日续传。
                if save_day(Session, record):
                    output.append({key: record[key] for key in RESULT_COLUMNS})
        return pd.DataFrame(output, columns=RESULT_COLUMNS)
    HISTORY_SOURCE_BASIS = 'joinquant_visible_revised_not_original_pit_v1'

    class BreadthHistoryUnavailable(ValueError):
        """A verified insufficient pool or warmup in the initial prefix only."""

    class BreadthHistoryStatus(Base):
        __tablename__ = 'microcap_breadth_history_status'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        series_id = Column(String(64), primary_key=True)
        trade_date = Column(Date, primary_key=True)
        status = Column(String(32), nullable=False)
        reason = Column(String(128), nullable=False)
        source_basis = Column(String(64), nullable=False)
        payload_digest = Column(String(64), nullable=False)
        updated_at = Column(DateTime, nullable=False)

    def history_calendar(start, end):
        if not callable(get_all_trade_days):
            raise ValueError('历史恢复需要聚宽 get_all_trade_days，以验证最早边界和全部日期。')
        full = _checked_trade_days(get_all_trade_days())
        if not full:
            raise ValueError('完整交易日历为空。')
        available = [day for day in full if day <= end]
        if not available:
            raise ValueError('结束日期前没有可验证交易日。')
        first = available[0] if start is None else as_date(start)
        expected = [day for day in available if day >= first]
        if trade_calendar(start=first, end=end) != expected:
            raise ValueError('范围交易日历与完整交易日历不一致。')
        return available, expected

    def history_digest(record):
        values = [record['series_id'], record['sample_count'], sorted(json.loads(record['member_codes']))]
        values += [record['{}_{}'.format(side, period)] for period in PERIODS for side in ('high', 'low')]
        return hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(',', ':')).encode('utf-8')).hexdigest()

    def history_existing(Session, day, series_id, retry_unavailable=False):
        with session_scope(Session) as session:
            record = session.query(MicrocapBreadth).filter_by(trade_date=day, series_id=series_id).first()
            status = session.query(BreadthHistoryStatus).filter_by(trade_date=day, series_id=series_id).first()
            if status is None or status.source_basis != HISTORY_SOURCE_BASIS:
                return None
            if record is None:
                if status.status == 'unavailable_prefix' and not retry_unavailable:
                    return 'unavailable_prefix'
                return None
            values = {column.name: getattr(record, column.name) for column in MicrocapBreadth.__table__.columns}
            if status.status == 'complete' and status.payload_digest == history_digest(values):
                return 'complete'
            return None

    def save_history_day(Session, day, micro_num, record=None, reason=None):
        """One append-only daily transaction includes its reconstruction receipt."""
        series_id = series_key(micro_num)
        _install_result_journal(Session, (MicrocapBreadth,), 'microcap-breadth')
        with session_scope(Session) as session:
            previous = session.query(MicrocapBreadth).filter_by(trade_date=day, series_id=series_id).first()
            added = False
            if record is not None:
                if record['trade_date'] != day or record['series_id'] != series_id or record['sample_count'] != micro_num:
                    raise ValueError('历史结果日期、序列或样本数量不一致。')
                validate_record(record)
                if previous is not None:
                    values = {column.name: getattr(previous, column.name) for column in MicrocapBreadth.__table__.columns}
                    if history_digest(values) != history_digest(record):
                        raise ValueError('已存微盘结果与当前历史重建值冲突，保留原值并停止。')
                else:
                    session.add(MicrocapBreadth(**record))
                    added = True
            elif previous is not None:
                raise ValueError('已有微盘结果不能标记为历史不可用。')
            status = session.query(BreadthHistoryStatus).filter_by(trade_date=day, series_id=series_id).first()
            if status is None:
                status = BreadthHistoryStatus(trade_date=day, series_id=series_id)
                session.add(status)
            status.status = 'complete' if record is not None else 'unavailable_prefix'
            status.reason = '' if record is not None else reason
            status.source_basis = HISTORY_SOURCE_BASIS
            status.payload_digest = history_digest(record) if record is not None else ''
            status.updated_at = china_now()
            session.commit()
            return added

    def run_history(start_date=None, end_date=None, micro_num=MICRO_NUM, skip_existing=True,
                    max_days=None, retry_unavailable=False):
        if skip_existing is not True:
            raise ValueError('历史恢复仅补缺，不覆盖已有结果。')
        if isinstance(micro_num, bool) or not isinstance(micro_num, int) or micro_num <= 0:
            raise ValueError('micro_num 必须为正整数。')
        if max_days is not None and (isinstance(max_days, bool) or not isinstance(max_days, int) or max_days <= 0):
            raise ValueError('max_days 必须为正整数或 None。')
        now = china_now()
        end = as_date(end_date) if end_date is not None else now.date()
        if end > now.date() or (start_date is not None and as_date(start_date) > end):
            raise ValueError('历史恢复日期范围无效。')
        end = _closed_day(end, now)
        if start_date is not None and as_date(start_date) > end:
            return pd.DataFrame(columns=RESULT_COLUMNS)
        full, days = history_calendar(start_date, end)
        output, attempted, available_started = [], 0, False
        with database_scope(Base, _config) as Session:
            _install_result_journal(Session, (MicrocapBreadth,), 'microcap-breadth')
            # A requested suffix after previously successful dates is not a prefix.
            with session_scope(Session) as session:
                first_saved = session.query(func.min(MicrocapBreadth.trade_date)).filter_by(series_id=series_key(micro_num)).scalar()
            for day in days:
                state = history_existing(Session, day, series_key(micro_num), retry_unavailable)
                if state == 'complete':
                    available_started = True
                    continue
                if state == 'unavailable_prefix':
                    if first_saved is not None and day >= first_saved:
                        raise ValueError('历史不可用标记位于已有成功日期之后，须人工核查或重试。')
                    continue
                if max_days is not None and attempted >= max_days:
                    break
                attempted += 1
                known = [value for value in full if value <= day]
                try:
                    if len(known) < 31:
                        raise BreadthHistoryUnavailable('verified_calendar_warmup_31')
                    record = calculate_day(day, micro_num, history_window=known[-31:])
                except BreadthHistoryUnavailable as exc:
                    if available_started or (first_saved is not None and day >= first_saved):
                        raise ValueError('{} 成功历史之后股票池不足，停止而不跳过。'.format(day)) from None
                    save_history_day(Session, day, micro_num, reason=str(exc))
                    print('{} 历史前缀不可计算：{}；未写入零值。'.format(day, str(exc)))
                    continue
                if save_history_day(Session, day, micro_num, record=record):
                    output.append({key: record[key] for key in RESULT_COLUMNS})
                available_started = True
                print('{} 新高新低历史恢复已验证；非原始时点快照。'.format(day))
            with session_scope(Session) as session:
                unavailable_count = session.query(BreadthHistoryStatus).filter(
                    BreadthHistoryStatus.series_id == series_key(micro_num),
                    BreadthHistoryStatus.status == 'unavailable_prefix',
                    BreadthHistoryStatus.trade_date.in_(days)).count()
            print('本次检查 {} 个待计算日；新增 {} 天；范围内 {} 个历史前缀日期不可计算，未当作完成或零值。'.format(
                attempted, len(output), unavailable_count))
            if max_days is not None and attempted >= max_days:
                print('已达到 max_days；按相同参数重跑会继续其余未认证日期。')
        return pd.DataFrame(output, columns=RESULT_COLUMNS)

    return SimpleNamespace(**locals())


# ---------- 高股息选股 ----------
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


def _etf_heat_strategy(_apis, _config):
    finance, query = _apis.get('finance'), _apis.get('query')
    AS_OF_DATE = None

    LOOKBACK_DAYS = 15

    MAX_STALE_TRADE_DAYS = 3

    BATCH_SIZE = 50

    PAGE_SIZE = 2000


    CROSS_BORDER_CODES = {
        '513100.XSHG', '159509.XSHE', '513030.XSHG', '513520.XSHG',
        '513690.XSHG', '513130.XSHG', '159550.XSHE',
    }

    BOND_CODES = {'511220.XSHG'}

    INCLUDE_LOF = True


    MAX_LOF_REPORT_AGE_DAYS = 180

    COMMODITY_DIRECTIONS = {
        '518880.XSHG': ('黄金现货方向', 'commodity:gold'),
        '501018.XSHG': ('原油方向（南方原油LOF）', 'commodity:oil'),
        '159985.XSHE': ('豆粕期货方向', 'commodity:soymeal'),
        '159980.XSHE': ('有色金属期货方向', 'commodity:nonferrous'),
    }

    CATEGORIES = ['宽基ETF', '行业ETF', '主题ETF', '策略/风格ETF', '跨境ETF', '商品ETF', '债券ETF', '国内LOF']

    OVERRIDES = {}

    RULES = [
        ('排除：货币ETF', '货币 现金 日利 添益 快线'),
        ('债券ETF', '债 国开 政金 政策性金融'),
        ('跨境ETF', '恒生 港股 香港 沪港深 中概 纳斯达克 纳指 标普 日经 德国 DAX 法国 东证 沙特 亚太 海外 全球 美国 日本 韩国 MSCI中国'),
        ('策略/风格ETF', '红利 低波 价值 质量 基本面 等权 高股息 自由现金流 成长 动量'),
        ('宽基ETF', '沪深300 中证A50 中证A100 中证A500 中证500 中证800 中证1000 中证2000 上证50 上证180 上证380 上证综 深证100 深证成 深证50 深证300 创业板指 创业板50 创业板综 科创50 科创100 科创200 科创综 科创创业50 国证2000 国证A'),
        ('主题ETF', '人工智能 机器人 新能源 光伏 电池 储能 智能 数字 云计算 大数据 物联网 互联网 网络安全 信息安全 创新药 疫苗 养老 环保 碳中和 绿色 一带一路 国企 央企 国资 改革 科技 创新 新材料 稀土 稀有金属 资源 5G 低空 卫星 航天'),
        ('行业ETF', '银行 证券 券商 保险 金融 医药 中药 医疗 生物 半导体 芯片 军工 国防 煤炭 钢铁 有色 化工 地产 房地产 食品 饮料 酒 农业 养殖 畜牧 家电 汽车 机械 电力 公用事业 建筑 建材 交通 运输 物流 航空 通信 传媒 计算机 软件 电子 消费 石油 油气'),
    ]

    def api(name):
        value = _apis.get(name)
        if value is None:
            raise ValueError('缺少聚宽接口：' + name)
        return value

    def text(value):
        return '' if value is None or pd.isna(value) else str(value).strip()

    def positive(value):
        value = _decimal_number(value)
        return value if value is not None and value > 0 else None

    def read_pages(table, filters=()):
        fetch_page = lambda cursor: finance.run_query(query(table).filter(*filters).filter(table.id > cursor)
                                                     .order_by(table.id).limit(PAGE_SIZE))
        frames = list(_source_pages(fetch_page, label='基金资料', progress=_progress_note))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def classify(target, override):
        if override.get('category'):
            category = override['category']
            if category not in CATEGORIES and category != '排除：货币ETF':
                raise ValueError('无效人工分类：' + category)
            return category, '人工指定'
        label = re.sub(r'\s+', '', target).upper()
        if ('黄金' in label and not builtins.any(w in label for w in ('股票', '黄金股', '产业'))) or\
                builtins.any(w in label for w in ('豆粕期货', '有色金属期货', '能源化工期货')):
            return '商品ETF', '商品标的关键词'
        for category, words in RULES:
            hit = next((word for word in words.split() if word.upper() in label), None)
            if hit and category == '宽基ETF':
                # 避免把‘中证500医药’等行业子指数误识别为宽基。
                suffix = label.split(hit.upper(), 1)[1]
                suffix = re.sub(r'[（(][^）)]*[）)]', '', suffix)
                for word in ('全收益', '净收益', '价格', '指数', '收益'):
                    suffix = suffix.replace(word, '')
                if suffix:
                    continue
            if hit:
                return category, '关键词：' + hit
        return '待核对', '请通过 OVERRIDES 指定分类'

    def commodity_direction(code, name, target_name=''):
        if code in COMMODITY_DIRECTIONS:
            return COMMODITY_DIRECTIONS[code]
        label = (name + ' ' + target_name).upper()
        # 黄金股/黄金产业股票与现货黄金不同，不能混为一个方向。
        if '黄金' in label and not builtins.any(w in label for w in ('股票', '黄金股', '产业', '矿业')):
            return '黄金现货方向', 'commodity:gold'
        if ('有色' in label and '期货' in label):
            return '有色金属期货方向', 'commodity:nonferrous'
        if '豆粕' in label and '期货' in label:
            return '豆粕期货方向', 'commodity:soymeal'
        return None

    def permitted(code, category):
        if category == '跨境ETF' and code not in CROSS_BORDER_CODES:
            return '跨境基金不在原 ETF 动量策略的七只白名单中'
        if category == '债券ETF' and code not in BOND_CODES:
            return '债券仅允许城投债 ETF 511220.XSHG'
        return None

    def lof_catalog(universe, day):
        """Use fund master classification; unknown/foreign/bond types fail closed."""
        if universe.empty:
            return []
        table = finance.FUND_MAIN_INFO
        codes = [str(c)[:6] for c in universe.index]
        info, records = {}, []
        for offset in range(0, len(codes), BATCH_SIZE):
            frame = read_pages(table, (table.main_code.in_(codes[offset:offset+BATCH_SIZE]), table.start_date <= day))
            for row in frame.to_dict('records'):
                if text(row.get('end_date')) and pd.Timestamp(row['end_date']).date() < day:
                    continue
                info.setdefault(text(row['main_code']), []).append(row)
        foreign = ('QDII', '跨境', '海外', '境外', '全球', '环球', '香港', '港股', '沪港深',
                   '纳指', '纳斯达克', '恒生', '标普', '美国', '日本', '德国', '亚太', '原油')
        for code, fund in universe.iterrows():
            name = text(fund['display_name'])
            rows = info.get(code[:6], [])
            if len(rows) != 1:
                continue
            row = rows[0]
            description = ' '.join(text(row.get(k)) for k in ('name', 'underlying_asset_type', 'invest_style', 'operate_mode')) + ' ' + name
            description = description.upper()
            if builtins.any(word in description for word in foreign):
                continue
            asset = text(row.get('underlying_asset_type'))
            if '债' in asset or '债券' in description:
                continue
            if not builtins.any(word in asset for word in ('股票', '混合')):
                continue
            # Actively managed LOFs have no common tracking index. Do not merge unrelated portfolios.
            records.append(dict(code=code, name=name, category='国内LOF', fund_type='LOF',
                                target_code='', target_name=text(row.get('name')) or name,
                                group='lof:'+code, target_source='FUND_MAIN_INFO 主体信息',
                                classification_reason='国内股票/混合 LOF，按基金代码独立保留',
                                listed_date=text(fund.get('start_date'))[:10]))
        return records

    def target_catalog(universe, day):
        table = finance.FUND_INVEST_TARGET
        frame = read_pages(table, (table.pub_date <= day, table.start_date <= day))
        if frame.empty:
            raise ValueError('有效跟踪标的表为空，请核对数据权限')
        required = {'code', 'end_date', 'traced_index_code', 'traced_index_name'}
        if not required.issubset(frame.columns):
            raise ValueError('跟踪标的字段不符：' + str(list(frame.columns)))
        mapping = {}
        for row in frame.to_dict('records'):
            if text(row['end_date']) and pd.Timestamp(row['end_date']).date() < day:
                continue
            if row['code'] in universe.index:
                pair = (text(row['traced_index_code']), text(row['traced_index_name']))
                if pair[0] or pair[1]:
                    mapping.setdefault(row['code'], set()).add(pair)
        records = []
        for code, info in universe.iterrows():
            name = text(info['display_name'])
            override = OVERRIDES.get(code, {})
            targets = mapping.get(code, set())
            if override.get('target_name'):
                targets = {(override.get('target_code', ''), override['target_name'])}
            if len(targets) > 1:
                continue
            target_code, target_name = next(iter(targets)) if targets else ('', '')
            direction = commodity_direction(code, name, target_name)
            source = '聚宽跟踪标的表'
            if not target_name:
                if direction:
                    target_name, _ = direction
                elif code in CROSS_BORDER_CODES or code in BOND_CODES:
                    target_name = name + '（指定基金方向）'
                else:
                    continue
                source = '指定基金/商品名称方向补充（非指数代码）'
            override = dict(OVERRIDES.get(target_code, {}), **override)
            category, reason = classify(target_name, override)
            group = override.get('group') or ('code:' + target_code if target_code else 'name:' + re.sub(r'\s+', '', target_name).upper())
            if direction and not override.get('category'):
                category, reason = '商品ETF', '商品方向规则：' + direction[0]
                group = override.get('group') or direction[1]
            if code in CROSS_BORDER_CODES:
                category, reason = '跨境ETF', '原 ETF 动量策略跨境白名单'
            if code in BOND_CODES:
                category, reason = '债券ETF', '指定城投债 ETF'
            excluded = permitted(code, category)
            if excluded:
                continue
            records.append(dict(code=code, name=name, category=category,
                                fund_type='ETF', listed_date=text(info.get('start_date'))[:10],
                                target_code=target_code, target_name=target_name, group=group,
                                target_source=source, classification_reason=reason))
        return records

    def load_navs(codes, days):
        # bcolz may open several files per fund; keep each request small.
        frames = []
        for offset in range(0, len(codes), 10):
            batch = codes[offset:offset + 10]
            print('ETF单位净值：{}/{} 只'.format(offset + len(batch), len(codes)), flush=True)
            gc.collect()
            for attempt in range(2):
                try:
                    frame = api('get_extras')('unit_net_value', batch,
                        start_date=days[0], end_date=days[-1], df=True)
                except OSError as exc:
                    if exc.errno != 24 or attempt:
                        raise
                else:
                    break
                # Leave except first so its traceback no longer holds SDK objects.
                gc.collect()
                print('ETF单位净值：句柄回收后重试当前 {} 只；仅重试一次'.format(len(batch)), flush=True)
            if frame is None:
                raise ValueError('单位净值接口返回 None')
            frames.append(frame.copy())
            del frame
        return pd.concat(frames, axis=1) if frames else pd.DataFrame()

    def load_sizes(codes, days):
        table, output = finance.FUND_SHARE_DAILY, {}
        for offset in range(0, len(codes), BATCH_SIZE):
            batch = codes[offset:offset + BATCH_SIZE]
            print('规模采集：{}/{} ETF'.format(builtins.min(offset + len(batch), len(codes)), len(codes)), flush=True)
            shares = read_pages(table, (table.code.in_(batch), table.date >= days[0], table.date <= days[-1]))
            navs = load_navs(batch, days)
            navs.index = pd.Index(pd.to_datetime(navs.index).date)
            if navs.index.has_duplicates:
                raise ValueError('单位净值日期重复')
            for row in shares.to_dict('records'):
                code, day = row['code'], pd.Timestamp(row['date']).date()
                if code not in batch or day not in days:
                    raise ValueError('份额数据超出查询范围')
                amount = positive(row['shares'])  # 聚宽该字段单位：份
                nav = positive(navs.at[day, code]) if day in navs.index and code in navs else None
                if amount is None or nav is None:
                    continue
                value = (amount * nav, amount, nav)
                previous = output.setdefault(code, {}).get(day)
                if previous is not None and previous != value:
                    raise ValueError('{} {} 份额记录冲突'.format(code, day))
                output[code][day] = value
        return output

    def load_lof_reports(records, as_of):
        selected = []
        table = finance.FUND_FIN_INDICATOR
        for record in records:
            code = record['code']
            print('LOF 财报规模：' + code, flush=True)
            frame = read_pages(table, (table.code == code[:6], table.pub_date <= as_of,
                                      table.period_end <= as_of))
            if frame.empty:
                continue
            rows = frame.to_dict('records')
            rows.sort(key=lambda r: (pd.Timestamp(r['period_end']).date(),
                                     pd.Timestamp(r['pub_date']).date(), r['id']), reverse=True)
            row = rows[0]
            period, published = pd.Timestamp(row['period_end']).date(), pd.Timestamp(row['pub_date']).date()
            values = {positive(r['total_tna']) for r in rows
                      if pd.Timestamp(r['period_end']).date() == period and
                      pd.Timestamp(r['pub_date']).date() == published}
            size = positive(row['total_tna'])  # 元；对应六位基金代码的份额类别（501018 为 A 类）
            if len(values) != 1 or size is None:
                continue
            if (as_of - period).days > MAX_LOF_REPORT_AGE_DAYS:
                continue
            item = dict(record, size_yuan=size, size_yi=size / Decimal('100000000'),
                        shares=None, unit_nav=positive(row['nav']), size_date=str(period),
                        size_pub_date=str(published), stale_trade_days=None,
                        size_source='FUND_FIN_INDICATOR.total_tna：对应份额类别净资产（非全类别合计）',
                        group_count=1, group_rank=1)
            if item['size_yi'] > MIN_SIZE_YI:
                selected.append(item)
        return selected

    def select_largest(records, sizes, days):
        groups, selected = {}, []
        for row in records:
            groups.setdefault(row['group'], []).append(row)
        for group, members in groups.items():
            categories = {row['category'] for row in members}
            if len(categories) != 1 or next(iter(categories)) not in CATEGORIES:
                continue
            common = set(days)
            for row in members:
                common.intersection_update(sizes.get(row['code'], {}))
            if not common:
                continue
            day = builtins.max(common)
            lag = len(days) - 1 - days.index(day)
            if lag > MAX_STALE_TRADE_DAYS:
                continue
            ranked = []
            for row in members:
                size, shares, nav = sizes[row['code']][day]
                ranked.append(dict(row, size_yuan=size, size_yi=size / Decimal('100000000'), shares=shares,
                                   size_source='同日基金份额 × 单位净值', size_pub_date=None,
                                   unit_nav=nav, size_date=str(day), stale_trade_days=lag, group_count=len(members)))
            ranked.sort(key=lambda row: (-row['size_yuan'], row['code']))
            for rank, row in enumerate(ranked, 1):
                row['group_rank'] = rank
            if ranked[0]['size_yi'] > MIN_SIZE_YI:
                selected.append(ranked[0])
        selected.sort(key=lambda row: (CATEGORIES.index(row['category']), -row['size_yuan'], row['code']))
        return selected

    def select_pool():
        if not 1 <= PAGE_SIZE <= 3000 or not 1 <= BATCH_SIZE <= 100:
            raise ValueError('PAGE_SIZE 必须为 1~3000；BATCH_SIZE 必须为 1~100')
        if LOOKBACK_DAYS <= MAX_STALE_TRADE_DAYS or MAX_STALE_TRADE_DAYS < 0:
            raise ValueError('回看天数必须大于允许滞后天数')
        if positive(MIN_SIZE_YI) is None:
            raise ValueError('规模门槛必须是正数')
        today = china_now().date()
        end = pd.Timestamp(AS_OF_DATE).date() if AS_OF_DATE else today - dt.timedelta(days=1)
        if end > today:
            raise ValueError('请指定已结束的日期，不能使用当日或未来日期')
        days = [pd.Timestamp(day).date() for day in api('get_trade_days')(end_date=end, count=LOOKBACK_DAYS)]
        if not days or days != sorted(set(days)):
            raise ValueError('交易日历为空或乱序')
        universe = api('get_all_securities')(types=['etf'], date=days[-1])
        if universe is None or universe.empty:
            raise ValueError('ETF 列表为空')
        lof = api('get_all_securities')(types=['lof'], date=days[-1])
        if lof is None:
            raise ValueError('LOF 列表查询失败')
        lof = lof.loc[~lof.index.isin(universe.index)] if INCLUDE_LOF else lof.iloc[0:0]
        _progress_note('截至 {}：筛选 {} 只 ETF、{} 只 LOF'.format(days[-1], len(universe), len(lof)))
        daily_records = target_catalog(universe, days[-1])
        lof_records = lof_catalog(lof, days[-1]) if not lof.empty else []
        sizes = load_sizes([row['code'] for row in daily_records], days)
        selected = select_largest(daily_records, sizes, days)
        if lof_records:
            selected.extend(load_lof_reports(lof_records, days[-1]))
        selected.sort(key=lambda r: (CATEGORIES.index(r['category']), -r['size_yuan'], r['code']))
        _progress_note('候选池筛选完成：入选 {} 只'.format(len(selected)))
        return pd.DataFrame(selected)

    # Collect and calculate in JoinQuant; MySQL contains final display results.
    settings = strategy_settings('ETF动量热度')
    MIN_SIZE_YI = Decimal(str(settings['min_size_yi']))
    RULE_VERSION = 'etf_close_momentum_v3'
    CALCULATION_VERSION = 'weighted-momentum-26-v1'
    Base = declarative_base()

    class Heat(Base):
        __tablename__ = 'etf_momentum_heat'
        trade_date = Column(Date, primary_key=True)
        code = Column(String(16), primary_key=True)
        name = Column(String(128), nullable=False)
        score = Column(Numeric(28, 10))
        close = Column(Numeric(24, 8))
        annual_return = Column(Numeric(28, 10))
        r2 = Column(Numeric(24, 10))
        reference_pass = Column(Integer)
        details = Column(JSON, nullable=False)
        updated_at = Column(DateTime, nullable=False)
        __table_args__ = (Index('ix_etf_heat_code_date', 'code', 'trade_date'),)

    class Pool(Base):
        __tablename__ = 'etf_momentum_pool'
        id = Column(Integer, primary_key=True)
        as_of_date = Column(Date, nullable=False)
        rule_version = Column(String(48), nullable=False)
        members = Column(JSON, nullable=False)
        updated_at = Column(DateTime, nullable=False)


    def metrics(prices, paused=False):
        empty = dict(score=None, annual_return=None, r2=None, reference_pass=None,
                     details={'status': 'missing', 'reason': '有效价格不足26期或当日停牌'})
        if paused or len(prices) != 26 or builtins.any(p is None or not math.isfinite(float(p)) or p <= 0 for p in prices):
            return empty
        # A constant price scale does not affect regression slope or R².
        y = [math.log(float(p) / float(prices[0])) for p in prices]
        weights = [1 + i / 25 for i in range(26)]
        squared = [w*w for w in weights]
        total = builtins.sum(squared)
        mx = builtins.sum(w*i for i, w in enumerate(squared)) / total
        my = builtins.sum(w*v for w, v in zip(squared, y)) / total
        slope = builtins.sum(w*(i-mx)*(v-my) for i, (w,v) in enumerate(zip(squared,y))) / builtins.sum(w*(i-mx)**2 for i,w in enumerate(squared))
        intercept = my-slope*mx
        residual = builtins.sum(w*(v-(slope*i+intercept))**2 for i,(w,v) in enumerate(zip(weights,y)))
        average = builtins.sum(y)/26
        variance = builtins.sum(w*(v-average)**2 for w,v in zip(weights,y))
        r2 = 1-residual/variance if variance > 1e-25 else 0.0
        try:
            annual = math.expm1(slope*250)
            values = [annual*r2, annual, r2]
            if builtins.any(not math.isfinite(v) or abs(v) >= 1e18 for v in values):
                raise OverflowError
        except OverflowError:
            return dict(empty, details={'status': 'missing', 'reason': '异常价格导致计算越界'})
        score, annual, r2 = [Decimal(str(v)).quantize(Decimal('0.0000000001')) for v in values]
        return dict(score=score, annual_return=annual, r2=r2, reference_pass=None,
                    details={'status': 'ok', 'calculation_version': CALCULATION_VERSION})

    def price_value(value):
        if not positive(value):
            return None
        result = Decimal(str(value)).quantize(Decimal('0.00000001'))
        if result >= Decimal('1e16'):
            raise ValueError('价格超出数据库精度')
        return result


    def save_rows(Session, records, repair_missing=False):
        _install_result_journal(Session, (Heat, Pool), 'etf-momentum')
        keys = {(r['trade_date'], r['code']) for r in records}
        if len(keys) != len(records):
            raise ValueError('交易日和基金代码重复')
        if not records:
            return []
        with session_scope(Session) as session:
            existing = {(r[0], r[1]): r[2] for r in session.query(Heat.trade_date, Heat.code, Heat.score).filter(
                Heat.code.in_(set(r['code'] for r in records)),
                Heat.trade_date >= builtins.min(r['trade_date'] for r in records),
                Heat.trade_date <= builtins.max(r['trade_date'] for r in records)).all()}
            added = [r for r in records if (r['trade_date'], r['code']) not in existing]
            session.add_all([Heat(**r) for r in added])
            repaired = []
            if repair_missing:
                for row in records:
                    if (row['trade_date'], row['code']) not in existing or existing[(row['trade_date'], row['code'])] is not None or row['score'] is None:
                        continue
                    values = {key: value for key, value in row.items() if key not in ('trade_date', 'code')}
                    changed = _result_bulk(session.query(Heat).filter(
                        Heat.trade_date == row['trade_date'], Heat.code == row['code'],
                        Heat.score.is_(None)), values)
                    if changed:
                        repaired.append(row)
            session.commit()
            if repaired:
                _progress_note('修复 {} 条历史空热度，已有有效得分保持不变'.format(len(repaired)))
            return added + repaired


    def get_retention_start(reference_date):
        """Validate the complete calendar before allowing any database deletion."""
        count, reference = settings['retention_trade_days'], as_date(reference_date)
        return _checked_trade_days(api('get_trade_days')(end_date=reference, count=count),
            end=reference, count=count,
            error='保留范围交易日历不足、重复、乱序或含未来日期，未清理数据库')[0]

    def cleanup_old_data(Session, retention_start):
        _install_result_journal(Session, (Heat, Pool), 'etf-momentum')
        """Remove expired results for all funds, including funds outside the current pool."""
        with session_scope(Session) as session:
            deleted = _result_bulk(session.query(Heat).filter(Heat.trade_date < retention_start))
            session.commit()
        _progress_note('保留最近 {} 个交易日（{} 起），已清理 {} 条过期热度'.format(
            settings['retention_trade_days'], retention_start, deleted))
        return deleted

    # Separate tables: ETF-only history must never enter the mixed ETF/LOF chart.
    HistoryBase = declarative_base()

    class HistoryHeat(HistoryBase):
        __tablename__ = 'etf_momentum_heat_etf_history'
        trade_date = Column(Date, primary_key=True)
        code = Column(String(16), primary_key=True)
        name = Column(String(128), nullable=False)
        score = Column(Numeric(28, 10))
        close = Column(Numeric(24, 8))
        annual_return = Column(Numeric(28, 10))
        r2 = Column(Numeric(24, 10))
        reference_pass = Column(Integer)
        details = Column(JSON, nullable=False)
        updated_at = Column(DateTime, nullable=False)

    class HistoryPool(HistoryBase):
        __tablename__ = 'etf_momentum_pool_etf_history'
        trade_date = Column(Date, primary_key=True)
        rule_version = Column(String(48), nullable=False)
        members = Column(JSON, nullable=False)
        updated_at = Column(DateTime, nullable=False)

    def historical_pool(day):
        days = _checked_trade_days(api('get_trade_days')(end_date=day, count=LOOKBACK_DAYS), end=day)
        if not days or days[-1] != day:
            raise ValueError('{} 规模交易日历缺失'.format(day))
        universe = api('get_all_securities')(types=['etf'], date=day)
        if universe is None or universe.index.has_duplicates:
            raise ValueError('{} ETF历史列表缺失或重复'.format(day))
        if universe.empty:
            return []
        table = finance.FUND_INVEST_TARGET
        frame = read_pages(table, (table.pub_date <= day, table.start_date <= day))
        required = {'code', 'pub_date', 'start_date', 'end_date', 'traced_index_code', 'traced_index_name'}
        if not required.issubset(frame.columns):
            raise ValueError('{} 历史跟踪标的字段或数据缺失'.format(day))
        targets = {}
        for row in frame.to_dict('records'):
            if not text(row['pub_date']) or not text(row['start_date']):
                raise ValueError('{} 跟踪标的缺少生效/发布日期'.format(day))
            if as_date(row['pub_date']) > day or as_date(row['start_date']) > day:
                raise ValueError('{} 跟踪标的含未来记录'.format(day))
            if text(row['end_date']) and as_date(row['end_date']) < day:
                continue
            if row['code'] in universe.index:
                targets.setdefault(row['code'], set()).add((text(row['traced_index_code']), text(row['traced_index_name'])))
        historical = universe.copy()
        for code in historical.index:
            pairs = targets.get(code, set())
            if len(pairs) != 1 or not next(iter(pairs))[1]:
                raise ValueError('{} {} 历史跟踪标的不唯一或缺失，不能重建完整ETF候选排名'.format(day, code))
            # Names returned by the listing API need not be historically versioned.
            historical.loc[code, 'display_name'] = next(iter(pairs))[1]
        records = target_catalog(historical, day)
        for row in records:
            if row['category'] not in CATEGORIES:
                raise ValueError('{} {} 历史分类无法核实'.format(day, row['code']))
        sizes = load_sizes([row['code'] for row in records], days)
        groups = {}
        for row in records:
            groups.setdefault(row['group'], []).append(row)
        for members in groups.values():
            common = set(days)
            for member in members:
                common.intersection_update(sizes.get(member['code'], {}))
            if not common or len(days)-1-days.index(builtins.max(common)) > MAX_STALE_TRADE_DAYS:
                raise ValueError('{} 候选组历史规模不完整：{}'.format(day, ','.join(r['code'] for r in members)))
            if len({r['category'] for r in members}) != 1:
                raise ValueError('{} 候选组分类冲突'.format(day))
        return select_largest(records, sizes, days)

    def history_record(member, day):
        code = member['code']
        warm = _checked_trade_days(api('get_trade_days')(end_date=day, count=26), end=day)
        if not warm or warm[-1] != day:
            raise ValueError('{} 行情预热交易日历缺失'.format(day))
        frames = []
        for fields, adjustment in [(['close', 'paused'], None), (['close'], 'post')]:
            frame = api('get_price')(code, start_date=warm[0], end_date=day, frequency='daily', fields=fields, skip_paused=False, fq=adjustment)
            if frame is None or frame.empty or not set(fields).issubset(frame.columns):
                raise ValueError('{} {} 历史行情缺失'.format(day, code))
            frame = frame.copy()
            frame.index = pd.Index(pd.to_datetime(frame.index).date)
            if frame.index.has_duplicates or not frame.index.is_monotonic_increasing or not set(frame.index).issubset(warm):
                raise ValueError('{} {} 历史行情日期异常'.format(day, code))
            frames.append(frame)
        raw, adjusted = frames
        listed = as_date(member['listed_date']) if member.get('listed_date') else warm[0]
        for date in warm:
            if date < listed:
                continue
            if date not in raw.index or date not in adjusted.index:
                raise ValueError('{} {} 历史行情缺口'.format(date, code))
            paused = raw.loc[date, 'paused']
            if pd.isna(paused) or paused not in (0, 1):
                raise ValueError('{} {} 停牌状态无效'.format(date, code))
            if not paused and (positive(raw.loc[date, 'close']) is None or positive(adjusted.loc[date, 'close']) is None):
                raise ValueError('{} {} 历史价格无效'.format(date, code))
        values = metrics([positive(adjusted.loc[d, 'close']) if d in adjusted.index else None for d in warm], bool(raw.loc[day, 'paused']))
        values['details'].update(selection_scope='etf_only_point_in_time', selection_date=str(day), display_name_source='published_tracking_target', rule_version=RULE_VERSION)
        return dict(trade_date=day, code=code, name=member['name'], close=price_value(raw.loc[day, 'close']), updated_at=china_now(), **values)

    def run_history(start_date=None, end_date=None, from_platform_earliest=False, include_lof=True, repair_missing=False):
        if include_lof is not False:
            raise ValueError('无法核实 FUND_MAIN_INFO 的历史LOF分类；混合候选池历史恢复已跳过。显式 include_lof=False 仅恢复独立ETF历史表，不写原混合图表。')
        if not isinstance(from_platform_earliest, bool) or not isinstance(repair_missing, bool):
            raise ValueError('历史开关必须为布尔值')
        if from_platform_earliest and start_date is not None:
            raise ValueError('start_date 与 from_platform_earliest 不能同时指定')
        if not from_platform_earliest and start_date is None:
            raise ValueError('历史恢复须指定 start_date 或 from_platform_earliest=True')
        now = china_now()
        end = as_date(end_date) if end_date is not None else now.date()
        if end > now.date():
            raise ValueError('结束日期不能晚于今天')
        end = _closed_day(end, now)
        if from_platform_earliest:
            all_days = _checked_trade_days(api('get_all_trade_days')())
            if not all_days:
                raise ValueError('平台完整交易日历为空')
            days = [day for day in all_days if day <= end]
        else:
            start = as_date(start_date)
            if start > end:
                raise ValueError('开始日期晚于结束日期')
            days = _checked_trade_days(api('get_trade_days')(start_date=start, end_date=end), start=start, end=end)
        if not days:
            raise ValueError('请求范围无交易日')
        report = []
        with database_scope(HistoryBase, _config) as Session:
            for day in _progress_items(days, 'ETF独立历史恢复', str):
                try:
                    members = historical_pool(day)
                    encoded = json.loads(json.dumps(members, default=str, ensure_ascii=False, allow_nan=False))
                    with session_scope(Session) as session:
                        existing = {row.code: row for row in session.query(HistoryHeat).filter_by(trade_date=day).all()}
                        head = session.query(HistoryPool).filter_by(trade_date=day).first()
                        if head and (head.rule_version != RULE_VERSION or head.members != encoded):
                            raise ValueError('历史候选池与已存快照不一致，拒绝覆盖')
                        if set(existing) - {m['code'] for m in members}:
                            raise ValueError('已有历史结果不属于当日候选池，拒绝混用')
                        added = 0
                        for member in members:
                            old = existing.get(member['code'])
                            if old is not None and (old.score is not None or not repair_missing):
                                continue
                            row = history_record(member, day)
                            if old is None:
                                session.add(HistoryHeat(**row))
                                added += 1
                            elif row['score'] is not None:
                                for key, value in row.items():
                                    setattr(old, key, value)
                                added += 1
                        if head is None:
                            session.add(HistoryPool(trade_date=day, rule_version=RULE_VERSION, members=encoded, updated_at=china_now()))
                        session.commit()
                    report.append(dict(trade_date=day, status='COMPLETE', members=len(members), written=added, error=''))
                except Exception as exc:
                    report.append(dict(trade_date=day, status='FAILED', members=None, written=0, error=_safe_error(exc, _config)))
                    _progress_note('{} 历史恢复失败：{}'.format(day, report[-1]['error']))
        result = pd.DataFrame(report)
        if builtins.any(row['status'] == 'FAILED' for row in report):
            error = ValueError('ETF独立历史恢复存在失败日期；详见异常 report，重跑会补全部缺口')
            error.report = result
            raise error
        return result

    def run(end_date=None, skip_existing=True, repair_missing=False, history_mode=None,
            start_date=None, from_platform_earliest=None, include_lof=None):
        nonlocal AS_OF_DATE
        history = settings.get('history_mode', False) if history_mode is None else history_mode
        if not isinstance(history, bool):
            raise ValueError('history_mode 必须为布尔值')
        if history:
            if skip_existing is not True:
                raise ValueError('历史恢复不覆盖已有有效记录')
            return run_history(start_date=start_date if start_date is not None else settings.get('start_date'),
                               end_date=end_date or settings['end_date'],
                               from_platform_earliest=settings.get('from_platform_earliest', False) if from_platform_earliest is None else from_platform_earliest,
                               include_lof=settings.get('include_lof', True) if include_lof is None else include_lof,
                               repair_missing=repair_missing)
        if start_date is not None or from_platform_earliest is not None or include_lof is not None:
            raise ValueError('历史参数需要 history_mode=True')
        if not isinstance(repair_missing, bool):
            raise ValueError('repair_missing 须为 True 或 False')
        if skip_existing is not True:
            raise ValueError('只允许新增；已有日期和基金的记录不覆盖')
        now = china_now()
        requested = as_date(end_date or settings['end_date']) if (end_date or settings['end_date']) else now.date()
        if requested > now.date():
            raise ValueError('结束日期不能晚于今天')
        # Retention follows the actual run date even when end_date is historical.
        retention_start = get_retention_start(now.date())
        end = _closed_day(requested, now)
        days = [as_date(d) for d in api('get_trade_days')(end_date=end, count=settings['initial_trade_days'])]
        if not days or days != sorted(set(days)) or days[-1] > end:
            raise ValueError('交易日历无效')
        days = [day for day in days if day >= retention_start]
        output = []
        with database_scope(Base, _config) as Session:
            _install_result_journal(Session, (Heat, Pool), 'etf-momentum')
            # Keep confirmed history, including funds no longer in the current pool.
            if not days:
                return pd.DataFrame(output)
            AS_OF_DATE = str(days[-1])
            _progress_note('筛选 ETF/LOF 候选池，计算热度后入库')
            chosen = select_pool()
            if chosen.empty:
                raise ValueError('本次未取得有效候选池，保留旧快照；请运行分类研究脚本查看待核对清单')
            members = []
            for row in chosen.to_dict('records'):
                members.append({key: (str(value) if isinstance(value, Decimal) else
                                (None if pd.isna(value) else (value.item() if hasattr(value, 'item') else value)))
                                for key, value in row.items()})
            members = json.loads(json.dumps(members, ensure_ascii=False, allow_nan=False))
            with session_scope(Session) as session:
                head = session.query(Pool).filter_by(id=1).first()
                if head and (head.as_of_date > days[-1] or head.rule_version not in
                             (RULE_VERSION, 'etf_close_momentum_v1', 'etf_close_momentum_v2', 'etf_raw_market_v1')):
                    raise ValueError('已有更新日期或未知规则版本，拒绝混用')
                repair = repair_missing or head is None or head.rule_version != RULE_VERSION
            if repair:
                _progress_note('检查当前候选的历史空热度，重新计算后仅填补有效结果')
            for member in _progress_items(members, 'ETF/LOF 热度计算', lambda r: r['name']):
                code = member['code']
                with session_scope(Session) as session:
                    saved = session.query(Heat.trade_date, Heat.score).filter(Heat.code == code).all()
                    existing = {r[0] for r in saved}
                    empty_dates = {r[0] for r in saved if r[1] is None} if repair else set()
                if existing and builtins.max(existing) > days[-1]:
                    raise ValueError('已有更新日期的基金结果，拒绝回退')
                # Examine every gap in the live request window; old saved rows do
                # not extend this request or get pruned as a side effect.
                first = days[0]
                calendar = [as_date(d) for d in api('get_trade_days')(start_date=first, end_date=days[-1])]
                if not calendar or calendar != sorted(set(calendar)) or calendar[0] < first or calendar[-1] != days[-1]:
                    raise ValueError('增量交易日历无效')
                listed = as_date(member['listed_date']) if member.get('listed_date') else calendar[0]
                missing = [d for d in calendar if d >= listed and (d not in existing or d in empty_dates)]
                if not missing:
                    continue
                warm = [as_date(d) for d in api('get_trade_days')(end_date=missing[0], count=26)]
                if not warm or warm != sorted(set(warm)) or warm[-1] != missing[0]:
                    raise ValueError('无法取得行情预热日期')
                window_days = [as_date(d) for d in api('get_trade_days')(start_date=warm[0], end_date=days[-1])]
                if window_days != sorted(set(window_days)) or not set(missing).issubset(window_days):
                    raise ValueError('计算窗口交易日历无效')
                frames = []
                for fields, adjustment in [(['close', 'paused'], None), (['close'], 'post')]:
                    frame = api('get_price')(code, start_date=warm[0], end_date=days[-1], frequency='daily',
                                             fields=fields, skip_paused=False, fq=adjustment)
                    if frame is None or frame.empty or not set(fields).issubset(frame.columns):
                        raise ValueError(code + ' 行情接口无数据，未发布新候选池')
                    frame = frame.copy()
                    frame.index = pd.Index(pd.to_datetime(frame.index).date)
                    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
                        raise ValueError(code + ' 行情日期重复或乱序')
                    frames.append(frame)
                raw, adjusted = frames
                # Each regression window comes from one provider request and one adjustment basis.
                for day in window_days:
                    if day < listed:
                        continue
                    if day not in raw.index or day not in adjusted.index:
                        raise ValueError('{} {} 行情缺失，保留已提交数据和旧候选池'.format(code, day))
                    paused = raw.loc[day, 'paused']
                    if pd.isna(paused) or paused not in (0, 1):
                        raise ValueError(code + ' 停牌状态缺失或无效')
                    if not paused and (positive(raw.loc[day, 'close']) is None or positive(adjusted.loc[day, 'close']) is None):
                        raise ValueError('{} {} 有效交易日价格缺失'.format(code, day))
                positions = {day: i for i, day in enumerate(window_days)}
                records = []
                for day in missing:
                    i = positions[day]
                    dates = window_days[builtins.max(0, i-25):i+1]
                    prices = [positive(adjusted.loc[d, 'close']) if d in adjusted.index else None for d in dates]
                    values = metrics(prices, bool(raw.loc[day, 'paused']))
                    records.append(dict(trade_date=day, code=code, name=member['name'],
                                        close=price_value(raw.loc[day, 'close']), updated_at=china_now(), **values))
                _progress_note('{} 计算完成，入库 {} 条热度结果'.format(code, len(records)))
                output.extend(save_rows(Session, records, repair_missing=repair))
            with session_scope(Session) as session:
                codes = {r[0] for r in session.query(Heat.code).filter(Heat.trade_date == days[-1]).all()}
                if set(m['code'] for m in members) - codes:
                    raise ValueError('最新交易日热度结果不完整，保留上一候选池快照')
                head = session.query(Pool).filter_by(id=1).with_for_update().first()
                if head and head.as_of_date > days[-1]:
                    raise ValueError('并发采集已有更新快照，拒绝回退')
                if head is None:
                    session.add(Pool(id=1, as_of_date=days[-1], rule_version=RULE_VERSION, members=members, updated_at=china_now()))
                elif head.as_of_date != days[-1] or head.members != members or head.rule_version != RULE_VERSION:
                    head.as_of_date, head.members, head.rule_version, head.updated_at = days[-1], members, RULE_VERSION, china_now()
                session.commit()
        return pd.DataFrame(output)
    return SimpleNamespace(**locals())


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

    def index_batches(items):
        # These queries only return scalar indexes, not holdings/JSON payloads.
        for offset in range(0, len(items), 300):
            yield items[offset:offset+300]

    def fund_document(row, fund_map):
        source_code = text_value(row['code'])
        fund = source_code.split('.')[0]
        canonical = fund_map.get(fund, fund)
        full = text_value(row.get('report_type')) in ('年度','年度报告','年报','半年度','半年度报告')
        priority = (int(full), day(row['pub_date']), int(fund == canonical), fund)
        # Preserve the physical identifier for exact source-side filtering.
        document = (source_code, day(row['pub_date']), int(row['report_type_id']))
        return canonical, priority, document

    def select_documents(period, as_of, fund_map, codes):
        # A stock can disappear in a later report or in the preferred share class.
        # Discover its related funds first, then select their complete documents.
        t = finance.FUND_PORTFOLIO_STOCK
        canonical_funds, observed_funds = set(), set()
        for batch in index_batches(sorted(set(codes))):
            symbols = sorted(set(batch) | {c.split('.')[0] for c in batch})
            for row in grouped_pages(t, [t.code], t.period_end == period, t.pub_date <= as_of,
                                     t.symbol.in_(symbols), label='关联基金'):
                source_code = text_value(row['code'])
                fund = source_code.split('.')[0]
                observed_funds.update((source_code, fund))
                canonical_funds.add(fund_map.get(fund, fund))
        related = sorted(observed_funds | canonical_funds |
                         {f for f, c in fund_map.items() if c in canonical_funds})
        _progress_message('{}：发现 {} 个相关基金代码（含关联份额），每批最多 300 个核对报告版本。'.format(period, len(related)))
        selected = {}
        columns = [t.code, t.pub_date, t.report_type_id, t.report_type]
        for batch in _progress_items(list(index_batches(related)), '相关基金报告版本', lambda b: b[0]):
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
        changed_funds, observed_funds, fund_periods, ignored = {}, {}, {}, set()
        allowed_periods = set(periods)
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
                columns.extend([table.symbol, table.period_end])
            for row in pages(table, *filters, columns=columns):
                row_counts[name] += 1
                if name == 'fund':
                    period = day(row['period_end'])
                    if period not in allowed_periods:
                        continue
                    fund = text_value(row['code']).split('.')[0]
                    changed_funds.setdefault(period, set()).add(fund_map.get(fund, fund))
                    observed_funds.setdefault(period, set()).add(text_value(row['code']))
                    code = stock_code(row['symbol'])
                    if code:
                        fund_periods.setdefault(period, set()).add(code)
                else:
                    code = stock_code(row['code'])
                    if code is None:
                        ignored.add(text_value(row['code']))
                if code:
                    affected[name].add(code)
        # Exits belong to the changed fund/report period, not its other quarters.
        for period, canonical_funds in sorted(changed_funds.items()):
            table = finance.FUND_PORTFOLIO_STOCK
            funds = sorted(canonical_funds | observed_funds[period] |
                           {f for f, c in fund_map.items() if c in canonical_funds})
            scope = fund_periods.setdefault(period, set())
            for batch in index_batches(funds):
                for row in grouped_pages(table, [table.symbol], table.code.in_(batch), table.period_end == period,
                                         table.pub_date <= as_of, label='新旧报告关联股票'):
                    code = stock_code(row['symbol'])
                    if code:
                        affected['fund'].add(code)
                        scope.add(code)
        if ignored:
            _progress_message('个股机构持仓：忽略 {} 个不在股票范围内的披露代码（如 {}）。'.format(
                len(ignored), '、'.join(sorted(ignored)[:5])))
        for name, label in (('fund', '公募持仓'), ('float', '十大流通股东'), ('holders', '股东户数')):
            if previous.get(name) != current.get(name):
                _progress_message('个股机构持仓：{}增量源记录 {} 条，影响 {} 只股票。'.format(
                    label, row_counts[name], len(affected[name])))
        result = {name: sorted(codes) for name, codes in affected.items()}
        result['fund_periods'] = {period: sorted(codes) for period, codes in fund_periods.items() if codes}
        return result

    def fund_document_conditions(table, selected):
        # Group exact document keys into IN predicates. Avoid one request per fund
        # or small fund batches multiplied by every stock batch. Bound unusually
        # large predicates by both code count and OR groups, without dropping keys.
        groups = {}
        for _, document in selected.values():
            fund, pub, report_type = document
            groups.setdefault((pub, report_type), set()).add(fund)
        conditions, count = [], 0
        for (pub, report_type), funds in sorted(groups.items()):
            funds, offset = sorted(funds), 0
            while offset < len(funds):
                size = builtins.min(8000-count, len(funds)-offset)
                conditions.append(and_(table.pub_date == pub, table.report_type_id == report_type,
                                       table.code.in_(funds[offset:offset+size])))
                offset += size
                count += size
                if count == 8000 or len(conditions) == 128:
                    yield or_(*conditions)
                    conditions, count = [], 0
        if conditions:
            yield or_(*conditions)

    def batch_funds(codes, period, as_of, fund_map, selected):
        if not codes or not selected:
            return {}
        t = finance.FUND_PORTFOLIO_STOCK
        code_map = {c.split('.')[0]:c for c in codes}
        candidates = {}
        columns = [t.id,t.code,t.symbol,t.pub_date,t.report_type_id,t.report_type,t.shares,t.market_cap]
        for document_filter in fund_document_conditions(t, selected):
            for row in pages(t, t.period_end == period, t.pub_date <= as_of,
                             t.symbol.in_(list(code_map)+list(codes)), document_filter, columns=columns):
                canonical, _, document = fund_document(row, fund_map)
                if canonical not in selected or document != selected[canonical][1]:
                    raise ValueError('基金明细与已选报告版本不一致，停止写入')
                code = code_map.get(text_value(row['symbol']).split('.')[0])
                if code:
                    # Each document is in exactly one predicate; ascending IDs
                    # retain the last row for duplicate positions in that document.
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

    def save_report_batch(session, codes, period, as_of, fund_map, selected, changed,
                          fund_codes=None, float_codes=None, preserve_followers=False):
        fund_codes = set(codes) if fund_codes is None else set(codes).intersection(fund_codes)
        float_codes = set(codes) if float_codes is None else set(codes).intersection(float_codes)
        fund_groups = batch_funds(sorted(fund_codes),period,as_of,fund_map,selected) if fund_codes else {}
        by_stock = batch_floats(sorted(float_codes),period,as_of) if float_codes else {}
        old_rows = {r.code:r for r in session.query(OwnershipReport).filter(
            OwnershipReport.report_date == period,OwnershipReport.code.in_(codes)).all()}
        previous_rows = {r.code:r for r in session.query(OwnershipReport).filter(
            OwnershipReport.report_date == previous_quarter(period),OwnershipReport.code.in_(float_codes)).all()} if float_codes else {}
        changed_float_codes = set()
        for code in sorted(set(by_stock) | set(fund_groups) | set(old_rows)):
            old_payload = old_rows[code].payload if code in old_rows else {}
            payload = deepcopy(old_payload) if old_payload else {
                'schema_version':1, 'categories':{'public_fund':None}, 'float_members':[],
                'float_complete':False, 'float_pub_date':None,
                'public_fund_scope':'full_disclosure' if period.month in (6,12) else 'quarterly_top_holdings',
                'fund_identity_rule':'statistics_main_code_else_product_code'}
            if code in fund_codes:
                # Recompute the stock's entire public-fund sum, including unchanged funds.
                payload['categories']['public_fund'] = fund_groups.get(code)
            if code in float_codes or not old_payload:
                members, complete, pub = float_snapshot(by_stock.get(code,[]))
                payload.update(float_members=members, float_complete=complete, float_pub_date=pub)
                previous = previous_rows.get(code)
                payload = classify_payload(payload, [previous.payload] if previous else [])
                payload['social_entry'] = social_entry(payload,previous.payload if previous else None)
                if code in float_codes and (old_payload.get('float_members') != payload['float_members']
                        or old_payload.get('float_complete') != payload['float_complete']
                        or old_payload.get('social_complete') != payload['social_complete']):
                    changed_float_codes.add(code)
            payload = shares_payload(payload)
            if upsert(session,OwnershipReport,{'code':code,'report_date':period},{'payload':payload},
                      existing=old_rows.get(code),loaded=True):
                changed.add(('持仓',code,period))
        next_quarter = period+datetime.timedelta(days=95)
        next_period = previous_quarter(datetime.date(next_quarter.year,next_quarter.month,1))
        if changed_float_codes and not preserve_followers:
            followers = session.query(OwnershipReport).filter(OwnershipReport.report_date == next_period,
                OwnershipReport.code.in_(changed_float_codes)).all()
            for follower in followers:
                follower.payload = dict(follower.payload,social_entry={'status':'unknown'})
                follower.updated_at = china_now()
        # Flush, not commit: release ORM state while retaining quarter-level rollback.
        session.flush()
        session.expunge_all()

    def repair_social_batch(session, codes, changed):
        for row, payload in classification_updates(session, codes):
            row.payload, row.updated_at = payload, china_now()
            changed.add(('持仓', row.code, row.report_date))
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

    def aggregate(items, scope, method):
        if not items:
            return None  # absence of disclosure is not zero ownership
        shares = [r.get('shares') for r in items]
        return {'shares': builtins.sum(shares) if builtins.all(v is not None for v in shares) else None,
                'holder_count': len(items), 'scope': scope, 'value_method': method}

    def shares_payload(payload):
        # Clean only reports already being processed; never scan/reprice history.
        result = deepcopy(payload)
        for key in CATEGORY_IDS:
            holding = result.get('categories', {}).get(key)
            if holding is not None:
                holding.pop('market_value', None)
                holding['value_method'] = 'reported_shares'
        social = result.get('social_entry', {})
        social.pop('new_market_value', None)
        for member in result.get('float_members', []) + social.get('new_combinations', []):
            member.pop('market_value', None)
        return result

    def float_snapshot(rows):
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
                           'ratio': number(r.get('share_ratio'))})
        # Unknown classifications or identities make absence/newness unreliable.
        complete = complete and builtins.all(r['class_id'] is not None and r['name'] for r in result)
        return result, bool(complete), latest.isoformat()

    CLASSIFICATION_VERSION = 1

    def holder_name(value):
        value = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text_value(value)))
        return re.sub(r'[‐‑‒–—―−﹣－]', '-', value).replace('（', '(').replace('）', ')')

    def social_flag(member):
        if 'is_social' in member:
            return member['is_social'] if isinstance(member['is_social'], bool) else None
        kind = member.get('class_id')
        return None if kind is None else kind == 307004

    def social_ready(payload):
        return bool(payload) and payload.get('social_complete', payload.get('float_complete')) is True

    def classify_member(member, history):
        result = dict(member)
        result.pop('market_value', None)
        raw = member.get('raw_class_id', member.get('class_id'))
        # Current source classifications take precedence over inferred evidence.
        if raw is not None:
            return result
        name = holder_name(member.get('name'))
        identity = text_value(member.get('id'))
        evidence = set()
        for old_payload in history:
            for old in old_payload.get('float_members', []):
                old_raw = old.get('raw_class_id', old.get('class_id'))
                same = (identity and identity == text_value(old.get('id'))) or (name and name == holder_name(old.get('name')))
                if same and old_raw is not None:
                    evidence.add(old_raw)
        hint = None
        if re.search(r'(?:^|-)(?:全国)?(?:社保基金|社会保障基金)[零〇○一二三四五六七八九十百千万0-9]+组合$', name):
            hint = 307004
        elif (not re.search(r'社保|养老|私募', name)
              and re.search(r'(?:交易型开放式指数|混合型|股票型|债券型|灵活配置|联接|货币市场|开放式).*证券投资基金$', name)):
            hint = 307003
        kind, source = None, 'unknown'
        if evidence:
            if len(evidence) == 1 and (hint is None or hint in evidence):
                kind, source = next(iter(evidence)), 'history'
            else:
                source = 'conflict'
        elif hint is not None:
            kind, source = hint, 'name'
        result.update(raw_class_id=None, class_id=kind, class_source=source,
                      is_social=None if kind is None else kind == 307004)
        return result

    def classify_payload(payload, history=()):
        result = dict(payload)
        members = [classify_member(m, history) for m in payload.get('float_members', [])]
        ranks = [m.get('rank') for m in members]
        structural = (len(members) == 10 and set(ranks) == set(range(1, 11))
                      and builtins.all(text_value(m.get('name')) for m in members))
        classified = structural and builtins.all(m.get('class_id') is not None for m in members)
        social_complete = structural and builtins.all(social_flag(m) is not None for m in members)
        categories = dict(payload.get('categories', {}))
        for category, ids in CATEGORY_IDS.items():
            items = [m for m in members if (social_flag(m) is True if category == 'social' else m.get('class_id') in ids)]
            total = aggregate(items, 'top10_float', 'reported_shares')
            if total is not None:
                total['complete'] = bool(social_complete if category == 'social' else classified)
            categories[category] = total
        result.update(float_members=members, categories=categories, float_list_complete=bool(structural),
                      float_complete=bool(classified), social_complete=bool(social_complete),
                      classification_version=CLASSIFICATION_VERSION,
                      classification_pending=builtins.any(m.get('raw_class_id', m.get('class_id')) is None for m in members))
        return result

    def classification_updates(session, codes, as_of=None):
        query_rows = session.query(OwnershipReport).filter(OwnershipReport.code.in_(codes))
        if as_of is not None:
            query_rows = query_rows.filter(OwnershipReport.report_date <= as_of)
        rows = query_rows.order_by(OwnershipReport.code, OwnershipReport.report_date).all()
        history, lookup, updates = {}, {}, []
        for row in rows:
            original = row.payload
            prior = history.setdefault(row.code, [])
            members = original.get('float_members', [])
            needs_repair = builtins.any(m.get('raw_class_id', m.get('class_id')) is None for m in members)
            payload = classify_payload(original, prior) if needs_repair else shares_payload(original)
            previous = lookup.get((row.code, previous_quarter(row.report_date)))
            payload['social_entry'] = social_entry(payload, previous)
            if payload != original:
                updates.append((row, payload))
            lookup[(row.code, row.report_date)] = payload
            # Only original source IDs are evidence; never promote an inference.
            prior.append(original)
        return updates

    def json_boolean(session, key, value):
        # JoinQuant may ship SQLAlchemy older than the JSON .as_boolean API.
        expression = func.json_extract(OwnershipReport.payload, '$.' + key)
        if session.get_bind().dialect.name == 'mysql':
            return func.json_unquote(expression) == ('true' if value else 'false')
        return expression == int(value)

    def repair_classifications(Session, as_of, changed=None, codes=None, dry_run=False):
        """Repair stored JSON only, with resumable batches and the normal sync journal."""
        changed = changed if changed is not None else set()
        key, cursor, repaired = 'classify-v1', '', 0
        if codes is None:
            with session_scope(Session) as session:
                checkpoint = session.query(DisclosureProbe).filter_by(key=key).first()
                if checkpoint is not None:
                    if checkpoint.payload.get('complete'):
                        return 0
                    cursor = checkpoint.payload.get('last_code', '')
        pending = sorted(set(codes)) if codes is not None else None
        while True:
            with session_scope(Session) as session:
                if not dry_run:
                    session.query(SyncHead).filter_by(id='main').with_for_update().one()
                if pending is None:
                    batch = [r[0] for r in session.query(OwnershipReport.code).filter(
                        OwnershipReport.code > cursor, OwnershipReport.report_date <= as_of,
                        json_boolean(session, 'float_complete', False),
                        func.json_extract(OwnershipReport.payload, '$.float_members[0].name').isnot(None)
                    ).distinct().order_by(OwnershipReport.code).limit(settings['stock_batch_size']).all()]
                else:
                    batch, pending = pending[:settings['stock_batch_size']], pending[settings['stock_batch_size']:]
                if not batch:
                    if codes is None and not dry_run:
                        checkpoint = session.query(DisclosureProbe).filter_by(key=key).first()
                        if checkpoint is None:
                            checkpoint = DisclosureProbe(key=key, as_of=as_of, payload={})
                            session.add(checkpoint)
                        checkpoint.payload = {'complete': True, 'last_code': cursor}
                        checkpoint.as_of = as_of
                        session.commit()
                    break
                updates = classification_updates(session, batch, as_of)
                for row, payload in updates:
                    changed.add(('持仓', row.code, row.report_date))
                    if not dry_run:
                        row.payload, row.updated_at = payload, china_now()
                repaired += len(updates)
                cursor = batch[-1]
                if not dry_run:
                    if codes is None:
                        checkpoint = session.query(DisclosureProbe).filter_by(key=key).first()
                        if checkpoint is None:
                            checkpoint = DisclosureProbe(key=key, as_of=as_of, payload={})
                            session.add(checkpoint)
                        checkpoint.payload = {'complete': False, 'last_code': cursor}
                        checkpoint.as_of = as_of
                    session.commit()
                _progress_note('修复已有分类：已处理至 {}，变更 {} 份报告'.format(cursor, repaired))
        if repaired:
            _progress_message('个股机构持仓：{} {} 份已有报告的分类及社保比较，无需重采历史。'.format(
                '待修复' if dry_run else '已修复', repaired))
        return repaired

    def refresh_classification_gaps(Session, as_of, changed):
        """Bounded, once-daily source metadata retry; never reload public fund holdings."""
        key = 'classify-retry'
        with session_scope(Session) as session:
            state = session.query(DisclosureProbe).filter_by(key=key).first()
            if state is not None and state.as_of >= as_of:
                return
            cursor = state.payload.get('last_code', '') if state else ''
            cutoff = previous_quarter(as_of + datetime.timedelta(days=1))
            for _ in range(settings['refresh_quarters'] - 1):
                cutoff = previous_quarter(cutoff)
            candidates = session.query(OwnershipReport).filter(
                OwnershipReport.report_date >= cutoff, OwnershipReport.report_date <= as_of,
                json_boolean(session, 'classification_pending', True))
            codes = [r[0] for r in candidates.with_entities(OwnershipReport.code).filter(
                OwnershipReport.code > cursor).distinct().order_by(OwnershipReport.code)
                .limit(builtins.min(settings['stock_batch_size'], 50)).all()]
            if not codes and cursor:
                codes = [r[0] for r in candidates.with_entities(OwnershipReport.code).distinct()
                         .order_by(OwnershipReport.code).limit(builtins.min(settings['stock_batch_size'], 50)).all()]
            if not codes:
                return
            originals = {(r.code, r.report_date): deepcopy(r.payload) for r in candidates.filter(OwnershipReport.code.in_(codes)).all()}
        _progress_message('个股机构持仓：定向检查 {} 只股票待补分类，仅查询十大流通股东。'.format(len(codes)))
        fresh = {}
        for period in sorted(set(p for _, p in originals)):
            scoped = [c for c, p in originals if p == period]
            for code, rows in batch_floats(scoped, period, as_of).items():
                old = originals[(code, period)]
                members, _, pub = float_snapshot(rows)
                old_members = old.get('float_members', [])
                old_ranks = {m.get('rank'): m for m in old_members}
                # A different document or changing holdings belongs to the normal disclosure flow.
                if pub != old.get('float_pub_date') or len(members) != len(old_members):
                    continue
                if builtins.any(m['rank'] not in old_ranks or
                    holder_name(m['name']) != holder_name(old_ranks[m['rank']]['name']) or
                    m.get('shares') != old_ranks[m['rank']].get('shares') or
                    m.get('ratio') != old_ranks[m['rank']].get('ratio') for m in members):
                    continue
                payload = deepcopy(old)
                by_rank = {m['rank']: m for m in members}
                updated = False
                for member in payload['float_members']:
                    source = by_rank[member['rank']]
                    if member.get('raw_class_id', member.get('class_id')) is None and source.get('class_id') is not None:
                        kind = source['class_id']
                        member.update(raw_class_id=kind, class_id=kind, class_source='source', is_social=kind == 307004)
                        if source.get('id'):
                            member['id'] = source['id']
                        updated = True
                if updated:
                    fresh[(code, period)] = payload
        with session_scope(Session) as session:
            session.query(SyncHead).filter_by(id='main').with_for_update().one()
            touched = set()
            for (code, period), payload in fresh.items():
                row = session.query(OwnershipReport).filter_by(code=code, report_date=period).first()
                if row is None or row.payload != originals[(code, period)]:
                    continue  # Another collector committed newer data while source reads ran.
                row.payload = classify_payload(payload)
                row.updated_at = china_now()
                changed.add(('持仓', code, period))
                touched.add(code)
            if touched:
                session.flush()
                repair_social_batch(session, sorted(touched), changed)
            state = session.query(DisclosureProbe).filter_by(key=key).first()
            if state is None:
                session.add(DisclosureProbe(key=key, as_of=as_of, payload={'last_code': codes[-1]}))
            else:
                state.as_of, state.payload = as_of, {'last_code': codes[-1]}
            session.commit()

    def social_entry(current, previous):
        empty = {'status': 'unknown', 'first_appearance': False, 'new_combinations': [],
                 'new_shares': None}
        if not social_ready(current) or not social_ready(previous):
            return empty
        current_members = [r for r in current['float_members'] if social_flag(r) is True]
        previous_members = previous['float_members']
        def same(a, b):
            return (a.get('id') and b.get('id') and a['id'] == b['id']) or holder_name(a['name']) == holder_name(b['name'])
        # Compare all prior shareholders: reclassification is not a new holding.
        new = [{k:v for k,v in r.items() if k != 'market_value'} for r in current_members
               if not builtins.any(same(r,p) for p in previous_members)]
        old_social = [r for r in previous_members if social_flag(r) is True]
        totals = aggregate(new, 'top10_float', 'reported_shares') or {}
        return {'status': 'comparable', 'first_appearance': bool(new) and not old_social,
                'new_combinations': new, 'new_shares': totals.get('shares')}

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

    def historical_index(as_of):
        result, status = {}, {}
        for name, table, period_name, code_name in (
                ('fund', finance.FUND_PORTFOLIO_STOCK, 'period_end', 'symbol'),
                ('float', finance.STK_SHAREHOLDER_FLOATING_TOP10, 'end_date', 'code'),
                ('holders', finance.STK_HOLDER_NUM, 'end_date', 'code')):
            period, identity = getattr(table, period_name), getattr(table, code_name)
            conditions = [period <= as_of, or_(table.pub_date == None, table.pub_date <= as_of)]
            if name != 'holders':
                conditions.append(table.pub_date != None)
            if name == 'float':
                conditions.extend([table.change_reason_id == 306019, table.sharesnature_id == 308007])
            index = {}
            for row in grouped_pages(table, [period, identity], *conditions, label='历史索引 ' + name):
                date, code = day(row[period_name]), text_value(row[code_name])
                if date and code:
                    index.setdefault(date, set()).add(code)
            result[name] = index
            dates = sorted(index)
            status[name] = {'status': 'observed' if dates else 'verified_empty_visible_scope',
                            'first_visible_period': str(dates[0]) if dates else None,
                            'last_visible_period': str(dates[-1]) if dates else None,
                            'as_of': str(as_of), 'provider_earliest_verified': False}
        return result, status

    def recover_history(as_of):
        indexes, status = historical_index(as_of)
        identities = set(c for name in ('float', 'holders') for scope in indexes[name].values()
                         for c in scope if c.endswith(('.XSHG', '.XSHE')))
        fund_map = load_fund_map() if indexes['fund'] else {}
        changed = set()
        with database_scope(Base, _config) as Session:
            install_change_tracking(Session)
            for period in _progress_items(sorted(set(indexes['fund']) | set(indexes['float'])), '历史报告期', str):
                securities = _apis['get_all_securities'](types=['stock'], date=period)
                if securities is None:
                    raise ValueError('历史股票清单查询失败：' + str(period))
                identities.update(str(c) for c in securities.index)
                mapping = {c.split('.')[0]: c for c in identities}
                fund_codes, unresolved = set(), set()
                for symbol in indexes['fund'].get(period, set()):
                    code = symbol if symbol in identities else mapping.get(symbol) if '.' not in symbol else None
                    if code:
                        fund_codes.add(code)
                    else:
                        unresolved.add(symbol)
                float_codes = indexes['float'].get(period, set()).intersection(identities)
                with session_scope(Session) as session:
                    session.query(SyncHead).filter_by(id='main').with_for_update().one()
                    present = {r[0] for r in session.query(OwnershipReport.code).filter_by(report_date=period)}
                    missing = sorted((fund_codes | float_codes) - present)
                    selected = select_documents(period, as_of, fund_map, missing) if set(missing) & fund_codes else {}
                    for batch in batches(missing):
                        for code in batch:
                            if session.query(OwnershipStock).filter_by(code=code).first() is None:
                                session.add(OwnershipStock(code=code, name=code, industry=None, updated_at=china_now()))
                        save_report_batch(session, batch, period, as_of, fund_map, selected, changed,
                                          fund_codes=fund_codes, float_codes=float_codes, preserve_followers=True)
                    key = 'history:' + str(period)
                    state = session.query(Checkpoint).get(key)
                    if state is None:
                        session.add(Checkpoint(key=key, as_of=as_of))
                    else:
                        state.as_of = builtins.max(state.as_of, as_of)
                    session.commit()
                if unresolved:
                    _progress_message('{}：{} 个基金持仓代码无法由历史 A 股身份确认，未猜测交易所。'.format(period, len(unresolved)))
                    status['fund'].setdefault('unresolved_symbols', {})[str(period)] = sorted(unresolved)
            t = finance.STK_HOLDER_NUM
            for period, source_codes in sorted(indexes['holders'].items()):
                with session_scope(Session) as session:
                    session.query(SyncHead).filter_by(id='main').with_for_update().one()
                    present = {r[0] for r in session.query(OwnershipHolders.code).filter_by(end_date=period)}
                    for batch in batches(sorted(source_codes.intersection(identities) - present)):
                        for code in batch:
                            if session.query(OwnershipStock).filter_by(code=code).first() is None:
                                session.add(OwnershipStock(code=code, name=code, industry=None, updated_at=china_now()))
                        save_holder_batch(session, batch,
                            [t.end_date == period, or_(t.pub_date == None, t.pub_date <= as_of)], period, changed)
                    session.commit()
            with session_scope(Session) as session:
                state = session.query(DisclosureProbe).filter_by(key='history-scope').first()
                if state is None:
                    session.add(DisclosureProbe(key='history-scope', as_of=as_of, payload=status))
                else:
                    state.as_of, state.payload = as_of, status
                session.commit()
        return pd.DataFrame(sorted(changed), columns=['kind', 'code', 'date'])

    def run(end_date=None, full_refresh=False, skip_existing=True, history_mode=None):
        as_of = as_date(end_date or settings.get('end_date') or china_now().date())
        if as_of > china_now().date():
            raise ValueError('结束日期不能晚于今天')
        for option in ('refresh_quarters','stock_batch_size'):
            if isinstance(settings[option],bool) or not isinstance(settings[option],int) or settings[option] < 1:
                raise ValueError(option+' 必须为正整数')
        if settings['stock_batch_size'] > 300:
            raise ValueError('stock_batch_size 最大为 300，防止研究环境内存溢出')
        mode = history_mode or settings.get('history_mode', 'recent')
        if mode not in ('recent', 'all'):
            raise ValueError('history_mode 必须为 recent 或 all')
        if mode == 'all':
            return recover_history(as_of)
        years = settings['history_years']
        if not isinstance(years,int) or years < 1 or years > 20:
            raise ValueError('history_years 必须在 1 到 20 之间')
        start = as_of.replace(year=as_of.year-years, day=builtins.min(as_of.day,28))
        periods = [datetime.date(y,m,d) for y in range(start.year,as_of.year+1)
                   for m,d in ((3,31),(6,30),(9,30),(12,31))
                   if start <= datetime.date(y,m,d) <= as_of]
        periods.insert(0, previous_quarter(periods[0]))
        _progress_message('个股机构持仓 [分源分期增量版 v8 · 披露股数口径]：开始检查新增披露。')
        signature = disclosure_signature(as_of)
        previous_probe, previous_day = None, None
        with database_scope(Base, _config) as Session:
            with session_scope(Session) as session:
                probe = session.query(DisclosureProbe).filter_by(key='main').first()
                if probe is not None:
                    previous_probe, previous_day = dict(probe.payload), probe.as_of
                if probe is not None and probe.as_of > as_of:
                    raise ValueError('已有较新采集结果，拒绝历史运行回退')
                unchanged = not full_refresh and probe is not None and probe.payload == signature
            changed = set()
            install_change_tracking(Session)
            repair_classifications(Session, as_of, changed)
            refresh_classification_gaps(Session, as_of, changed)
            if unchanged:
                _progress_message('个股机构持仓：未检测到新增披露，跳过持仓复查、户数采集和社保比较。')
                return pd.DataFrame(sorted(changed), columns=['kind', 'code', 'date'])

            securities = _apis['get_all_securities'](types=['stock'],date=as_of)
            if securities is None or securities.empty:
                raise ValueError('股票清单为空，停止写入')
            codes = sorted(str(code) for code in securities.index)
            targeted = (not full_refresh and previous_probe is not None
                        and previous_probe.get('history_years') == settings['history_years']
                        and previous_probe.get('refresh_quarters') == settings['refresh_quarters'])
            fund_map, review_codes, holder_codes = None, None, None
            fund_periods, float_codes, float_periods = {}, [], set()
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
                    fund_map or {}, set(codes) | stored_codes, periods)
                fund_periods = affected['fund_periods']
                float_codes = affected['float']
                float_periods = set(periods[-settings['refresh_quarters']:]) if float_codes else set()
                review_codes = sorted(set(affected['fund']) | set(affected['float']))
                holder_codes = affected['holders']
                # Keep existing stock metadata on holder-only updates; add new stocks normally.
                metadata_codes = set(review_codes) | (set(holder_codes) - stored_codes)
                codes = [c for c in codes if c in metadata_codes]
                periods = sorted(set(fund_periods) | float_periods)
                if fund_periods:
                    _progress_message('个股机构持仓：公募仅更新发生披露变化的 {} 个报告期、{} 个股票季度；未变季度沿用原结果。'.format(
                        len(fund_periods), sum(len(scope) for scope in fund_periods.values())))
                if float_codes:
                    _progress_message('个股机构持仓：十大流通股东影响 {} 只股票，仅复查这些股票最近 {} 个季度的名单及社保比较。'.format(
                        len(float_codes), len(float_periods)))
                if not periods:
                    _progress_message('个股机构持仓：无持仓新增披露，跳过报告期、持仓明细和社保比较。')
                _progress_message('个股机构持仓：股东户数更新范围 {} 只股票。'.format(len(holder_codes)))
            else:
                reason = '手动完整刷新' if full_refresh else ('首次建立基线' if previous_probe is None else '采集范围参数变化')
                _progress_message('个股机构持仓：{}，按原有断点采集。'.format(reason))
            if fund_map is None and not targeted:
                fund_map = load_fund_map()
            # Compact keys only: no raw rows or JSON payloads are accumulated for the preview.
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
                    if checkpoint and not targeted and not full_refresh and period not in periods[-settings['refresh_quarters']:]:
                        continue
                    # Only scalar codes; JSON payloads are loaded by bounded stock batch below.
                    old_codes = [] if targeted else [r[0] for r in session.query(OwnershipReport.code).filter_by(report_date=period).all()]
                if targeted:
                    fund_scope = fund_periods.get(period, [])
                    float_scope = float_codes if period in float_periods else []
                    scope_codes = sorted(set(fund_scope) | set(float_scope))
                else:
                    scope_codes = sorted(set(codes)|set(old_codes))
                    fund_scope, float_scope = scope_codes, scope_codes
                _progress_message('{}：公募汇总 {} 只，十大流通股东 {} 只；各自按来源更新。'.format(
                    period, len(fund_scope), len(float_scope)))
                selected = select_documents(period,as_of,fund_map,fund_scope) if fund_scope else {}
                with session_scope(Session) as session:
                    for batch in _progress_items(list(batches(scope_codes)), '持仓分批',lambda b:b[0]):
                        save_report_batch(session,batch,period,as_of,fund_map,selected,changed,
                                          fund_codes=fund_scope, float_codes=float_scope)
                    head = session.query(Checkpoint).get('report:'+str(period))
                    if head is None:
                        session.add(Checkpoint(key='report:'+str(period),as_of=as_of))
                    else:
                        head.as_of = as_of
                    session.commit()
                del selected, old_codes
            # Include historical/delisted stock codes, but never load every historical JSON at once.
            if not targeted or float_codes:
                with session_scope(Session) as session:
                    social_codes = float_codes if targeted else [r[0] for r in session.query(OwnershipReport.code).distinct().all()]
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


# BEGIN EMBEDDED INDEX HISTORY
def _valuation_ruler_strategy(_apis, _config):
    WRITE_DATABASE = True
    MAX_DAYS = None  # 默认一次补完最后完成日期之后的交易日；可设整数限制本轮天数。
    END_DATE = None
    Base = declarative_base()

    class HistoryError(ValueError):
        """Fixed credential-free source/schema diagnostics."""
        pass

    class Progress:
        def update(self, stage, done=0, total=None, day=None, force=False):
            suffix = ' {}/{} 日'.format(done, total) if total is not None else ''
            _progress_note('{}{} {}'.format(stage, suffix, day or ''))
        def finish(self, status):
            _progress_note(status)

    def api(name):
        """Research APIs may live in Notebook globals/builtins instead of jqdata."""
        for value in (_apis.get(name), builtins.getattr(jq, name, None), builtins.getattr(builtins, name, None)):
            valid = (builtins.all(builtins.hasattr(value, field) for field in ('code', 'day', 'pe_ratio'))
                     if name == 'valuation' else builtins.callable(value))
            if valid:
                return value
        raise HistoryError('聚宽研究环境缺少接口：{}。请在研究 Notebook 完整运行脚本。'.format(name))

    def diagnostic(exc, stage):
        # Never include exception messages, SQL, parameters, filenames or connection URLs.
        kind = builtins.type(exc).__name__
        if not re.match(r'^[A-Za-z_][A-Za-z_0-9]{0,63}$', kind):
            kind = 'Exception'
        original = builtins.getattr(exc, 'orig', exc)
        args = builtins.getattr(original, 'args', ())
        code = args[0] if args and builtins.type(args[0]) is builtins.int and 1000 <= args[0] <= 9999 else None
        frames, tb = [], exc.__traceback__
        while tb is not None:
            if tb.tb_frame.f_globals is globals():
                frames.append('{}:{}'.format(tb.tb_frame.f_code.co_name, tb.tb_lineno))
            tb = tb.tb_next
        return '阶段={}；异常={}{}；位置={}'.format(stage, kind,
            '；数据库错误码={}'.format(code) if code is not None else '', ' > '.join(frames[-4:]) or '外部接口')

    def china_now():
        return dt.datetime.utcnow() + dt.timedelta(hours=8)

    def as_date(value):
        if builtins.isinstance(value, dt.datetime):
            return value.date()
        if builtins.isinstance(value, dt.date):
            return value
        if not builtins.isinstance(value, builtins.str) or not re.match(r'^\d{4}-\d{2}-\d{2}$', value):
            raise HistoryError('日期必须为 YYYY-MM-DD。')
        return dt.datetime.strptime(value, '%Y-%m-%d').date()

    def number(value, precision, scale, field='数值', round_source=False):
        if builtins.isinstance(value, builtins.bool) or value is None:
            raise HistoryError('{}：数值缺失或类型错误。'.format(field))
        try:
            result = Decimal(builtins.str(value))
        except (InvalidOperation, ValueError):
            raise HistoryError('{}：数值无效。'.format(field)) from None
        with localcontext() as ctx:
            ctx.prec = 80
            if not result.is_finite():
                raise HistoryError('{}：数值非有限（NaN/Infinity）。'.format(field))
            bound = Decimal(10) ** (precision-scale)
            if result.copy_abs() >= bound:
                raise HistoryError('{}：数值超出 DECIMAL({},{}) 范围。'.format(field, precision, scale))
            rounded = result.quantize(Decimal(10) ** -scale, rounding=ROUND_HALF_EVEN)
            if result != rounded:
                if not round_source:
                    raise HistoryError('{}：小数精度超过 {} 位；已有数据及汇总不自动舍入。'.format(field, scale))
                if rounded.copy_abs() >= bound or rounded == 0:
                    raise HistoryError('{}：按 {} 位小数舍入后越界或归零。'.format(field, scale))
                result = rounded
        return result

    def derived(value, scale):
        with localcontext() as ctx:
            ctx.prec = 80
            return value.quantize(Decimal(10) ** -scale, rounding=ROUND_HALF_EVEN)

    def calendar(start, end):
        # Independent full-calendar crosscheck detects truncated range responses.
        full = [as_date(x) for x in api('get_all_trade_days')()]
        if not full or full != builtins.sorted(builtins.set(full)):
            raise HistoryError('平台完整交易日历无效。')
        expected = [d for d in full if (start is None or d >= start) and d <= end]
        if not expected:
            raise HistoryError('区间没有平台交易日。')
        actual = [as_date(x) for x in api('get_trade_days')(start_date=expected[0], end_date=end)]
        if actual != expected:
            raise HistoryError('交易日历区间覆盖不完整或乱序。')
        return expected

    def range_days(start, end, max_days):
        if max_days is not None and (builtins.isinstance(max_days, builtins.bool) or not builtins.isinstance(max_days, builtins.int) or not 1 <= max_days <= 250):
            raise HistoryError('max_days 必须为 None（本次全区间）或 1 至 250 的整数。')
        now = china_now()
        closed = now.date() if (now.hour, now.minute) >= (18, 30) else now.date()-dt.timedelta(days=1)
        end = closed if end is None else as_date(end)
        if end > closed:
            raise HistoryError('结束日期尚未在北京时间18:30收盘确认。')
        start = None if start is None else as_date(start)
        if start is not None and start > end:
            raise HistoryError('开始日期晚于结束日期。')
        return calendar(start, end)

    def validate_schema(engine, models):
        inspector = inspect(engine)
        for model in models:
            table = model.__table__
            cols = {c['name']: c for c in inspector.get_columns(table.name)}
            # The retired nullable metric may remain until the operator runs DROP COLUMN.
            if table.name == 'valuation_ruler_daily_v2' and cols.get('overall_pe_ttm', {}).get('nullable') is True:
                cols.pop('overall_pe_ttm')
            if builtins.set(cols) != builtins.set(c.name for c in table.columns):
                raise HistoryError('数据库字段集合不一致。')
            if inspector.get_pk_constraint(table.name)['constrained_columns'] != [c.name for c in table.primary_key]:
                raise HistoryError('数据库主键与采集协议不一致。')
            if engine.dialect.name == 'mysql' and inspector.get_table_options(table.name).get('mysql_engine', '').lower() != 'innodb':
                raise HistoryError('历史表必须使用 InnoDB。')
            for col in table.columns:
                actual = cols.get(col.name)
                if actual is None or actual['nullable'] != col.nullable:
                    raise HistoryError('数据库字段或可空约束不一致。')
                typ = actual['type']
                if builtins.getattr(typ, 'unsigned', False):
                    raise HistoryError('数据库数值字段不得使用无符号类型。')
                if builtins.isinstance(col.type, Numeric):
                    if not builtins.isinstance(typ, Numeric) or (typ.precision, typ.scale) != (col.type.precision, col.type.scale):
                        raise HistoryError('数据库数值精度不一致。')
                elif builtins.isinstance(col.type, String):
                    if not builtins.isinstance(typ, String) or typ.length != col.type.length:
                        raise HistoryError('数据库文本类型不一致。')
                elif col.type._type_affinity is not typ._type_affinity:
                    raise HistoryError('数据库字段类型不一致。')

    def database(models, module):
        # Configuration import and credentials are deliberately deferred until write mode.
        engine = connect_database(_config)
        engine.echo = False
        if builtins.hasattr(engine, 'hide_parameters'):
            engine.hide_parameters = True
        try:
            Base.metadata.create_all(engine)
            validate_schema(engine, models)
            Session = sessionmaker(bind=engine)
            _install_result_journal(Session, builtins.tuple(m for m in models if m.__tablename__ != 'market_crowding'), module)
            return engine, Session
        except HistoryError:
            engine.dispose()
            raise
        except Exception as exc:
            engine.dispose()
            raise HistoryError(diagnostic(exc, '连接数据库/校验表结构') + '；未写入历史结果。') from None

    def complete(row, model):
        for col in model.__table__.columns:
            value = builtins.getattr(row, col.name)
            if not col.nullable and value is None:
                raise HistoryError('已有记录不完整，需人工核实；不会覆盖。')
            if value is not None and builtins.isinstance(col.type, Numeric):
                number(value, col.type.precision, col.type.scale, model.__tablename__+'.'+col.name)
        return True

    START_DATE = '2010-01-01'
    INDEX = '000300.XSHG'
    INDEXES = {
        '000300.XSHG': ('沪深300', 300, '2010-01-01'),
        '000016.XSHG': ('上证50', 50, '2010-01-01'),
        '000852.XSHG': ('中证1000', 1000, '2014-10-17'),
    }
    INDICES = ('000300.XSHG', '000016.XSHG', '000852.XSHG')
    METHOD = 'index_pe_lyr_ttm_cap300_v2'
    SOURCE = 'JQ_PLATFORM.VALUATION.V2'
    PUBLIC_SOURCE = 'EASTMONEY.VALUATION.V2'
    CALENDAR_SOURCE = 'JQDATA.GET_TRADE_DAYS'

    class ValuationDaily(Base):
        __tablename__ = 'valuation_ruler_daily_v2'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        trade_date = Column(Date, primary_key=True)
        index_code = Column(String(16), primary_key=True)
        method_version = Column(String(48), primary_key=True)
        index_close = Column(Numeric(24, 8), nullable=False)
        mean_pe_ttm = Column(Numeric(30, 12))
        median_pe_ttm = Column(Numeric(30, 12))
        overall_pe_lyr = Column(Numeric(30, 12))
        mean_pe_lyr = Column(Numeric(30, 12))
        median_pe_lyr = Column(Numeric(30, 12))
        member_count = Column(Integer, nullable=False)
        statistics = Column(Text, nullable=False)
        members_sha256 = Column(String(64), nullable=False)
        source = Column(String(48), nullable=False)
        updated_at = Column(DateTime, nullable=False)

    class ValuationMember(Base):
        # One auditable payload per index/day, not millions of individual stock rows.
        __tablename__ = 'valuation_ruler_member_v2'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        trade_date = Column(Date, primary_key=True)
        index_code = Column(String(16), primary_key=True)
        method_version = Column(String(48), primary_key=True)
        payload = Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False)
        source = Column(String(48), nullable=False)
        updated_at = Column(DateTime, nullable=False)

    MODELS = (ValuationDaily, ValuationMember)

    def valid_code(code):
        return (builtins.isinstance(code, builtins.str)
                and re.match(r'^[036]\d{5}\.(?:XSHG|XSHE)\Z', code) is not None
                and ((code.startswith('6') and code.endswith('.XSHG'))
                     or (code.startswith(('0', '3')) and code.endswith('.XSHE'))))

    def json_text(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

    def source_number(value, field):
        if value is None:
            return None
        try:
            if Decimal(builtins.str(value)).is_nan():
                return None
        except (InvalidOperation, ValueError):
            pass
        return number(value, 30, 12, field, round_source=True)

    def aggregate(members, field):
        """Equal-weight and median for both bases; overall PE is static only."""
        active = [row for row in members if row['suspended_listing'] is not True]
        values = [Decimal(row[field]) for row in active if row[field] is not None]
        selected = builtins.sorted(value for value in values if 0 < value <= 300)
        counts = dict(eligible=builtins.len(selected), loss=builtins.sum(v < 0 for v in values),
                      over_limit=builtins.sum(v > 300 for v in values), missing_pe=builtins.len(active)-builtins.len(values),
                      suspended_listing=builtins.len(members)-builtins.len(active),
                      listing_status_unverified=builtins.sum(row['suspended_listing'] is None for row in members))
        with localcontext() as ctx:
            ctx.prec = 80
            mean = derived(builtins.sum(selected, Decimal(0))/builtins.len(selected), 12) if selected else None
            middle = builtins.len(selected)//2
            median = (selected[middle] if builtins.len(selected)%2 else
                      derived((selected[middle-1]+selected[middle])/2, 12)) if selected else None
            overall = None
            if field == 'pe_lyr':
                valid = [row for row in active if row[field] is not None and row['market_cap'] is not None]
                counts.update(overall_valid=builtins.len(valid), overall_missing=builtins.len(active)-builtins.len(valid))
                cap = builtins.sum((Decimal(r['market_cap']) for r in valid), Decimal(0))
                profit = builtins.sum((Decimal(r['market_cap'])/Decimal(r[field]) for r in valid), Decimal(0))
                # Missing/zero-profit PE must not produce a misleading whole-index ratio.
                overall = derived(cap/profit, 12) if valid and builtins.len(valid) == builtins.len(active) and profit > 0 else None
                counts.update(market_cap_yi=format(cap, 'f'), implied_profit_yi=format(derived(profit, 12), 'f'))
        return overall, mean, median, counts

    def summarize(day, close, members, index_code=INDEX, source=SOURCE):
        if source not in (SOURCE, PUBLIC_SOURCE):
            raise HistoryError('估值来源无效。')
        if index_code not in INDEXES:
            raise HistoryError('指数不在支持范围。')
        name, expected, _ = INDEXES[index_code]
        codes = [row['code'] for row in members]
        if (builtins.len(codes) != expected or builtins.len(builtins.set(codes)) != expected
                or not builtins.all(valid_code(code) for code in codes)):
            raise HistoryError('{}须有{}个唯一当日成分。'.format(name, expected))
        close = number(close, 24, 8, index_code+' close')
        if close <= 0:
            raise HistoryError('指数收盘价必须为正。')
        checked = []
        for row in builtins.sorted(members, key=lambda r: r['code']):
            status = row.get('suspended_listing')
            if status is not None and builtins.type(status) is not builtins.bool:
                raise HistoryError('暂停上市标记必须为布尔值或未独立核验的NULL。')
            item = dict(code=row['code'], suspended_listing=status)
            for field in ('pe_ttm', 'pe_lyr', 'market_cap'):
                value = row[field]
                if value is not None:
                    value = number(value, 30, 12, row['code']+' '+field)
                    if value == 0 or (field == 'market_cap' and value < 0):
                        raise HistoryError('{} {}：零值或负市值无效。'.format(row['code'], field))
                item[field] = None if value is None else format(value, '.12f')
            checked.append(item)
        key = dict(trade_date=day, index_code=index_code, method_version=METHOD, source=source, updated_at=china_now())
        daily = dict(key, index_close=close, member_count=expected)
        stats = dict(listing_status_basis=('historical_index_members' if source == SOURCE else 'eastmoney_current_members'), profit_basis='market_cap_divided_by_pe',
                     market_cap_unit='亿元', ordinary_trading_suspension_excluded=False)
        for basis in ('ttm', 'lyr'):
            overall, mean, median, stats[basis] = aggregate(checked, 'pe_'+basis)
            for metric, value in (('overall', overall), ('mean', mean), ('median', median)):
                if metric != 'overall' or basis == 'lyr':
                    daily[metric+'_pe_'+basis] = None if value is None else number(value, 30, 12)
        payload = json_text(checked)
        daily.update(statistics=json_text(stats), members_sha256=hashlib.sha256(payload.encode('utf-8')).hexdigest())
        return daily, dict(key, payload=payload)

    def source_day(day, index_code=INDEX):
        codes = api('get_index_stocks')(index_code, date=day)
        expected = INDEXES[index_code][1]
        if (codes is None or builtins.len(codes) != expected or builtins.len(builtins.set(codes)) != expected
                or not builtins.all(valid_code(code) for code in codes)):
            raise HistoryError('{} 当日成分缺失、重复或无效。'.format(INDEXES[index_code][0]))
        v = api('valuation')
        fields = ('code', 'day', 'pe_ratio', 'pe_ratio_lyr', 'market_cap')
        if not builtins.all(builtins.hasattr(v, field) for field in fields):
            raise HistoryError('聚宽valuation缺少静态PE或总市值字段，拒绝用TTM代替。')
        records = {}
        for offset in builtins.range(0, expected, 200):
            batch = codes[offset:offset+200]
            q = api('query')(*[builtins.getattr(v, field) for field in fields]).filter(v.code.in_(batch))
            frame = api('get_fundamentals')(q, date=day)
            if frame is None or not builtins.set(fields).issubset(frame.columns):
                raise HistoryError('{} {}：估值响应缺少字段。'.format(index_code, day))
            for row in frame.to_dict('records'):
                code = row['code']
                if code not in batch or code in records:
                    raise HistoryError('{} {}：估值代码越界或重复：{}。'.format(index_code, day, code))
                if as_date(row['day']) != day:
                    raise HistoryError('{} {}：{} 估值日期实际为 {}。'.format(index_code, day, code, row['day']))
                records[code] = row
        if not records:
            raise HistoryError('{} {}：全部成分均无估值记录，拒绝写入空结果。'.format(index_code, day))
        missing = [code for code in codes if code not in records]
        # Verify an absent row individually; a truncated batch must not become a false NULL.
        for code in missing:
            q = api('query')(*[builtins.getattr(v, field) for field in fields]).filter(v.code.in_([code]))
            frame = api('get_fundamentals')(q, date=day)
            if frame is None or not builtins.set(fields).issubset(frame.columns) or builtins.len(frame) > 1:
                raise HistoryError('{} {}：{} 缺失估值复核失败。'.format(index_code, day, code))
            if not frame.empty:
                row = frame.to_dict('records')[0]
                if row['code'] != code or as_date(row['day']) != day:
                    raise HistoryError('{} {}：{} 复核返回代码或日期不一致。'.format(index_code, day, code))
                records[code] = row
        prices = api('get_price')(index_code, start_date=day, end_date=day, frequency='daily',
                                  fields=['close'], fq=None, skip_paused=False)
        if prices is None or builtins.len(prices) != 1 or 'close' not in prices.columns or as_date(prices.index[0]) != day:
            raise HistoryError('指数收盘行情缺失或日期不匹配。')
        close = number(prices.iloc[0]['close'], 24, 8, index_code+' close', round_source=True)
        members = [dict(code=code, pe_ttm=source_number(records.get(code, {}).get('pe_ratio'), code+' pe_ratio'),
                        pe_lyr=source_number(records.get(code, {}).get('pe_ratio_lyr'), code+' pe_ratio_lyr'),
                        market_cap=source_number(records.get(code, {}).get('market_cap'), code+' market_cap'), suspended_listing=None)
                   for code in codes]
        return summarize(day, close, members, index_code)

    def normalized_statistics(value):
        """Compare retained statistics without requiring historical rewrites."""
        result = json.loads(value)
        for key in ('overall_valid', 'overall_missing', 'market_cap_yi', 'implied_profit_yi'):
            result['ttm'].pop(key, None)
        return result

    def existing_day(session, day, index_code=INDEX):
        key = dict(trade_date=day, index_code=index_code, method_version=METHOD)
        daily = session.query(ValuationDaily).filter_by(**key).one_or_none()
        raw = session.query(ValuationMember).filter_by(**key).one_or_none()
        if daily is None and raw is None:
            return False
        if daily is None or raw is None:
            raise HistoryError('已有单日数据不完整；不会覆盖。')
        complete(daily, ValuationDaily)
        complete(raw, ValuationMember)
        if daily.source not in (SOURCE, PUBLIC_SOURCE) or raw.source != daily.source:
            raise HistoryError('已有记录来源不匹配。')
        computed, archive = summarize(day, daily.index_close, json.loads(raw.payload), index_code, source=daily.source)
        if (archive['payload'] != raw.payload or builtins.any((normalized_statistics(daily.statistics) != normalized_statistics(v) if k == 'statistics' else builtins.getattr(daily, k) != v)
                for k, v in computed.items() if k != 'updated_at')):
            raise HistoryError('已有汇总、指纹与成分原值不一致；不会覆盖。')
        return True

    def save_day(Session, daily, raw, progress, done, total):
        """Retry only rolled-back lock conflicts, reusing the already collected inputs."""
        day, code = daily['trade_date'], daily['index_code']
        stage = '{}：写入结果'.format(INDEXES[code][0])
        for attempt in builtins.range(3):
            progress.update(stage, done, total, day)
            session = Session()
            try:
                session.info['_result_journal_callbacks'][1](session)
                added = not existing_day(session, day, code)
                if added:
                    session.add(ValuationDaily(**daily))
                    session.add(ValuationMember(**raw))
                else:
                    old = session.query(ValuationDaily).filter_by(trade_date=day, index_code=code, method_version=METHOD).one()
                    if old.source != PUBLIC_SOURCE and builtins.any((normalized_statistics(old.statistics) != normalized_statistics(v) if k == 'statistics' else builtins.getattr(old, k) != v) for k, v in daily.items() if k != 'updated_at'):
                        raise HistoryError('并发已有结果冲突，本次事务取消。')
                session.commit()
                return builtins.int(added)
            except OperationalError as exc:
                session.rollback()
                args = builtins.getattr(exc.orig, 'args', ())
                if not args or args[0] not in (1205, 1213):
                    raise
                if attempt == 2:
                    raise HistoryError('数据库锁冲突（{}），3次写入均未完成；请检查其他估值写入任务或未提交事务后重跑。'.format(args[0])) from None
            except BaseException:
                session.rollback()
                raise
            finally:
                session.close()
            progress.update('{}：锁冲突，准备第 {}/3 次写入'.format(INDEXES[code][0], attempt+2),
                            done, total, day, force=True)
            time.sleep(2*(attempt+1))  # Never wait while retaining the failed transaction/connection.

    def collect(start_date=START_DATE, end_date=END_DATE, write_database=WRITE_DATABASE, max_days=MAX_DAYS,
            return_report=False, indices=INDICES):
        if not builtins.isinstance(write_database, builtins.bool):
            raise HistoryError('write_database 必须为 True 或 False。')
        if (not builtins.isinstance(indices, (list, tuple)) or not indices
                or builtins.len(builtins.set(indices)) != builtins.len(indices)
                or builtins.any(code not in INDEXES for code in indices)):
            raise HistoryError('indices须为不重复的受支持指数代码列表。')
        if as_date(start_date) < dt.date(2010, 1, 1):
            raise HistoryError('起点不得早于2010年。')
        builtins.print('估值采集v2：静态整体PE、静态/滚动等权PE及中位数；等权/中位数仅取0<PE≤300。')
        engine = Session = None
        current_day, stage = None, '读取交易日历'
        progress = Progress()
        progress.update(stage, force=True)
        try:
            days = range_days(start_date, end_date, max_days)
            if write_database:
                stage = '连接数据库/校验表结构'
                progress.update(stage)
                engine, Session = database(MODELS, 'valuation-ruler-v2')
            pending, skipped = {}, 0
            for code in indices:
                candidates = [d for d in days if d >= as_date(INDEXES[code][2])]
                latest = None
                public_days = builtins.set()
                if write_database:
                    session = Session()
                    try:
                        # Web collection can already contain today's row while JQ is still
                        # backfilling 2014. Its date must not jump the historical checkpoint.
                        latest = session.query(ValuationDaily.trade_date).filter_by(index_code=code, method_version=METHOD, source=SOURCE).order_by(
                            ValuationDaily.trade_date.desc()).first()
                        if latest:
                            stage = '核对{}最后完成日'.format(INDEXES[code][0])
                            progress.update(stage, day=latest[0])
                            existing_day(session, latest[0], code)
                        public_days = builtins.set(row[0] for row in session.query(ValuationDaily.trade_date).filter_by(
                            index_code=code, method_version=METHOD, source=PUBLIC_SOURCE).filter(
                                ValuationDaily.trade_date >= (latest[0] if latest else as_date(start_date)),
                                ValuationDaily.trade_date <= days[-1]).all())
                    finally:
                        session.close()
                pending[code] = [d for d in candidates if (latest is None or d > latest[0]) and d not in public_days]
                skipped += builtins.len(candidates)-builtins.len(pending[code])
            selected_days = builtins.sorted(builtins.set(d for values in pending.values() for d in values))[:max_days]
            pending = {code: builtins.set(values) for code, values in pending.items()}
            total = builtins.sum(builtins.len(values) for values in pending.values())
            selected = [(day, code) for day in selected_days for code in indices if day in pending[code]]
            results, added = [], 0
            for offset, (day, code) in enumerate(selected):
                current_day = day
                stage = '{}：读取成分与估值'.format(INDEXES[code][0])
                progress.update(stage, offset, builtins.len(selected), day)
                daily, raw = source_day(day, code)
                if write_database:
                    stage = '{}：写入结果'.format(INDEXES[code][0])
                    added += save_day(Session, daily, raw, progress, offset, builtins.len(selected))
                results.append(daily)
                progress.update(stage, offset+1, builtins.len(selected), day)
            progress.finish('完成' if selected else '无需补采')
            builtins.print('处理 {} 个指数日，剩余 {}；{}'.format(builtins.len(results), total-builtins.len(results),
                '重复运行按各指数最后完成日继续。' if write_database else '仅预览，未连接数据库。'))
            stats = [json.loads(row['statistics']) for row in results]
            missing = [builtins.sum(s[basis]['missing_pe'] for s in stats) for basis in ('lyr', 'ttm')]
            if builtins.any(missing):
                builtins.print('缺失PE汇总：静态 {} 条，滚动 {} 条（按成分×指数日统计）；保留NULL，明细随结果保存。'.format(*missing))
            if return_report:
                return dict(added=added, skipped=skipped+builtins.len(selected)-added,
                            remaining=total-builtins.len(selected), gaps=[], unit='指数日')
            return results
        except HistoryError as exc:
            progress.finish('失败：本次事务未提交')
            raise HistoryError('{}：{} 之前完成日期保留。'.format(current_day or '初始化', exc)) from None
        except Exception as exc:
            progress.finish('失败：本次事务未提交')
            raise RuntimeError('{}：{}；之前完成日期保留。'.format(current_day or '初始化', diagnostic(exc, stage))) from None
        except KeyboardInterrupt:
            progress.finish('已中断')
            raise
        finally:
            if engine is not None:
                engine.dispose()

    settings = strategy_settings('估值标尺')

    def run(start_date=settings['start_date'], end_date=settings['end_date'],
            max_days=settings['max_days'], indices=settings['indices'], skip_existing=True):
        _require_append(skip_existing)
        return collect(start_date=start_date, end_date=end_date, write_database=True,
                       max_days=max_days, indices=indices, return_report=True)

    return SimpleNamespace(**locals())
# END EMBEDDED INDEX HISTORY




# 注册信息：(策略工厂, 写入模式, 预览字段)。顺序由顶部 STRATEGY_ORDER 控制。
STRATEGY_SPECS = {
    '估值标尺': (_valuation_ruler_strategy, 'history', ()),
    '个股机构持仓': (_ownership_strategy, 'changes',
        ('kind', 'code', 'date')),
    '行业动量': (_momentum_strategy, 'append',
        ('trade_date', 'industry_name', 'momentum', 'momentum_rank')),
    '市场情绪跟踪': (_sentiment_strategy, 'append',
        ('trade_date', 'ratio', 'above_count', 'eligible_count', 'universe_count')),
    '抱团拥挤度': (_crowding_strategy, 'append',
        ('trade_date', 'top_pct', 'ratio', 'top_money', 'total_money')),
    '微盘温度计': (_microcap_strategy, 'append',
        ('trade_date', 'score', 'micro_index', 'sample_count')),
    '微盘新高新低': (_breadth_strategy, 'append',
        ('trade_date', 'sample_count', 'high_5', 'low_5', 'high_30', 'low_30')),
    '高股息选股': (_dividend_strategy, 'latest',
        ('method', 'rank', 'code', 'name', 'dividend_yield_1y', 'pe_ttm')),
    'ETF动量热度': (_etf_heat_strategy, 'append',
        ('trade_date', 'code', 'name', 'score', 'annual_return', 'r2')),
}
STRATEGIES = {name: spec[0] for name, spec in STRATEGY_SPECS.items()}

# ==================== 批量调度 ====================
class BatchRunError(RuntimeError):
    def __init__(self, summary):
        self.summary = summary
        failed = [
            '{}（{}）'.format(r['strategy'], '执行失败' if r['status'] == 'FAILED'
                             else '缺口 {} {}'.format(r['gaps'], r['unit']))
            for r in summary if r['status'] in ('FAILED', 'PARTIAL')
        ]
        super(BatchRunError, self).__init__('采集未全部完成：{}；请查看控制台结果。'.format('、'.join(failed)))


class _QuietStream(io.TextIOBase):
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
    apis = {key: api_namespace.get(key, getattr(jq, key, getattr(builtins, key, None)))
            for key in _API_NAMES}
    return STRATEGIES[name](apis, config).run


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


def run_all(strategy_names=None, options=None, db_config=None, preview_rows=None,
            raise_on_failure=True, api_namespace=None):
    """依次调用文件内策略；高股息替换最新结果，机构持仓更新变化，其余只新增；控制台展示进度及结果，失败后继续，最后统一报告。"""
    names = list(STRATEGY_ORDER if strategy_names is None else strategy_names)
    config = dict(DB_CONFIG if db_config is None else db_config)
    options = dict(STRATEGY_OPTIONS if options is None else options)
    preview_rows = PREVIEW_ROWS if preview_rows is None else preview_rows
    if not names or len(names) != len(set(names)) or builtins.any(n not in STRATEGIES for n in names):
        raise ValueError('策略列表不能为空、重复或包含未知名称。')
    if set(config) != {'host', 'port', 'dbname', 'dbuser', 'dbpassword'}:
        raise ValueError('DB_CONFIG 必须包含 host、port、dbname、dbuser、dbpassword。')
    if builtins.any(n not in STRATEGIES or not isinstance(v, dict) for n, v in options.items()):
        raise ValueError('策略参数必须为按策略名称组织的字典。')
    if builtins.any(v.get('skip_existing', True) is not True for v in options.values()):
        raise ValueError('不允许关闭 skip_existing；已有股票及其他明细保持原值。')
    if not isinstance(preview_rows, int) or preview_rows < 0:
        raise ValueError('preview_rows 必须是非负整数。')
    if not isinstance(PROGRESS_ENABLED, bool):
        raise ValueError('PROGRESS_ENABLED 必须为布尔值。')
    if (isinstance(PROGRESS_INTERVAL_SECONDS, bool) or not isinstance(PROGRESS_INTERVAL_SECONDS, (int, float))
            or not math.isfinite(PROGRESS_INTERVAL_SECONDS) or PROGRESS_INTERVAL_SECONDS <= 0):
        raise ValueError('PROGRESS_INTERVAL_SECONDS 必须是有限正数。')
    apis = globals() if api_namespace is None else api_namespace
    summary = []
    with _batch_guard():
        for strategy_index, name in enumerate(names, 1):
            started = time.monotonic()
            row = dict(strategy=name, status='FAILED', rows=None, error_type=None, error=None)
            result = None
            run = None
            _, write_mode, preview_columns = STRATEGY_SPECS[name]
            try:
                with _progress_scope(name, strategy_index, len(names)), _QuietStream() as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
                    run = _load_strategy(name, config, apis)
                    kwargs = dict(options.get(name, {}))
                    if write_mode == 'latest':
                        kwargs.pop('skip_existing', None)  # 此模块显式授权每次更新最新结果。
                    else:
                        kwargs['skip_existing'] = True
                    result = run(**kwargs)
                    if isinstance(result, dict):
                        # Historical archives return a progress report, not a list of selected stocks.
                        required = ('added', 'skipped', 'remaining', 'gaps')
                        if builtins.any(key not in result for key in required):
                            raise ValueError('历史采集报告缺少进度字段。')
                        if builtins.any(isinstance(result[key], bool) or not isinstance(result[key], int)
                                        or result[key] < 0 for key in required[:3]) or not isinstance(result['gaps'], list):
                            raise ValueError('历史采集报告进度字段无效。')
                        unresolved = result.get('unresolved_gaps', len(result['gaps']))
                        if unresolved is None:  # Preview does not inspect saved gaps.
                            unresolved = len(result['gaps'])
                        if isinstance(unresolved, bool) or not isinstance(unresolved, int) or unresolved < 0:
                            raise ValueError('历史采集报告缺口数无效。')
                        row.update(status='PARTIAL' if unresolved else ('PENDING' if result['remaining'] else 'SUCCESS'),
                                   rows=result['added'], archive=True, remaining=result['remaining'],
                                   skipped=result['skipped'], gaps=unresolved, unit=result.get('unit', '日'))
                    else:
                        row.update(status='SUCCESS', rows=len(result))
            except KeyboardInterrupt:
                print('【{}】已中断；已提交数据保留，后续策略不再执行。'.format(name))
                raise
            except Exception as exc:
                row.update(error_type=type(exc).__name__, error=_safe_error(exc, config))
            finally:
                run = None
                # Factories and third-party responses can form cycles across Notebook runs.
                # Explicit session/engine closure remains each strategy's responsibility.
                gc.collect()
            row['elapsed_seconds'] = builtins.round(time.monotonic() - started, 3)
            summary.append(row)
            if row.get('archive'):
                print('\n【{}】历史档案：新增 {} {}，已完成跳过 {} {}，待续跑 {} {}，失败缺口 {} {}{}。'.format(
                    name, row['rows'], row['unit'], row['skipped'], row['unit'], row['remaining'], row['unit'],
                    row['gaps'], row['unit'], '；未覆盖最新选股' if write_mode != 'history' else ''))

            elif row['status'] == 'SUCCESS':
                label = {'changes': '新增或更新', 'latest': '当前选股结果', 'append': '新增', 'history': '新增'}[write_mode]
                print('\n【{}】成功：{} {} 条，耗时 {:.2f} 秒。'.format(name, label, row['rows'], row['elapsed_seconds']))
                if not row['rows']:
                    print('本次选股无匹配，最新结果已保存。' if write_mode == 'latest'
                          else '无新增数据；已有记录跳过，或数据源尚无可入库数据。')
                elif preview_rows:
                    try:
                        columns = [c for c in preview_columns if c in result.columns]
                        print('本次结果预览（最后 {} 条，字段单位沿用策略）：'.format(builtins.min(preview_rows, row['rows'])))
                        print(result[columns].tail(preview_rows).to_string(index=False))
                    except Exception:
                        print('结果预览不可用，入库已完成。')
            else:
                print('\n【{}】失败：{}；已提交部分保留，继续后续策略。'.format(name, row['error']))
            del result
        print('\n采集结果汇总\n策略 | 结果 | 条数（高股息为当前结果，机构持仓为新增或更新，其余为新增） | 耗时（秒）')
        for row in summary:
            print('{} | {} | {} | {:.2f}'.format(row['strategy'], row['status'],
                  row['rows'] if row['rows'] is not None else '未完成', row['elapsed_seconds']))
        failed = builtins.sum(r['status'] in ('FAILED', 'PARTIAL') for r in summary)
        pending = builtins.sum(r['status'] == 'PENDING' for r in summary)
        print('完成 {}，待续跑 {}，失败或存在缺口 {}。'.format(len(summary) - failed - pending, pending, failed))
        if failed and raise_on_failure:
            raise BatchRunError(summary)
    return summary


# ---------- 行业动量 ----------


if __name__ == '__main__':
    batch_summary = run_all()
