import importlib.util
from pathlib import Path
import subprocess
import pytest
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("impact", ROOT/"scripts/deployment_impact.py")
impact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(impact)

@pytest.mark.parametrize('paths,expected', [
    (['脚本/本地统计/短线五策略胜率统计_本地版.py'],False),
    (['docs/short-statistics.md','backend/tests/test_seven_strategy_statistics.py'],False),
    (['脚本/本地统计/短线五策略胜率统计_本地版.py','backend/app/main.py'],True),
    (['scripts/deploy.py'],False), (['scripts/sync_oversold_rules.py'],False),
    (['strategy_rules/oversold.py'],True), (['frontend/package-lock.json'],True),
    (['backend/requirements.lock.txt'],True), (['.github/workflows/deploy.yml'],True),
    (['脚本/本地统计/unknown.py'],False), ([],True),
    (['脚本/本地统计/短线五策略胜率统计_本地版.py','renamed_runtime.py'],True),
])
def test_impact_paths(paths, expected):
    assert impact.requires_deployment(paths) is expected


def test_impact_invalid_missing_or_nonancestor_blocks_deploy(tmp_path):
    assert impact.compare('', 'a'*40, tmp_path)[0] is None
    assert impact.compare('0'*40, 'a'*40, tmp_path)[0] is None
    assert impact.compare('a'*40, 'b'*40, tmp_path)[0] is None


def test_real_git_research_diff_then_mixed_diff(tmp_path):
    def git(*args):
        return subprocess.run(['git',*args],cwd=tmp_path,check=True,capture_output=True,text=True).stdout.strip()
    git('init','-q');git('config','user.email','test@example.invalid');git('config','user.name','Test')
    (tmp_path/'README').write_text('base')
    git('add','.');git('commit','-qm','base');base=git('rev-parse','HEAD')
    target=tmp_path/'docs/short-statistics.md';target.parent.mkdir();target.write_text('research')
    git('add','.');git('commit','-qm','research');head=git('rev-parse','HEAD')
    assert impact.compare(base,head,tmp_path)[0] is False
    (tmp_path/'README').write_text('runtime change')
    git('add','.');git('commit','-qm','mixed');head=git('rev-parse','HEAD')
    assert impact.compare(base,head,tmp_path)[0] is True


def test_workflow_keeps_ci_and_manual_deploy():
    workflow=(ROOT/'.github/workflows/deploy.yml').read_text()
    assert 'paths-ignore' not in workflow and 'needs: [backend, frontend]' in workflow
    assert "needs.backend.outputs.deploy_required == 'true'" in workflow
    assert "github.event_name == 'workflow_dispatch' && inputs.operation == 'deploy' && vars.DEPLOY_ENABLED == 'true'" in workflow
    assert 'fetch-depth: 0' in workflow
