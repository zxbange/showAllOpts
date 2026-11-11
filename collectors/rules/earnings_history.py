# BEGIN SHARED EARNINGS HISTORY
from contextlib import contextmanager as _earnings_history_contextmanager


@_earnings_history_contextmanager
def _history_session(Session):
    # JoinQuant notebooks can still use SQLAlchemy 1.3 Sessions without __enter__.
    session = Session()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

def _history_periods(first, last):
    """Closed calendar quarters, inclusive; never silently round a configured date."""
    first, last = as_date(first), as_date(last)
    if latest_period(first + dt.timedelta(days=1)) != first:
        raise ValueError('start_period 必须是报告季末日期。')
    while first <= last:
        yield first
        month = first.month + 3
        year = first.year + (month > 12)
        month = (month - 1) % 12 + 1
        first = (dt.date(year + (month == 12), month % 12 + 1, 1)
                 - dt.timedelta(days=1))


def _history_checkpoint_table():
    # Independent checkpoint: an existing daily run is NOT evidence that history
    # was replayed. A crash after snapshot commit and before this commit safely
    # repeats append-only work instead of claiming an incomplete quarter is done.
    from sqlalchemy import MetaData, Table
    return Table('earnings_history_checkpoint', MetaData(),
        Column('report_period', Date, primary_key=True),
        Column('as_of_date', Date, nullable=False),
        Column('method', String(40), nullable=False),
        Column('updated_at', DateTime, nullable=False),
        mysql_engine='InnoDB', mysql_charset='utf8mb4')


def _history_source_floor(table, field, fetch):
    frame = fetch(query(field).filter(field != None).order_by(field).limit(1))
    rows = checked_rows(frame, [field.key], '历史财务覆盖探测')
    if not rows:
        raise ValueError('财务源未返回可验证的起点；权限或覆盖未知，不能记为零命中。')
    return as_date(rows[0][field.key])


