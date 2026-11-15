# -*- coding: utf-8 -*-
"""聚宽指数估值采集：三指数的静态/滚动整体、等权、中位数PE。
配置日期和指数后整段运行即入库；按各指数最后完成日续采，仅依赖mysql_connector。
新口径写入valuation_ruler_daily_v2/member_v2，不改旧表。历史为平台当前可见修订值。
整体PE以总市值/个股PE反推利润；等权与中位数只取0<PE<=300，普通停牌不剔除。
暂停上市目前以当日指数成分名单为基础，状态来源随结果保存，不声称完全复刻乐咕。
"""
from __future__ import print_function, division
import datetime as dt
import builtins as _builtins
import time
import hashlib
import re
from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_EVEN
import jqdata as jq
from sqlalchemy import Column, Date, DateTime, Integer, Numeric, String, inspect
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import json
import uuid
from sqlalchemy import BigInteger, MetaData, Table, Text
from sqlalchemy import event as sql_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.schema import CreateTable
from sqlalchemy import text as sql_text


WRITE_DATABASE = True
MAX_DAYS = None  # 默认一次补完最后完成日期之后的交易日；可设整数限制本轮天数。
END_DATE = None
Base = declarative_base()

class HistoryError(ValueError):
    """Fixed credential-free source/schema diagnostics."""
    pass


class Progress:
    """Update one Notebook output (or terminal line), without clearing other output."""
    def __init__(self):
        self.started = time.monotonic()
        self.last_draw = -1
        self.width = 0
        self.stage, self.done, self.total, self.day = '', 0, None, None
        shell = _builtins.getattr(_builtins, 'get_ipython', lambda: None)()
        self.publisher = _builtins.getattr(shell, 'display_pub', None) if _builtins.getattr(shell, 'kernel', None) else None
        self.display_id, self.displayed = uuid.uuid4().hex, False

    def update(self, stage, done=0, total=None, day=None, force=False):
        now = time.monotonic()
        self.stage, self.done, self.total, self.day = stage, done, total, day
        if not force and now-self.last_draw < 0.5:
            return
        if total is None:
            bar = '[--------------------] --'
        else:
            ratio = done/total if total else 1
            filled = _builtins.int(20*ratio)
            percent = 100*done//total if total else 100
            bar = '[{}{}] {}/{} 指数日 {}%'.format('#'*filled, '-'*(20-filled), done, total, percent)
        line = '估值历史补采 | {} | {} | {} | 已用 {:.0f}s'.format(stage, bar, day or '--', now-self.started)
        if self.publisher is not None:
            try:
                self.publisher.publish(data={'text/plain': line}, metadata={},
                    transient={'display_id': self.display_id}, update=self.displayed)
                self.displayed = True
            except Exception:
                self.publisher = None  # Display failure must not interrupt committed collection.
        if self.publisher is None:
            _builtins.print('\r'+line+' '*_builtins.max(0, self.width-_builtins.len(line)), end='', flush=True)
        self.width, self.last_draw = _builtins.len(line), now

    def finish(self, status):
        self.update(status, self.done, self.total, self.day, force=True)
        if self.publisher is None:
            _builtins.print(flush=True)


def api(name):
    """Research APIs may live in Notebook globals/builtins instead of jqdata."""
    for value in (globals().get(name), _builtins.getattr(jq, name, None), _builtins.getattr(_builtins, name, None)):
        valid = (_builtins.all(_builtins.hasattr(value, field) for field in ('code', 'day', 'pe_ratio'))
                 if name == 'valuation' else _builtins.callable(value))
        if valid:
            return value
    raise HistoryError('聚宽研究环境缺少接口：{}。请在研究 Notebook 完整运行脚本。'.format(name))


def diagnostic(exc, stage):
    # Never include exception messages, SQL, parameters, filenames or connection URLs.
    kind = _builtins.type(exc).__name__
    if not re.match(r'^[A-Za-z_][A-Za-z_0-9]{0,63}$', kind):
        kind = 'Exception'
    original = _builtins.getattr(exc, 'orig', exc)
    args = _builtins.getattr(original, 'args', ())
    code = args[0] if args and _builtins.type(args[0]) is _builtins.int and 1000 <= args[0] <= 9999 else None
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
    if _builtins.isinstance(value, dt.datetime):
        return value.date()
    if _builtins.isinstance(value, dt.date):
        return value
    if not _builtins.isinstance(value, _builtins.str) or not re.match(r'^\d{4}-\d{2}-\d{2}$', value):
        raise HistoryError('日期必须为 YYYY-MM-DD。')
    return dt.datetime.strptime(value, '%Y-%m-%d').date()


