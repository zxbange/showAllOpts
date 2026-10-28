"""Offline tests only: no server, credentials, sudo or network access."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import signal
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legacy = load('legacy_deploy_tests', Path(__file__).with_name('test_deploy.py'))
deploy = legacy.deploy
entry = load('ssh_entry_tests', ROOT/'deploy/automation/showallopts-ssh')
admin = load('admin_entry_tests', ROOT/'deploy/automation/showallopts-admin')


class RestrictedTransactionTests(legacy.TransactionTests):
    """Run the existing staging/failure/recovery matrix through the new privilege path."""
    def setUp(self):
        super().setUp()
        p = patch.object(deploy, 'RESTRICTED_HELPER', True)
        p.start()
        self.addCleanup(p.stop)
        self.actual_calls = []

    def run_command(self, args, **kwargs):
        self.actual_calls.append(list(args))
        if args[:3] == ['sudo', '-n', deploy.ADMIN_HELPER]:
            self.assertEqual(len(args), 4)
            mapping = {'api-stop': ['systemctl', 'stop', 'showallopts-api'],
                       'api-restart': ['systemctl', 'restart', 'showallopts-api']}
            args = ['sudo', '-n', *mapping[args[3]]]
        return super().run_command(args, **kwargs)

    def test_activation_and_recovery_use_only_minimal_privilege(self):
        self.failure = 'index'
        with self.assertRaises(deploy.DeployError):
            deploy.deploy(self.options)
        self.assert_restored()
        privileged = [c for c in self.actual_calls if c[0] == 'sudo']
        self.assertTrue(privileged)
        self.assertTrue(all(c[:3] == ['sudo', '-n', deploy.ADMIN_HELPER] for c in privileged))
        self.assertEqual({c[3] for c in privileged}, {'api-stop', 'api-restart'})
        self.assertTrue(any(c[0] == 'rsync' for c in self.actual_calls))

    @unittest.skipUnless(hasattr(signal, 'SIGHUP'), 'Unix SSH signal')
    def test_ssh_hangup_during_activation_recovers(self):
        def health(url, attempts=20):
            if self.state['generation'] == 1:
                deploy.interrupted(signal.SIGHUP, None)
        with patch.object(deploy, 'health_check', health), self.assertRaises(deploy.DeployError):
            deploy.deploy(self.options)
        self.assert_restored()


class HelperTests(unittest.TestCase):
    def test_restricted_preflight_rejects_missing_url_wrong_service_root_and_interactive(self):
        for arguments, interactive, root in (
                ([], False, '/var/www/showallopts'),
                (['--web-url', 'https://site', '--service', 'ssh'], False, '/var/www/showallopts'),
                (['--web-url', 'https://site'], False, '/var/www/another-site'),
                (['--web-url', 'https://site'], True, '/var/www/showallopts')):
            with patch.object(deploy, 'RESTRICTED_HELPER', True), \
                    patch.object(deploy, 'NON_INTERACTIVE', not interactive), \
                    patch.object(deploy.shutil, 'which', return_value='/usr/bin/tool'), \
                    patch.object(deploy, 'static_root', return_value=Path(root)), \
                    self.assertRaisesRegex(deploy.DeployError, 'Restricted helper requires'):
                deploy.preflight(deploy.parse_args(arguments))

    def test_restricted_preflight_rejects_static_symlink_or_other_owner(self):
        for symlink, owner in ((True, 1000), (False, -1)):
            with patch.object(deploy, 'RESTRICTED_HELPER', True), \
                    patch.object(deploy, 'NON_INTERACTIVE', True), \
                    patch.object(deploy.shutil, 'which', return_value='/usr/bin/tool'), \
                    patch.object(deploy, 'static_root', return_value=Path('/var/www/showallopts')), \
                    patch.object(Path, 'rglob', return_value=[]), \
                    patch.object(Path, 'is_symlink', return_value=symlink), \
                    patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=owner)), \
                    self.assertRaisesRegex(deploy.DeployError, 'symlink-free static tree'):
                deploy.preflight(deploy.parse_args(['--web-url', 'https://site']))

    @unittest.skipUnless(hasattr(signal, 'SIGHUP'), 'Unix SSH signal')
    def test_main_installs_and_restores_ssh_hangup_handler(self):
        original = signal.getsignal(signal.SIGHUP)
        def hangup(options):
            self.assertEqual(signal.getsignal(signal.SIGHUP), deploy.interrupted)
            deploy.interrupted(signal.SIGHUP, None)
        with tempfile.TemporaryDirectory() as temp, patch.object(deploy, 'ROOT', Path(temp)), \
                patch.object(deploy, 'deploy', side_effect=hangup), patch.object(deploy.logging, 'basicConfig'), \
                patch.object(deploy.logging, 'FileHandler'):
            self.assertEqual(deploy.main(['--non-interactive', '--restricted-helper']), 1)
        self.assertEqual(signal.getsignal(signal.SIGHUP), original)

    def test_fixed_privilege_map_and_no_fallback(self):
        with patch.object(deploy, 'RESTRICTED_HELPER', True):
            self.assertEqual(deploy.sudo('-v'), ['sudo', '-n', deploy.ADMIN_HELPER, 'check'])
            self.assertEqual(deploy.sudo('nginx', '-t'), ['sudo', '-n', deploy.ADMIN_HELPER, 'nginx-test'])
            for command in [('bash',), ('systemctl', 'restart', 'ssh'), ('restorecon', '-R', '/'), ('-v', 'extra')]:
                with self.assertRaises(deploy.DeployError):
                    deploy.sudo(*command)
            for program in ('rsync', 'install', 'mv', 'rm'):
                self.assertEqual(deploy.sudo(program, 'anything'), [program, 'anything'])

    def test_admin_rejects_unknown_or_extra_arguments_before_subprocess(self):
        with patch.object(admin.os, 'geteuid', return_value=0), patch.object(admin.subprocess, 'run') as run:
            for args in ([], ['sh'], ['api-stop', 'ssh'], ['api-restart;id'], ['check', '--help']):
                self.assertEqual(admin.main(args), 2)
            run.assert_not_called()

    def test_admin_requires_root_and_ubuntu_service(self):
        with patch.object(admin.os, 'geteuid', return_value=1000), patch.object(admin.subprocess, 'run') as run:
            self.assertEqual(admin.main(['check']), 2)
            run.assert_not_called()
        with patch.object(admin.os, 'geteuid', return_value=0), patch.object(admin.subprocess, 'run',
                return_value=SimpleNamespace(returncode=0, stdout='root\n')) as run:
            self.assertEqual(admin.main(['api-restart']), 2)
            self.assertEqual(run.call_count, 1)

    def test_admin_has_no_shell_or_caller_environment(self):
        with patch.object(admin.os, 'geteuid', return_value=0), patch.object(admin.subprocess, 'run',
                return_value=SimpleNamespace(returncode=0, stdout='ubuntu\n')) as run:
            self.assertEqual(admin.main(['api-restart']), 0)
            args, kwargs = run.call_args
            self.assertEqual(args[0], ['/usr/bin/systemctl', 'restart', 'showallopts-api.service'])
            self.assertEqual(kwargs['env'], admin.SAFE_ENV)
            self.assertNotIn('shell', kwargs)
            self.assertEqual(kwargs['cwd'], '/')


class EntryTests(unittest.TestCase):
    def test_strict_command_parser(self):
        sha = 'a'*40
        self.assertIsNone(entry.parse_command('check'))
        self.assertEqual(entry.parse_command('deploy '+sha), sha)
        for command in ('', 'bash', 'deploy main', 'deploy '+'A'*40, ' deploy '+sha,
                        'deploy '+sha+'\n', 'deploy '+sha+';id', 'deploy '+sha+' extra',
                        'deploy\t'+sha, 'check\n', 'scp -t /tmp/file', 'sftp'):
            with self.subTest(command=command), self.assertRaises(ValueError):
                entry.parse_command(command)

    def test_untrusted_installed_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'runner'
            path.write_text('not installed')
            path.chmod(0o666)
            with self.assertRaises(ValueError):
                entry.trusted_file(path)

    def test_missing_or_invalid_site_url_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)/'config.json'
            node = Path(temp)/'bin'
            node.mkdir()
            (node/'node').touch()
            (node/'npm').touch()
            with patch.object(entry, 'CONFIG', config), patch.object(entry, 'trusted_file'):
                for url in ('', 'REQUIRED', 'ftp://site', 'https://u:p@site', 'https://site/path',
                            'https://site?secret=x', 'https://site:99999'):
                    config.write_text(json.dumps({'DEPLOY_WEB_URL': url, 'NODE_BIN': str(node)}))
                    with self.subTest(url=url), self.assertRaises(ValueError):
                        entry.load_config()
                config.write_text(json.dumps({'DEPLOY_WEB_URL': 'https://site:8443', 'NODE_BIN': str(node)}))
                self.assertEqual(entry.load_config()['DEPLOY_WEB_URL'], 'https://site:8443')
                config.write_text(json.dumps({'DEPLOY_WEB_URL': 'https://site', 'NODE_BIN': str(node), 'extra': 'x'}))
                with self.assertRaises(ValueError):
                    entry.load_config()


class WorkflowTests(unittest.TestCase):
    def test_fail_closed_workflow_shape(self):
        text = (ROOT/'.github/workflows/deploy.yml').read_text()
        self.assertIn('branches: [main]', text)
        self.assertIn('needs: [backend, frontend]', text)
        self.assertIn("vars.DEPLOY_ENABLED == 'true'", text)
        self.assertIn("vars.DEPLOY_PREFLIGHT_ENABLED == 'true'", text)
        self.assertIn('cancel-in-progress: false', text)
        self.assertIn('python -m pytest -q', text)
        self.assertNotIn('continue-on-error', text)
        self.assertIn('StrictHostKeyChecking=yes', text)
        self.assertNotIn('ssh-keyscan', text)
        deploy_job = text.split('\n  deploy:\n', 1)[1]
        self.assertNotIn('uses:', deploy_job)
        self.assertIn('permissions: {}', deploy_job)


if __name__ == '__main__':
    unittest.main()
