"""Verify the actual workflow's event/aggregator contracts without invoking Actions."""

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
# BaseLoader preserves the literal 'on' key rather than YAML 1.1 booleans.
WORKFLOW = yaml.load((ROOT / '.github/workflows/deploy.yml').read_text(), Loader=yaml.BaseLoader)
JOBS = WORKFLOW['jobs']
CHECKS = ('backend', 'frontend', 'backend-quality', 'mysql-integration')


def evaluate_condition(expression, context):
    """Evaluate only the boolean operators and comparisons used by these guards."""

    def resolve(match):
        return repr(context.get(match.group(), ''))

    expression = (
        re.sub(
            r'(?<![\w.])(?:github|needs|vars|inputs)(?:\.[\w-]+)+',
            resolve,
            expression,
        )
        .replace('always()', 'True')
        .replace('&&', ' and ')
        .replace('||', ' or ')
    )

    def walk(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BoolOp):
            values = (walk(item) for item in node.values)
            if isinstance(node.op, ast.And):
                return all(values)
            if isinstance(node.op, ast.Or):
                return any(values)
        if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1:
            assert isinstance(node.ops[0], ast.Eq)
            return walk(node.left) == walk(node.comparators[0])
        raise AssertionError(f'Unexpected workflow guard expression: {ast.dump(node)}')

    return bool(walk(ast.parse(' '.join(expression.split()), mode='eval').body))


@pytest.mark.parametrize(
    'event,ref,base,repository,operation,enabled,ci,deploy',
    [
        (
            'pull_request',
            'refs/pull/3/merge',
            'main',
            'zxbange/showAllOpts',
            '',
            'true',
            True,
            False,
        ),
        (
            'pull_request',
            'refs/pull/3/merge',
            'other',
            'zxbange/showAllOpts',
            '',
            'true',
            False,
            False,
        ),
        (
            'pull_request',
            'refs/pull/3/merge',
            'main',
            'other/showAllOpts',
            '',
            'true',
            False,
            False,
        ),
        ('push', 'refs/heads/main', '', 'zxbange/showAllOpts', '', 'true', True, True),
        ('push', 'refs/heads/main', '', 'zxbange/showAllOpts', '', 'false', True, False),
        ('push', 'refs/heads/feature', '', 'zxbange/showAllOpts', '', 'true', False, False),
        (
            'workflow_dispatch',
            'refs/heads/main',
            '',
            'zxbange/showAllOpts',
            'check',
            'false',
            True,
            True,
        ),
        (
            'workflow_dispatch',
            'refs/heads/main',
            '',
            'zxbange/showAllOpts',
            'deploy',
            'true',
            True,
            True,
        ),
        (
            'workflow_dispatch',
            'refs/heads/main',
            '',
            'zxbange/showAllOpts',
            'deploy',
            'false',
            True,
            False,
        ),
        (
            'workflow_dispatch',
            'refs/heads/feature',
            '',
            'zxbange/showAllOpts',
            'deploy',
            'true',
            False,
            False,
        ),
    ],
)
def test_ci_and_deploy_event_guards(event, ref, base, repository, operation, enabled, ci, deploy):
    context = {
        'github.event_name': event,
        'github.ref': ref,
        'github.repository': repository,
        'github.event.pull_request.base.ref': base,
        'inputs.operation': operation,
        'vars.DEPLOY_ENABLED': enabled,
        'needs.ci-gate.result': 'success',
    }
    for job in (*CHECKS, 'ci-gate'):
        assert evaluate_condition(JOBS[job]['if'], context) is ci, job
    assert evaluate_condition(JOBS['deploy']['if'], context) is deploy
    context['needs.ci-gate.result'] = 'failure'
    assert not evaluate_condition(JOBS['deploy']['if'], context)


