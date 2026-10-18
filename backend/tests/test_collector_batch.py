"""Offline standalone batch orchestration/console tests; never contact JoinQuant or MySQL."""
import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / 'collectors' / '策略批量采集_数据库版.py'
FILES = {
    '行业动量': '行业动量计算_数据库版.py', '抱团拥挤度': '抱团拥挤度_数据库版.py',
    '微盘温度计': '微盘温度计_数据库版.py', 'ETF份额': 'ETF份额_数据库版.py',
    '净利润断层': '净利润断层_数据库版.py',
}
CONFIG = dict(host='fake.example.test', port=3306, dbname='fixture_database',
              dbuser='fixture_writer', dbpassword='secret/@ pass')


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def batch(monkeypatch, tmp_path):
    fake_platform(monkeypatch)
    module = load(TARGET, 'offline_collector_batch')
    return module


def fake_platform(monkeypatch):
    jq = ModuleType('jqdata')
    jq.jy = SimpleNamespace()
    jq.finance = SimpleNamespace()
    monkeypatch.setitem(sys.modules, 'jqdata', jq)
    monkeypatch.setitem(sys.modules, 'pandas', ModuleType('pandas'))
    return jq


def test_factories_isolate_state_and_use_shared_config(batch, monkeypatch):
    fake_platform(monkeypatch)
    marker = object()
    namespaces = [factory({'get_price': marker}, CONFIG) for factory in batch.STRATEGIES.values()]
    assert len({id(n.Base) for n in namespaces}) == len(batch.STRATEGIES)
    for ns in namespaces:
        if hasattr(ns, 'get_price'):
            assert ns.get_price is marker
        assert ns._config == CONFIG
    fresh = batch.STRATEGIES['净利润断层']({}, CONFIG)
    assert fresh._config == CONFIG
    assert namespaces[batch.STRATEGY_ORDER.index('净利润断层')].run is not fresh.run


def test_loader_supplies_notebook_platform_and_builtin_apis(batch, monkeypatch):
    jq = fake_platform(monkeypatch)
    marker = object()
    jq.get_price = 'platform default'
    monkeypatch.setattr(batch, 'jq', jq)
    import builtins
    monkeypatch.setattr(builtins, 'get_trade_days', 'builtin default', raising=False)
    received = []
    def factory(apis, config):
        received.append((apis, config))
        return SimpleNamespace(run=lambda: None)
    monkeypatch.setitem(batch.STRATEGIES, '行业动量', factory)
    batch._load_strategy('行业动量', CONFIG, {'get_price': marker})
    assert received[0][0]['get_price'] is marker
    assert received[0][0]['get_trade_days'] == 'builtin default'
    assert received[0][0]['finance'] is jq.finance
    assert received[0][1] == CONFIG


def test_calculation_and_model_definitions_keep_original_rules(batch):
    # Formulas/screening/schema remain identical; connection lifecycles are tested separately.
    tree = ast.parse(TARGET.read_text(encoding='utf-8'), feature_version=(3, 6))
    factories = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    compared = 0
    for name, filename in FILES.items():
        original = ast.parse((TARGET.parent/filename).read_text(encoding='utf-8'))
        body = factories[batch.STRATEGIES[name].__name__].body
        for node in original.body:
            if not isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                continue
            if node.name.startswith('Mysql') or node.name in ('as_date', 'china_now'):
                continue
            # Resource-owning functions have intentionally moved to common context managers.
            used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if 'Session' in used or 'engine' in used:
                continue
            actual = next(n for n in body if isinstance(n, type(node)) and n.name == node.name)
            class WithoutProgressOutput(ast.NodeTransformer):
                def visit_Expr(self, n):
                    if isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and n.value.func.id in ('print', '_progress_note'):
                        return None
                    return self.generic_visit(n)
                def visit_Call(self, n):
                    if isinstance(n.func, ast.Name) and n.func.id == '_progress_items':
                        return self.visit(n.args[0])
                    return self.generic_visit(n)
                def visit_If(self, n):
                    n = self.generic_visit(n)
                    if not n.body and not n.orelse: return None
                    if not n.body: n.body = [ast.Pass()]
                    return n
            import copy
            def normalized(n):
                return ast.dump(WithoutProgressOutput().visit(copy.deepcopy(n)), include_attributes=False)
            assert normalized(actual) == normalized(node), (name, node.name)
            compared += 1
    assert compared >= 35
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert all(n in tree.body for n in imports)
    assert batch.DB_CONFIG['dbpassword'] == 'your_password'


