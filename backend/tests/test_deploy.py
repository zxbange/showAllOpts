"""Deployment order and failure boundaries without touching a running server."""
import importlib.util
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('release_script', ROOT / 'scripts/deploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


@pytest.fixture
def release(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    (project / 'frontend/dist/assets').mkdir(parents=True)
    (project / 'frontend/dist/index.html').write_text('new frontend')
    web = tmp_path / 'site'
    web.mkdir()
    (web / 'index.html').write_text('old frontend')
    monkeypatch.setattr(deploy, 'ROOT', project)
    monkeypatch.setattr(deploy.shutil, 'which', lambda name: None)
    monkeypatch.setattr(deploy, 'preflight', lambda options: (web, {'APP_ENV_FILE': 'private'}))
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[:3] == ['git', 'rev-parse', 'HEAD']:
            return 'test-commit'
        if args[:2] == ['sudo', 'mv']:
            (web / 'index.html').write_text('new frontend')
        return ''
    monkeypatch.setattr(deploy, 'run', run)
    monkeypatch.setattr(deploy, 'health_check', lambda url: calls.append((['health', url], {})))
    return deploy.parse_args([]), calls, web


def test_release_orders_build_stop_dependencies_database_health_assets_index(release):
    options, calls, web = release
    deploy.deploy(options)
    commands = [args for args, _ in calls]
    def index(prefix):
        return next(i for i, args in enumerate(commands) if args[:len(prefix)] == prefix)
    assert index(['git','merge-base']) < index(['git','merge']) < index(['npm','ci'])
    assert index(['npm','run','build']) < index(['sudo','systemctl','stop'])
    assert index(['sudo','systemctl','stop']) < index([deploy.sys.executable,'-m','pip'])
    db = [deploy.sys.executable, str(deploy.ROOT / 'scripts/check_database.py')]
    assert index([deploy.sys.executable,'-m','pip']) < index(db) < index(['sudo','systemctl','restart'])
    assert index(['sudo','systemctl','restart']) < index(['health']) < index(['sudo','rsync'])
    assert index(['sudo','rsync']) < index(['sudo','install']) < index(['sudo','mv'])
    assert '--delete' not in commands[index(['sudo','rsync'])]
    assert '--exclude=/index.html' in commands[index(['sudo','rsync'])]
    assert calls[index(db)][1] == {'capture': True, 'env': {'APP_ENV_FILE':'private'}}
    assert (web / 'index.html').read_text() == 'new frontend'
    assert not any('reset' in c or 'clean' in c or 'daemon-reload' in c for c in commands)


@pytest.mark.parametrize('failure', ['fetch','merge-base','build','pip','database','restart','health','rsync'])
def test_failure_does_not_publish_new_entry_page(release, monkeypatch, failure):
    options, calls, web = release
    original = deploy.run
    def run(args, **kwargs):
        failed = ((failure in ('fetch','merge-base') and args[:2] == ['git',failure])
                  or (failure == 'build' and args[:3] == ['npm','run','build'])
                  or (failure == 'pip' and 'pip' in args)
                  or (failure == 'database' and any(a.endswith('check_database.py') for a in args))
                  or (failure == 'restart' and args[:3] == ['sudo','systemctl','restart'])
                  or (failure == 'rsync' and args[:2] == ['sudo','rsync']))
        if failed:
            raise deploy.DeployError('simulated failure')
        return original(args, **kwargs)
    monkeypatch.setattr(deploy, 'run', run)
    if failure == 'health':
        monkeypatch.setattr(deploy, 'health_check', Mock(side_effect=deploy.DeployError('not ready')))
    with pytest.raises(deploy.DeployError):
        deploy.deploy(options)
    assert (web / 'index.html').read_text() == 'old frontend'
    assert not any(args[:2] == ['sudo','mv'] for args, _ in calls)
    if failure in ('fetch','merge-base','build'):
        assert not any(args[:3] == ['sudo','systemctl','stop'] for args, _ in calls)


def test_skip_pull_still_builds_and_restarts_and_checks_optional_proxy(release):
    options, calls, _ = release
    options.skip_pull = True
    options.web_url = 'http://127.0.0.1:8081'
    deploy.deploy(options)
    commands = [args for args, _ in calls]
    assert not any(c[:2] in (['git','fetch'],['git','merge']) for c in commands)
    assert ['sudo','systemctl','restart','showallopts-api'] in commands
    assert ['health','http://127.0.0.1:8081'] in commands


@pytest.mark.parametrize('value', ['https://user:secret@example.org','http://example.org/path','http://example.org?q=1','file:///tmp'])
def test_reject_non_origin_urls(value):
    with pytest.raises(deploy.DeployError):
        deploy.validate_url(value)


def test_local_health_url_and_static_root_validation(tmp_path, monkeypatch):
    assert deploy.validate_url('http://127.0.0.1:8081/', local=True) == 'http://127.0.0.1:8081'
    with pytest.raises(deploy.DeployError):
        deploy.validate_url('http://remote.example', local=True)
    monkeypatch.setattr(deploy, 'ROOT', tmp_path / 'project')
    (tmp_path / 'project').mkdir()
    site = tmp_path / 'site'
    site.mkdir()
    assert deploy.static_root(str(site)) == site
    for path in (tmp_path, tmp_path / 'project', tmp_path / 'missing'):
        with pytest.raises(deploy.DeployError):
            deploy.static_root(str(path))


def test_capture_failure_does_not_expose_environment_or_output(monkeypatch):
    monkeypatch.setattr(deploy.subprocess, 'run', Mock(return_value=subprocess.CompletedProcess([],1,'secret','password')))
    with pytest.raises(deploy.DeployError) as error:
        deploy.run(['python','check'], capture=True, env={'DB_PASSWORD':'secret'})
    assert 'secret' not in str(error.value) and 'password' not in str(error.value)


def test_health_requires_correct_service_and_retries(monkeypatch):
    class Response:
        def __init__(self, payload): self.payload=payload
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps(self.payload).encode()
    opener = Mock()
    opener.open.side_effect = [Response([]), Response({'status':'ok','service':'another'}),
                               Response({'status':'ok','service':'showallopts-api'})]
    monkeypatch.setattr(deploy, 'build_opener', lambda *args: opener)
    sleep = Mock()
    monkeypatch.setattr(deploy.time, 'sleep', sleep)
    deploy.health_check('http://127.0.0.1:8000', attempts=3)
    assert opener.open.call_count == 3 and sleep.call_count == 2
    opener.open.side_effect = OSError('not listening')
    with pytest.raises(deploy.DeployError):
        deploy.health_check('http://127.0.0.1:8000', attempts=2)


@pytest.fixture
def preflight_setup(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    (project / 'config').mkdir(parents=True)
    (project / 'config/app.config').write_text('DB_PASSWORD=preserved-secret')
    web = tmp_path / 'site'
    web.mkdir()
    monkeypatch.setattr(deploy, 'ROOT', project)
    monkeypatch.setattr(deploy.shutil, 'which', lambda name: '/bin/'+name)
    monkeypatch.setattr(deploy.getpass, 'getuser', lambda: 'ubuntu')
    props = {'LoadState':'loaded', 'User':'ubuntu', 'WorkingDirectory':str(project / 'backend'),
             'ExecStart':str(project / 'backend/.venv/bin/python')+' '+str(project / 'backend/run.py'),
             'EnvironmentFiles':'', 'Environment':'APP_ENV_FILE=config/app.config DB_HOST=service-host'}
    monkeypatch.setattr(deploy, 'property_value', lambda service,key: props[key])
    state = {'dirty':'', 'branch':'main'}
    commands = []
    def run(args, **kwargs):
        commands.append(args)
        if args[:2] == ['git','rev-parse']: return str(project)
        if args[:2] == ['git','status']: return state['dirty']
        if args[:2] == ['git','branch']: return state['branch']
        return ''
    monkeypatch.setattr(deploy, 'run', run)
    return deploy.parse_args(['--web-root',str(web)]), props, state, commands


def test_preflight_preserves_config_and_uses_service_environment(preflight_setup, monkeypatch):
    options, props, _, commands = preflight_setup
    monkeypatch.setenv('DB_HOST', 'wrong-shell-host')
    monkeypatch.setenv('DB_PASSWORD', 'wrong-shell-password')
    web, env = deploy.preflight(options)
    assert str(web) == options.web_root
    assert env['DB_HOST'] == 'service-host' and 'DB_PASSWORD' not in env
    assert env['APP_ENV_FILE'] == str((deploy.ROOT / 'config/app.config').resolve())
    assert (deploy.ROOT / 'config/app.config').read_text() == 'DB_PASSWORD=preserved-secret'
    assert ['sudo','nginx','-t'] in commands
    assert not any(c[:3] == ['sudo','systemctl','stop'] for c in commands)


@pytest.mark.parametrize('problem', ['dirty','branch','user','directory','command','environment_file','missing_config'])
def test_preflight_rejects_incompatible_checkout_or_service(preflight_setup, problem):
    options, props, state, commands = preflight_setup
    if problem == 'dirty': state['dirty']=' M source.py'
    if problem == 'branch': state['branch']='another-branch'
    if problem == 'user': props['User']='root'
    if problem == 'directory': props['WorkingDirectory']='/other/backend'
    if problem == 'command': props['ExecStart']='/other/python /other/run.py'
    if problem == 'environment_file': props['EnvironmentFiles']='/etc/other.env'
    if problem == 'missing_config': props['Environment']=''
    with pytest.raises(deploy.DeployError):
        deploy.preflight(options)
    assert not any(c[:2] in (['git','fetch'],['sudo','systemctl']) for c in commands)
