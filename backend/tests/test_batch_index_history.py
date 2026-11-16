"""Run the two embedded collectors against real SQLite tables and synthetic platform APIs."""
import datetime as dt
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import event, func, select, text

from test_collector_batch import CONFIG, load
from app.services import growth_dividend_import as growth

ROOT = Path(__file__).resolve().parents[2]
DAYS = [dt.date(2026, 1, day) for day in (5, 6, 7)]


@pytest.fixture
def collectors(monkeypatch, engine):
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    monkeypatch.setitem(sys.modules, 'mysql_connector', SimpleNamespace(
        get_database_config=lambda: dict(CONFIG), get_database_options=lambda: {},
        mysql_drivername=lambda _: 'mysql+pymysql', engine_options=lambda _: {}))
    batch = load(ROOT / 'collectors/策略批量采集_数据库版.py', 'batch_index_integration')
    connections, calls = [], []
    def connect(config):
        connections.append(dict(config))
        assert config == CONFIG
        return engine
    monkeypatch.setattr(batch, 'connect_database', connect)
    monkeypatch.setattr(engine, 'dispose', lambda: None)
    codes = ['{:06d}.XSHG'.format(600000+i) for i in range(300)]
    failure = {}
    def fundamentals(query, date):
        calls.append(('pe', date, len(query)))
        if failure.get('pe') == date:
            raise RuntimeError('synthetic PE provider failure')
        frame = pd.DataFrame({'code': codes, 'day': [date]*300,
                             'pe_ratio': [None, -2] + [10]*298,
                             'pe_ratio_lyr': [20]*300, 'market_cap': [100]*300})
        return frame[frame['code'].isin(query)].copy()
    def prices(code, start_date, end_date, **kwargs):
        assert kwargs['fq'] is None and kwargs['fields'] == ['close']
        calls.append(('price', code, start_date, end_date))
        dates = [day for day in DAYS if start_date <= day <= end_date and failure.get(code) != day]
        return pd.DataFrame({'close': [4000]*len(dates)}, index=dates)
    apis = dict(get_all_trade_days=lambda: DAYS,
                get_trade_days=lambda end_date, start_date=DAYS[0]: [d for d in DAYS if start_date <= d <= end_date],
                get_index_stocks=lambda index, date: codes,
                valuation=SimpleNamespace(code=SimpleNamespace(in_=lambda codes: codes), day=object(), pe_ratio=object(),
                                          pe_ratio_lyr=object(), market_cap=object()),
                query=lambda *args: SimpleNamespace(filter=lambda codes: codes),
                get_fundamentals=fundamentals, get_price=prices)
    options = {name: dict(start_date=str(DAYS[0]), end_date=str(DAYS[-1])) for name in ('估值标尺', '创红比')}
    options['估值标尺']['indices'] = ['000300.XSHG']
    return SimpleNamespace(batch=batch, apis=apis, options=options, calls=calls, failure=failure,
                           connections=connections, engine=engine)


def run(c, names=None):
    return c.batch.run_all(names or ['估值标尺', '创红比'], options=c.options, db_config=CONFIG,
                           api_namespace=c.apis, preview_rows=0, raise_on_failure=False)


def test_both_collectors_commit_existing_schema_and_noop_without_price_reads(collectors):
    c = collectors
    first = run(c)
    assert [(r['strategy'], r['status'], r['rows']) for r in first] == [('估值标尺', 'SUCCESS', 3), ('创红比', 'SUCCESS', 9)]
    assert c.connections == [CONFIG, CONFIG]
    with c.engine.connect() as conn:
        assert conn.scalar(text('SELECT COUNT(*) FROM valuation_ruler_member_v2')) == 3
        assert conn.scalar(select(func.count()).select_from(growth.PRICES)) == 9
        import json
        stats = json.loads(conn.scalar(text('SELECT statistics FROM valuation_ruler_daily_v2 LIMIT 1')))
        assert stats['ttm']['eligible'] == 298
        assert stats['ttm']['loss'] == 1
    c.calls.clear()
    second = run(c)
    assert [(r['status'], r['rows']) for r in second] == [('SUCCESS', 0), ('SUCCESS', 0)]
    assert not c.calls
    # V2 lives in separate tables: the legacy importer cannot mix methodologies.
    days, points = growth.read_source(c.engine, DAYS[-1], lambda: None)
    assert days == DAYS and len(points) == 9