def test_pasted_notebook_without_file_or_sibling_files(batch, monkeypatch, tmp_path):
    fake_platform(monkeypatch)
    monkeypatch.chdir(tmp_path)
    source = TARGET.read_text(encoding='utf-8')
    assert '__file__' not in source and '_SOURCES' not in source
    assert 'importlib' not in source
    tree = ast.parse(source)
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id in ('exec', 'eval', 'compile', 'open') for n in ast.walk(tree))
    namespace = {'__name__': 'notebook_cell'}
    exec(compile(source, '<ipython-input-test>', 'exec'), namespace)
    assert len(namespace['STRATEGIES']) == 7
    # Construct all seven real strategies without accessing any project files.
    for name in namespace['STRATEGY_ORDER']:
        assert callable(namespace['_load_strategy'](name, CONFIG, {}))
    events = []
    def factory(name):
        def run(skip_existing=False):
            assert skip_existing is (name != '高股息选股')
            events.append(name)
            return []
        return lambda apis, config: SimpleNamespace(run=run)
    namespace['STRATEGIES'] = {n: factory(n) for n in namespace['STRATEGY_ORDER']}
    rows = namespace['run_all'](db_config=CONFIG)
    assert events == namespace['STRATEGY_ORDER']
    assert all(r['status'] == 'SUCCESS' for r in rows)
    assert not list(tmp_path.iterdir())


def test_serial_order_quiet_console_and_no_logs(batch, monkeypatch, tmp_path, capsys):
    events = []
    def loader(name, config, apis):
        assert config == CONFIG
        def run(**kwargs):
            events.append((name, 'start', kwargs))
            print('progress must be hidden')
            print('stderr must be hidden', file=sys.stderr)
            events.append((name, 'end', kwargs))
            return [1, 2]
        return run
    monkeypatch.setattr(batch, '_load_strategy', loader)
    monkeypatch.setattr(batch, 'any', lambda _: pytest.fail('shadowed any'), raising=False)
    monkeypatch.setattr(batch, 'sum', lambda _: pytest.fail('shadowed sum'), raising=False)
    options = {'行业动量': {'end_date': '2026-10-16'}}
    for _ in range(2):
        result = batch.run_all(options=options, db_config=CONFIG, preview_rows=0)
        assert [r['strategy'] for r in result] == batch.STRATEGY_ORDER
        assert all(r['status'] == 'SUCCESS' and r['rows'] == 2 for r in result)
    assert [(n, s) for n, s, _ in events] == [
        (n, s) for _ in range(2) for n in batch.STRATEGY_ORDER for s in ('start', 'end')]
    assert events[0][2] == dict(options['行业动量'], skip_existing=True)
    assert events[2][2] == {'skip_existing': True}
    assert options == {'行业动量': {'end_date': '2026-10-16'}}
    output = capsys.readouterr()
    assert 'SUCCESS | 2' in output.out and '新增 2 条' in output.out
    assert 'must be hidden' not in output.out and output.err == ''
    assert not list(tmp_path.iterdir())


