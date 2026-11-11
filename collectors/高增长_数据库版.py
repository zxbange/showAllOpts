# -*- coding: utf-8 -*-
"""聚宽研究：高增长四类财报筛选；完整单文件，最新名单与独立历史档案，不下单。"""
import builtins
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
from sqlalchemy import Column, Date, DateTime, Index, Integer, JSON, Numeric, String, and_, or_, create_engine
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

STRATEGY_CONFIG = {'高增长': {'end_date': None, 'batch_size': 100, 'page_size': 2000}}

from mysql_connector import get_database_config, get_database_options, mysql_drivername, engine_options
DB_CONFIG = get_database_config()
DB_OPTIONS = get_database_options()




PROGRESS_ENABLED = True  # 控制台进度条；关闭后仍保留各策略结果和最终汇总。


PROGRESS_INTERVAL_SECONDS = 5.0  # 最多每隔多少秒刷新；等待接口/数据库时也刷新耗时。


_PROGRESS_LOCAL = threading.local()


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
    secrets = set()
    for key in ('host', 'dbname', 'dbuser', 'dbpassword'):
        value = str(config[key])
        if value:
            secrets.update((value, quote(value, safe=''), quote_plus(value)))
    for value in sorted(secrets, key=len, reverse=True):
        message = message.replace(value, '[已隐藏]')
    return '{}: {}'.format(type(exc).__name__, ' '.join(message.split())[:400])