def test_valuation_limit_reports_pending_and_next_run_continues(collectors):
    c = collectors
    c.options['估值标尺']['max_days'] = 1
    result = run(c, ['估值标尺'])[0]
    assert (result['status'], result['rows'], result['remaining']) == ('PENDING', 1, 2)
    c.options['估值标尺']['max_days'] = None
    c.calls.clear()
    result = run(c, ['估值标尺'])[0]
    assert (result['status'], result['rows'], result['skipped']) == ('SUCCESS', 2, 1)
    assert [(call[1], call[2]) for call in c.calls if call[0] == 'pe'] == [(day, size) for day in DAYS[1:] for size in (200, 100)]


def test_failed_valuation_keeps_committed_day_and_batch_continues(collectors):
    c = collectors
    c.failure['pe'] = DAYS[1]
    result = run(c)
    assert [r['status'] for r in result] == ['FAILED', 'SUCCESS']
    with c.engine.connect() as conn:
        assert str(conn.scalar(text('SELECT trade_date FROM valuation_ruler_daily_v2'))) == str(DAYS[0])
    c.failure.clear()
    c.calls.clear()
    assert run(c, ['估值标尺'])[0]['rows'] == 2
    assert [(call[1], call[2]) for call in c.calls if call[0] == 'pe'] == [(day, size) for day in DAYS[1:] for size in (200, 100)]


def test_growth_mid_history_gap_is_partial_then_filled_without_rereading_good_dates(collectors):
    c = collectors
    c.failure['399006.XSHE'] = DAYS[1]
    result = run(c, ['创红比'])[0]
    assert (result['status'], result['rows'], result['gaps']) == ('PARTIAL', 8, 1)
    c.failure.clear()
    c.calls.clear()
    result = run(c, ['创红比'])[0]
    assert (result['status'], result['rows'], result['gaps']) == ('SUCCESS', 1, 0)
    assert c.calls == [('price', '399006.XSHE', DAYS[1], DAYS[1])]


def growth_runner(c, entry, **overrides):
    options = dict(start_date=DAYS[0], end_date=DAYS[-1], preview=False,
                   api_namespace=c.apis, engine_factory=lambda: c.engine)
    options.update(overrides)
    if entry == 'standalone':
        module = load(ROOT / 'collectors/创红比历史补采_聚宽研究版.py', 'growth_recovery_test')
        return lambda: module.run(**options)
    return lambda: c.batch._growth_dividend_strategy(c.apis, CONFIG).collect(**options)


def prepare_growth_prices(c):
    days = list(pd.bdate_range('2018-01-02', periods=502).date)
    c.apis['get_trade_days'] = lambda **kwargs: days
    def prices(code, start_date, end_date, **kwargs):
        c.calls.append((code, start_date, end_date))
        selected = [day for day in days if start_date <= day <= end_date]
        return pd.DataFrame({'close': [1234.5]*len(selected)}, index=selected)
    c.apis['get_price'] = prices
    return days


@pytest.mark.parametrize('entry', ['standalone', 'batch'])
def test_growth_prices_bulk_commit_then_repeat_without_quotes_or_writes(collectors, entry):
    c = collectors
    days = prepare_growth_prices(c)
    statements, stages = [], []
    def record(conn, cursor, statement, parameters, context, executemany):
        if 'growth_dividend_index_history' in statement and statement.startswith(('SELECT', 'INSERT', 'UPDATE', 'DELETE')):
            statements.append(statement)
    event.listen(c.engine, 'before_cursor_execute', record)
    collect = growth_runner(c, entry, start_date=days[0], end_date=days[-1], progress=stages.append)
    try:
        assert collect()['added'] == 1506
        assert len(c.calls) == 9
        assert len(statements) == 19  # One saved-date list, then one SELECT + bulk INSERT per window.
        assert any('本批新增 250 条，跳过已有 0 条' in message for message in stages)
        c.calls.clear()
        statements.clear()
        result = collect()
        assert result['added'] == 0 and result['skipped'] == 1506
        assert not c.calls
        assert len(statements) == 1 and statements[0].startswith('SELECT')
    finally:
        event.remove(c.engine, 'before_cursor_execute', record)


@pytest.mark.parametrize('entry', ['standalone', 'batch'])
def test_growth_bulk_write_preserves_rows_committed_during_source_read(collectors, entry):
    c = collectors
    original, inserted = c.apis['get_price'], []
    stamp = dt.datetime(2020, 1, 1)
    def prices(code, **kwargs):
        if not inserted:
            with c.engine.begin() as conn:
                conn.execute(growth.PRICES.insert().values(index_code=code, trade_date=DAYS[0],
                             close=123, source=growth.SOURCE, updated_at=stamp))
            inserted.append(code)
        return original(code, **kwargs)
    c.apis['get_price'] = prices
    result = growth_runner(c, entry)()
    assert result['added'] == 8 and result['skipped'] == 1
    with c.engine.connect() as conn:
        row = conn.execute(select(growth.PRICES.c.close, growth.PRICES.c.updated_at).where(
            (growth.PRICES.c.index_code == inserted[0]) & (growth.PRICES.c.trade_date == DAYS[0]))).one()
        assert tuple(row) == (123, stamp)