def test_preview_only_displays_selected_fields_and_tail(batch, monkeypatch, capsys):
    class Frame:
        columns = ['code', 'name', 'events', 'prices']
        def __len__(self): return 20
        def __getitem__(self, columns):
            assert columns == ['code', 'name']
            return self
        def tail(self, rows):
            assert rows == 5
            return self
        def to_string(self, index):
            assert index is False
            return '600000.XSHG example'
    monkeypatch.setattr(batch, '_load_strategy', lambda *_: lambda **kw: Frame())
    summary = batch.run_all(['净利润断层'], db_config=CONFIG)
    output = capsys.readouterr().out
    assert '最后 5 条' in output and '600000.XSHG example' in output
    assert summary[0]['rows'] == 20


@pytest.mark.parametrize('raises', [True, False])
def test_failure_continues_and_marks_partial_completion(batch, monkeypatch, capsys, raises):
    called = []
    def loader(name, *_):
        def run(**kwargs):
            called.append(name)
            if name == '抱团拥挤度':
                raise RuntimeError('synthetic failure')
            return []
        return run
    monkeypatch.setattr(batch, '_load_strategy', loader)
    if raises:
        with pytest.raises(batch.BatchRunError) as caught:
            batch.run_all(db_config=CONFIG)
        result = caught.value.summary
    else:
        result = batch.run_all(db_config=CONFIG, raise_on_failure=False)
    assert called == batch.STRATEGY_ORDER
    assert result[1]['status'] == 'FAILED' and result[1]['rows'] is None
    assert result[-1]['status'] == 'SUCCESS'
    output = capsys.readouterr().out
    assert '成功 6，失败 1' in output and '未完成' in output and 'synthetic failure' in output


def test_strategy_initialization_failure_does_not_stop_later_strategy(batch, monkeypatch, capsys):
    def loader(name, *args):
        if name == '行业动量':
            raise ImportError('platform dependency unavailable')
        return lambda **kw: []
    monkeypatch.setattr(batch, '_load_strategy', loader)
    rows = batch.run_all(['行业动量', 'ETF份额'], db_config=CONFIG, raise_on_failure=False)
    assert [r['status'] for r in rows] == ['FAILED', 'SUCCESS']
    assert rows[0]['error_type'] == 'ImportError'
    assert 'ImportError' in capsys.readouterr().out


def test_credentials_hidden_in_results_and_console(batch, monkeypatch, capsys):
    from urllib.parse import quote, quote_plus
    secret = CONFIG['dbpassword']
    variants = [secret, quote(secret, safe=''), quote_plus(secret), CONFIG['host'], CONFIG['dbname'], CONFIG['dbuser']]
    def loader(*_):
        def run(**kw):
            print(secret)
            raise RuntimeError('mysql://other:other-secret@host ' + ' '.join(variants) + " password='unrelated-secret'")
        return run
    monkeypatch.setattr(batch, '_load_strategy', loader)
    rows = batch.run_all(['ETF份额'], db_config=CONFIG, raise_on_failure=False)
    output = capsys.readouterr().out + str(rows)
    for value in variants + ['other-secret', 'unrelated-secret']:
        assert value not in output
    assert '已隐藏' in output


def test_interrupt_restores_streams_and_releases_lock(batch, monkeypatch):
    streams = sys.stdout, sys.stderr
    called = []
    def loader(name, *_):
        called.append(name)
        def run(**kw): raise KeyboardInterrupt()
        return run
    monkeypatch.setattr(batch, '_load_strategy', loader)
    with pytest.raises(KeyboardInterrupt): batch.run_all(db_config=CONFIG)
    assert called == ['行业动量']
    assert (sys.stdout, sys.stderr) == streams
    with batch._batch_guard(): pass


def test_lock_prevents_overlapping_runs(batch, tmp_path):
    with batch._batch_guard():
        with pytest.raises(RuntimeError, match='已有采集任务'):
            with batch._batch_guard(): pytest.fail('overlapping execution')
    with batch._batch_guard(): pass


