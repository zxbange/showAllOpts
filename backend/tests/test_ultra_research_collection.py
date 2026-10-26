"""离线模拟聚宽，检查历史筛选、缺失行情和MySQL写入语义。"""
import datetime as dt
import importlib.util
import io
from pathlib import Path
import sys
import types
from unittest.mock import patch

import pandas as pd
import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


PATH = Path(__file__).resolve().parents[2] / 'collectors/超短线历史行情采集_研究版.py'
spec = importlib.util.spec_from_file_location('ultra_collect', PATH)
m = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'jqdata': types.ModuleType('jqdata')}):
    spec.loader.exec_module(m)


def test_shsz_includes_mainboard_chinext_star_excludes_other_markets():
    for code in ['600001.XSHG', '601001.XSHG', '603001.XSHG', '605001.XSHG',
                 '000001.XSHE', '001001.XSHE', '002001.XSHE', '003001.XSHE',
                 '688001.XSHG', '300001.XSHE', '301001.XSHE']:
        assert m.included_stock(code)
    for code in ['200001.XSHE', '900001.XSHG', '920001.XBSE', '689001.XSHG']:
        assert not m.included_stock(code)


def test_expanded_scope_does_not_reuse_mainboard_snapshot_checkpoint():
    date = dt.date(2024, 1, 2)
    assert m.task_key('universe', m.RULE_VERSION, m.MIN_MARKET_CAP, date) != m.task_key(
        'universe', 'mainboard_nonst_cap30_v1', m.MIN_MARKET_CAP, date)


def test_universe_filters_historical_st_cap_and_unknown(monkeypatch):
    date = dt.date(2024, 3, 1)
    codes = ['600001.XSHG', '600002.XSHG', '600003.XSHG', '600004.XSHG', '600005.XSHG']
    securities = pd.DataFrame({'display_name': codes, 'start_date': [dt.date(2000, 1, 1)]*5}, index=codes)
    class Field:
        def in_(self, values):
            return values
    class Query:
        def filter(self, value):
            return self
    seen = []
    def listing(**kwargs):
        assert kwargs['date'] == date
        return securities
    def fundamentals(**kwargs):
        assert kwargs['date'] == date
        return pd.DataFrame({'code': codes[:4], 'market_cap': [30, 50, 29.99, 50]})
    apis = {'get_all_securities': listing,
        'get_extras': lambda **kw: pd.DataFrame([[False, True, False, None, False]], columns=codes, index=[date]),
        'valuation': types.SimpleNamespace(code=Field(), market_cap=Field()),
        'query': lambda *args: Query(), 'get_fundamentals': fundamentals}
    monkeypatch.setattr(m, 'api', apis.__getitem__)
    monkeypatch.setattr(m, 'read_records', lambda *args: [])
    monkeypatch.setattr(m, 'task_row_count', lambda *args: None)
    monkeypatch.setattr(m, 'mark_stage_day', lambda *args: None)
    monkeypatch.setattr(m, 'commit_task', lambda engine, key, table, rows, **kw: seen.extend(rows))
    m.collect_universe(None, [date])
    assert [r['eligible'] for r in seen] == [1, 0, 0, 0, 0]
    assert seen[-1]['market_cap'] is None
    assert seen[3]['is_st'] is None


def test_completed_universe_does_not_refetch(monkeypatch):
    row = dict(code='600001.XSHG', display_name='测试', listed_date=dt.date(2000, 1, 1),
               rule_version=m.RULE_VERSION, is_st=0, market_cap=30, eligible=1)
    monkeypatch.setattr(m, 'task_row_count', lambda *args: 1)
    monkeypatch.setattr(m, 'mark_stage_day', lambda *args: None)
    monkeypatch.setattr(m, 'read_records', lambda *args: [row])
    monkeypatch.setattr(m, 'api', lambda name: pytest.fail('已完成任务不应请求API'))
    m.collect_universe(None, [dt.date(2024, 1, 2)])


def test_price_validation_rejects_truncated_or_duplicate_results():
    date = dt.date(2024, 1, 2)
    expected = {(date, '600001.XSHG'), (date, '600002.XSHG')}
    frame = pd.DataFrame({'time': [date], 'code': ['600001.XSHG'], 'close': [10.]})
    with pytest.raises(ValueError, match='缺少'):
        m.normalize_prices(frame, expected, ['close'])
    with pytest.raises(ValueError, match='重复'):
        m.normalize_prices(pd.concat([frame, frame]), {(date, '600001.XSHG')}, ['close'])
    assert len(m.normalize_prices(frame, {(date, '600001.XSHG')}, ['close'])) == 1


def test_mysql_upsert_and_task_commit_share_transaction():
    statements = []
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def execute(self, statement):
            statements.append(str(statement.compile(dialect=mysql.dialect())))
    class Engine:
        def begin(self):
            return Connection()
    m.commit_task(Engine(), 'test_key', m.CALENDAR, [{'trade_date': dt.date(2024, 1, 2)}])
    assert len(statements) == 2
    assert all('ON DUPLICATE KEY UPDATE' in statement for statement in statements)
    assert 'ultra_research_calendar' in statements[0]
    assert 'ultra_research_task' in statements[1]


def test_task_failure_never_writes_completion(monkeypatch):
    seen = []
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            assert args[0] is RuntimeError
            return False
    class Engine:
        def begin(self):
            return Connection()
    def fail(conn, table, rows):
        seen.append(table.name)
        raise RuntimeError('模拟写入失败')
    monkeypatch.setattr(m, 'upsert', fail)
    with pytest.raises(RuntimeError):
        m.commit_task(Engine(), 'key', m.CALENDAR, [{'trade_date': dt.date(2024, 1, 2)}])
    assert seen == ['ultra_research_calendar']