def number(value, precision, scale, field='数值', round_source=False):
    if _builtins.isinstance(value, _builtins.bool) or value is None:
        raise HistoryError('{}：数值缺失或类型错误。'.format(field))
    try:
        result = Decimal(_builtins.str(value))
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
    if not full or full != _builtins.sorted(_builtins.set(full)):
        raise HistoryError('平台完整交易日历无效。')
    expected = [d for d in full if (start is None or d >= start) and d <= end]
    if not expected:
        raise HistoryError('区间没有平台交易日。')
    actual = [as_date(x) for x in api('get_trade_days')(start_date=expected[0], end_date=end)]
    if actual != expected:
        raise HistoryError('交易日历区间覆盖不完整或乱序。')
    return expected


def range_days(start, end, max_days):
    if max_days is not None and (_builtins.isinstance(max_days, _builtins.bool) or not _builtins.isinstance(max_days, _builtins.int) or not 1 <= max_days <= 250):
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
        if _builtins.set(cols) != _builtins.set(c.name for c in table.columns):
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
            if _builtins.getattr(typ, 'unsigned', False):
                raise HistoryError('数据库数值字段不得使用无符号类型。')
            if _builtins.isinstance(col.type, Numeric):
                if not _builtins.isinstance(typ, Numeric) or (typ.precision, typ.scale) != (col.type.precision, col.type.scale):
                    raise HistoryError('数据库数值精度不一致。')
            elif _builtins.isinstance(col.type, String):
                if not _builtins.isinstance(typ, String) or typ.length != col.type.length:
                    raise HistoryError('数据库文本类型不一致。')
            elif col.type._type_affinity is not typ._type_affinity:
                raise HistoryError('数据库字段类型不一致。')


def database(models, module):
    # Configuration import and credentials are deliberately deferred until write mode.
    from mysql_connector import create_mysql_engine
    engine = create_mysql_engine()
    engine.echo = False
    if _builtins.hasattr(engine, 'hide_parameters'):
        engine.hide_parameters = True
    try:
        Base.metadata.create_all(engine)
        validate_schema(engine, models)
        Session = sessionmaker(bind=engine)
        _install_result_journal(Session, _builtins.tuple(m for m in models if m.__tablename__ != 'market_crowding'), module)
        return engine, Session
    except HistoryError:
        engine.dispose()
        raise
    except Exception as exc:
        engine.dispose()
        raise HistoryError(diagnostic(exc, '连接数据库/校验表结构') + '；未写入历史结果。') from None


def complete(row, model):
    for col in model.__table__.columns:
        value = _builtins.getattr(row, col.name)
        if not col.nullable and value is None:
            raise HistoryError('已有记录不完整，需人工核实；不会覆盖。')
        if value is not None and _builtins.isinstance(col.type, Numeric):
            number(value, col.type.precision, col.type.scale, model.__tablename__+'.'+col.name)
    return True


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
    with engine.begin() as connection:
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
    overall_pe_ttm = Column(Numeric(30, 12))
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
    return (_builtins.isinstance(code, _builtins.str)
            and re.match(r'^[036]\d{5}\.(?:XSHG|XSHE)\Z', code) is not None
            and ((code.startswith('6') and code.endswith('.XSHG'))
                 or (code.startswith(('0', '3')) and code.endswith('.XSHE'))))


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def source_number(value, field):
    if value is None:
        return None
    try:
        if Decimal(_builtins.str(value)).is_nan():
            return None
    except (InvalidOperation, ValueError):
        pass
    return number(value, 30, 12, field, round_source=True)


