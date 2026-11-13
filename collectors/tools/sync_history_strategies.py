"""Embed standalone index/valuation collectors as isolated batch factories; never run on JoinQuant."""
import ast
from pathlib import Path
import textwrap

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / 'collectors/策略批量采集_数据库版.py'
START, END = '# BEGIN EMBEDDED INDEX HISTORY', '# END EMBEDDED INDEX HISTORY'


def factory(filename, name, label):
    source = (ROOT / 'collectors' / filename).read_text(encoding='utf-8')
    if '# BEGIN SHARED RESULT JOURNAL' in source:
        start, end = source.index('# BEGIN SHARED RESULT JOURNAL'), source.index('# END SHARED RESULT JOURNAL')
        source = source[:start] + source[end+len('# END SHARED RESULT JOURNAL'):]
    lines, blocks = source.splitlines(), []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == '__future__':
            continue
        if isinstance(node, ast.If):
            assert ast.unparse(node.test) == "__name__ == '__main__'"
            continue
        block = '\n'.join(lines[node.lineno-1:node.end_lineno])
        if label == '估值标尺':
            if isinstance(node, ast.FunctionDef) and node.name == 'api':
                block = block.replace('globals().get(name)', '_apis.get(name)')
            if isinstance(node, ast.FunctionDef) and node.name == 'database':
                block = block.replace('    from mysql_connector import create_mysql_engine\n', '')
                block = block.replace('engine = create_mysql_engine()', 'engine = connect_database(_config)')
            if isinstance(node, ast.ClassDef) and node.name == 'Progress':
                block = '''class Progress:
    def update(self, stage, done=0, total=None, day=None, force=False):
        suffix = ' {}/{} 日'.format(done, total) if total is not None else ''
        _progress_note('{}{} {}'.format(stage, suffix, day or ''))
    def finish(self, status):
        _progress_note(status)'''
        blocks.append(block)
    blocks += ["settings = strategy_settings('{}')".format(label), 'collect = run']
    if label == '估值标尺':
        blocks.append('''def run(start_date=settings['start_date'], end_date=settings['end_date'],
        max_days=settings['max_days'], skip_existing=True):
    _require_append(skip_existing)
    return collect(start_date=start_date, end_date=end_date, write_database=True,
                   max_days=max_days, return_report=True)''')
    else:
        blocks += ['Base = SimpleNamespace(metadata=metadata)', '''def run(start_date=settings['start_date'], end_date=settings['end_date'],
        window_days=settings['window_days'], max_days=settings['max_days'],
        max_windows=settings['max_windows'], retry_earlier_missing=settings['retry_earlier_missing'],
        skip_existing=True):
    _require_append(skip_existing)
    return collect(start_date=start_date, end_date=end_date, preview=False,
                   window_days=window_days, max_days=max_days, max_windows=max_windows,
                   api_namespace=_apis, engine_factory=lambda: connect_database(_config),
                   progress=_progress_note, retry_earlier_missing=retry_earlier_missing)''']
    blocks.append('return SimpleNamespace(**locals())')
    return 'def {}(_apis, _config):\n'.format(name) + textwrap.indent('\n\n'.join(blocks), '    ')


def sync(check=False):
    factories = [factory('估值标尺历史补采_聚宽研究版.py', '_valuation_ruler_strategy', '估值标尺'),
                 factory('创红比历史补采_聚宽研究版.py', '_growth_dividend_strategy', '创红比')]
    block = START + '\n' + '\n\n\n'.join(factories) + '\n' + END
    source = TARGET.read_text(encoding='utf-8')
    if START in source:
        updated = source[:source.index(START)] + block + source[source.index(END)+len(END):]
    else:
        position = source.index('# 注册信息：')
        updated = source[:position] + block + '\n\n\n' + source[position:]
    ast.parse(updated, feature_version=(3, 6))
    if check:
        assert updated == source, 'run python collectors/tools/sync_history_strategies.py'
    else:
        TARGET.write_text(updated, encoding='utf-8')


if __name__ == '__main__':
    import sys
    sync('--check' in sys.argv)