def test_bars_keep_ineligible_context_and_separate_adjustments(monkeypatch):
    dates = [dt.date(2024, 1, 2), dt.date(2024, 1, 3)]
    code = '600001.XSHG'
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def execute(self, statement):
            return types.SimpleNamespace(fetchall=lambda: [(date, code) for date in dates])
    class Engine:
        def connect(self):
            return Connection()
    saved = []
    calls = []
    def get_price(**kwargs):
        calls.append(kwargs)
        values = {'time': dates, 'code': [code, code]}
        for field in kwargs['fields']:
            values[field] = ([20., 20.] if kwargs['fq'] == 'post' else [10., 5.])
        if kwargs['fq'] is None:
            values['paused'] = [0, 0]
        return pd.DataFrame(values)
    monkeypatch.setattr(m, 'INDEX_CODES', [])
    monkeypatch.setattr(m, 'complete_bar_keys', lambda *args: set())
    monkeypatch.setattr(m, 'mark_stage_day', lambda *args: None)
    monkeypatch.setattr(m, 'api', lambda name: get_price)
    monkeypatch.setattr(m, 'commit_task', lambda engine, key, table, rows: saved.extend(rows))
    m.collect_bars(Engine(), dates)
    assert [r['close'] for r in saved] == [10., 5.]
    assert [r['adj_close'] for r in saved] == [20., 20.]
    assert [r['trade_date'] for r in saved] == dates
    assert len(calls) == 2


@pytest.mark.parametrize('installed,expected', [
    ({'MySQLdb', 'pymysql'}, 'mysql+mysqldb'),
    ({'MySQLdb'}, 'mysql+mysqldb'),
    ({'pymysql'}, 'mysql+pymysql'),
])
def test_database_driver_selection(monkeypatch, installed, expected):
    monkeypatch.setattr(m, 'DB_DRIVER', 'auto')
    monkeypatch.setattr(m, 'DB_TLS_VERIFY', True)
    def load(name):
        if name not in installed:
            raise ImportError(name)
        return types.ModuleType(name)
    monkeypatch.setattr(m.importlib, 'import_module', load)
    monkeypatch.setattr(m, 'create_engine', lambda url, **kw: (url.drivername, kw))
    driver, options = m.make_engine(dict(dbuser='test', dbpassword='test', host='localhost', dbname='test'))
    assert driver == expected
    assert options['connect_args']['connect_timeout'] == m.DB_OPTIONS['connect_timeout']
    assert options['pool_size'] == 1
    assert options['max_overflow'] == 0
    if expected == 'mysql+pymysql':
        assert options['connect_args']['ssl'].check_hostname
        assert options['connect_args']['ssl'].verify_mode == m.ssl.CERT_REQUIRED


def test_explicit_pymysql_does_not_select_old_mysqldb(monkeypatch):
    loaded = []
    def load(name):
        loaded.append(name)
        return types.ModuleType(name)
    monkeypatch.setattr(m, 'DB_DRIVER', 'pymysql')
    monkeypatch.setattr(m.importlib, 'import_module', load)
    monkeypatch.setattr(m, 'create_engine', lambda url, **kw: url.drivername)
    assert m.make_engine(dict(dbuser='test', dbpassword='test', host='localhost', dbname='test')) == 'mysql+pymysql'
    assert loaded == ['pymysql']


def test_missing_driver_reports_actionable_error(monkeypatch):
    def load(name):
        raise ImportError(name)
    monkeypatch.setattr(m.importlib, 'import_module', load)
    with pytest.raises(RuntimeError, match='MySQLdb或PyMySQL'):
        m.make_engine({})


@pytest.mark.parametrize('failures,succeeds', [(1, True), (3, False)])
def test_connection_retry_and_failure_cleanup(monkeypatch, failures, succeeds):
    attempts = []
    disposed = []
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    class Engine:
        def connect(self):
            attempts.append(1)
            if len(attempts) <= failures:
                raise RuntimeError('private connection error')
            return Connection()
        def dispose(self):
            disposed.append(True)
    engine = Engine()
    monkeypatch.setattr(m, 'make_engine', lambda config: engine)
    monkeypatch.setattr(m, 'DB_OPTIONS', dict(connect_timeout=10, retry_count=3,
                                           retry_interval=0, pool_recycle=3600))
    if succeeds:
        assert m.connect_database({}) is engine
        assert len(attempts) == 2
        assert not disposed
    else:
        with pytest.raises(RuntimeError, match='MySQL 连接失败'):
            m.connect_database({})
        assert len(attempts) == 3
        assert disposed == [True]


def test_connection_diagnostics_show_original_error_without_password(monkeypatch, capsys):
    secret = 'test@pass:/word'
    config = dict(host='example.invalid', port=3306, dbname='research', dbuser='researcher',
                  dbpassword=secret)
    class Engine:
        def connect(self):
            error = RuntimeError('mysql://researcher:' + m.quote_plus(secret, safe='') + '@example.invalid')
            error.orig = Exception(1045, 'Access denied for researcher ' + secret)
            raise error
        def dispose(self):
            pass
    monkeypatch.setattr(m, 'make_engine', lambda config: Engine())
    monkeypatch.setattr(m, 'DB_OPTIONS', dict(connect_timeout=10, retry_count=1,
                                           retry_interval=0, pool_recycle=3600))
    with pytest.raises(RuntimeError) as error:
        m.connect_database(config)
    output = capsys.readouterr().out + str(error.value)
    assert '1045' in output and 'Access denied' in output
    assert 'example.invalid' in output and 'researcher' in output
    assert secret not in output
    assert m.quote_plus(secret, safe='') not in output