@pytest.mark.parametrize('kwargs', [
    {'strategy_names': []}, {'strategy_names': ['ETF份额', 'ETF份额']},
    {'strategy_names': ['unknown']}, {'options': {'ETF份额': {'skip_existing': False}}},
    {'options': {'unknown': {}}}, {'preview_rows': -1}, {'db_config': {}},
])
def test_invalid_options_fail_before_any_execution(batch, monkeypatch, tmp_path, kwargs):
    monkeypatch.setattr(batch, '_load_strategy', lambda *_: pytest.fail('must not execute'))
    with pytest.raises(ValueError): batch.run_all(**kwargs)
    assert not list(tmp_path.iterdir())


def test_isolated_etf_api_validation_uses_strategy_scope(batch, monkeypatch):
    fake_platform(monkeypatch)
    table = SimpleNamespace(code='code', date='date', shares='shares')
    ns = batch.STRATEGIES['ETF份额']({
        'finance': SimpleNamespace(FUND_SHARE_DAILY=table),
        'query': lambda *args: None, 'get_trade_days': lambda **kw: [],
    }, CONFIG)
    assert 'finance' not in ns.share_table.__globals__
    assert ns.share_table() is table
    missing = batch.STRATEGIES['ETF份额']({}, CONFIG)
    with pytest.raises(ValueError, match='缺少聚宽'):
        missing.share_table()


def test_standalone_earnings_insert_only_preserves_first_snapshot(batch, monkeypatch):
    import datetime as dt
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    fake_platform(monkeypatch)
    ns = batch.STRATEGIES['净利润断层']({}, CONFIG)
    engine = create_engine('sqlite://')
    ns.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    period, day = dt.date(2026, 9, 30), dt.date(2026, 10, 16)
    first = dict(report_period=period, code='600001.XSHG', name='first snapshot',
                 signal_date=day, announcement_date=day, signal_source='formal',
                 profit_basis='single quarter', profit_yoy=25, net_profit=1e8,
                 open_gap_pct=4, intraday_pct=0, market_cap=50, pe_ttm=10,
                 valuation_date=day, events=[], prices=[])
    try:
        assert len(ns.save_results(Session, period, day, 6000, [first], skip_existing=True)) == 1
        second = dict(first, code='600002.XSHG')
        fresh = ns.save_results(Session, period, day, 6000,
                                [dict(first, name='changed', pe_ttm=99), second], skip_existing=True)
        assert [r['code'] for r in fresh] == [second['code']]
        writes = []
        def capture(conn, cursor, statement, *args):
            if statement.lstrip().split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE'):
                writes.append(statement)
        event.listen(engine, 'before_cursor_execute', capture)
        assert ns.save_results(Session, period, day, 6000, [first, second], skip_existing=True) == []
        assert not writes
        with Session() as session:
            stored = session.get(ns.EarningsStock, (period, first['code']))
            assert stored.name == 'first snapshot' and stored.pe_ttm == 10
            assert session.get(ns.EarningsRun, period).matched_count == 2
    finally:
        engine.dispose()


@pytest.mark.parametrize('name,changes', [
    ('行业动量', {'start_date': '2025-02-01', 'end_date': '2026-10-16', 'industry_type': 'sw_l1'}),
    ('抱团拥挤度', {'start_date': '2024-01-01', 'top_pct': 0.1}),
    ('微盘温度计', {'start_date': '2022-01-01', 'micro_num': 500, 'sample_step': 10}),
    ('ETF份额', {'start_date': '2025-01-01', 'lookback_days': 7}),
    ('净利润断层', {'end_date': '2026-10-16'}),
])
def test_grouped_configuration_changes_run_defaults(batch, name, changes):
    import inspect
    batch.STRATEGY_CONFIG[name].update(changes)
    strategy = batch.STRATEGIES[name]({}, CONFIG)
    defaults = {k: p.default for k, p in inspect.signature(strategy.run).parameters.items()}
    assert all(defaults[k] == v for k, v in changes.items())


