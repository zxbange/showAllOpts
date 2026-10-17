"""Offline batch orchestration/logging tests; never contact JoinQuant or MySQL."""
import ast
import datetime
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import quote, quote_plus

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
def batch():
    return load(TARGET, 'offline_collector_batch')


def content(directory):
    return '\n'.join(p.read_text(encoding='utf-8') for p in sorted(directory.glob('collector-batch-*.log')))


def test_generated_bundle_matches_sources_and_python36_syntax(batch):
    builder = load(ROOT / 'scripts' / 'build_collector_batch.py', 'batch_builder')
    assert TARGET.read_text(encoding='utf-8') == builder.render()
    ast.parse(TARGET.read_text(encoding='utf-8'), feature_version=(3, 6))
    assert list(batch._SOURCES) == batch.STRATEGY_ORDER
    for name, filename in FILES.items():
        assert batch._SOURCES[name] == (ROOT / 'collectors' / filename).read_text(encoding='utf-8')
        ast.parse(batch._SOURCES[name], feature_version=(3, 6))
    assert batch.DB_CONFIG['dbpassword'] == 'your_password'


def test_real_loader_isolates_globals_and_injects_config_and_notebook_apis(batch, monkeypatch):
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
        connector = env[batch._STRATEGY_CLASSES[name]]
        assert {key: getattr(connector, key) for key in CONFIG} == CONFIG
        bases.append(env['Base'])
    assert len(set(bases)) == 5
    assert len({id(run.__globals__) for run in runs}) == 5
    assert apis['run'] == 'old run' and apis['Base'] == 'old Base'


def test_serial_order_labeled_logs_parameters_and_repeated_runs(batch, monkeypatch, tmp_path, capsys):
    events = []
    def loader(name, config, apis):
        assert config == CONFIG
        def run(**kwargs):
            events.append((name, 'start', kwargs))
            print('正在计算', end='')
            print('，入库完成。')
            print('源数据警告', file=sys.stderr)
            print('最后一行', end='')
            events.append((name, 'end', kwargs))
            return [1, 2]
        return run
    monkeypatch.setattr(batch, '_load_strategy', loader)
    # Simulate a JoinQuant notebook where numpy.any/sum shadow Python builtins.
    monkeypatch.setattr(batch, 'any', lambda _: pytest.fail('shadowed any called'), raising=False)
    monkeypatch.setattr(batch, 'sum', lambda _: pytest.fail('shadowed sum called'), raising=False)
    options = {'行业动量': {'end_date': '2026-10-16'}}
    for _ in range(2):
        result = batch.run_all(options=options, db_config=CONFIG, log_dir=tmp_path)
        assert [r['strategy'] for r in result] == batch.STRATEGY_ORDER
        assert all(r['status'] == 'SUCCESS' and r['rows'] == 2 for r in result)
    assert [(name, stage) for name, stage, _ in events] == [
        (name, stage) for _ in range(2) for name in batch.STRATEGY_ORDER for stage in ('start', 'end')]
    assert events[0][2] == dict(options['行业动量'], skip_existing=True)
    assert events[2][2] == {'skip_existing': True}
    output = capsys.readouterr().out
    saved = content(tmp_path)
    assert output == saved
    assert 'SUCCESS | 2' in saved and '本次新增' in saved
    assert '| WARNING |' in saved
    assert '| 行业动量 | 正在计算，入库完成。' in saved
    assert saved.count('| ETF份额 | 最后一行') == 2
    assert all('+08:00 | ' in line and ' | run=' in line for line in saved.splitlines())


@pytest.mark.parametrize('raises', [True, False])
def test_failure_continues_and_summary_marks_partial_completion(batch, monkeypatch, tmp_path, raises):
    called = []
    def loader(name, *_):
        def run(**kwargs):
            called.append(name)
            print('一个窗口已提交')
            if name == '抱团拥挤度':
                raise RuntimeError('synthetic failure')
            return []
        return run
    monkeypatch.setattr(batch, '_load_strategy', loader)
    kwargs = dict(db_config=CONFIG, log_dir=tmp_path, raise_on_failure=raises)
    if raises:
        with pytest.raises(batch.BatchRunError) as caught:
            batch.run_all(**kwargs)
        result = caught.value.summary
    else:
        result = batch.run_all(**kwargs)
    assert called == batch.STRATEGY_ORDER
    assert result[1]['status'] == 'FAILED' and result[1]['rows'] is None
    assert result[-1]['status'] == 'SUCCESS'
    assert '成功 4，失败 1' in content(tmp_path)
    assert '未完成，详见过程日志' in content(tmp_path)


