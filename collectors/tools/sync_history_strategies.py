"""Embed historical collection rules; this local tool never runs on JoinQuant."""
import ast
from pathlib import Path
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / 'collectors/策略批量采集_数据库版.py'
START, END = '# BEGIN EMBEDDED INDEX HISTORY', '# END EMBEDDED INDEX HISTORY'


def join_blocks(blocks):
    """Keep local definitions apart without inserting blank lines between every assignment."""
    result, previous_definition = '', False
    for block in blocks:
        definition = block.lstrip().startswith(('def ', 'class ', '@'))
        separator = '\n\n' if definition or previous_definition else '\n'
        result += (separator if result else '') + block
        previous_definition = definition
    return result


def factory(filename, name, label):
    source = (ROOT / 'collectors' / filename).read_text(encoding='utf-8')
    if '# BEGIN SHARED RESULT JOURNAL' in source:
        start, end = source.index('# BEGIN SHARED RESULT JOURNAL'), source.index('# END SHARED RESULT JOURNAL')
        source = source[:start] + source[end+len('# END SHARED RESULT JOURNAL'):]
    lines, blocks = source.splitlines(), []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        # Entry dependencies are declared once in the batch file header.
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.If):
            assert ast.unparse(node.test) == "__name__ == '__main__'"
            continue
        block = '\n'.join(lines[node.lineno-1:node.end_lineno])
        if isinstance(node, ast.FunctionDef) and node.name == 'run':
            block = block.replace('def run(', 'def collect(', 1)
        if label == '估值标尺':
            block = block.replace('_builtins', 'builtins')
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
        else:
            # Keep the datetime class distinct from the batch datetime module.
            block = block.replace('datetime.', '_growth_datetime.').replace('value, datetime)', 'value, _growth_datetime)')
            if isinstance(node, ast.FunctionDef) and node.name == 'api':
                block = block.replace('    import jqdata\n', '').replace('getattr(jqdata,', 'getattr(jq,')
            if isinstance(node, ast.FunctionDef) and node.name == 'database':
                block = 'def database():\n    return connect_database(_config)'
        blocks.append(block)
    blocks += ["settings = strategy_settings('{}')".format(label)]
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
    return 'def {}(_apis, _config):\n'.format(name) + textwrap.indent(join_blocks(blocks), '    ')


def sync_selection_archive(check=False):
    start, end = '# BEGIN SHARED SELECTION ARCHIVE', '# END SHARED SELECTION ARCHIVE'
    source = (ROOT / 'collectors/rules/selection_archive.py').read_text(encoding='utf-8')
    source = source[source.index(start):source.index(end)+len(end)]
    for filename in ('策略批量采集_数据库版.py', '高股息选股_数据库版.py', '高增长_数据库版.py'):
        path = ROOT / 'collectors' / filename
        text = path.read_text(encoding='utf-8')
        updated = text[:text.index(start)] + source + text[text.index(end)+len(end):]
        if check:
            assert updated == text, filename + ': run python collectors/tools/sync_history_strategies.py'
        else:
            path.write_text(updated, encoding='utf-8')


def sync(check=False):
    sync_selection_archive(check)
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
    sync('--check' in sys.argv)