@pytest.mark.parametrize('entry', ['standalone', 'batch'])
@pytest.mark.parametrize('failure', ['write', 'cancel'])
def test_growth_price_batch_rollback_keeps_prior_window_and_resumes(collectors, entry, failure):
    c = collectors
    days = prepare_growth_prices(c)
    growth.metadata.create_all(c.engine)
    if failure == 'write':
        with c.engine.begin() as conn:
            conn.exec_driver_sql("CREATE TRIGGER fail_price BEFORE INSERT ON growth_dividend_index_history "
                                "WHEN NEW.trade_date = '{}' BEGIN SELECT RAISE(ABORT, 'test'); END".format(days[300]))
    inserted = []
    def after_write(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO growth_dividend_index_history'):
            inserted.append(True)
    def cancel():
        if failure == 'cancel' and len(inserted) == 2:
            raise KeyboardInterrupt()
    event.listen(c.engine, 'after_cursor_execute', after_write)
    try:
        with pytest.raises(KeyboardInterrupt if failure == 'cancel' else RuntimeError):
            growth_runner(c, entry, start_date=days[0], end_date=days[-1], check_cancel=cancel)()
        with c.engine.begin() as conn:
            assert conn.scalar(select(func.count()).select_from(growth.PRICES)) == 250
            if failure == 'write':
                conn.exec_driver_sql('DROP TRIGGER fail_price')
    finally:
        event.remove(c.engine, 'after_cursor_execute', after_write)
    c.calls.clear()
    result = growth_runner(c, entry, start_date=days[0], end_date=days[-1])()
    assert result['added'] == 1256 and result['skipped'] == 250
    assert c.calls[0] == ('399006.XSHE', days[250], days[499])


@pytest.mark.parametrize('entry', ['standalone', 'batch'])
def test_growth_failure_preserves_commits_and_reports_safe_original_error(collectors, entry):
    c = collectors
    original, calls = c.apis['get_price'], []
    def failing_price(code, **kwargs):
        calls.append(code)
        if len(calls) == 2:
            raise ValueError('SECRET SQL user:password@private-host')
        return original(code, **kwargs)
    c.apis['get_price'] = failing_price
    with pytest.raises(RuntimeError) as error:
        growth_runner(c, entry)()
    message = str(error.value)
    assert '异常=ValueError' in message and 'failing_price:' in message
    assert calls[-1] in message and str(DAYS[0]) in message and str(DAYS[-1]) in message
    assert 'SECRET' not in message and 'private-host' not in message
    with c.engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(growth.PRICES)) == 3
        assert conn.scalar(select(func.count()).select_from(growth.CALENDAR)) == 3
    c.apis['get_price'] = original
    c.calls.clear()
    assert growth_runner(c, entry)()['added'] == 6
    assert calls[0] not in [call[1] for call in c.calls]
    assert growth_runner(c, entry)()['added'] == 0


@pytest.mark.parametrize('entry', ['standalone', 'batch'])
@pytest.mark.parametrize('errno,transient,attempts', [(24, True, 2), (24, False, 2), (13, False, 1)])
def test_growth_price_retry_is_bounded_and_preserves_query(collectors, entry, errno, transient, attempts):
    c = collectors
    original, calls = c.apis['get_price'], []
    def price(code, **kwargs):
        calls.append((code, kwargs))
        if not transient or len(calls) == 1:
            raise OSError(errno, 'SECRET source path')
        return original(code, **kwargs)
    c.apis['get_price'] = price
    if transient:
        assert growth_runner(c, entry)()['added'] == 9
        assert calls[0] == calls[1] and len(calls) == 4
    else:
        with pytest.raises(RuntimeError) as error:
            growth_runner(c, entry)()
        assert len(calls) == attempts and all(call == calls[0] for call in calls)
        assert '系统错误码={}'.format(errno) in str(error.value)
        assert 'SECRET' not in str(error.value)
        with c.engine.connect() as conn:
            assert conn.scalar(select(func.count()).select_from(growth.PRICES)) == 0