def test_internal_limits_and_fund_lists_use_central_config(batch):
    batch.STRATEGY_CONFIG['行业动量']['retention_trade_days'] = 200
    batch.STRATEGY_CONFIG['微盘温度计']['batch_size'] = 500
    batch.STRATEGY_CONFIG['ETF份额'].update(initial_trade_days=250, window_size=60)
    batch.STRATEGY_CONFIG['净利润断层']['page_size'] = 200
    namespaces = {name: factory({}, CONFIG) for name, factory in batch.STRATEGIES.items()}
    assert namespaces['行业动量'].RETENTION_TRADE_DAYS == 200
    assert namespaces['微盘温度计'].BATCH_SIZE == 500
    assert namespaces['ETF份额'].INITIAL_TRADE_DAYS == 250
    assert namespaces['ETF份额'].WINDOW_SIZE == 60
    assert namespaces['净利润断层'].PAGE_SIZE == 200
    namespaces['ETF份额'].ETF_GROUPS['hs300'].clear()
    assert len(batch.STRATEGY_CONFIG['ETF份额']['funds']['hs300']) == 5


@pytest.mark.parametrize('failures,interrupt', [(0, False), (2, False), (3, False), (0, True)])
def test_shared_connection_retry_and_failure_cleanup(batch, monkeypatch, failures, interrupt):
    from contextlib import nullcontext
    seen = []
    class Engine:
        attempts = 0
        disposed = 0
        def connect(self):
            self.attempts += 1
            if interrupt: raise KeyboardInterrupt()
            if self.attempts <= failures: raise RuntimeError('synthetic connection failure')
            return nullcontext()
        def dispose(self): self.disposed += 1
    engine = Engine()
    def create(url, **kwargs):
        assert url.password == CONFIG['dbpassword']
        assert kwargs['connect_args']['connect_timeout'] == 10
        seen.append(kwargs)
        return engine
    monkeypatch.setattr(batch, 'create_engine', create)
    monkeypatch.setattr(batch.time, 'sleep', lambda seconds: None)
    batch.DB_OPTIONS['retry_count'] = 3
    if interrupt:
        with pytest.raises(KeyboardInterrupt): batch.connect_database(CONFIG)
    elif failures == 3:
        with pytest.raises(RuntimeError, match='MySQL'): batch.connect_database(CONFIG)
    else:
        assert batch.connect_database(CONFIG) is engine
    assert len(seen) == 1
    assert engine.attempts == (1 if interrupt else min(failures+1, 3))
    assert engine.disposed == (1 if interrupt or failures == 3 else 0)


@pytest.mark.parametrize('failure', ['none', 'create', 'body', 'interrupt'])
def test_database_scope_always_disposes(batch, monkeypatch, failure):
    disposed = []
    engine = SimpleNamespace(dispose=lambda: disposed.append(True))
    monkeypatch.setattr(batch, 'connect_database', lambda config: engine)
    def create_all(bind):
        assert bind is engine
        if failure == 'create': raise ValueError('table creation failed')
    table = SimpleNamespace(dialect_options={'mysql': {}})
    base = SimpleNamespace(metadata=SimpleNamespace(tables={'test': table}, create_all=create_all))
    factory = object()
    monkeypatch.setattr(batch, 'sessionmaker', lambda bind: factory)
    def use():
        with batch.database_scope(base, CONFIG) as Session:
            assert Session is factory
            if failure == 'body': raise ValueError('body failure')
            if failure == 'interrupt': raise KeyboardInterrupt()
    if failure == 'none': use()
    else:
        with pytest.raises(KeyboardInterrupt if failure == 'interrupt' else ValueError): use()
    assert disposed == [True]
    assert table.dialect_options['mysql']['engine'] == 'InnoDB'


