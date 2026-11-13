"""Sharding must execute the entire suite exactly once across all CI runners."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ci_shard import module_shards, parse_shard


@pytest.mark.parametrize('value', ['0/3', '4/3', '1/0', '1/33', '1', '1/2/3', '-1/3', '1.0/3'])
def test_invalid_shards_fail_closed(value):
    with pytest.raises(pytest.UsageError):
        parse_shard(value)


def test_balanced_modules_are_order_independent_and_cover_every_node():
    nodes = [f'test_{module}.py::test_value[{n}]'
             for module, count in enumerate([40, 30, 20, 10, 9, 8, 7, 6]) for n in range(count)]
    assignment = module_shards(nodes, 3)
    assert assignment == module_shards(list(reversed(nodes)), 3)
    groups = [{node for node in nodes if assignment[node.split('::')[0]] == shard}
              for shard in (1, 2, 3)]
    assert set.union(*groups) == set(nodes)
    assert sum(map(len, groups)) == len(nodes)
    assert max(map(len, groups)) - min(map(len, groups)) <= 6


def test_real_pytest_shards_keep_module_fixtures_and_execute_all_tests(tmp_path):
    for module in range(6):
        (tmp_path / f'test_part{module}.py').write_text(
            'import pytest\n'
            '@pytest.fixture(scope="module")\n'
            'def shared(): return []\n'
            'def test_first(shared):\n'
            '    assert shared == []\n'
            '    shared.append(1)\n'
            'def test_second(shared): assert shared == [1]\n')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    env.pop('PYTEST_ADDOPTS', None)
    base = [sys.executable, '-m', 'pytest', '-q', '-p', 'ci_shard']
    for shard in (1, 2, 3):
        result = subprocess.run(base + [f'--ci-shard={shard}/3'], cwd=tmp_path, env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert '4 passed, 8 deselected' in result.stdout
    full = subprocess.run(base, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert full.returncode == 0, full.stdout + full.stderr
    assert '12 passed' in full.stdout
    empty = subprocess.run(base + ['--ci-shard=7/7'], cwd=tmp_path, env=env,
                           capture_output=True, text=True, timeout=30)
    assert empty.returncode != 0
    assert 'CI shard has no tests' in empty.stderr


def test_workflow_waits_for_every_shard_and_keeps_failure_evidence_separate():
    workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/deploy.yml').read_text()
    assert 'shard: [1, 2, 3]' in workflow
    assert 'shard: [1, 2]' in workflow
    assert workflow.count('fail-fast: false') == 2
    assert '--ci-shard=${{ matrix.shard }}/3 --durations=20' in workflow
    assert '--shard=${{ matrix.shard }}/2' in workflow
    assert 'needs: [backend, frontend]' in workflow
    assert 'frontend-e2e-failure-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.shard }}' in workflow
