"""Cumulative deployment uses the last completed live release, not push markers."""
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import pytest
spec = importlib.util.spec_from_file_location('cumulative', Path(__file__).resolve().parents[2]/'scripts/deploy.py')
deploy = importlib.util.module_from_spec(spec); spec.loader.exec_module(deploy)

@pytest.fixture
def repo(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(['git',*args],cwd=tmp_path,check=True,capture_output=True,text=True).stdout.strip()
    git('init','-q','-b','main'); git('config','user.email','test@example.invalid');git('config','user.name','Test')
    def commit(path, value):
        p=tmp_path/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value)
        git('add',path);git('commit','-qm',path);return git('rev-parse','HEAD')
    base=commit('backend/app/main.py','v1')
    stage=tmp_path/'.runtime/releases/active';(stage/'venv').mkdir(parents=True)
    (tmp_path/'backend/.venv').symlink_to(stage/'venv',target_is_directory=True)
    record=stage/'status.json';record.write_text(json.dumps(dict(status='complete',target=base,service='showallopts-api',branch='main')))
    monkeypatch.setattr(deploy,'ROOT',tmp_path)
    return SimpleNamespace(root=tmp_path,git=git,commit=commit,base=base,record=record,options=deploy.parse_args([]))

def test_feature_failure_then_test_fix_requires_release(repo):
    repo.commit('backend/app/main.py','v2');head=repo.commit('backend/tests/test_x.py','fix')
    assert deploy.cumulative_deployment_required(repo.base,head,True,repo.options)
    assert json.loads(repo.record.read_text())['target']==repo.base

@pytest.mark.parametrize('path',['collectors/strategy.py','scripts/research.py','docs/notes.md','backend/tests/test_x.py','frontend/tests/x.spec.ts'])
def test_pure_excluded_changes_keep_baseline(repo,path):
    head=repo.commit(path,'offline');before=repo.record.read_bytes()
    assert not deploy.cumulative_deployment_required(repo.base,head,True,repo.options)
    assert repo.record.read_bytes()==before

def test_repeated_target_noop_and_runtime_after_noop_detected(repo):
    assert not deploy.cumulative_deployment_required(repo.base,repo.base,True,repo.options)
    repo.commit('docs/notes.md','notes');head=repo.commit('frontend/src/main.ts','runtime')
    assert deploy.cumulative_deployment_required(repo.base,head,True,repo.options)

@pytest.mark.parametrize('path',['scripts/deploy.py','.github/workflows/deploy.yml','backend/requirements.lock.txt'])
def test_deployment_dependencies_remain_runtime(repo,path):
    head=repo.commit(path,'runtime');assert deploy.cumulative_deployment_required(repo.base,head,True,repo.options)

@pytest.mark.parametrize('problem',['missing','failed','mismatch','inactive','bad_json','symlink','wrong_service','nonancestor'])
def test_unknown_baseline_blocks(repo,problem):
    head=repo.commit('backend/app/main.py','v2');record=json.loads(repo.record.read_text())
    if problem=='missing':repo.record.unlink()
    elif problem=='bad_json':repo.record.write_text('bad')
    elif problem=='symlink':
        backup=repo.record.with_name('copy');repo.record.rename(backup);repo.record.symlink_to(backup)
    else:
        if problem=='failed':record['status']='recovery-failed'
        if problem=='mismatch':record['target']=head
        if problem=='wrong_service':record['service']='other'
        repo.record.write_text(json.dumps(record))
    if problem=='nonancestor':
        repo.git('checkout','--orphan','other');head=repo.commit('other.txt','other')
    with pytest.raises(deploy.DeployError):
        deploy.cumulative_deployment_required(repo.base,head,problem!='inactive',repo.options)

def test_noop_does_not_build_or_add_release(repo,monkeypatch):
    head=repo.commit('backend/tests/test_x.py','test')
    options=deploy.parse_args(['--non-interactive','--commit',head])
    monkeypatch.setattr(deploy,'preflight',lambda o:(repo.root/'web',{}))
    monkeypatch.setattr(deploy,'clean_head',lambda **kw:repo.base)
    monkeypatch.setattr(deploy,'identity',lambda *a:('active','1','generation'))
    monkeypatch.setattr(deploy,'ready',lambda *a,**kw:None)
    monkeypatch.setattr(deploy,'target_commit',lambda *a:head)
    monkeypatch.setattr(deploy,'prepare_release',lambda *a:pytest.fail('unexpected build'))
    before=repo.record.read_bytes();deploy.deploy(options)
    assert repo.record.read_bytes()==before
    assert len(list((repo.root/'.runtime/releases').iterdir()))==1


def test_similar_or_space_prefixed_paths_are_not_excluded(repo):
    head=repo.commit(' scripts/research.py','runtime')
    assert deploy.cumulative_deployment_required(repo.base,head,True,repo.options)


def test_missing_or_mismatching_live_frontend_blocks(repo, monkeypatch):
    repo.options.web_url='https://example.invalid'
    with pytest.raises(deploy.DeployError):
        deploy.cumulative_deployment_required(repo.base,repo.base,True,repo.options)
    expected=repo.record.parent/'source/frontend/dist/index.html'
    expected.parent.mkdir(parents=True);expected.write_text('index')
    def mismatch(*args):raise deploy.DeployError('content mismatch')
    monkeypatch.setattr(deploy,'frontend_check',mismatch)
    with pytest.raises(deploy.DeployError):
        deploy.cumulative_deployment_required(repo.base,repo.base,True,repo.options)
