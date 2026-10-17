"""Offline batch module orchestration/console tests; never contact JoinQuant or MySQL."""
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
    monkeypatch.setattr(module, '_MODULE_DIR', tmp_path)
    return module


def test_real_loader_isolates_globals_and_injects_config_and_notebook_apis(batch, monkeypatch):
    monkeypatch.setattr(batch, '_MODULE_DIR', TARGET.parent)
    jq = ModuleType('jqdata')
    jq.jy = SimpleNamespace()
    jq.finance = SimpleNamespace()
    pd = ModuleType('pandas')
    monkeypatch.setitem(sys.modules, 'jqdata', jq)
    monkeypatch.setitem(sys.modules, 'pandas', pd)
    marker = object()
    apis = dict(get_price=marker, __name__='__main__', Base='old Base', run='old run')
    runs = [batch._load_strategy(name, CONFIG, apis) for name in batch.STRATEGY_ORDER]
    bases = []
    for name, run in zip(batch.STRATEGY_ORDER, runs):
        env = run.__globals__
        assert env['get_price'] is marker
        assert env['__name__'] != '__main__'
        connector = env[batch.STRATEGIES[name][1]]
        assert {key: getattr(connector, key) for key in CONFIG} == CONFIG
        bases.append(env['Base'])
    assert len(set(bases)) == 5
    assert len({id(run.__globals__) for run in runs}) == 5
    assert apis['run'] == 'old run' and apis['Base'] == 'old Base'


def test_entry_uses_external_modules_and_python36_syntax(batch):
    source = TARGET.read_text(encoding='utf-8')
    ast.parse(source, feature_version=(3, 6))
    assert '_SOURCES' not in source and 'exec(compile(' not in source
    assert not (ROOT/'scripts/build_collector_batch.py').exists()
    for name, filename in FILES.items():
        assert batch.STRATEGIES[name][0] == filename
        ast.parse((TARGET.parent/filename).read_text(encoding='utf-8'), feature_version=(3, 6))
    assert batch.DB_CONFIG['dbpassword'] == 'your_password'


def test_loader_rereads_modules_on_repeat(batch, tmp_path):
    filename, connector = batch.STRATEGIES['ETF份额']
    path = tmp_path/filename
    for value in (1, 12345):
        path.write_text('class {}: pass\ndef run(): return {}, get_price\n'.format(connector, value), encoding='utf-8')
        run = batch._load_strategy('ETF份额', CONFIG, {'get_price': 'notebook api', 'unexpected': 'do not inject'})
        assert run() == (value, 'notebook api')
        assert 'unexpected' not in run.__globals__


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
    assert {p.name for p in tmp_path.iterdir()} == {'.collector-batch.lock'}


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


def test_missing_module_does_not_stop_later_strategy(batch, monkeypatch, capsys):
    real = batch._load_strategy
    def loader(name, *args):
        if name == '行业动量':
            return real(name, *args)
        return lambda **kw: []
    monkeypatch.setattr(batch, '_load_strategy', loader)
    rows = batch.run_all(['行业动量', 'ETF份额'], db_config=CONFIG, raise_on_failure=False)
    assert [r['status'] for r in rows] == ['FAILED', 'SUCCESS']
    assert rows[0]['error_type'] == 'FileNotFoundError'
    assert 'FileNotFoundError' in capsys.readouterr().out


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
    with batch._BatchLock(batch._MODULE_DIR): pass


def test_lock_prevents_overlapping_runs(batch, tmp_path):
    with batch._BatchLock(tmp_path):
        with pytest.raises(RuntimeError, match='采集锁'):
            with batch._BatchLock(tmp_path): pytest.fail('overlapping execution')
    with batch._BatchLock(tmp_path): pass


@pytest.mark.parametrize('kwargs', [
    {'strategy_names': []}, {'strategy_names': ['ETF份额', 'ETF份额']},
    {'strategy_names': ['unknown']}, {'options': {'ETF份额': {'skip_existing': False}}},
    {'options': {'unknown': {}}}, {'preview_rows': -1}, {'db_config': {}},
])
def test_invalid_options_fail_before_any_execution(batch, monkeypatch, tmp_path, kwargs):
    monkeypatch.setattr(batch, '_load_strategy', lambda *_: pytest.fail('must not execute'))
    with pytest.raises(ValueError): batch.run_all(**kwargs)
    assert not list(tmp_path.iterdir())
