import importlib.util
from pathlib import Path
import subprocess
import pytest
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("impact", ROOT/"scripts/deployment_impact.py")
impact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(impact)

@pytest.mark.parametrize('paths,expected', [
    (['collectors/research.py'],False),
    (['collectors/README.md'],False),
    (['collectors/README.md','backend/app/main.py'],True),
    (['scripts/deploy.py'],True), (['collectors/tools/sync_result_journal.py'],False),
    (['collectors/rules/result_journal.py'],False), (['frontend/package-lock.json'],True),
    (['collectors/tools/sync_earnings_version.py','collectors/净利润断层_数据库版.py'],False),
    (['collectors-other/tool.py'],True),
    (['backend/requirements.lock.txt'],True), (['.github/workflows/deploy.yml'],True),
    (['collectors/unknown.py'],False), ([],True),
    (['scripts/research.py','renamed_runtime.py'],True),
    (['collectors/etf_exchange_collector.py'],False),
    (['collectors/策略批量采集_数据库版.py'],False),
    (['collectors/估值标尺历史补采_聚宽研究版.py',
      'backend/tests/test_recovered_joinquant_history.py',
      'docs/joinquant-market-valuation-history.md'],False),
    (['frontend/tests/overview.spec.ts'],False),
    (['backend/tests-other/runtime.py'],True),
    (['frontend/tests-other/runtime.ts'],True),
    (['docs-other/runtime.py'],True),
    (['docs/setup.md', 'backend/app/main.py'],True),
    (['collectors/research.py', 'frontend/src/App.tsx'],True),
    (['collectors/research.py', 'deploy/nginx.conf'],True),
    (['scripts-other/tool.py'],True),
    (['脚本/unknown.py'],True),
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
    target=tmp_path/'collectors/research.py';target.parent.mkdir();target.write_text('# research')
    doc=tmp_path/'docs/research.md';doc.parent.mkdir();doc.write_text('research')
    test=tmp_path/'backend/tests/test_research.py';test.parent.mkdir(parents=True);test.write_text('# test')
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
