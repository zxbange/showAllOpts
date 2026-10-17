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
    assert len({id(n.Base) for n in namespaces}) == 5
    for ns in namespaces:
        if hasattr(ns, 'get_price'):
            assert ns.get_price is marker
        connector = next(v for k, v in vars(ns).items() if k.startswith('Mysql'))
        assert {key: getattr(connector, key) for key in CONFIG} == CONFIG
    fresh = batch.STRATEGIES['净利润断层']({}, CONFIG)
    namespaces[-1].MysqlEarnings.host = 'modified'
    assert fresh.MysqlEarnings.host == CONFIG['host']
    assert namespaces[-1].run is not fresh.run


def test_loader_supplies_notebook_platform_and_builtin_apis(batch, monkeypatch):
    jq = fake_platform(monkeypatch)
    marker = object()
    jq.get_price = 'platform default'
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


def test_standalone_business_definitions_match_individual_collectors(batch):
    # Moving functions into an isolated scope must not change screening or database rules.
    import copy
    tree = ast.parse(TARGET.read_text(encoding='utf-8'), feature_version=(3, 6))
    factories = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    def normalized(node):
        node = copy.deepcopy(node)
        if isinstance(node, ast.ClassDef) and node.name.startswith('Mysql'):
            node.body = [n for n in node.body if not isinstance(n, ast.Assign)]
        return ast.dump(node, include_attributes=False)
    for name, filename in FILES.items():
        original = ast.parse((TARGET.parent/filename).read_text(encoding='utf-8'))
        body = factories[batch.STRATEGIES[name].__name__].body
        for node in original.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                actual = next(n for n in body if isinstance(n, type(node)) and n.name == node.name)
                assert normalized(actual) == normalized(node), (name, node.name)
            elif isinstance(node, ast.Assign):
                assert any(normalized(n) == normalized(node) for n in body)
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
    assert len(namespace['STRATEGIES']) == 5
    # Construct all five real strategies without accessing any project files.
    for name in namespace['STRATEGY_ORDER']:
        assert callable(namespace['_load_strategy'](name, CONFIG, {}))
    events = []
    def factory(name):
        def run(skip_existing=False):
            assert skip_existing is True
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
    assert '成功 4，失败 1' in output and '未完成' in output and 'synthetic failure' in output


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