def test_explicit_temporary_certificate_bypass(monkeypatch):
    monkeypatch.setattr(m, 'DB_DRIVER', 'pymysql')
    monkeypatch.setattr(m, 'DB_TLS_VERIFY', False)
    monkeypatch.setattr(m.importlib, 'import_module', lambda name: types.ModuleType(name))
    monkeypatch.setattr(m, 'create_engine', lambda url, **kw: kw)
    options = m.make_engine(dict(dbuser='test', dbpassword='test', host='localhost', dbname='test'))
    context = options['connect_args']['ssl']
    assert not context.check_hostname
    assert context.verify_mode == m.ssl.CERT_NONE


def test_certificate_failure_stops_retries_and_disposes(monkeypatch):
    attempts = []
    disposed = []
    class Engine:
        def connect(self):
            attempts.append(True)
            raise RuntimeError('[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed')
        def dispose(self):
            disposed.append(True)
    monkeypatch.setattr(m, 'make_engine', lambda config: Engine())
    with pytest.raises(RuntimeError, match='TLS证书验证失败'):
        m.connect_database({})
    assert len(attempts) == 1
    assert disposed == [True]


@pytest.fixture
def research_db(monkeypatch):
    """实际执行读写和事务，只将MySQL upsert换成SQLite语法。"""
    engine = create_engine('sqlite:///:memory:')
    m.SCHEMA.create_all(engine)
    def upsert(conn, table, rows):
        if not rows:
            return
        statement = sqlite_insert(table).values(rows)
        updates = {c.name: statement.excluded[c.name] for c in table.c if not c.primary_key}
        if updates:
            statement = statement.on_conflict_do_update(
                index_elements=[c.name for c in table.primary_key], set_=updates)
        else:
            statement = statement.on_conflict_do_nothing()
        conn.execute(statement)
    monkeypatch.setattr(m, 'upsert', upsert)
    yield engine
    engine.dispose()


@pytest.fixture
def market(monkeypatch):
    dates = [dt.date(2024, 1, d) for d in (2, 3, 4, 5)]
    codes = ['600001.XSHG', '300001.XSHE']
    calls = []
    statuses = {}
    class Field:
        def in_(self, values):
            return values
    class Query:
        def filter(self, codes):
            self.codes = codes
            return self
    def listing(**kwargs):
        calls.append(('listing', kwargs))
        return pd.DataFrame({'display_name': ['股票一', '股票二'],
            'start_date': [dt.date(2000, 1, 1)]*2}, index=codes)
    def extras(**kwargs):
        calls.append(('st', kwargs))
        return pd.DataFrame([[statuses.get((kwargs['start_date'], code), False)
                              for code in kwargs['security_list']]],
                            columns=kwargs['security_list'], index=[kwargs['start_date']])
    def caps(**kwargs):
        calls.append(('caps', kwargs))
        selected = kwargs['query_object'].codes
        return pd.DataFrame({'code': selected, 'market_cap': [50.] * len(selected)})
    def prices(**kwargs):
        calls.append(('price', kwargs))
        rows = []
        for date in dates:
            if kwargs['start_date'] <= date <= kwargs['end_date']:
                for code in kwargs['security']:
                    row = dict(time=date, code=code)
                    row.update({field: 20. if kwargs['fq'] == 'post' else 10.
                                for field in kwargs['fields']})
                    if 'paused' in row:
                        row['paused'] = 0
                    rows.append(row)
        return pd.DataFrame(rows)
    apis = dict(get_all_securities=listing, get_extras=extras,
        get_fundamentals=caps, query=lambda *args: Query(),
        valuation=types.SimpleNamespace(code=Field(), market_cap=Field()), get_price=prices)
    monkeypatch.setattr(m, 'api', apis.__getitem__)
    monkeypatch.setattr(m, 'INDEX_CODES', [])
    return types.SimpleNamespace(dates=dates, codes=codes, calls=calls, statuses=statuses)


def all_records(engine, table):
    return m.read_records(engine, table, table.c.trade_date >= dt.date(2024, 1, 1))


def test_repeat_full_collection_skips_market_apis(research_db, market):
    m.collect_universe(research_db, market.dates)
    m.collect_bars(research_db, market.dates)
    before = all_records(research_db, m.BARS)
    market.calls.clear()
    universe = m.collect_universe(research_db, market.dates)
    bars = m.collect_bars(research_db, market.dates)
    assert universe == dict(written=0, skipped=8, incomplete=0)
    assert bars == dict(written=0, skipped=8)
    assert not market.calls
    assert all_records(research_db, m.BARS) == before


def test_universe_repairs_deleted_rows_despite_completed_task(research_db, market):
    date = market.dates[0]
    m.collect_universe(research_db, [date])
    with research_db.begin() as conn:
        conn.execute(m.UNIVERSE.delete().where(m.UNIVERSE.c.code == market.codes[1]))
    market.calls.clear()
    stats = m.collect_universe(research_db, [date])
    assert stats['written'] == 1 and stats['skipped'] == 1
    extras = [kw for name, kw in market.calls if name == 'st']
    assert extras[0]['security_list'] == [market.codes[1]]
    key = m.task_key('universe', m.RULE_VERSION, m.MIN_MARKET_CAP, date)
    assert m.task_row_count(research_db, key) == 2
    market.calls.clear()
    m.collect_universe(research_db, [date])
    assert not market.calls