def aggregate(members, field):
    """Separate samples for each PE basis; never apply the 300 cap to overall PE."""
    active = [row for row in members if row['suspended_listing'] is not True]
    values = [Decimal(row[field]) for row in active if row[field] is not None]
    selected = _builtins.sorted(value for value in values if 0 < value <= 300)
    valid = [row for row in active if row[field] is not None and row['market_cap'] is not None]
    counts = dict(eligible=_builtins.len(selected), loss=_builtins.sum(v < 0 for v in values),
                  over_limit=_builtins.sum(v > 300 for v in values), missing_pe=_builtins.len(active)-_builtins.len(values),
                  suspended_listing=_builtins.len(members)-_builtins.len(active),
                  listing_status_unverified=_builtins.sum(row['suspended_listing'] is None for row in members),
                  overall_valid=_builtins.len(valid), overall_missing=_builtins.len(active)-_builtins.len(valid))
    with localcontext() as ctx:
        ctx.prec = 80
        mean = derived(_builtins.sum(selected, Decimal(0))/_builtins.len(selected), 12) if selected else None
        middle = _builtins.len(selected)//2
        median = (selected[middle] if _builtins.len(selected)%2 else
                  derived((selected[middle-1]+selected[middle])/2, 12)) if selected else None
        cap = _builtins.sum((Decimal(r['market_cap']) for r in valid), Decimal(0))
        profit = _builtins.sum((Decimal(r['market_cap'])/Decimal(r[field]) for r in valid), Decimal(0))
        # Missing/zero-profit PE must not produce a misleading whole-index ratio.
        overall = derived(cap/profit, 12) if valid and _builtins.len(valid) == _builtins.len(active) and profit > 0 else None
        counts.update(market_cap_yi=format(cap, 'f'), implied_profit_yi=format(derived(profit, 12), 'f'))
    return overall, mean, median, counts


def summarize(day, close, members, index_code=INDEX, source=SOURCE):
    if source not in (SOURCE, PUBLIC_SOURCE):
        raise HistoryError('估值来源无效。')
    if index_code not in INDEXES:
        raise HistoryError('指数不在支持范围。')
    name, expected, _ = INDEXES[index_code]
    codes = [row['code'] for row in members]
    if (_builtins.len(codes) != expected or _builtins.len(_builtins.set(codes)) != expected
            or not _builtins.all(valid_code(code) for code in codes)):
        raise HistoryError('{}须有{}个唯一当日成分。'.format(name, expected))
    close = number(close, 24, 8, index_code+' close')
    if close <= 0:
        raise HistoryError('指数收盘价必须为正。')
    checked = []
    for row in _builtins.sorted(members, key=lambda r: r['code']):
        status = row.get('suspended_listing')
        if status is not None and _builtins.type(status) is not _builtins.bool:
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
            daily[metric+'_pe_'+basis] = None if value is None else number(value, 30, 12)
    payload = json_text(checked)
    daily.update(statistics=json_text(stats), members_sha256=hashlib.sha256(payload.encode('utf-8')).hexdigest())
    return daily, dict(key, payload=payload)


def source_day(day, index_code=INDEX):
    codes = api('get_index_stocks')(index_code, date=day)
    expected = INDEXES[index_code][1]
    if (codes is None or _builtins.len(codes) != expected or _builtins.len(_builtins.set(codes)) != expected
            or not _builtins.all(valid_code(code) for code in codes)):
        raise HistoryError('{} 当日成分缺失、重复或无效。'.format(INDEXES[index_code][0]))
    v = api('valuation')
    fields = ('code', 'day', 'pe_ratio', 'pe_ratio_lyr', 'market_cap')
    if not _builtins.all(_builtins.hasattr(v, field) for field in fields):
        raise HistoryError('聚宽valuation缺少静态PE或总市值字段，拒绝用TTM代替。')
    records = {}
    for offset in _builtins.range(0, expected, 200):
        batch = codes[offset:offset+200]
        q = api('query')(*[_builtins.getattr(v, field) for field in fields]).filter(v.code.in_(batch))
        frame = api('get_fundamentals')(q, date=day)
        if frame is None or not _builtins.set(fields).issubset(frame.columns):
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
        q = api('query')(*[_builtins.getattr(v, field) for field in fields]).filter(v.code.in_([code]))
        frame = api('get_fundamentals')(q, date=day)
        if frame is None or not _builtins.set(fields).issubset(frame.columns) or _builtins.len(frame) > 1:
            raise HistoryError('{} {}：{} 缺失估值复核失败。'.format(index_code, day, code))
        if not frame.empty:
            row = frame.to_dict('records')[0]
            if row['code'] != code or as_date(row['day']) != day:
                raise HistoryError('{} {}：{} 复核返回代码或日期不一致。'.format(index_code, day, code))
            records[code] = row
    prices = api('get_price')(index_code, start_date=day, end_date=day, frequency='daily',
                              fields=['close'], fq=None, skip_paused=False)
    if prices is None or _builtins.len(prices) != 1 or 'close' not in prices.columns or as_date(prices.index[0]) != day:
        raise HistoryError('指数收盘行情缺失或日期不匹配。')
    close = number(prices.iloc[0]['close'], 24, 8, index_code+' close', round_source=True)
    members = [dict(code=code, pe_ttm=source_number(records.get(code, {}).get('pe_ratio'), code+' pe_ratio'),
                    pe_lyr=source_number(records.get(code, {}).get('pe_ratio_lyr'), code+' pe_ratio_lyr'),
                    market_cap=source_number(records.get(code, {}).get('market_cap'), code+' market_cap'), suspended_listing=None)
               for code in codes]
    return summarize(day, close, members, index_code)


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
    if (archive['payload'] != raw.payload or _builtins.any(_builtins.getattr(daily, k) != v
            for k, v in computed.items() if k != 'updated_at')):
        raise HistoryError('已有汇总、指纹与成分原值不一致；不会覆盖。')
    return True


