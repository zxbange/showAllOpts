"""JoinQuant entry points depend only on mysql_connector, never other strategies."""
import ast
from pathlib import Path

import pytest

COLLECTORS = Path(__file__).resolve().parents[2] / 'collectors'
ENTRY_POINTS = sorted(p for p in COLLECTORS.glob('*.py')
                      if p.name not in ('etf_exchange_collector.py', 'mysql_connector.py'))


@pytest.mark.parametrize('path', ENTRY_POINTS, ids=lambda p: p.stem)
def test_joinquant_entry_only_uses_shared_database_configuration(path):
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    forbidden_modules = {'collectors', 'collector_rules', 'scripts', 'app', 'importlib', 'runpy'}
    sibling_modules = {p.stem for p in ENTRY_POINTS}
    if path.name != 'ETF分类去重_研究版.py':
        assert any(isinstance(n, ast.ImportFrom) and n.module == 'mysql_connector' for n in tree.body)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                names = {n.id for n in node.targets if isinstance(n, ast.Name)}
                if names & {'DB_CONFIG', 'DB_OPTIONS'}:
                    assert isinstance(node.value, ast.Call), 'Database settings must come from mysql_connector'
                if names & {'host', 'port', 'dbname', 'dbuser', 'dbpassword'}:
                    assert not isinstance(node.value, ast.Constant), 'No connection constants in strategy scripts'
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, 'Relative import requires project files'
            assert (node.module or '').split('.')[0] not in forbidden_modules | sibling_modules
        elif isinstance(node, ast.Import):
            assert not {alias.name.split('.')[0] for alias in node.names} & (forbidden_modules | sibling_modules)
        elif isinstance(node, ast.Name):
            assert node.id not in {'__file__', '__import__', 'exec', 'eval'}, 'Notebook must not load another script'
        elif isinstance(node, ast.Call):
            called = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', None)
            assert called not in {'open', 'read_text', 'read_bytes', 'run_path', 'import_module'}, 'No local source reads'