def _history_formal_events(period, as_of):
    """Current visible quarterly values, never represented as unrevised PIT."""
    events, cursor = [], ''
    fields = ['code', 'publication_date', 'report_period', 'net_profit',
              'profit_yoy', 'indicator_publication_date', 'indicator_period']
    while True:
        q = query(income.code, income.pubDate.label('publication_date'),
            income.statDate.label('report_period'),
            income.np_parent_company_owners.label('net_profit'),
            indicator.inc_net_profit_to_shareholders_year_on_year.label('profit_yoy'),
            indicator.pubDate.label('indicator_publication_date'),
            indicator.statDate.label('indicator_period')).filter(
                income.code > cursor, income.statDate == period, indicator.statDate == period,
                income.pubDate <= as_of, indicator.pubDate <= as_of).order_by(income.code).limit(PAGE_SIZE)
        rows = checked_rows(get_fundamentals(q, statDate='{}q{}'.format(period.year, period.month // 3)),
                            fields, '历史季度正式财报')
        if not rows:
            return events
        keys = [row['code'] for row in rows]
        if len(keys) > PAGE_SIZE or builtins.any(not isinstance(code, str) for code in keys) or keys != sorted(set(keys)) or keys[0] <= cursor:
            raise ValueError('历史正式财报股票分页重复、乱序、超限或未前进。')
        cursor = keys[-1]
        for row in rows:
            if as_date(row['report_period']) != period or as_date(row['indicator_period']) != period:
                raise ValueError('历史财报与指标的报告期不一致。')
            pub = builtins.max(as_date(row['publication_date']), as_date(row['indicator_publication_date']))
            if pub > as_of:
                raise ValueError('历史财报接口返回截止日期后公告。')
            events.append(event(row['code'], 'formal', pub, period, row['net_profit'], row['profit_yoy'], row))


def _history_events(period, as_of, pool_cache):
    """Date each universe to actual disclosures, including subsequently delisted stocks."""
    t = jy.LC_FSPerformedLetters
    express = list(paged(t, [t.EndDate == period, t.InfoPublDate < as_of + dt.timedelta(days=1)], jy.run_query))
    t = finance.STK_FIN_FORCAST
    forecasts = list(paged(t, [t.end_date == period, t.pub_date <= as_of], finance.run_query))
    formal = _history_formal_events(period, as_of)
    if builtins.any(as_date(row['EndDate']) != period for row in express) or builtins.any(as_date(row['end_date']) != period for row in forecasts):
        raise ValueError('历史公告接口返回其他报告期。')
    # All-empty is UNKNOWN, not evidence that a complete period had zero matches.
    if not express and not forecasts and not formal:
        return {}, [], False
    dates = {as_date(r['InfoPublDate']) for r in express} | {as_date(r['pub_date']) for r in forecasts}
    dates.update(as_date(e['publication_date']) for e in formal)
    if builtins.any(day > as_of for day in dates):
        raise ValueError('公告接口返回截止日期后记录。')
    names, membership = {}, {}
    for day in sorted(dates):
        if day not in pool_cache:
            frame = get_all_securities(types=['stock'], date=day)
            if frame is None or frame.empty or not {'display_name', 'start_date', 'end_date'}.issubset(frame.columns):
                raise ValueError('历史股票池/上市退市日期不可验证；保留已成功季度。')
            pool = {}
            for code, row in frame.iterrows():
                if not as_date(row['start_date']) <= day <= as_date(row['end_date']):
                    raise ValueError('历史股票池上市区间与快照日期冲突。')
                pool[str(code)] = str(row['display_name'])
            pool_cache[day] = pool
            if len(pool_cache) > 32:
                del pool_cache[next(iter(pool_cache))]
        membership[day] = pool_cache[day]
        names.update(pool_cache[day])
    # Mapping is based on historical dated codes, never today's surviving list.
    code_map = {code[:6]: code for code in names}
    companies = {}
    t = jy.SecuMain
    for row in paged(t, [t.SecuCategory == 1], jy.run_query):
        code = code_map.get(str(row['SecuCode']))
        if code:
            companies.setdefault(row['CompanyCode'], set()).add(code)
    if express and not companies:
        raise ValueError('历史 A 股公司代码映射不可验证。')
    events = list(formal)
    for row in express:
        for code in sorted(companies.get(row['CompanyCode'], [])):
            events.append(event(code, 'express', row['InfoPublDate'], period,
                row['NPParentCompanyOwners'], row['NPCOwnersYOY'], row))
    for row in forecasts:
        events.append(event(row['code'], 'forecast', row['pub_date'], period, None,
            row['profit_ratio_min'], row, row.get('profit_ratio_max')))
    verified = []
    for e in events:
        if e['code'] in membership[as_date(e['publication_date'])]:
            verified.append(e)
        elif e['kind'] == 'formal':
            # Could be a delisted issuer filing after exit. There is no listed
            # announcement-day trade to screen; do not substitute today's pool.
            print('{} {}：披露日不在当日上市股票池，跳过。'.format(period, e['code']))
    return names, verified, True


def _history_write_checkpoint(Session, checkpoint, period, as_of, method):
    with _history_session(Session) as session:
        if method == 'quarter-visible-v2':
            _lock_earnings_version(session, period)
        condition = checkpoint.c.report_period == period
        prior = session.execute(checkpoint.select().where(condition)).first()
        if prior is None or prior.as_of_date <= as_of:
            session.execute(checkpoint.delete().where(condition))
            session.execute(checkpoint.insert().values(report_period=period, as_of_date=as_of,
                method=method, updated_at=china_now()))
        session.commit()


def collect_history(Session, requested, history_mode, start_period, max_periods):
    """Bound attempted quarters; successful snapshots are always append-only."""
    if history_mode not in ('gaps', 'full'):
        raise ValueError('history_mode 只支持 latest / gaps / full。')
    if max_periods is not None and (isinstance(max_periods, bool) or not isinstance(max_periods, int) or max_periods < 1):
        raise ValueError('max_periods 必须为正整数或 None。')
    now = china_now()
    requested = as_date(requested) if requested is not None else now.date()
    if requested > now.date():
        raise ValueError('结束日期不能晚于今天。')
    raw_days = get_all_trade_days()
    if raw_days is None or len(raw_days) == 0:
        raise ValueError('无法验证聚宽完整历史交易日历。')
    days = [as_date(d) for d in raw_days if as_date(d) <= _closed_day(requested, now)]
    if not days or days != sorted(set(days)):
        raise ValueError('历史交易日历为空、重复或乱序。')
    as_of, last = days[-1], latest_period(requested)
    m = ((days[0].month - 1) // 3 + 1) * 3
    first = dt.date(days[0].year + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)
    # Permission/schema failures abort before any success marker. These earliest
    # rows describe current readable coverage, not a promised historical start.
    express_floor = _history_source_floor(jy.LC_FSPerformedLetters, jy.LC_FSPerformedLetters.EndDate, jy.run_query)
    forecast_floor = _history_source_floor(finance.STK_FIN_FORCAST, finance.STK_FIN_FORCAST.end_date, finance.run_query)
    print('可见快报/预告最早报告期：{} / {}；正式财报逐季验证。'.format(express_floor, forecast_floor))
    if start_period is not None:
        first = as_date(start_period)
    if first > last:
        raise ValueError('起始报告期晚于最新已结束报告期。')
    checkpoint = _history_checkpoint_table()
    checkpoint.create(Session.kw['bind'], checkfirst=True)
    _prepare_earnings_version(Session)
    attempted, output, pool_cache = 0, [], {}
    for period in _history_periods(first, last):
        with _history_session(Session) as session:
            done = session.execute(checkpoint.select().where(checkpoint.c.report_period == period)).first()
        if history_mode == 'gaps' and done and done.as_of_date >= as_of and done.method in ('quarter-visible-v2', 'unknown-empty-v2'):
            if done.method == 'unknown-empty-v2':
                print('{}：上次覆盖仍为未知；full 可复核，不视为成功/零命中。'.format(period))
            continue
        if max_periods is not None and attempted >= max_periods:
            print('达到本次季度尝试上限；同一 end_date 再运行 gaps 继续。')
            break
        attempted += 1
        names, events, verified = _history_events(period, as_of, pool_cache)
        if not verified:
            _history_write_checkpoint(Session, checkpoint, period, as_of, 'unknown-empty-v2')
            print('{}：覆盖未知，已记录探测状态；未写季度结果。'.format(period))
            continue
        for e in events:
            e['source_kind'] = 'joinquant_current_visible_history'
            e['reconstruction_as_of'] = str(now.date())
            e['historical_cutoff'] = str(as_of)
        with _history_session(Session) as session:
            existing = {r[0] for r in session.query(EarningsStock.code).filter(EarningsStock.report_period == period).all()}
        codes = sorted(names)
        pending = [code for code in codes if code not in existing]
        pending_events = [e for e in events if e['code'] not in existing]
        records = screen(pending, names, pending_events, period, as_of, historical=True) if pending_events else []
        fresh = save_results(Session, period, as_of, len(codes), records, skip_existing=True)
        _migrate_earnings_versions(Session)
        # Separate durable checkpoint: crash after snapshot commit only repeats
        # safe append work. No failed/incomplete quarter is published as done.
        _history_write_checkpoint(Session, checkpoint, period, as_of, 'quarter-visible-v2')
        output.extend(fresh)
        print('历史季度 {} 完成：新增 {} 只。'.format(period, len(fresh)))
    return pd.DataFrame(output)
# END SHARED EARNINGS HISTORY