def test_missing_task_does_not_redownload_complete_data(research_db, market):
    m.collect_universe(research_db, market.dates)
    m.collect_bars(research_db, market.dates)
    before = all_records(research_db, m.BARS)
    with research_db.begin() as conn:
        conn.execute(m.TASKS.delete())
    market.calls.clear()
    m.collect_universe(research_db, market.dates)
    m.collect_bars(research_db, market.dates)
    assert all(name == 'listing' for name, _ in market.calls)
    assert all_records(research_db, m.BARS) == before


def test_unknown_st_snapshot_is_retried(research_db, market):
    date = market.dates[0]
    market.statuses[(date, market.codes[1])] = None
    initial = m.collect_universe(research_db, [date])
    assert initial['incomplete'] == 1
    market.statuses[(date, market.codes[1])] = False
    market.calls.clear()
    repaired = m.collect_universe(research_db, [date])
    assert repaired == dict(written=1, skipped=1, incomplete=0)
    assert next(kw['security_list'] for name, kw in market.calls if name == 'st') == [market.codes[1]]


def test_bars_fill_disjoint_holes_without_touching_complete_dates(research_db, market):
    m.collect_universe(research_db, market.dates)
    m.collect_bars(research_db, market.dates)
    hole_dates = [market.dates[0], market.dates[2]]
    with research_db.begin() as conn:
        conn.execute(m.BARS.delete().where(m.and_(m.BARS.c.code == market.codes[0],
                                                 m.BARS.c.trade_date.in_(hole_dates))))
    before = {(r['trade_date'], r['code']): r for r in all_records(research_db, m.BARS)}
    market.calls.clear()
    stats = m.collect_bars(research_db, market.dates)
    assert stats == dict(written=2, skipped=6)
    calls = [kw for name, kw in market.calls if name == 'price']
    assert len(calls) == 4  # 每个缺失日独立查询未复权、后复权。
    assert all(kw['start_date'] == kw['end_date'] and kw['start_date'] in hole_dates for kw in calls)
    assert all(kw['security'] == [market.codes[0]] for kw in calls)
    after = {(r['trade_date'], r['code']): r for r in all_records(research_db, m.BARS)}
    assert all(after[key] == row for key, row in before.items())


def test_range_extension_only_downloads_new_dates_and_ignores_batch_change(research_db, market, monkeypatch):
    m.collect_universe(research_db, market.dates)
    m.collect_bars(research_db, market.dates[:2])
    market.calls.clear()
    stats = m.collect_bars(research_db, market.dates)
    assert stats == dict(written=4, skipped=4)
    assert all(kw['start_date'] == market.dates[2] for name, kw in market.calls if name == 'price')
    market.calls.clear()
    monkeypatch.setattr(m, 'BATCH_SIZE', 1)
    m.collect_bars(research_db, market.dates)
    assert not market.calls


def test_partial_price_field_is_repaired(research_db, market):
    m.collect_universe(research_db, market.dates)
    m.collect_bars(research_db, market.dates)
    with research_db.begin() as conn:
        conn.execute(m.BARS.update().where(m.and_(m.BARS.c.trade_date == market.dates[1],
            m.BARS.c.code == market.codes[0])).values(adj_close=None))
    market.calls.clear()
    stats = m.collect_bars(research_db, market.dates)
    assert stats == dict(written=1, skipped=7)
    assert all(kw['start_date'] == kw['end_date'] == market.dates[1]
               for name, kw in market.calls if name == 'price')


def test_paused_null_prices_are_not_retried_forever(research_db, market):
    m.collect_universe(research_db, market.dates)
    m.collect_bars(research_db, market.dates)
    with research_db.begin() as conn:
        conn.execute(m.BARS.update().where(m.BARS.c.trade_date == market.dates[0]).values(
            paused=1, open=None, close=None, high=None, low=None, adj_open=None,
            adj_close=None, adj_high=None, adj_low=None, volume=None, money=None))
    market.calls.clear()
    assert m.collect_bars(research_db, market.dates)['written'] == 0
    assert not market.calls


def test_force_remains_explicit_full_refresh(research_db, market):
    m.collect_universe(research_db, market.dates)
    m.collect_bars(research_db, market.dates)
    market.calls.clear()
    assert m.collect_universe(research_db, market.dates, force=True)['written'] == 8
    assert m.collect_bars(research_db, market.dates, force=True)['written'] == 8
    assert any(name == 'price' for name, _ in market.calls)


def test_terminal_progress_reuses_line_and_reports_counts():
    stream = io.StringIO()
    with m.CollectionProgress(stream=stream) as progress:
        progress.update('筛选快照', 1, 2, written=2, skipped=3, force=True)
        progress.update('行情', 2, 2, written=5, skipped=10, force=True)
    output = stream.getvalue()
    assert output.count('\n') == 1
    assert output.count('\r') == 3
    assert '[####################]' in output
    assert '补采5行 跳过10行' in output
    assert '100.0%' in output


def test_progress_closes_cleanly_on_interruption():
    stream = io.StringIO()
    with pytest.raises(KeyboardInterrupt):
        with m.CollectionProgress(stream=stream) as progress:
            progress.update('行情', 1, 3, force=True)
            raise KeyboardInterrupt()
    assert '已中断/失败' in stream.getvalue()
    assert stream.getvalue().endswith('\n')


