# BEGIN SHARED EARNINGS FOLLOWUP
import builtins as _ef_b
import datetime as _ef_dt
import math as _ef_math
from decimal import Decimal as _EFDecimal
from sqlalchemy import Column, Date, DateTime, MetaData, Numeric, String, Table
from sqlalchemy import func as _ef_func


def _earnings_followup_table():
    return Table('earnings_gap_performance', MetaData(),
        Column('report_period', Date, primary_key=True), Column('code', String(16), primary_key=True),
        Column('signal_date', Date, nullable=False), Column('as_of_date', Date, nullable=False),
        Column('latest_date', Date, nullable=False),
        Column('signal_close', Numeric(24, 8), nullable=False),
        Column('latest_close', Numeric(24, 8), nullable=False),
        Column('since_signal_pct', Numeric(24, 6), nullable=False),
        Column('updated_at', DateTime, nullable=False),
        mysql_engine='InnoDB', mysql_charset='utf8mb4')


def _refresh_earnings_followup(Session, Stock, as_of, get_price, progress=None):
    """仅跟踪已入选股票；同股票多季度共用一次行情，同日重复运行跳过。"""
    table = _earnings_followup_table()
    table.create(Session.kw['bind'], checkfirst=True)
    session = Session()
    try:
        signals = session.query(Stock.report_period, Stock.code, Stock.signal_date).filter(Stock.signal_date <= as_of).all()
        previous = {(r.report_period, r.code): (r.signal_date, r.as_of_date)
                    for r in session.execute(table.select())}
    finally:
        session.close()
    grouped = {}
    for period, code, signal_day in signals:
        old = previous.get((period, code))
        if old and old[0] == signal_day and old[1] >= as_of:
            continue
        grouped.setdefault(code, []).append((period, signal_day))
    records = []
    for index, (code, entries) in enumerate(sorted(grouped.items()), 1):
        if progress:
            progress('更新已入选股票收盘表现 {}/{} {}'.format(index, len(grouped), code))
        first = _ef_b.min(day for _, day in entries)
        # 两个收盘价必须来自同一次前复权查询，不能拼接不同复权基准的旧快照。
        frame = get_price(code, start_date=first, end_date=as_of, frequency='daily',
                          fields=['close', 'paused'], skip_paused=False, fq='pre')
        if frame is None or not {'close', 'paused'}.issubset(frame.columns):
            raise ValueError('{} 收盘跟踪行情缺失。'.format(code))
        closes, seen = {}, set()
        for raw_day, row in frame.iterrows():
            day = raw_day.date() if isinstance(raw_day, _ef_dt.datetime) else _ef_dt.datetime.strptime(str(raw_day)[:10], '%Y-%m-%d').date()
            if not first <= day <= as_of or day in seen:
                raise ValueError('{} 收盘跟踪日期越界或重复。'.format(code))
            seen.add(day)
            paused = _ef_b.float(row['paused'])
            if paused not in (0, 1):
                raise ValueError('{} 停牌状态缺失。'.format(code))
            if paused:
                continue
            close = _ef_b.float(row['close'])
            if not _ef_math.isfinite(close) or not 0 < close < 10 ** 16:
                raise ValueError('{} 收盘价无效。'.format(code))
            closes[day] = _EFDecimal(str(close))
        if not closes or _ef_b.any(day not in closes for _, day in entries):
            raise ValueError('{} 断层日收盘价缺失，保留原跟踪数据。'.format(code))
        latest = _ef_b.max(closes)
        for period, signal_day in entries:
            change = (closes[latest] / closes[signal_day] - 1) * 100
            if _ef_b.abs(change) >= 10 ** 18:
                raise ValueError('{} 收盘涨幅超出字段范围。'.format(code))
            records.append(dict(report_period=period, code=code, signal_date=signal_day,
                as_of_date=as_of, latest_date=latest, signal_close=closes[signal_day].quantize(_EFDecimal('1e-8'), rounding='ROUND_HALF_UP'),
                latest_close=closes[latest].quantize(_EFDecimal('1e-8'), rounding='ROUND_HALF_UP'),
                since_signal_pct=change.quantize(_EFDecimal('1e-6'), rounding='ROUND_HALF_UP'),
                updated_at=_ef_dt.datetime.utcnow()))
    # 网络读取结束后再写入，避免查询行情时长时间占用数据库事务。
    session = Session()
    try:
        for period in sorted({r['report_period'] for r in records}):
            _lock_earnings_version(session, period)
            current = dict(session.query(Stock.code, Stock.signal_date).filter(Stock.report_period == period).all())
            for row in (r for r in records if r['report_period'] == period):
                if current.get(row['code']) != row['signal_date']:
                    continue
                condition = (table.c.report_period == period) & (table.c.code == row['code'])
                old = session.execute(table.select().where(condition)).first()
                if old and old.signal_date == row['signal_date'] and old.as_of_date >= as_of:
                    continue
                session.execute(table.delete().where(condition))
                session.execute(table.insert().values(**row))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return len(records)


def _prune_earnings_history(Session, Stock, Run, as_of):
    """按报告期保留滚动五年，连同季度版本、迁移标记和跟踪价格一起清理。"""
    performance = _earnings_followup_table()
    performance.create(Session.kw['bind'], checkfirst=True)
    session = Session()
    try:
        # 补跑较早日期不能让保留边界倒退。
        reference = _ef_b.max(day for day in (as_of, session.query(_ef_func.max(Run.as_of_date)).scalar(),
                          session.execute(_ef_func.max(performance.c.as_of_date).select()).scalar()) if day is not None)
        try:
            cutoff = reference.replace(year=reference.year - 5)
        except ValueError:
            cutoff = reference.replace(year=reference.year - 5, day=28)
        periods = [row[0] for row in session.query(Run.report_period).filter(Run.report_period < cutoff).all()]
        for period in sorted(periods):
            _lock_earnings_version(session, period)
            session.query(Stock).filter(Stock.report_period == period).delete(synchronize_session=False)
            session.query(Run).filter(Run.report_period == period).delete(synchronize_session=False)
            for table in (_earnings_version_table(), _earnings_migration_table()):
                session.execute(table.delete().where(table.c.report_period == period))
        # 清除被季度重建移除的股票跟踪记录，包括没有剩余季度头的历史数据。
        live = session.query(Stock.code).filter(Stock.report_period == performance.c.report_period,
                                              Stock.code == performance.c.code).exists()
        session.execute(performance.delete().where(~live))
        session.commit()
        return len(periods)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
# END SHARED EARNINGS FOLLOWUP