def test_pr_trigger_and_production_privilege_are_separate():
    assert WORKFLOW['on']['pull_request']['branches'] == ['main']
    assert WORKFLOW['on']['push']['branches'] == ['main']
    assert 'pull_request_target' not in WORKFLOW['on']
    assert WORKFLOW['permissions'] == {'contents': 'read'}
    assert 'showallopts-pr-' in WORKFLOW['concurrency']['group']
    assert 'showallopts-production' in WORKFLOW['concurrency']['group']
    assert WORKFLOW['concurrency']['cancel-in-progress'] == 'false'
    for name in (*CHECKS, 'ci-gate'):
        assert 'environment' not in JOBS[name]
        assert 'secrets.' not in yaml.dump(JOBS[name])
    assert JOBS['deploy']['environment'] == 'production'
    assert JOBS['deploy']['permissions'] == {}
    assert JOBS['deploy']['needs'] == ['ci-gate']


@pytest.mark.parametrize('failed', [None, *CHECKS])
@pytest.mark.parametrize('bad_result', ['failure', 'cancelled', 'skipped'])
def test_aggregate_gate_runs_real_shell_and_fails_closed(failed, bad_result):
    gate = JOBS['ci-gate']
    assert gate['needs'] == list(CHECKS)
    assert 'always()' in gate['if']
    (step,) = gate['steps']
    assert len(step['env']) == len(CHECKS)
    result_env = {}
    for name, expression in step['env'].items():
        job = re.fullmatch(r'\$\{\{ needs\.([\w-]+)\.result \}\}', expression)[1]
        assert job in CHECKS
        result_env[name] = bad_result if job == failed else 'success'
    result = subprocess.run(
        ['bash', '-c', step['run']],
        text=True,
        env=os.environ | result_env,
        capture_output=True,
        timeout=10,
    )
    assert (result.returncode == 0) == (failed is None)


def test_real_mysql_job_is_isolated_required_and_versioned():
    mysql = JOBS['mysql-integration']
    assert mysql['strategy']['matrix']['mysql'] == ['8.4', '5.7.44']
    image = mysql['services']['mysql']
    assert image['image'] == 'mysql:${{ matrix.mysql }}'
    assert image['env']['MYSQL_DATABASE'] == image['env']['MYSQL_USER'] == 'showallopts_ci'
    assert image['ports'] == ['3306/tcp']
    assert '--health-cmd=' in image['options']
    step = mysql['steps'][-1]
    assert step['env'] == {
        'SHOWALLOPTS_TEST_MYSQL': '1',
        'SHOWALLOPTS_TEST_MYSQL_PORT': '${{ job.services.mysql.ports[3306] }}',
    }
    assert 'pytest -q tests/integration/test_mysql_dividend.py' in step['run']
    assert 'case.find("skipped")' in step['run']


def test_quality_job_compares_correct_event_without_shell_expression_injection():
    step = JOBS['backend-quality']['steps'][-1]
    source = step['env']['QUALITY_BASE_SHA']
    assert 'github.event.pull_request.base.sha' in source
    assert 'github.event.before' in source
    assert 'git cat-file -e "$QUALITY_BASE_SHA^{commit}"' in step['run']
    assert 'python backend/check_quality.py "${args[@]}"' in step['run']


@pytest.mark.parametrize(
    'content,accepted',
    [
        ('<testsuites><testsuite><testcase name="real"/></testsuite></testsuites>', True),
        (
            '<testsuites><testsuite><testcase name="real"><skipped/></testcase></testsuite></testsuites>',
            False,
        ),
        ('<testsuites><testsuite/></testsuites>', False),
    ],
)
def test_real_mysql_gate_cannot_greenwash_empty_or_skipped_tests(tmp_path, content, accepted):
    step = JOBS['mysql-integration']['steps'][-1]
    # Run the exact post-pytest XML guard from the workflow. No DB needed for
    # this contract regression; actual MySQL compatibility remains a CI job.
    guard = step['run'].split("<<'PYTHON'\n", 1)[1].rsplit('\nPYTHON', 1)[0]
    report = tmp_path / 'mysql-tests.xml'
    report.write_text(content)
    result = subprocess.run(
        [sys.executable, '-c', guard, str(report)], text=True, capture_output=True, timeout=10
    )
    assert (result.returncode == 0) == accepted