@pytest.mark.parametrize('failure', ['none', 'error', 'interrupt'])
def test_session_scope_closes_rolls_back_without_implicit_commit(batch, failure):
    events = []
    session = SimpleNamespace(close=lambda: events.append('close'), rollback=lambda: events.append('rollback'),
                              commit=lambda: events.append('commit'))
    def use():
        with batch.session_scope(lambda: session) as current:
            assert current is session
            if failure == 'error': raise ValueError()
            if failure == 'interrupt': raise KeyboardInterrupt()
    if failure == 'none': use()
    else:
        with pytest.raises(KeyboardInterrupt if failure == 'interrupt' else ValueError): use()
    assert events == (['close'] if failure == 'none' else ['rollback', 'close'])


@pytest.mark.parametrize('name,key', [
    ('行业动量', 'retention_trade_days'), ('微盘温度计', 'batch_size'),
    ('ETF份额', 'initial_trade_days'), ('ETF份额', 'window_size'), ('净利润断层', 'page_size'),
])
def test_invalid_loop_limits_fail_before_connecting(batch, monkeypatch, name, key):
    batch.STRATEGY_CONFIG[name][key] = 0
    monkeypatch.setattr(batch, 'connect_database', lambda *_: pytest.fail('must not connect'))
    with pytest.raises(ValueError, match=key):
        batch.STRATEGIES[name]({}, CONFIG)


@pytest.mark.parametrize('name,model_name', [
    ('行业动量', 'IndustryMomentum'), ('抱团拥挤度', 'MarketCrowding'),
    ('微盘温度计', 'MicrocapTemperature'), ('ETF份额', 'ETFShare'),
])
def test_shared_sessions_keep_insert_only_across_strategies(batch, monkeypatch, name, model_name):
    import datetime as dt
    from decimal import Decimal
    from sqlalchemy import create_engine, event, Date, DateTime, Integer, Numeric
    strategy = batch.STRATEGIES[name]({}, CONFIG)
    model = getattr(strategy, model_name)
    day = dt.date(2026, 10, 16)
    record = {}
    for column in model.__table__.columns:
        if isinstance(column.type, DateTime): record[column.name] = dt.datetime(2026, 10, 16, 17)
        elif isinstance(column.type, Date): record[column.name] = day
        elif isinstance(column.type, Integer): record[column.name] = 1
        elif isinstance(column.type, Numeric): record[column.name] = Decimal('0.5')
        else: record[column.name] = 'fixture'
    record.update({
        '行业动量': {'industry_type': 'sw_l2'},
        '抱团拥挤度': {'top_pct': Decimal('0.05')},
        '微盘温度计': {'member_codes': '[]'},
        'ETF份额': {'previous_trade_date': day-dt.timedelta(days=1)},
    }[name])
    def save(Session):
        if name == '行业动量': return strategy.save_day(Session, day, 'sw_l2', [record], skip_existing=True)
        if name == '抱团拥挤度': return strategy.save_day(Session, day, 0.05, record, skip_existing=True)
        if name == '微盘温度计': return strategy.save_period(Session, record, skip_existing=True)
        return strategy.save_window(Session, record['fund_code'], [record], skip_existing=True)
    engine = create_engine('sqlite://')
    monkeypatch.setattr(batch, 'connect_database', lambda _: engine)
    with batch.database_scope(strategy.Base, CONFIG) as Session:
        assert save(Session)
        writes = []
        def capture(conn, cursor, statement, *args):
            if statement.lstrip().split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE'):
                writes.append(statement)
        event.listen(engine, 'before_cursor_execute', capture)
        assert not save(Session)
        assert not writes
        with Session() as session:
            assert session.query(model).count() == 1