def test_full_run_reuses_calendar_and_only_adds_new_day(research_db, market, monkeypatch):
    # 保留内存池模拟持久数据库；同时检查每次run均调用引擎释放。
    engine = research_db
    original_dispose = engine.dispose
    disposed = []
    monkeypatch.setattr(engine, 'dispose', lambda: disposed.append(True))
    original_api = m.api
    def trade_days(start_date=None, end_date=None, count=None):
        selected = [date for date in market.dates
                    if (start_date is None or date >= start_date) and date <= end_date]
        return selected[-count:] if count else selected
    monkeypatch.setattr(m, 'api', lambda name: trade_days if name == 'get_trade_days' else original_api(name))
    monkeypatch.setattr(m, 'connect_database', lambda config: engine)
    monkeypatch.setattr(m, 'WARMUP_DAYS', 1)
    monkeypatch.setattr(m, 'PROGRESS_ENABLED', False)
    try:
        initial = m.run({'host': 'unused'}, start_date=market.dates[1], end_date=market.dates[2])
        assert initial['calendar_added'] == 3
        assert initial['universe']['written'] == initial['bars']['written'] == 6
        market.calls.clear()
        extended = m.run({'host': 'unused'}, start_date=market.dates[1], end_date=market.dates[3])
        assert extended['calendar_added'] == 1
        assert extended['universe']['written'] == extended['bars']['written'] == 2
        assert all(kw['date'] == market.dates[3] for name, kw in market.calls if name == 'listing')
        market.calls.clear()
        repeated = m.run({'host': 'unused'}, start_date=market.dates[1], end_date=market.dates[3])
        assert repeated['calendar_added'] == repeated['universe']['written'] == repeated['bars']['written'] == 0
        assert not market.calls
        assert len(disposed) == 3
    finally:
        original_dispose()


def price_response(days, codes, fields, value=10.):
    return pd.DataFrame([dict(time=date, code=code, **{field: value for field in fields})
                         for date in days for code in codes])


@pytest.mark.parametrize('fq', [None, 'post'])
def test_auto_fill_three_hundred_rows_missing_from_batch(monkeypatch, fq):
    # 50只股票×19天，模拟批量接口只返回650行，缺少4月21日起的300行。
    dates = [stamp.date() for stamp in pd.bdate_range('2023-04-03', '2023-04-28')
             if stamp.date() != dt.date(2023, 4, 5)]
    codes = ['{:06d}.XSHE'.format(value) for value in range(1, 51)]
    expected = set((date, code) for date in dates for code in codes)
    calls, missing_events = [], []
    def price(**kwargs):
        calls.append(kwargs)
        selected = [date for date in dates if kwargs['start_date'] <= date <= kwargs['end_date']]
        return price_response(selected, kwargs['security'], kwargs['fields']).iloc[:650]
    monkeypatch.setattr(m, 'api', lambda name: price)
    result = m.fetch_prices(expected, ['open', 'close'], fq, dates,
                           on_missing=lambda *args: missing_events.append(args))
    assert len(result) == 950
    assert m.price_keys(result.index) == expected
    assert missing_events == [(300, 950, dt.date(2023, 4, 21), dt.date(2023, 4, 28))]
    assert len(calls) == 2
    assert calls[1]['start_date'] == dt.date(2023, 4, 21)
    assert all(call['fq'] == fq for call in calls)


def test_daily_interval_boundary_falls_back_to_count_one(monkeypatch):
    dates = [dt.date(2023, 4, 20), dt.date(2023, 4, 21)]
    code = '000001.XSHE'
    calls = []
    def price(**kwargs):
        calls.append(kwargs)
        if 'count' in kwargs:
            return price_response([kwargs['end_date']], kwargs['security'], kwargs['fields'])
        selected = [date for date in dates if kwargs['start_date'] <= date < kwargs['end_date']]
        return price_response(selected, kwargs['security'], kwargs['fields'])
    monkeypatch.setattr(m, 'api', lambda name: price)
    expected = set((date, code) for date in dates)
    assert m.price_keys(m.fetch_prices(expected, ['close'], None, dates).index) == expected
    assert len(calls) == 3
    assert calls[-1]['count'] == 1
    assert 'start_date' not in calls[-1]
    assert calls[-1]['end_date'] == dates[-1]


def test_single_day_fallback_rejects_previous_trading_day(monkeypatch):
    date = dt.date(2023, 4, 21)
    code = '000001.XSHE'
    def price(**kwargs):
        return price_response([dt.date(2023, 4, 20)], [code], ['close'])
    monkeypatch.setattr(m, 'api', lambda name: price)
    with pytest.raises(ValueError, match='单股单日补采仍缺少') as error:
        m.fetch_prices({(date, code)}, ['close'], None, [date])
    assert '2023-04-20' in str(error.value)
    assert 'count=1返回' in str(error.value)
    assert '未提交' in str(error.value)


def test_empty_batch_is_split_until_smaller_requests_work(monkeypatch):
    date = dt.date(2023, 4, 21)
    codes = ['000001.XSHE', '000002.XSHE', '000004.XSHE', '000005.XSHE']
    calls = []
    def price(**kwargs):
        calls.append(kwargs)
        if len(kwargs['security']) > 1:
            return pd.DataFrame()
        return price_response([date], kwargs['security'], kwargs['fields'])
    monkeypatch.setattr(m, 'api', lambda name: price)
    expected = set((date, code) for code in codes)
    assert m.price_keys(m.fetch_prices(expected, ['close'], None, [date]).index) == expected
    assert len(calls) == 7