def test_loading_failure_is_attributed_and_later_strategy_runs(batch, monkeypatch, tmp_path):
    def loader(name, *_):
        if name == '行业动量':
            raise ImportError('mock missing dependency')
        return lambda **kwargs: []
    monkeypatch.setattr(batch, '_load_strategy', loader)
    result = batch.run_all(['行业动量', 'ETF份额'], db_config=CONFIG, log_dir=tmp_path, raise_on_failure=False)
    assert [r['status'] for r in result] == ['FAILED', 'SUCCESS']
    assert '| 行业动量 | ImportError:' in content(tmp_path)


def test_secrets_redacted_from_prints_exceptions_and_file(batch, monkeypatch, tmp_path, capsys):
    secret = CONFIG['dbpassword']
    def loader(*_):
        def run(**kwargs):
            for value in (secret, quote(secret, safe=''), quote_plus(secret)):
                print(value)
            print("password='unrelated-secret'")
            raise RuntimeError('mysql://other:other-secret@host {} {} {}'.format(CONFIG['host'], CONFIG['dbname'], CONFIG['dbuser']))
        return run
    monkeypatch.setattr(batch, '_load_strategy', loader)
    batch.run_all(['ETF份额'], db_config=CONFIG, log_dir=tmp_path, raise_on_failure=False)
    output = capsys.readouterr().out + content(tmp_path)
    for value in (secret, quote(secret, safe=''), quote_plus(secret), CONFIG['host'], CONFIG['dbname'], CONFIG['dbuser'], 'unrelated-secret', 'other-secret'):
        assert value not in output
    assert '[REDACTED]' in output


def test_daily_files_follow_beijing_date_and_append(batch, monkeypatch, tmp_path):
    current = [datetime.datetime(2026, 10, 17, 23, 59, 59)]
    monkeypatch.setattr(batch, '_batch_now', lambda: current[0])
    log = batch._BatchLog(tmp_path, CONFIG)
    try:
        log.write('INFO', 'ETF份额', 'before midnight')
        current[0] = datetime.datetime(2026, 10, 18, 0, 0, 1)
        log.write('INFO', 'ETF份额', 'after midnight')
    finally:
        log.close()
    next_log = batch._BatchLog(tmp_path, CONFIG)
    next_log.write('INFO', '批次', 'next run')
    next_log.close()
    first = (tmp_path/'collector-batch-2026-10-17.log').read_text(encoding='utf-8')
    second = (tmp_path/'collector-batch-2026-10-18.log').read_text(encoding='utf-8')
    assert 'before midnight' in first and 'after midnight' not in first
    assert 'after midnight' in second and 'next run' in second


def test_interrupt_stops_remaining_strategies_and_restores_streams(batch, monkeypatch, tmp_path):
    old_stdout, old_stderr = sys.stdout, sys.stderr
    called = []
    def loader(name, *_):
        called.append(name)
        def run(**kwargs):
            raise KeyboardInterrupt()
        return run
    monkeypatch.setattr(batch, '_load_strategy', loader)
    with pytest.raises(KeyboardInterrupt):
        batch.run_all(db_config=CONFIG, log_dir=tmp_path)
    assert called == ['行业动量']
    assert sys.stdout is old_stdout and sys.stderr is old_stderr
    with batch._BatchLock(tmp_path):
        pass  # Interrupt releases lock.
    assert '用户中断' in content(tmp_path)


def test_lock_prevents_overlap_and_is_reusable(batch, tmp_path):
    with batch._BatchLock(tmp_path):
        with pytest.raises(RuntimeError, match='采集锁'):
            with batch._BatchLock(tmp_path):
                pytest.fail('overlapping execution')
    with batch._BatchLock(tmp_path):
        pass


@pytest.mark.parametrize('names', [[], ['ETF份额', 'ETF份额'], ['unknown']])
def test_invalid_selection_rejected_before_execution(batch, monkeypatch, tmp_path, names):
    monkeypatch.setattr(batch, '_load_strategy', lambda *_: pytest.fail('must not execute'))
    with pytest.raises(ValueError):
        batch.run_all(names, db_config=CONFIG, log_dir=tmp_path)
    assert not list(tmp_path.iterdir())


def test_invalid_log_path_prevents_execution(batch, monkeypatch, tmp_path):
    invalid = tmp_path/'not_a_directory'
    invalid.write_text('occupied', encoding='utf-8')
    monkeypatch.setattr(batch, '_load_strategy', lambda *_: pytest.fail('must not execute'))
    with pytest.raises(OSError):
        batch.run_all(db_config=CONFIG, log_dir=invalid)


def test_batch_cannot_disable_insert_only(batch, monkeypatch, tmp_path):
    monkeypatch.setattr(batch, '_load_strategy', lambda *_: pytest.fail('must not execute'))
    with pytest.raises(ValueError, match='skip_existing'):
        batch.run_all(options={'ETF份额': {'skip_existing': False}}, db_config=CONFIG, log_dir=tmp_path)
    assert not list(tmp_path.iterdir())