@pytest.mark.parametrize('name', ['行业动量', '抱团拥挤度', '微盘温度计', 'ETF份额', '净利润断层', '微盘新高新低'])
def test_progress_visible_through_quiet_stream_for_each_strategy(batch, monkeypatch, capsys, name):
    processed = []
    def run(**kwargs):
        for day in batch._progress_items(['2026-10-15', '2026-10-16'], '交易日'):
            batch._progress_note('计算并写入')
            print('internal details must stay hidden')
            processed.append(day)
        return [1]
    monkeypatch.setattr(batch, '_load_strategy', lambda *_: run)
    result = batch.run_all([name], db_config=CONFIG, preview_rows=0)
    output = capsys.readouterr().out
    assert '【1/1 ' + name + '】' in output
    assert '0/2 0%' in output and '2/2 100%' in output and '2026-10-16' in output
    assert '新增 1 条' in output and 'internal details' not in output
    assert len(processed) == 2 and result[0]['rows'] == 1
    assert batch._PROGRESS_LOCAL.current is None


def test_heartbeat_refreshes_during_blocking_call_and_stops(batch, monkeypatch):
    import io
    import threading
    signalled = threading.Event()
    class Stream(io.StringIO):
        def write(self, text):
            value = super().write(text)
            if '等待模拟行情接口' in text:
                signalled.set()
            return value
    stream = Stream()
    monkeypatch.setattr(sys, 'stdout', stream)
    monkeypatch.setattr(batch, 'PROGRESS_INTERVAL_SECONDS', .01)
    reports = []
    real = batch._ConsoleProgress
    def capture(*args):
        report = real(*args)
        reports.append(report)
        return report
    monkeypatch.setattr(batch, '_ConsoleProgress', capture)
    caller = threading.get_ident()
    def run(**kwargs):
        assert threading.get_ident() == caller
        batch._progress_note('等待模拟行情接口')
        assert signalled.wait(2), 'heartbeat did not refresh while collector was blocked'
        return []
    monkeypatch.setattr(batch, '_load_strategy', lambda *_: run)
    batch.run_all(['行业动量'], db_config=CONFIG)
    assert '已用' in stream.getvalue() and '无新增数据' in stream.getvalue()
    assert reports[0].stopped.is_set() and not reports[0].thread.is_alive()
    assert sys.stdout is stream and batch._PROGRESS_LOCAL.current is None