@pytest.mark.parametrize('missing_fq', [None, 'post'])
def test_permanent_source_gap_never_commits_partial_batch(research_db, market, monkeypatch, missing_fq):
    m.collect_universe(research_db, market.dates)
    task_count = len(m.read_records(research_db, m.TASKS, m.TASKS.c.row_count >= 0))
    original_api = m.api
    def price(**kwargs):
        kwargs = dict(kwargs)
        if 'count' in kwargs:
            kwargs['start_date'] = kwargs['end_date']
        frame = original_api('get_price')(**kwargs)
        if kwargs['fq'] == missing_fq:
            frame = frame[~((frame['time'] == market.dates[-1]) & (frame['code'] == market.codes[0]))]
        return frame
    monkeypatch.setattr(m, 'api', lambda name: price if name == 'get_price' else original_api(name))
    with pytest.raises(ValueError, match='单股单日补采仍缺少'):
        m.collect_bars(research_db, market.dates)
    assert not all_records(research_db, m.BARS)
    assert len(m.read_records(research_db, m.TASKS, m.TASKS.c.row_count >= 0)) == task_count


def test_duplicate_price_rows_still_raise_immediately(monkeypatch):
    date, code = dt.date(2023, 4, 21), '000001.XSHE'
    calls = []
    def price(**kwargs):
        calls.append(kwargs)
        return price_response([date, date], [code], ['close'])
    monkeypatch.setattr(m, 'api', lambda name: price)
    with pytest.raises(ValueError, match='重复'):
        m.fetch_prices({(date, code)}, ['close'], None, [date])
    assert len(calls) == 1


def emulate_old_pandas_datetime_index(monkeypatch):
    """模拟聚宽旧pandas对MultiIndex中datetime.date的自动Timestamp转换。"""
    original = pd.DataFrame.set_index
    def set_index(frame, keys, *args, **kwargs):
        result = original(frame, keys, *args, **kwargs)
        if keys == ['trade_date', 'code'] and result is not None:
            result.index = pd.MultiIndex.from_arrays([
                pd.to_datetime(result.index.get_level_values(0)),
                result.index.get_level_values(1)], names=['trade_date', 'code'])
        return result
    monkeypatch.setattr(pd.DataFrame, 'set_index', set_index)


@pytest.mark.parametrize('time_value', [
    dt.date(2023, 4, 21), dt.datetime(2023, 4, 21, 15, 0),
    pd.Timestamp('2023-04-21'), '2023-04-21 00:00:00',
])
def test_same_day_row_survives_old_pandas_date_index_coercion(monkeypatch, time_value):
    emulate_old_pandas_datetime_index(monkeypatch)
    date, code = dt.date(2023, 4, 21), '000001.XSHE'
    source = price_response([time_value], [code], ['close'])
    legacy = source.copy()
    legacy['trade_date'] = legacy['time'].map(m.day)
    legacy = legacy.set_index(['trade_date', 'code'])
    assert (date, code) not in set(legacy.index)  # 复现：有同日数据，但原集合比较认为缺行。
    normalized = m.normalize_prices(source, {(date, code)}, ['close'])
    assert normalized.index.tolist() == [('2023-04-21', code)]
    assert normalized.loc[('2023-04-21', code), 'close'] == 10.


@pytest.mark.parametrize('raw_code', ['000001.XSHE', ' 000001.xshe ', b'000001.XSHE'])
def test_returned_code_representations_share_one_key(raw_code):
    date = dt.date(2023, 4, 21)
    normalized = m.normalize_prices(price_response([date], [raw_code], ['close']),
                                    {(date, '000001.XSHE')}, ['close'])
    assert m.price_keys(normalized.index) == {(date, '000001.XSHE')}


def test_old_pandas_matching_row_does_not_trigger_fallback(monkeypatch):
    emulate_old_pandas_datetime_index(monkeypatch)
    date, code = dt.date(2023, 4, 21), '000001.XSHE'
    calls = []
    def price(**kwargs):
        calls.append(kwargs)
        return price_response([pd.Timestamp(date)], [code], ['close'])
    monkeypatch.setattr(m, 'api', lambda name: price)
    result = m.fetch_prices({(date, code)}, ['close'], None, [date],
                           on_missing=lambda *args: pytest.fail('同日数据存在，不应进入缺口重试'))
    assert len(result) == 1
    assert len(calls) == 1


def test_original_and_adjusted_time_types_join_and_persist_date(research_db, market, monkeypatch):
    emulate_old_pandas_datetime_index(monkeypatch)
    m.collect_universe(research_db, market.dates)
    original_api = m.api
    def price(**kwargs):
        frame = original_api('get_price')(**kwargs)
        frame['time'] = pd.to_datetime(frame['time'])
        if kwargs['fq'] == 'post':
            frame['time'] += pd.Timedelta(hours=15)
        return frame
    monkeypatch.setattr(m, 'api', lambda name: price if name == 'get_price' else original_api(name))
    assert m.collect_bars(research_db, market.dates)['written'] == 8
    rows = all_records(research_db, m.BARS)
    assert all(type(row['trade_date']) is dt.date for row in rows)
    assert all(row['close'] == 10. and row['adj_close'] == 20. for row in rows)


def test_code_normalization_cannot_hide_duplicate_price_rows():
    date = dt.date(2023, 4, 21)
    source = price_response([date], ['000001.XSHE', b'000001.XSHE'], ['close'])
    with pytest.raises(ValueError, match='重复'):
        m.normalize_prices(source, {(date, '000001.XSHE')}, ['close'])