def _growth_strategy(_apis, _config):
    """四类累计财报，各取最新三期；归母净利润和基本 EPS 双增长。"""
    settings = strategy_settings('高增长')
    get_all_securities, get_trade_days, get_fundamentals, get_industry, query, valuation = [
        _apis.get(k) for k in ('get_all_securities', 'get_trade_days', 'get_fundamentals', 'get_industry', 'query', 'valuation')]
    BATCH_SIZE, PAGE_SIZE = settings['batch_size'], settings['page_size']
    if BATCH_SIZE > 500 or PAGE_SIZE > 3000:
        raise ValueError('batch_size 不得超过 500，page_size 不得超过 3000。')
    END_DATE = settings['end_date']
    RULE_VERSION = 'growth_cumulative_parent_eps_v1'
    GROUPS = {'annual': (12, 31), 'q1': (3, 31), 'q2': (6, 30), 'q3': (9, 30)}
    Base = declarative_base()

    class GrowthSnapshot(Base):
        __tablename__ = 'high_growth_snapshot'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        id = Column(Integer, primary_key=True)
        as_of_date = Column(Date, nullable=False)
        valuation_date = Column(Date, nullable=False)
        updated_at = Column(DateTime, nullable=False)
        rule_version = Column(String(48), nullable=False)
        universe_count = Column(Integer, nullable=False)
        evaluated_count = Column(Integer, nullable=False)
        matched_count = Column(Integer, nullable=False)


    class GrowthStock(Base):
        __tablename__ = 'high_growth_stock'
        __table_args__ = (Index('ux_high_growth_rank', 'rank', unique=True),
                          {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'})
        code = Column(String(16), primary_key=True)
        as_of_date = Column(Date, nullable=False)
        rank = Column(Integer, nullable=False)
        name = Column(String(128), nullable=False)
        industry_code = Column(String(32), nullable=True)
        industry_name = Column(String(128), nullable=True)
        industry_level = Column(String(16), nullable=True)
        pe_ttm = Column(Numeric(24, 6), nullable=True)
        pb = Column(Numeric(24, 6), nullable=True)
        market_cap = Column(Numeric(24, 6), nullable=True)
        annual_profit_growth = Column(Numeric(28, 8), nullable=False)
        reports = Column(JSON, nullable=False)

    RESULT_COLUMNS = [c.name for c in GrowthStock.__table__.columns]
    META_COLUMNS = [c.name for c in GrowthSnapshot.__table__.columns if c.name not in ('id', 'updated_at')]

    number = _decimal_number

    rounded = _round_decimal

    rows = _checked_records

    def latest_reports(source, as_of):
        # 先选最新三期再检查质量，不可用旧的合格报表顶替最新不合格报表。
        unique = {}
        for raw in source:
            period, pub = as_date(raw['report_period']), as_date(raw['publication_date'])
            if (period.month, period.day) not in GROUPS.values() or period > as_of or pub > as_of:
                raise ValueError('财报报告期或公告日期无效。')
            if pub < period:
                raise ValueError('财报公告早于报告期末。')
            row = dict(raw, report_period=period, publication_date=pub)
            old = unique.get(period)
            if old is None or pub > old['publication_date']:
                unique[period] = row
            elif pub == old['publication_date'] and builtins.any(number(row[k]) != number(old[k]) for k in ('net_profit', 'eps')):
                raise ValueError('相同报告期和公告日期存在冲突财务数据。')
        selected = {}
        for name, month_day in GROUPS.items():
            group = sorted((r for d, r in unique.items() if (d.month, d.day) == month_day), key=lambda r: r['report_period'])[-3:]
            if len(group) != 3:
                return None
            years = [r['report_period'].year for r in group]
            if years != list(range(years[0], years[0]+3)):
                return None  # 同比必须比较连续年份的同类报告。
            if builtins.any(number(r[k]) is None for r in group for k in ('net_profit', 'eps')):
                return None
            selected[name] = group
        return selected

    def evaluate(groups):
        if groups is None:
            return None
        evidence = {}
        annual_growth = None
        for name, group in groups.items():
            if builtins.any(number(group[-1][key]) <= 0 for key in ('net_profit', 'eps')):
                return None
            output = []
            for i, row in enumerate(group):
                record = {key: str(row[key]) for key in ('report_period', 'publication_date')}
                for field, target in (('net_profit', 'profit_growth'), ('eps', 'eps_growth')):
                    value = number(row[field])
                    record[field] = str(value)
                    record[target] = None
                    if i:
                        previous = number(group[i-1][field])
                        if previous == 0:
                            return None
                        # 先用原值交叉相乘判断，避免把 19.999...% 四舍五入成达标。
                        if (value-previous)*100 < builtins.abs(previous)*20:
                            return None
                        rate = (value-previous)/builtins.abs(previous)*100
                        record[target] = str(rounded(rate, 8))
                        if name == 'annual' and i == 2 and field == 'net_profit':
                            annual_growth = rate
                output.append(record)
            evidence[name] = output
        return annual_growth, evidence

    def select(all_reports, as_of):
        chosen, evaluated = [], 0
        for code in _progress_items(sorted(all_reports), '检查四类财报'):
            groups = latest_reports(all_reports[code], as_of)
            if groups is not None:
                evaluated += 1
                result = evaluate(groups)
                if result is not None:
                    chosen.append((code, result[0], result[1]))
        chosen.sort(key=lambda item: (-item[1], item[0]))
        return chosen, evaluated

    def financial_reports(codes, as_of):
        # 聚宽合并利润表：不使用 *_PARENT 母公司报表，也不把单季度 EPS 相加。
        finance = _apis.get('finance')
        if finance is None or query is None:
            raise RuntimeError('当前环境缺少 finance/query，请在聚宽研究环境运行。')
        table = finance.STK_INCOME_STATEMENT
        fields = ('id', 'code', 'pub_date', 'start_date', 'end_date', 'report_date',
                  'report_type', 'np_parent_company_owners', 'basic_eps')
        missing = [f for f in fields if not hasattr(table, f)]
        if missing:
            raise ValueError('聚宽累计利润表缺少字段：{}'.format(', '.join(missing)))
        result = {code: [] for code in codes}
        for offset in _progress_items(range(0, len(codes), BATCH_SIZE), '累计财报批次'):
            pending = set(codes[offset:offset+BATCH_SIZE])
            cursor = None
            while pending:
                _progress_note('倒序读取累计财报：本批待完成 {} 只'.format(len(pending)))
                q = query(*[getattr(table, f) for f in fields]).filter(
                    table.code.in_(sorted(pending)), table.pub_date <= as_of,
                    table.end_date <= as_of, table.report_date == table.end_date)
                if cursor is not None:
                    q = q.filter(or_(table.end_date < cursor[0],
                                     and_(table.end_date == cursor[0], table.id < cursor[1])))
                q = q.order_by(table.end_date.desc(), table.id.desc()).limit(PAGE_SIZE)
                page = rows(finance.run_query(q), fields, '累计利润表')
                if not page:
                    break
                keys = [(as_date(r['end_date']), int(r['id'])) for r in page]
                if len(page) > PAGE_SIZE or keys != sorted(set(keys), reverse=True) or (cursor is not None and keys[0] >= cursor):
                    raise ValueError('累计财报分页重复、乱序或超限。')
                for raw in page:
                    code = str(raw['code'])
                    period, pub = as_date(raw['end_date']), as_date(raw['pub_date'])
                    report_date, start = as_date(raw['report_date']), as_date(raw['start_date'])
                    if code not in pending or period > as_of or pub > as_of or report_date != period:
                        raise ValueError('累计财报返回越界股票、未来数据或比较期数据。')
                    # 必须从当年 1 月 1 日开始，排除半年/三季报告内的单季度栏。
                    if start != datetime.date(period.year, 1, 1) or (period.month, period.day) not in GROUPS.values():
                        continue
                    result[code].append(dict(report_period=period, publication_date=pub,
                        net_profit=raw['np_parent_company_owners'], eps=raw['basic_eps']))
                cursor = keys[-1]
                completed = set()
                for code in pending:
                    values = result[code]
                    keep = set()
                    enough = True
                    for month_day in GROUPS.values():
                        periods = sorted({r['report_period'] for r in values
                                          if (r['report_period'].month, r['report_period'].day) == month_day}, reverse=True)[:3]
                        keep.update(periods)
                        enough = enough and len(periods) == 3
                    result[code] = [r for r in values if r['report_period'] in keep]
                    # 游标越过最早所需报告期，才证明该期所有更正版本均已读取。
                    if enough and cursor[0] < builtins.min(keep):
                        completed.add(code)
                pending -= completed
                # A short server page is not proof of exhaustion; continue to an empty page.
        return result

    def calculate(as_of, valuation_day):
        _progress_note('读取 A 股股票池')
        frame = get_all_securities(types=['stock'], date=as_of)
        if frame is None or frame.empty or 'display_name' not in frame.columns:
            raise ValueError('股票池为空或缺少名称，保留原快照。')
        names = {str(code): str(row['display_name']) for code, row in frame.iterrows()}
        if len(names) != len(frame):
            raise ValueError('股票代码重复。')
        # get_all_securities(stock) 的平台 A 股范围，不额外剔除 ST、科创板、停牌或涨跌停。
        all_reports = financial_reports(sorted(names), as_of)
        if not builtins.any(all_reports.values()):
            raise ValueError('全部财报为空，保留原快照。')
        chosen, evaluated = select(all_reports, as_of)
        if evaluated == 0:
            raise ValueError('没有任何股票取得完整四类三期财报，检查数据权限及字段；保留原快照。')
        codes = [item[0] for item in chosen]
        caps, industries = {}, {}
        for offset in _progress_items(range(0, len(codes), BATCH_SIZE), '补充估值与行业'):
            batch = codes[offset:offset+BATCH_SIZE]
            fields = ('code', 'pe_ratio', 'pb_ratio', 'market_cap')
            q = query(*[getattr(valuation, f) for f in fields]).filter(valuation.code.in_(batch))
            values = rows(get_fundamentals(q, date=valuation_day), fields, '估值')
            for row in values:
                code = str(row['code'])
                if code not in batch or code in caps:
                    raise ValueError('估值包含重复或越界股票。')
                caps[code] = row
            if set(batch)-set(caps):
                raise ValueError('估值未完整返回，保留原快照。')
            result = get_industry(batch, date=as_of)
            if not isinstance(result, dict) or set(batch)-set(result):
                raise ValueError('行业未完整返回，保留原快照。')
            industries.update(result)
        records = []
        for rank, (code, growth, reports) in enumerate(chosen, 1):
            info = industries.get(code) or {}
            level = next((k for k in ('sw_l3', 'sw_l2', 'sw_l1') if info.get(k)), None)
            industry = info.get(level, {})
            cap = caps.get(code, {})
            records.append(dict(code=code, as_of_date=as_of, rank=rank, name=names[code],
                industry_code=industry.get('industry_code'), industry_name=industry.get('industry_name'), industry_level=level,
                pe_ttm=rounded(cap.get('pe_ratio')), pb=rounded(cap.get('pb_ratio')), market_cap=rounded(cap.get('market_cap')),
                annual_profit_growth=rounded(growth, 8), reports=reports))
        meta = dict(as_of_date=as_of, valuation_date=valuation_day, rule_version=RULE_VERSION,
                    universe_count=len(names), evaluated_count=evaluated, matched_count=len(records))
        return meta, records

    def validate_records(meta, records):
        if (len({r['code'] for r in records}) != len(records) or len(records) != meta['matched_count']
                or sorted(r['rank'] for r in records) != list(range(1, len(records)+1))
                or builtins.any(r['as_of_date'] != meta['as_of_date'] for r in records)):
            raise ValueError('股票重复、日期、数量或排名与快照不一致。')

    def save_latest(Session, meta, records):
        validate_records(meta, records)
        _progress_note('事务更新最新高增长名单')
        return _replace_latest_snapshot(Session, GrowthSnapshot, GrowthStock, meta, records,
                                        RESULT_COLUMNS, META_COLUMNS, 'as_of_date', json_fields=('reports',))

    # Archives deliberately do not share the website's latest-only tables.
    from sqlalchemy import Text
    from sqlalchemy.dialects.mysql import LONGTEXT
    ArchiveBase = declarative_base()

    class SelectionArchive(ArchiveBase):
        __tablename__ = 'high_growth_archive'
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
        from sqlalchemy import inspect
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
            if meta['as_of_date'] != day or meta['rule_version'] != RULE_VERSION:
                raise ValueError('档案日期或规则不一致。')
            validate_records(meta, records)
        with session_scope(Session) as session:
            saved = session.query(SelectionArchive).filter_by(
                as_of_date=day, source_kind=source_kind, rule_version=RULE_VERSION).with_for_update().first()
            # Successful archives are immutable, including empty valid selections.
            if saved is not None and saved.status == 'complete':
                return False
            if saved is None:
                saved = SelectionArchive(as_of_date=day, source_kind=source_kind, rule_version=RULE_VERSION)
                session.add(saved)
            saved.status = 'gap' if error is not None else 'complete'
            saved.captured_at = china_now()
            saved.snapshot_json = archive_json(meta) if error is None else None
            saved.stocks_json = archive_json(records) if error is None else None
            saved.provenance_json = archive_json(dict(provider='JoinQuant', schema_version=1,
                rule_version=RULE_VERSION, source_kind=source_kind, original_point_in_time=False,
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
                    SelectionArchive.source_kind == source_kind, SelectionArchive.rule_version == RULE_VERSION).all()
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
                    meta, records = calculate(day, day)
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
                    SelectionArchive.source_kind == source_kind, SelectionArchive.rule_version == RULE_VERSION,
                    SelectionArchive.status == 'gap').count()
        return summary

    def restore_observed(day):
        """Restore an actual captured run, never silently promote a historical reconstruction."""
        day = as_date(day)
        with database_scope(ArchiveBase, _config) as Session:
            validate_archive_schema(Session)
            with session_scope(Session) as session:
                saved = session.query(SelectionArchive).filter_by(as_of_date=day,
                    source_kind='observed', rule_version=RULE_VERSION, status='complete').first()
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
        meta = decode(meta, GrowthSnapshot.__table__)
        records = [decode(record, GrowthStock.__table__) for record in records]
        with database_scope(Base, _config) as Session:
            return save_latest(Session, meta, records)

    def run(end_date=END_DATE, mode='latest', **options):
        if mode == 'history':
            return archive_history(end_date=end_date, **options)
        if mode == 'restore':
            return restore_observed(**options)
        if mode != 'latest' or options:
            raise ValueError('mode 仅支持 latest、history、restore；latest 不接受额外参数。')
        now = china_now()
        as_of = as_date(end_date) if end_date is not None else now.date()
        if as_of > now.date():
            raise ValueError('结束日期不能晚于今天。')
        valuation_end = _closed_day(as_of, now)
        days = get_trade_days(end_date=valuation_end, count=1)
        if days is None or len(days) != 1 or as_date(days[0]) > valuation_end:
            raise ValueError('交易日历无效。')
        # 全部采集成功后再进入写入流程，任何源接口失败都不会清空旧名单。
        meta, records = calculate(as_of, as_date(days[0]))
        with database_scope(Base, _config) as Session:
            if not save_latest(Session, meta, records):
                _progress_note('名单与指标未变化，未重复写入')
        with database_scope(ArchiveBase, _config) as Session:
            save_archive(Session, meta['as_of_date'], 'observed', meta, records)
        return pd.DataFrame(records, columns=RESULT_COLUMNS)
    return SimpleNamespace(**locals())

_API_NAMES = ('get_all_trade_days', 'get_all_securities', 'get_trade_days', 'get_fundamentals', 'get_industry', 'query', 'valuation', 'finance', 'jy')


def run(database_config=None, **options):
    apis = {key: globals().get(key, getattr(jq, key, getattr(builtins, key, None))) for key in _API_NAMES}
    with _progress_scope('高增长', 1, 1):
        return _growth_strategy(apis, DB_CONFIG if database_config is None else dict(database_config)).run(**options)


if __name__ == '__main__':
    try:
        growth_df = run()
        print('高增长：当前入选 {} 只，数据库仅保留最新名单。'.format(len(growth_df)))
        if len(growth_df):
            print(growth_df[['rank', 'code', 'name', 'industry_name', 'annual_profit_growth', 'pe_ttm', 'pb']].to_string(index=False))
    except Exception as exc:
        raise RuntimeError(_safe_error(exc, DB_CONFIG)) from None