@pytest.mark.parametrize('error', [RuntimeError('source unavailable'), KeyboardInterrupt()])
def test_failed_item_never_counts_as_completed_and_thread_stops(batch, monkeypatch, capsys, error):
    reports = []
    def run(**kwargs):
        report = batch._PROGRESS_LOCAL.current
        reports.append(report)
        for index in batch._progress_items(range(3), '交易日'):
            if index == 1:
                assert report.frames[-1]['done'] == 1
                report.render(force=True)
                raise error
        return []
    monkeypatch.setattr(batch, '_load_strategy', lambda *_: run)
    if isinstance(error, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            batch.run_all(['行业动量'], db_config=CONFIG)
    else:
        result = batch.run_all(['行业动量'], db_config=CONFIG, raise_on_failure=False)
        assert result[0]['status'] == 'FAILED'
    output = capsys.readouterr().out
    assert '1/3 33%' in output and '100%' not in output
    assert '未完成' in output or '已中断' in output
    assert not reports[0].thread.is_alive() and batch._PROGRESS_LOCAL.current is None
    with batch._batch_guard():
        pass


def test_progress_disabled_keeps_results_and_no_progress_thread(batch, monkeypatch, capsys):
    monkeypatch.setattr(batch, 'PROGRESS_ENABLED', False)
    monkeypatch.setattr(batch, '_ConsoleProgress', lambda *a: pytest.fail('must not start progress'))
    def run(**kwargs):
        batch._progress_note('hidden stage')
        assert list(batch._progress_items([1, 2], 'hidden loop')) == [1, 2]
        return []
    monkeypatch.setattr(batch, '_load_strategy', lambda *_: run)
    batch.run_all(['行业动量'], db_config=CONFIG)
    output = capsys.readouterr().out
    assert 'SUCCESS' in output and 'hidden' not in output and '[处理中]' not in output


def test_progress_output_failure_does_not_change_collection(batch, monkeypatch):
    import io
    class Stream(io.StringIO):
        def write(self, text):
            if text.startswith('\r'):
                raise OSError('simulated notebook display failure')
            return super().write(text)
    stream = Stream()
    monkeypatch.setattr(sys, 'stdout', stream)
    monkeypatch.setattr(batch, '_load_strategy', lambda *_: lambda **kw: list(batch._progress_items([1], '日期')))
    result = batch.run_all(['行业动量'], db_config=CONFIG, preview_rows=0)
    assert result[0]['rows'] == 1 and result[0]['status'] == 'SUCCESS'
    assert 'display failure' not in stream.getvalue()


def test_progress_throttles_frequent_updates_and_handles_empty_work(batch, monkeypatch, capsys):
    import io
    clock = [0.0]
    monkeypatch.setattr(batch.time, 'monotonic', lambda: clock[0])
    stream = io.StringIO()
    report = batch._ConsoleProgress('行业动量', 1, 1, stream, 5)
    report.render(force=True)
    for i in range(100):
        report.note = 'stage ' + str(i)
        report.render()
    assert stream.getvalue().count('\r') == 1
    clock[0] = 5
    report.render()
    assert stream.getvalue().count('\r') == 2
    monkeypatch.setattr(batch, '_load_strategy', lambda *_: lambda **kw: list(batch._progress_items([], '交易日')))
    batch.run_all(['行业动量'], db_config=CONFIG)
    output = capsys.readouterr().out
    assert '0/0 无待处理项' in output and '无新增数据' in output


@pytest.mark.parametrize('interval', [0, -1, True, '5', float('inf'), float('nan')])
def test_invalid_progress_interval_fails_before_collection(batch, monkeypatch, interval):
    monkeypatch.setattr(batch, 'PROGRESS_INTERVAL_SECONDS', interval)
    monkeypatch.setattr(batch, '_load_strategy', lambda *_: pytest.fail('must not collect'))
    with pytest.raises(ValueError, match='PROGRESS_INTERVAL_SECONDS'):
        batch.run_all(['行业动量'], db_config=CONFIG)


def test_progress_does_not_round_incomplete_work_to_100_percent(batch):
    import io
    stream = io.StringIO()
    report = batch._ConsoleProgress('微盘新高新低', 1, 1, stream, 5)
    report.frames.append(dict(label='交易日', total=400, done=399, item='2026-10-16'))
    report.render(force=True)
    assert '399/400 99%' in stream.getvalue() and '100%' not in stream.getvalue()


def test_progress_thread_unavailable_falls_back_without_failing_strategy(batch, monkeypatch, capsys):
    def unavailable(*args):
        raise RuntimeError('thread creation not available')
    monkeypatch.setattr(batch.threading.Thread, 'start', unavailable)
    monkeypatch.setattr(batch, '_load_strategy', lambda *_: lambda **kw: list(batch._progress_items([1], '日期')))
    result = batch.run_all(['行业动量'], db_config=CONFIG, preview_rows=0)
    assert result[0]['status'] == 'SUCCESS' and result[0]['rows'] == 1
    assert '按任务推进刷新' in capsys.readouterr().out


def test_latest_dividend_mode_does_not_receive_insert_only_flag(batch, monkeypatch, capsys):
    received=[]
    def loader(name,*args):
        def run(**kwargs):received.append((name,kwargs));return [1,2]
        return run
    monkeypatch.setattr(batch,'_load_strategy',loader)
    batch.run_all(['高股息选股','行业动量'],preview_rows=0)
    assert received==[('高股息选股',{}),('行业动量',{'skip_existing':True})]
    output=capsys.readouterr().out
    assert '当前选股结果 2 条' in output and '新增 2 条' in output