def test_growth_unknown_prefix_is_visible_but_not_queried_each_daily_run(collectors):
    c = collectors
    c.failure['399006.XSHE'] = DAYS[0]
    first = run(c, ['创红比'])[0]
    assert first['rows'] == 8
    assert first['coverage']['399006.XSHE']['observed_first_day'] == str(DAYS[1])
    assert first['coverage']['399006.XSHE']['missing'] == 1
    c.calls.clear()
    assert run(c, ['创红比'])[0]['rows'] == 0
    assert not c.calls
    c.failure.clear()
    c.options['创红比']['retry_earlier_missing'] = True
    assert run(c, ['创红比'])[0]['rows'] == 1
    assert c.calls == [('price', '399006.XSHE', DAYS[0], DAYS[0])]


def test_empty_index_is_partial_and_retried_on_next_run(collectors):
    c = collectors
    original = c.apis['get_price']
    def price(code, **kwargs):
        return pd.DataFrame(columns=['close']) if code == '399006.XSHE' else original(code, **kwargs)
    c.apis['get_price'] = price
    first = run(c, ['创红比'])[0]
    assert (first['status'], first['rows'], first['gaps']) == ('PARTIAL', 6, 3)
    assert first['coverage']['399006.XSHE']['observed_first_day'] is None
    c.apis['get_price'] = original
    c.calls.clear()
    second = run(c, ['创红比'])[0]
    assert (second['status'], second['rows'], second['gaps']) == ('SUCCESS', 3, 0)
    assert c.calls == [('price', '399006.XSHE', DAYS[0], DAYS[-1])]


def test_embedded_history_matches_maintained_sources_and_python36():
    sync = load(ROOT / 'collectors/tools/sync_history_strategies.py', 'sync_index_history_test')
    sync.sync(check=True)


def test_growth_requests_explicit_calendar_bounds_before_database_access(collectors):
    c = collectors
    def calendar(start_date, end_date):
        assert start_date == DAYS[0] and end_date == DAYS[-1]
        return DAYS
    c.apis['get_trade_days'] = calendar
    assert run(c, ['创红比'])[0]['status'] == 'SUCCESS'


@pytest.mark.parametrize('raw', [None, [None, None]])
def test_growth_rejects_missing_calendar_without_none_comparisons_or_database_writes(collectors, raw):
    c = collectors
    c.apis['get_trade_days'] = lambda **kwargs: raw
    result = run(c, ['创红比'])[0]
    assert result['status'] == 'FAILED' and result['error_type'] == 'ValueError'
    assert '交易日历返回空值' in result['error']
    assert 'collect:' in result['error']
    assert c.connections == []


@pytest.mark.parametrize('name', ['估值标尺', '创红比'])
def test_new_modules_refuse_overwrite_before_connecting(collectors, name):
    c = collectors
    c.options[name]['skip_existing'] = False
    with pytest.raises(ValueError, match='skip_existing'):
        run(c, [name])
    assert c.connections == []


@pytest.mark.parametrize('entry', ['standalone', 'batch'])
@pytest.mark.parametrize('start_option', [{}, {'start_date': None}])
def test_growth_default_starts_in_2018_and_preserves_older_rows(collectors, entry, start_option):
    c = collectors
    old_day, first = dt.date(2009, 12, 31), dt.date(2018, 1, 2)
    days = [old_day, dt.date(2017, 12, 29), first] + DAYS
    c.apis['get_trade_days'] = lambda end_date, **kw: [d for d in days if d <= end_date]
    calls = []
    def prices(code, start_date, end_date, **kwargs):
        assert start_date >= first
        calls.append((code, start_date))
        target = [day for day in days if start_date <= day <= end_date]
        return pd.DataFrame({'close': [4000]*len(target)}, index=target)
    c.apis['get_price'] = prices
    growth.metadata.create_all(c.engine)
    with c.engine.begin() as conn:
        conn.execute(growth.CALENDAR.insert().values(trade_date=old_day,
            source=growth.CALENDAR_SOURCE, updated_at=dt.datetime(2020, 1, 1)))
        conn.execute(growth.PRICES.insert().values(index_code='399006.XSHE', trade_date=old_day,
            close=1234, source=growth.SOURCE, updated_at=dt.datetime(2020, 1, 1)))
    if entry == 'batch':
        c.options['创红比'] = dict(end_date=str(DAYS[-1]), **start_option)
        assert run(c, ['创红比'])[0]['status'] == 'SUCCESS'
    else:
        module = load(ROOT / 'collectors/创红比历史补采_聚宽研究版.py', 'growth_default_start')
        result = module.run(end_date=str(DAYS[-1]), preview=False, api_namespace=c.apis,
                            engine_factory=lambda: c.engine, **start_option)
        assert result['start'] == str(first)
    assert len(calls) == 3 and all(start == first for _, start in calls)
    with c.engine.connect() as conn:
        stored = conn.execute(select(growth.CALENDAR.c.trade_date).order_by(growth.CALENDAR.c.trade_date)).scalars().all()
        assert stored == [old_day, first] + DAYS
        assert conn.scalar(select(growth.PRICES.c.close).where(growth.PRICES.c.trade_date == old_day)) == 1234