@pytest.fixture
def incremental_run(research_db, market, monkeypatch):
    engine = research_db
    original_dispose = engine.dispose
    monkeypatch.setattr(engine, 'dispose', lambda: None)
    original_api = m.api
    trade_calls = []
    def trade_days(start_date=None, end_date=None, count=None):
        trade_calls.append(dict(start_date=start_date, end_date=end_date, count=count))
        selected = [date for date in market.dates
                    if (start_date is None or date >= start_date) and date <= end_date]
        return selected[-count:] if count else selected
    monkeypatch.setattr(m, 'api', lambda name: trade_days if name == 'get_trade_days' else original_api(name))
    monkeypatch.setattr(m, 'connect_database', lambda config: engine)
    monkeypatch.setattr(m, 'WARMUP_DAYS', 1)
    monkeypatch.setattr(m, 'PROGRESS_ENABLED', False)
    def run(end=None, force=False):
        return m.run({'host': 'unused'}, start_date=market.dates[1],
                     end_date=market.dates[-1] if end is None else end, force=force)
    yield types.SimpleNamespace(run=run, trade_calls=trade_calls)
    original_dispose()


def test_latest_day_repeat_does_not_scan_history(research_db, market, incremental_run, monkeypatch):
    incremental_run.run()
    market.calls.clear()
    incremental_run.trade_calls.clear()
    monkeypatch.setattr(m, 'collect_universe', lambda *a, **kw: pytest.fail('无新日期不应扫描股票池'))
    monkeypatch.setattr(m, 'collect_bars', lambda *a, **kw: pytest.fail('无新日期不应扫描行情'))
    monkeypatch.setattr(m, 'read_records', lambda *a, **kw: pytest.fail('已存在完整日期标记，无需逐日读取'))
    result = incremental_run.run()
    assert result['collected_from'] is None
    assert result['last_complete_date'] == market.dates[-1]
    assert not market.calls
    assert incremental_run.trade_calls == [dict(start_date=None, end_date=market.dates[-1], count=1)]


def test_new_run_only_passes_new_dates_to_collectors(research_db, market, incremental_run, monkeypatch):
    incremental_run.run(end=market.dates[-2])
    universe, bars = m.collect_universe, m.collect_bars
    visited = []
    def collect_universe(engine, dates, **kwargs):
        visited.append(('universe', dates))
        return universe(engine, dates, **kwargs)
    def collect_bars(engine, dates, **kwargs):
        visited.append(('bars', dates))
        return bars(engine, dates, **kwargs)
    monkeypatch.setattr(m, 'collect_universe', collect_universe)
    monkeypatch.setattr(m, 'collect_bars', collect_bars)
    result = incremental_run.run()
    assert visited == [('universe', [market.dates[-1]]), ('bars', [market.dates[-1]])]
    assert result['collected_from'] == market.dates[-1]
    assert result['warmup_start'] is None
    assert result['last_complete_date'] == market.dates[-1]


def test_month_failure_does_not_advance_cursor_to_partial_latest_date(
        research_db, market, incremental_run, monkeypatch):
    market.dates[:] = [dt.date(2024, 1, 30), dt.date(2024, 1, 31),
                       dt.date(2024, 2, 1), dt.date(2024, 2, 2)]
    monkeypatch.setattr(m, 'BATCH_SIZE', 1)
    original_api = m.api
    def price(**kwargs):
        kwargs = dict(kwargs)
        if 'count' in kwargs:
            kwargs['start_date'] = kwargs['end_date']
        frame = original_api('get_price')(**kwargs)
        return frame[~((frame['time'] == market.dates[-1]) & (frame['code'] == market.codes[0]))]
    monkeypatch.setattr(m, 'api', lambda name: price if name == 'get_price' else original_api(name))
    with pytest.raises(ValueError, match='单股单日补采仍缺少'):
        incremental_run.run()
    assert m.last_stage_day(research_db, 'bars') == dt.date(2024, 1, 31)
    with research_db.connect() as conn:
        assert conn.execute(m.select_compat(m.func.max(m.BARS.c.trade_date))).scalar() == dt.date(2024, 2, 2)
    monkeypatch.setattr(m, 'api', original_api)
    result = incremental_run.run()
    assert result['collected_from'] == dt.date(2024, 2, 1)
    assert result['bars'] == dict(written=2, skipped=2)
    assert result['last_complete_date'] == dt.date(2024, 2, 2)


def test_legacy_database_bootstraps_from_latest_complete_day(research_db, market, incremental_run):
    m.collect_universe(research_db, market.dates[:-1])
    m.collect_bars(research_db, market.dates[:-1])
    with research_db.begin() as conn:
        conn.execute(m.TASKS.delete().where(m.TASKS.c.task_key.like('complete-day-v1:%')))
    market.calls.clear()
    result = incremental_run.run()
    assert result['collected_from'] == market.dates[-1]
    assert result['bars']['written'] == 2
    assert all(kw['date'] == market.dates[-1] for name, kw in market.calls if name == 'listing')


def test_legacy_partial_month_is_repaired_before_cursor_moves(
        research_db, market, incremental_run, monkeypatch):
    monkeypatch.setattr(m, 'BATCH_SIZE', 1)
    m.collect_universe(research_db, market.dates)
    original_api = m.api
    def price(**kwargs):
        kwargs = dict(kwargs)
        if 'count' in kwargs:
            kwargs['start_date'] = kwargs['end_date']
        frame = original_api('get_price')(**kwargs)
        return frame[frame['code'] != market.codes[0]]
    monkeypatch.setattr(m, 'api', lambda name: price if name == 'get_price' else original_api(name))
    with pytest.raises(ValueError, match='单股单日补采仍缺少'):
        m.collect_bars(research_db, market.dates)
    assert m.last_stage_day(research_db, 'bars') is None
    monkeypatch.setattr(m, 'api', original_api)
    result = incremental_run.run()
    assert result['collected_from'] == market.dates[0]
    assert result['bars'] == dict(written=4, skipped=4)
    assert result['last_complete_date'] == market.dates[-1]