def run(start_date=START_DATE, end_date=END_DATE, write_database=WRITE_DATABASE, max_days=MAX_DAYS,
        return_report=False, indices=INDICES):
    if not _builtins.isinstance(write_database, _builtins.bool):
        raise HistoryError('write_database 必须为 True 或 False。')
    if (not _builtins.isinstance(indices, (list, tuple)) or not indices
            or _builtins.len(_builtins.set(indices)) != _builtins.len(indices)
            or _builtins.any(code not in INDEXES for code in indices)):
        raise HistoryError('indices须为不重复的受支持指数代码列表。')
    if as_date(start_date) < dt.date(2010, 1, 1):
        raise HistoryError('起点不得早于2010年。')
    _builtins.print('估值采集v2：静态/滚动整体PE、等权PE及中位数；等权/中位数仅取0<PE≤300。')
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
            public_days = _builtins.set()
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
                    public_days = _builtins.set(row[0] for row in session.query(ValuationDaily.trade_date).filter_by(
                        index_code=code, method_version=METHOD, source=PUBLIC_SOURCE).filter(
                            ValuationDaily.trade_date >= (latest[0] if latest else as_date(start_date)),
                            ValuationDaily.trade_date <= days[-1]).all())
                finally:
                    session.close()
            pending[code] = [d for d in candidates if (latest is None or d > latest[0]) and d not in public_days]
            skipped += _builtins.len(candidates)-_builtins.len(pending[code])
        selected_days = _builtins.sorted(_builtins.set(d for values in pending.values() for d in values))[:max_days]
        pending = {code: _builtins.set(values) for code, values in pending.items()}
        total = _builtins.sum(_builtins.len(values) for values in pending.values())
        selected = [(day, code) for day in selected_days for code in indices if day in pending[code]]
        results, added = [], 0
        for offset, (day, code) in enumerate(selected):
            current_day = day
            stage = '{}：读取成分与估值'.format(INDEXES[code][0])
            progress.update(stage, offset, _builtins.len(selected), day)
            daily, raw = source_day(day, code)
            if write_database:
                stage = '{}：写入结果'.format(INDEXES[code][0])
                progress.update(stage, offset, _builtins.len(selected), day)
                session = Session()
                try:
                    session.info['_result_journal_callbacks'][1](session)
                    if existing_day(session, day, code):
                        old = session.query(ValuationDaily).filter_by(trade_date=day, index_code=code, method_version=METHOD).one()
                        if old.source != PUBLIC_SOURCE and _builtins.any(_builtins.getattr(old, k) != v for k, v in daily.items() if k != 'updated_at'):
                            raise HistoryError('并发已有结果冲突，本次事务取消。')
                    else:
                        session.add(ValuationDaily(**daily))
                        session.add(ValuationMember(**raw))
                        added += 1
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                finally:
                    session.close()
            results.append(daily)
            progress.update(stage, offset+1, _builtins.len(selected), day)
        progress.finish('完成' if selected else '无需补采')
        _builtins.print('处理 {} 个指数日，剩余 {}；{}'.format(_builtins.len(results), total-_builtins.len(results),
            '重复运行按各指数最后完成日继续。' if write_database else '仅预览，未连接数据库。'))
        stats = [json.loads(row['statistics']) for row in results]
        missing = [_builtins.sum(s[basis]['missing_pe'] for s in stats) for basis in ('lyr', 'ttm')]
        if _builtins.any(missing):
            _builtins.print('缺失PE汇总：静态 {} 条，滚动 {} 条（按成分×指数日统计）；保留NULL，明细随结果保存。'.format(*missing))
        if return_report:
            return dict(added=added, skipped=skipped+_builtins.len(selected)-added,
                        remaining=total-_builtins.len(selected), gaps=[], unit='指数日')
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


if __name__ == '__main__':
    run()
