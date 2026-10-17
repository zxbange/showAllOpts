# -*- coding: utf-8 -*-
"""聚宽研究环境：粘贴运行或 %run -i 净利润断层_数据库版.py。无交易操作。
统一最新已结束季度；重查该期公告，原子替换该期结果，保留其他季度。
规则、数据口径、限制详见 docs/earnings-gap.md。
"""
from jqdata import *
from jqdata import jy, finance
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
    host = 'your_mysql_host'
    port = 3306
    dbname = 'your_database'
    dbuser = 'your_database_user'
    dbpassword = 'your_password'

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


def save_results(Session, period, as_of, universe_count, records):
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
        # 清空与重建只针对当前季度，并在同一个事务提交，合法零命中也有元数据。
        session.query(EarningsStock).filter(EarningsStock.report_period == period).delete(synchronize_session=False)
        session.query(EarningsRun).filter(EarningsRun.report_period == period).delete(synchronize_session=False)
        session.add_all([EarningsStock(**r) for r in db_records])
        session.add(EarningsRun(report_period=period, as_of_date=as_of, updated_at=china_now(),
                               universe_count=universe_count, matched_count=len(records), rule_version=RULE_VERSION))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run(end_date=END_DATE):
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
    events = fetch_events(codes, period, as_of)
    records = screen(codes, names, events, period, as_of)
    engine = MysqlEarnings.connect_to_mysql()
    try:
        # 显式指定 InnoDB，确保旧数据库默认引擎不会破坏整批事务。
        for table in Base.metadata.tables.values():
            table.dialect_options['mysql']['engine'] = 'InnoDB'
            table.dialect_options['mysql']['charset'] = 'utf8mb4'
        Base.metadata.create_all(engine)
        save_results(sessionmaker(bind=engine), period, as_of, len(codes), records)
    finally:
        engine.dispose()
    print('完成：{} 只股票已入库；其他季度数据保留。'.format(len(records)))
    return pd.DataFrame(records)


if __name__ == '__main__':
    try:
        earnings_df = run()
    except Exception as exc:
        print('采集未完成，旧结果保留。异常类型：{}。'.format(type(exc).__name__))
        # 数据源校验错误可读；数据库驱动错误不暴露凭据、SQL 参数。
        if isinstance(exc, (ValueError, KeyError, AttributeError)):
            raise
        raise RuntimeError('采集失败，请核对聚宽权限、接口字段和私有数据库配置。') from None
