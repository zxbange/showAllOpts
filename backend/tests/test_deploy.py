"""Staged deployment tests: stdlib only; never contact or mutate a live server."""
import importlib.util
import io
import json
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('release_script', Path(__file__).resolve().parents[2] / 'scripts/deploy.py')
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)
OLD, NEW = 'a' * 40, 'b' * 40


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project, self.web = root/'project', root/'site'
        self.live = self.project/'backend/.venv'
        self.live.mkdir(parents=True)
        (self.live/'old-env').write_text('old dependencies')
        self.web.mkdir()
        (self.web/'assets').mkdir()
        (self.web/'index.html').write_text('old frontend')
        (self.web/'assets/shared.js').write_text('old asset')
        (self.web/'assets/old.js').write_text('old hash')
        self.options = deploy.parse_args([])
        self.calls = []
        self.state = {'head': OLD, 'branch': 'main', 'active': 'active', 'generation': 0, 'dirty': ''}
        self.failure = None
        self.raised = False
        self.after = False
        self.exception = deploy.DeployError
        for target, value in [
            ('ROOT', self.project), ('NON_INTERACTIVE', False),
            ('preflight', lambda o: (self.web, {'APP_ENV_FILE': '/private/config'})),
            ('run', self.run_command), ('property_value', self.property),
            ('health_check', self.health), ('owns_listener', lambda *a: True),
            ('frontend_check', self.frontend),
        ]:
            p = patch.object(deploy, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(deploy.shutil, 'which', lambda _: None)
        p.start()
        self.addCleanup(p.stop)

    def property(self, service, name):
        if name == 'ActiveState':
            return self.state['active']
        if name == 'MainPID':
            return str(100+self.state['generation']) if self.state['active'] == 'active' else '0'
        if name == 'InvocationID':
            return 'invocation-'+str(self.state['generation'])
        raise AssertionError(name)

    def maybe_fail(self, name):
        if name is not None and self.failure == name and not self.raised:
            self.raised = True
            if name == 'dirty-recovery':
                self.state['dirty'] = ' M important.py'
            if name == 'branch-race':
                self.state['branch'] = 'other'
            raise self.exception('injected '+name)

    def health(self, url, attempts=20):
        self.calls.append(['health', url])
        if self.state['generation'] == 1:
            for name in ('health', 'dirty-recovery', 'branch-race'):
                self.maybe_fail(name)

    def frontend(self, *args):
        self.maybe_fail('website')

    def run_command(self, args, **kwargs):
        args = [str(a) for a in args]
        self.calls.append(args)
        cmd = args[1:] if args[0] == 'sudo' else args
        if cmd and cmd[0] == '-n':
            cmd = cmd[1:]
        point = None
        if cmd[:2] == ['git', 'fetch']: point = 'fetch'
        if cmd[:2] == ['git', 'merge']: point = 'merge'
        if cmd[:3] == ['npm', 'run', 'build']: point = 'build'
        if cmd[:2] == ['npm', 'ci']: point = 'npm'
        if 'pip' in cmd: point = 'pip-check' if 'check' in cmd else 'pip'
        if any(a.endswith('check_database.py') for a in cmd): point = 'database'
        if cmd[-1:] == ['import app.main']: point = 'import'
        if cmd[:2] == ['systemctl', 'stop']: point = 'stop'
        if cmd[:2] == ['systemctl', 'restart']: point = 'restart'
        if cmd[:1] == ['rsync']: point = 'assets'
        if cmd[:1] == ['mv']: point = 'index'
        if not self.after:
            self.maybe_fail(point)
        result = ''
        if cmd[:2] == ['git', 'status']:
            result = self.state['dirty'] if kwargs.get('cwd') is None else ''
        elif cmd[:2] == ['git', 'branch']:
            result = self.state['branch']
        elif cmd[:2] == ['git', 'rev-parse']:
            result = self.state['head'] if cmd[-1] == 'HEAD' else NEW
        elif cmd[:2] == ['git', 'merge']:
            self.state['head'] = cmd[-1]
        elif cmd[:2] == ['git', 'switch']:
            self.state.update(head=cmd[-1], branch='')
        elif cmd[:3] == ['git', 'worktree', 'add']:
            source = Path(cmd[-2])
            (source/'frontend/dist/assets').mkdir(parents=True)
            (source/'frontend/dist/index.html').write_text('<script src="/assets/shared.js"></script>')
            (source/'frontend/dist/assets/shared.js').write_text('new asset')
            (source/'frontend/dist/assets/new.js').write_text('new hash')
        elif cmd[1:3] == ['-m', 'venv']:
            Path(cmd[-1]).mkdir()
            (Path(cmd[-1])/'new-env').write_text('new dependencies')
        elif cmd[:2] == ['systemctl', 'stop']:
            self.state['active'] = 'inactive'
        elif cmd[:2] == ['systemctl', 'restart']:
            self.state['active'] = 'active'
            self.state['generation'] += 1
        elif cmd[:1] == ['rsync']:
            source, target = Path(cmd[-2]), Path(cmd[-1])
            for item in source.rglob('*'):
                relative = item.relative_to(source)
                if relative == Path('index.html'): continue
                destination = target/relative
                if item.is_dir(): destination.mkdir(exist_ok=True)
                else: shutil.copy2(item, destination)
        elif cmd[:1] == ['install']:
            shutil.copy2(cmd[-2], cmd[-1])
        elif cmd[:1] == ['mv']:
            Path(cmd[-2]).replace(cmd[-1])
        elif cmd[:1] == ['rm']:
            Path(cmd[-1]).unlink(missing_ok=True)
        if self.after:
            self.maybe_fail(point)
        return result

    def status(self):
        return json.loads(next((self.project/'.runtime/releases').glob('*/status.json')).read_text())['status']

    def assert_restored(self, active=True):
        self.assertEqual(self.state['head'], OLD)
        self.assertTrue((self.live/'old-env').is_file())
        self.assertEqual((self.web/'index.html').read_text(), 'old frontend')
        self.assertEqual((self.web/'assets/shared.js').read_text(), 'old asset')
        self.assertEqual((self.web/'assets/old.js').read_text(), 'old hash')
        self.assertEqual(self.state['active'], 'active' if active else 'inactive')
        self.assertEqual(self.status(), 'recovered')

    def test_success_stages_before_stop_and_preserves_old_artifacts(self):
        deploy.deploy(self.options)
        commands = self.calls
        index = lambda needle: next(i for i, c in enumerate(commands) if needle in c)
        self.assertLess(index('import app.main'), index('stop'))
        self.assertLess(index('build'), index('stop'))
        self.assertLess(index('stop'), index('merge'))
        self.assertLess(index('merge'), index('restart'))
        self.assertEqual(self.state['head'], NEW)
        self.assertTrue(self.live.is_symlink())
        self.assertTrue((self.live/'new-env').is_file())
        self.assertEqual(self.status(), 'complete')
        self.assertTrue(next((self.project/'.runtime/releases').glob('*/previous-venv/old-env')).is_file())
        self.assertTrue((self.web/'assets/old.js').exists())
        self.assertFalse(any('reset' in c or 'clean' in c or '--delete' in c for c in commands))
        self.assertIn('--no-overwrite-ignore', next(c for c in commands if c[:2] == ['git','merge']))

    def test_skip_pull_still_builds_same_commit(self):
        self.options.skip_pull = True
        self.options.commit = OLD
        deploy.deploy(self.options)
        self.assertFalse(any(c[:2] == ['git','fetch'] for c in self.calls))
        self.assertTrue(any(c[:2] == ['npm','ci'] for c in self.calls))
        self.assertEqual(self.state['head'], OLD)

    def test_skip_pull_rejects_different_sha(self):
        self.options.skip_pull = True
        self.options.commit = NEW
        with self.assertRaises(deploy.DeployError):
            deploy.deploy(self.options)
        self.assertFalse(any('stop' in c for c in self.calls))

    def test_exact_target_ancestry_is_checked(self):
        self.options.commit = NEW
        deploy.deploy(self.options)
        self.assertIn(['git','merge-base','--is-ancestor',NEW,NEW], self.calls)
        self.assertIn(['git','merge-base','--is-ancestor',OLD,NEW], self.calls)

    def test_initial_inactive_failure_stays_stopped(self):
        self.state['active'] = 'inactive'
        self.failure = 'health'
        with self.assertRaises(deploy.DeployError):
            deploy.deploy(self.options)
        self.assert_restored(active=False)
        self.assertEqual(self.state['generation'], 1)

    def test_web_failure_restores_all_overwritten_assets(self):
        self.options.web_url = 'http://127.0.0.1:8081'
        self.failure = 'website'
        with self.assertRaises(deploy.DeployError):
            deploy.deploy(self.options)
        self.assert_restored()
        self.assertTrue((self.web/'assets/new.js').exists())  # Deliberately not destructive.

    def test_dirty_recovery_is_explicit_not_destructive(self):
        self.failure = 'dirty-recovery'
        with self.assertRaisesRegex(deploy.DeployError, 'recovery is incomplete'):
            deploy.deploy(self.options)
        self.assertEqual(self.status(), 'recovery-failed')
        self.assertFalse(any('switch' in c for c in self.calls))
        self.assertEqual(self.state['active'], 'inactive')

    def test_changed_branch_recovery_is_refused(self):
        self.failure = 'branch-race'
        with self.assertRaisesRegex(deploy.DeployError, 'recovery is incomplete'):
            deploy.deploy(self.options)
        self.assertEqual(self.status(), 'recovery-failed')

    def test_existing_activation_journal_blocks_new_update(self):
        stage = self.project/'.runtime/releases/interrupted'
        stage.mkdir(parents=True)
        (stage/'status.json').write_text('{"status":"activating"}')
        with self.assertRaisesRegex(deploy.DeployError, 'Unfinished'):
            deploy.deploy(self.options)
        self.assertFalse(any('fetch' in c or 'stop' in c for c in self.calls))

    def test_branch_changed_during_preparation_never_stops(self):
        original = deploy.prepare_release
        def prepare(*args):
            result = original(*args)
            self.state['branch'] = 'other'
            return result
        with patch.object(deploy, 'prepare_release', prepare):
            with self.assertRaises(deploy.DeployError):
                deploy.deploy(self.options)
        self.assertFalse(any('stop' in c for c in self.calls))
        self.assertEqual(self.status(), 'preparation-failed')

    def test_legacy_venv_rename_interruption(self):
        original = Path.rename
        fired = []
        def rename(path, target):
            result = original(path, target)
            if path == self.live and not fired:
                fired.append(True)
                raise KeyboardInterrupt()
            return result
        with patch.object(Path, 'rename', rename):
            with self.assertRaises(KeyboardInterrupt):
                deploy.deploy(self.options)
        self.assert_restored()

    def test_new_venv_symlink_interruption(self):
        original = Path.symlink_to
        fired = []
        def symlink(path, target, **kwargs):
            result = original(path, target, **kwargs)
            if path == self.live and not fired:
                fired.append(True)
                raise KeyboardInterrupt()
            return result
        with patch.object(Path, 'symlink_to', symlink):
            with self.assertRaises(KeyboardInterrupt):
                deploy.deploy(self.options)
        self.assert_restored()

    def test_noninteractive_all_sudo_commands_have_n(self):
        deploy.NON_INTERACTIVE = True
        deploy.deploy(self.options)
        self.assertTrue(all(c[1] == '-n' for c in self.calls if c[0] == 'sudo'))

    def test_reserved_static_filename_stops_before_activation(self):
        (self.web/'.showallopts-index.next').write_text('user file')
        with self.assertRaises(deploy.DeployError):
            deploy.deploy(self.options)
        self.assertEqual((self.web/'.showallopts-index.next').read_text(), 'user file')
        self.assertFalse(any('stop' in c for c in self.calls))


def preparation_case(point):
    def test(self):
        self.failure = point
        with self.assertRaises(deploy.DeployError):
            deploy.deploy(self.options)
        self.assertFalse(any('stop' in c for c in self.calls))
        self.assertEqual(self.state['active'], 'active')
        self.assertEqual(self.state['head'], OLD)
        self.assertTrue((self.live/'old-env').is_file())
    return test


def activation_case(point, after=False, interrupted=False):
    def test(self):
        self.failure, self.after = point, after
        self.exception = KeyboardInterrupt if interrupted else deploy.DeployError
        with self.assertRaises(self.exception):
            deploy.deploy(self.options)
        self.assert_restored()
    return test


for point in ('fetch','npm','build','pip','pip-check','database','import'):
    setattr(TransactionTests, 'test_preparation_failure_'+point.replace('-','_'), preparation_case(point))
for point in ('stop','merge','restart','health','assets','index'):
    setattr(TransactionTests, 'test_activation_failure_'+point, activation_case(point))
for point in ('stop','merge','restart','assets','index'):
    setattr(TransactionTests, 'test_interrupt_after_'+point, activation_case(point, after=True, interrupted=True))


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'project'
        (self.root/'config').mkdir(parents=True)
        (self.root/'config/app.config').write_text('DB_PASSWORD=preserved-secret')
        self.web = Path(self.temp.name)/'site'
        self.web.mkdir()
        self.options = deploy.parse_args(['--web-root',str(self.web)])
        self.props = {'LoadState':'loaded','User':'ubuntu','WorkingDirectory':str(self.root/'backend'),
                      'ExecStart':str(self.root/'backend/.venv/bin/python')+' '+str(self.root/'backend/run.py'),
                      'EnvironmentFiles':'','Environment':'APP_ENV_FILE=config/app.config DB_HOST=service-host'}
        self.dirty, self.branch, self.tracked = '', 'main', ''
        self.calls = []
        def run(args, **kwargs):
            self.calls.append(args)
            if args[:2] == ['git','rev-parse']: return str(self.root)
            if args[:2] == ['git','status']: return self.dirty
            if args[:2] == ['git','branch']: return self.branch
            if args[:2] == ['git','ls-files']: return self.tracked
            return ''
        for target,value in [('ROOT',self.root),('run',run),('property_value',lambda s,k:self.props[k])]:
            p=patch.object(deploy,target,value); p.start(); self.addCleanup(p.stop)
        for target,attribute,value in [(deploy.shutil,'which',lambda n:'/bin/'+n),(deploy.getpass,'getuser',lambda:'ubuntu')]:
            p=patch.object(target,attribute,value); p.start(); self.addCleanup(p.stop)

    def test_uses_service_environment_preserves_private_config(self):
        with patch.dict(deploy.os.environ,{'DB_HOST':'wrong-shell-host','DB_PASSWORD':'wrong-password'}):
            web, env=deploy.preflight(self.options)
        self.assertEqual(web,self.web)
        self.assertEqual(env['DB_HOST'],'service-host')
        self.assertNotIn('DB_PASSWORD',env)
        self.assertEqual((self.root/'config/app.config').read_text(),'DB_PASSWORD=preserved-secret')
        self.assertFalse(any('stop' in c for c in self.calls))

    def test_rejects_service_and_checkout_mismatch(self):
        for key,value in [('LoadState','not-found'),('User','root'),('WorkingDirectory','/other'),
                          ('ExecStart','/other/python'),('EnvironmentFiles','/etc/private'),('Environment','')]:
            with self.subTest(key=key), patch.dict(self.props,{key:value}):
                with self.assertRaises(deploy.DeployError): deploy.preflight(self.options)
        self.dirty=' M file'
        with self.assertRaises(deploy.DeployError): deploy.preflight(self.options)
        self.dirty=''; self.branch='other'
        with self.assertRaises(deploy.DeployError): deploy.preflight(self.options)

    def test_tracked_private_config_is_rejected(self):
        self.tracked='config/app.config'
        with self.assertRaisesRegex(deploy.DeployError,'Git-tracked'): deploy.preflight(self.options)

    def test_rejects_unsafe_urls_and_static_roots(self):
        for value in ('https://user:secret@example.org','http://example.org/path','http://example.org?q=1','file:///tmp'):
            with self.subTest(url=value), self.assertRaises(deploy.DeployError): deploy.validate_url(value)
        with self.assertRaises(deploy.DeployError): deploy.validate_url('http://remote.example',local=True)
        for value in (self.root,self.root.parent,self.root/'missing',Path('/var/www')):
            with self.subTest(path=value), self.assertRaises(deploy.DeployError): deploy.static_root(str(value))


class PrimitiveTests(unittest.TestCase):
    def test_manual_sudo_keeps_terminal_session(self):
        process = Mock(returncode=0)
        process.communicate.return_value = ('','')
        with patch.object(deploy,'NON_INTERACTIVE',False), patch.object(deploy.subprocess,'Popen',return_value=process) as popen:
            deploy.run(['sudo','-v'])
            self.assertIsNone(popen.call_args.kwargs['stdin'])
            self.assertFalse(popen.call_args.kwargs['start_new_session'])

    def test_full_sha_validation(self):
        for value in ('short',''):
            options = deploy.parse_args(['--commit',value])
            with self.subTest(sha=value), patch.object(deploy.shutil,'which',return_value='/bin/tool'):
                with self.assertRaisesRegex(deploy.DeployError,'full'):
                    deploy.preflight(options)

    def test_protected_target_refuses_runtime_and_config(self):
        for path in ('backend/.venv/bin/python','backend/.env','config/app.config','log/app.log','.runtime/state'):
            with self.subTest(path=path), patch.object(deploy,'run',return_value=path):
                with self.assertRaises(deploy.DeployError):
                    deploy.protected_target(NEW)

    def test_target_allows_example_configuration(self):
        with patch.object(deploy,'run',return_value='config/app.config.example\nbackend/run.py'):
            deploy.protected_target(NEW)

    def test_ready_rejects_old_pid_and_unowned_listener(self):
        with patch.object(deploy,'health_check'), patch.object(deploy,'identity',return_value=('active','123','new')), patch.object(deploy,'owns_listener',return_value=False):
            with self.assertRaises(deploy.DeployError):
                deploy.ready('service','http://127.0.0.1:8000')
        with patch.object(deploy,'health_check'), patch.object(deploy,'identity',return_value=('active','123','new')):
            with self.assertRaises(deploy.DeployError):
                deploy.ready('service','http://127.0.0.1:8000',previous=('active','123','old'))

    def test_ready_rejects_process_changing_during_check(self):
        with patch.object(deploy,'health_check'), patch.object(deploy,'identity',side_effect=[('active','123','new'),('active','124','newer')]), patch.object(deploy,'owns_listener',return_value=True):
            with self.assertRaises(deploy.DeployError):
                deploy.ready('service','http://127.0.0.1:8000')

    def test_lock_rejects_concurrent_deployment(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(deploy,'ROOT',Path(temp)):
            with deploy.deployment_lock():
                with self.assertRaises(deploy.DeployError):
                    with deploy.deployment_lock():
                        pass

    def test_run_noninteractive_redacts_failed_command_output(self):
        process = Mock(returncode=1)
        process.communicate.return_value = ('secret', 'password')
        with patch.object(deploy,'NON_INTERACTIVE',True), patch.object(deploy.subprocess,'Popen',return_value=process) as popen:
            with self.assertRaises(deploy.DeployError) as raised:
                deploy.run(['python','check'],env={'DB_PASSWORD':'secret'})
            self.assertNotIn('secret',str(raised.exception))
            self.assertNotIn('password',str(raised.exception))
            self.assertEqual(popen.call_args.kwargs['env']['GIT_TERMINAL_PROMPT'],'0')
            self.assertEqual(popen.call_args.kwargs['stdin'],subprocess.DEVNULL)

    def test_run_interrupt_reaps_child_group(self):
        process = Mock(pid=123)
        process.communicate.side_effect = [KeyboardInterrupt(), ('',''), ('','')]
        with patch.object(deploy.subprocess,'Popen',return_value=process), patch.object(deploy.os,'killpg') as kill:
            with self.assertRaises(KeyboardInterrupt):
                deploy.run(['npm','ci'])
            self.assertEqual([call.args[1] for call in kill.call_args_list],[signal.SIGTERM,signal.SIGKILL])
            self.assertEqual(process.communicate.call_count,3)

    def test_custom_config_paths_and_parents_are_protected(self):
        for tracked in ('private/settings', 'private', 'private/settings/child'):
            with patch.object(deploy,'run',return_value=tracked):
                with self.assertRaises(deploy.DeployError):
                    deploy.protected_target(NEW, ['private/settings'])

    def test_run_masks_repeated_termination_during_cleanup(self):
        process = Mock(pid=123)
        handlers = {signal.SIGTERM: deploy.interrupted, signal.SIGINT: deploy.interrupted}
        count = []
        def set_handler(sig, handler):
            previous = handlers[sig]
            handlers[sig] = handler
            return previous
        def communicate(**kwargs):
            count.append(True)
            if len(count) == 1:
                raise KeyboardInterrupt()
            self.assertEqual(handlers[signal.SIGTERM], signal.SIG_IGN)
            self.assertEqual(handlers[signal.SIGINT], signal.SIG_IGN)
            return '', ''
        process.communicate.side_effect = communicate
        with patch.object(deploy.subprocess,'Popen',return_value=process), patch.object(deploy.signal,'signal',side_effect=set_handler), patch.object(deploy.os,'killpg') as kill:
            with self.assertRaises(KeyboardInterrupt):
                deploy.run(['npm','ci'])
            self.assertEqual(handlers[signal.SIGTERM], deploy.interrupted)
            self.assertEqual([call.args[1] for call in kill.call_args_list],[signal.SIGTERM,signal.SIGKILL])

    def test_health_rejects_wrong_service_and_redirect(self):
        class Response(io.BytesIO):
            def geturl(self): return 'http://127.0.0.1:8000/api/v1/health'
        opener = Mock()
        opener.open.side_effect = [Response(b'{"status":"ok","service":"other"}'), Response(b'{"status":"ok","service":"showallopts-api"}')]
        with patch.object(deploy,'build_opener',return_value=opener), patch.object(deploy.time,'sleep'):
            deploy.health_check('http://127.0.0.1:8000',attempts=2)
        redirect = Response(b'{}')
        redirect.geturl = lambda: 'http://elsewhere/health'
        opener.open.side_effect = None
        opener.open.return_value = redirect
        with patch.object(deploy,'build_opener',return_value=opener):
            with self.assertRaises(deploy.DeployError):
                deploy.health_check('http://127.0.0.1:8000',attempts=1)

    def test_localhost_is_normalized_to_unambiguous_loopback(self):
        self.assertEqual(deploy.validate_url('http://localhost:8000',local=True),'http://127.0.0.1:8000')


if __name__ == '__main__':
    unittest.main()