def test_complete_date_never_moves_backwards(research_db):
    m.mark_stage_day(research_db, 'bars', dt.date(2024, 1, 5), 20)
    m.mark_stage_day(research_db, 'bars', dt.date(2024, 1, 3), 10)
    assert m.last_stage_day(research_db, 'bars') == dt.date(2024, 1, 5)
    assert len(m.SCHEMA.tables) == 4


def test_force_explicitly_reopens_history_after_cursor(research_db, market, incremental_run):
    incremental_run.run()
    market.calls.clear()
    result = incremental_run.run(end=market.dates[-2], force=True)
    assert result['collected_from'] == market.dates[0]
    assert result['bars']['written'] == 6
    assert result['last_complete_date'] == market.dates[-1]
    assert any(kw['date'] == market.dates[0] for name, kw in market.calls if name == 'listing')


@pytest.mark.parametrize('legacy_without_snapshot_marker', [False, True])
def test_existing_snapshots_skip_entire_stage_when_prices_are_empty(
        research_db, market, incremental_run, monkeypatch, capsys, legacy_without_snapshot_marker):
    m.collect_universe(research_db, market.dates)
    if legacy_without_snapshot_marker:
        with research_db.begin() as conn:
            conn.execute(m.TASKS.delete().where(m.TASKS.c.task_key.like('snapshot-day-v1:%')))
    assert not all_records(research_db, m.BARS)
    market.calls.clear()
    monkeypatch.setattr(m, 'collect_universe', lambda *a, **kw: pytest.fail('已有快照应整段跳过'))
    result = incremental_run.run()
    assert result['universe'] == dict(written=0, skipped=0, incomplete=0)
    assert result['bars']['written'] == 8
    assert all(name == 'price' for name, _ in market.calls)
    assert '整段跳过，不逐日检查' in capsys.readouterr().out


def test_snapshot_tail_resumes_independently_of_price_progress(
        research_db, market, incremental_run, monkeypatch):
    m.collect_universe(research_db, market.dates[:2])
    market.calls.clear()
    original = m.collect_universe
    visited = []
    def collect(engine, dates, **kwargs):
        visited.append(dates)
        return original(engine, dates, **kwargs)
    monkeypatch.setattr(m, 'collect_universe', collect)
    result = incremental_run.run()
    assert visited == [market.dates[2:]]
    assert result['universe']['written'] == 4
    assert result['bars']['written'] == 8
    assert all(kw['date'] in market.dates[2:] for name, kw in market.calls if name == 'listing')


def test_price_retry_does_not_recheck_completed_snapshot_days(
        research_db, market, incremental_run, monkeypatch):
    m.collect_universe(research_db, market.dates)
    original_api = m.api
    def fail_price(**kwargs):
        raise RuntimeError('模拟行情失败')
    monkeypatch.setattr(m, 'api', lambda name: fail_price if name == 'get_price' else original_api(name))
    # 不测试API重试的等待，只验证行情失败不会丢失独立快照进度。
    monkeypatch.setattr(m, 'retry', lambda function, **kwargs: function(**kwargs))
    with pytest.raises(RuntimeError, match='模拟行情失败'):
        incremental_run.run()
    assert m.resume_point(research_db, 'snapshot')[0] == market.dates[-1]
    assert m.last_stage_day(research_db, 'bars') is None  # 快照进度绝不能推进尚未完成的行情日期。
    monkeypatch.setattr(m, 'api', original_api)
    monkeypatch.setattr(m, 'collect_universe', lambda *a, **kw: pytest.fail('行情重试不应重跑快照'))
    result = incremental_run.run()
    assert result['bars']['written'] == 8


def test_shared_stage_helpers_keep_existing_checkpoint_keys(research_db):
    date = dt.date(2024, 1, 5)
    old_snapshot = 'snapshot-day-v1:' + m.task_key('snapshot_scope_v1', m.RULE_VERSION, m.MIN_MARKET_CAP)[:16] + ':'
    old_bars = 'complete-day-v1:' + m.task_key(
        'complete_scope_v1', m.RULE_VERSION, m.MIN_MARKET_CAP, ','.join(sorted(m.INDEX_CODES)))[:16] + ':'
    with research_db.begin() as conn:
        m.upsert(conn, m.TASKS, [dict(task_key=prefix + date.isoformat(), row_count=10,
            completed_at=dt.datetime.utcnow()) for prefix in [old_snapshot, old_bars]])
    assert m.last_stage_day(research_db, 'snapshot') == date
    assert m.last_stage_day(research_db, 'bars') == date
    assert m.stage_prefix('snapshot') == old_snapshot
    assert m.stage_prefix('bars') == old_bars


def test_snapshot_repair_preserves_known_fields_if_source_temporarily_missing(
        research_db, market, monkeypatch):
    date, code = market.dates[0], market.codes[0]
    m.collect_universe(research_db, [date])
    with research_db.begin() as conn:
        conn.execute(m.UNIVERSE.update().where(m.UNIVERSE.c.code == code).values(listed_date=None))
    market.statuses[(date, code)] = None
    original_api = m.api
    def fundamentals(**kwargs):
        return pd.DataFrame({'code': [code], 'market_cap': [None]})
    monkeypatch.setattr(m, 'api', lambda name: fundamentals if name == 'get_fundamentals' else original_api(name))
    stats = m.collect_universe(research_db, [date])
    row = next(row for row in all_records(research_db, m.UNIVERSE) if row['code'] == code)
    assert stats['written'] == 1
    assert row['is_st'] == 0 and row['market_cap'] == 50. and row['eligible'] == 1
    assert row['listed_date'] == dt.date(2000, 1, 1)