def prepare_growth_calendar_gap(c):
    """已有行情、缺少日历，验证补日历不会逐日往返或重采行情。"""
    days = list(pd.bdate_range('2024-01-02', periods=502).date)
    growth.metadata.create_all(c.engine)
    with c.engine.begin() as conn:
        conn.execute(growth.PRICES.insert(), [dict(index_code=code, trade_date=day,
            close=4000, source=growth.SOURCE, updated_at=dt.datetime(2024, 1, 1))
            for code in ('399006.XSHE', '000922.XSHG', '000688.XSHG') for day in days])
        conn.execute(growth.CALENDAR.insert().values(trade_date=days[0],
            source=growth.CALENDAR_SOURCE, updated_at=dt.datetime(2024, 1, 1)))
    c.apis['get_trade_days'] = lambda **kwargs: days
    c.apis['get_price'] = lambda *args, **kwargs: pytest.fail('已有行情不应重采')
    c.options['创红比'] = dict(start_date=str(days[0]), end_date=str(days[-1]))
    return days


@pytest.mark.parametrize('entry', ['standalone', 'batch'])
def test_growth_calendar_bulk_fill_preserves_existing_and_reports_actual_stages(collectors, monkeypatch, entry):
    c = collectors
    days = prepare_growth_calendar_gap(c)
    statements, stages = [], []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(c.engine, 'before_cursor_execute', record)
    monkeypatch.setattr(c.batch, '_progress_note', stages.append)
    module = load(ROOT / 'collectors/创红比历史补采_聚宽研究版.py', 'growth_calendar_bulk')
    def collect():
        if entry == 'batch':
            assert run(c, ['创红比'])[0]['status'] == 'SUCCESS'
        else:
            module.run(**c.options['创红比'], preview=False, api_namespace=c.apis,
                       engine_factory=lambda: c.engine, progress=stages.append)
    try:
        collect()
        calendar_sql = [s for s in statements if 'growth_dividend_trading_day' in s
                        and s.startswith(('SELECT', 'INSERT'))]
        assert len(calendar_sql) == 7  # 一次清单 + 三批各一次查询和写入。
        assert stages.index('数据库已连接：检查创红比表结构') < stages.index('创红比：读取已有指数日期')
        assert '创红比：补写交易日历 501/501 日' in stages
        statements.clear()
        collect()
        assert not any(s.startswith(('INSERT', 'UPDATE', 'DELETE')) for s in statements)
        with c.engine.connect() as conn:
            assert conn.scalar(select(func.count()).select_from(growth.CALENDAR)) == len(days)
            assert conn.scalar(select(growth.CALENDAR.c.updated_at).where(
                growth.CALENDAR.c.trade_date == days[0])) == dt.datetime(2024, 1, 1)
    finally:
        event.remove(c.engine, 'before_cursor_execute', record)


@pytest.mark.parametrize('failure', ['cancel', 'write'])
def test_growth_calendar_partial_run_resumes_without_losing_committed_batches(collectors, failure):
    c = collectors
    days = prepare_growth_calendar_gap(c)
    module = load(ROOT / 'collectors/创红比历史补采_聚宽研究版.py', 'growth_calendar_resume')
    if failure == 'write':
        with c.engine.begin() as conn:
            conn.exec_driver_sql("CREATE TRIGGER fail_calendar BEFORE INSERT ON growth_dividend_trading_day "
                "WHEN NEW.trade_date = '{}' BEGIN SELECT RAISE(ABORT, 'test failure'); END".format(days[300]))
    def progress(message):
        if failure == 'cancel' and message == '创红比：补写交易日历 250/501 日':
            raise KeyboardInterrupt()
    kwargs = dict(c.options['创红比'], preview=False, api_namespace=c.apis, engine_factory=lambda: c.engine)
    with pytest.raises(KeyboardInterrupt if failure == 'cancel' else RuntimeError):
        module.run(progress=progress, **kwargs)
    with c.engine.begin() as conn:
        assert conn.scalar(select(func.count()).select_from(growth.CALENDAR)) == 251
        if failure == 'write':
            conn.exec_driver_sql('DROP TRIGGER fail_calendar')
    module.run(**kwargs)
    with c.engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(growth.CALENDAR)) == len(days)
