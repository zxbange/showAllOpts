"""Exercise installer with temporary files and mocked system operations only."""
import base64
import os
import shutil
import subprocess
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from test_autodeploy import load, ROOT

bootstrap = load('bootstrap_tests', ROOT/'deploy/automation/bootstrap.py')
PUBLIC = 'ssh-ed25519 '+base64.b64encode(b'\0\0\0\x0bssh-ed25519\0\0\0\x20'+b'x'*32).decode()


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        base = Path(self.temp.name)
        self.ssh = base/'ssh'
        self.ssh.mkdir()
        self.auth = self.ssh/'authorized_keys'
        self.auth.write_text('existing-key-without-final-newline')
        self.commands = []
        self.runner = SimpleNamespace(preflight=lambda _: None, ready=lambda *a: None,
                                      health_check=lambda *a: None,
                                      parse_args=lambda _: SimpleNamespace(service='service', api_url='api', web_url='web'))
        spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda _: None))
        for target, value in [('ROOT', base), ('SSH', self.ssh), ('DEST', base/'libexec'),
                              ('CONFIG', base/'config.json'), ('safe_parent', lambda _: None),
                              ('run', self.run_command), ('installed_matches', self.matches)]:
            p = patch.object(bootstrap, target, value)
            p.start()
            self.addCleanup(p.stop)
        for target, value in [('sys.argv', ['bootstrap', '--public-key', PUBLIC, '--commit', 'a'*40]),
                              ('pwd.getpwuid', SimpleNamespace(pw_name='ubuntu')),
                              ('importlib.util.spec_from_file_location', spec),
                              ('importlib.util.module_from_spec', self.runner),
                              ('subprocess.run', SimpleNamespace(returncode=0))]:
            if target == 'sys.argv':
                p = patch(target, value)
            else:
                p = patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)

    def run_command(self, *args, **kwargs):
        self.commands.append(args)
        if args[:3] == ('git', 'branch', '--show-current'):
            return 'main'
        if args[:3] == ('git', 'remote', 'get-url'):
            return 'https://github.com/zxbange/showAllOpts.git'
        if args[:3] == ('sudo', '-n', 'install'):
            if '-d' in args:
                Path(args[-1]).mkdir(parents=True, exist_ok=True)
            else:
                shutil.copyfile(args[-2], args[-1])
                Path(args[-1]).chmod(int(args[args.index('-m')+1], 8))
        if args[:3] == ('sudo', '-n', 'mv'):
            if not Path(args[-1]).exists():
                Path(args[-2]).rename(args[-1])
        if args[:3] == ('sudo', '-n', 'rm'):
            Path(args[-1]).unlink(missing_ok=True)
        return ''

    def matches(self, source, target, mode):
        if not target.exists():
            return False
        if target.read_bytes() != source.read_bytes():
            raise ValueError('Existing installation differs')
        return True

    def test_installs_frozen_files_preserves_keys_and_uses_existing_sudo(self):
        bootstrap.main()
        self.assertEqual(self.auth.read_text(), 'existing-key-without-final-newline\n'+bootstrap.KEY_PREFIX+PUBLIC+' showallopts-actions\n')
        self.assertEqual(self.auth.stat().st_mode & 0o777, 0o600)
        installs = [c for c in self.commands if 'install' in c]
        self.assertEqual(len(installs), 3)
        self.assertFalse(any('chown' in c or 'visudo' in c or 'ssh-keygen' in c for c in self.commands))

    def test_existing_installation_refused_before_mutation(self):
        bootstrap.CONFIG.touch()
        with self.assertRaisesRegex(ValueError, 'Existing installation'):
            bootstrap.main()
        self.assertFalse(any('install' in c for c in self.commands))

    def test_failed_preflight_does_not_install_or_authorize(self):
        self.runner.preflight = lambda _: (_ for _ in ()).throw(ValueError('unready'))
        with self.assertRaisesRegex(ValueError, 'unready'):
            bootstrap.main()
        self.assertFalse(any('install' in c for c in self.commands))
        self.assertEqual(self.auth.read_text(), 'existing-key-without-final-newline')

    def test_symlink_authorized_keys_refused(self):
        self.auth.unlink()
        self.auth.symlink_to(self.ssh/'elsewhere')
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            bootstrap.main()
        self.assertFalse(any('install' in c for c in self.commands))

    def test_duplicate_key_is_not_added(self):
        self.auth.write_text(PUBLIC+'\n')
        with self.assertRaisesRegex(ValueError, 'different entry'):
            bootstrap.main()
        self.assertEqual(self.auth.read_text(), PUBLIC+'\n')

    def test_successful_rerun_preserves_key_without_duplicates(self):
        bootstrap.main()
        previous = self.auth.read_bytes()
        bootstrap.main()
        self.assertEqual(self.auth.read_bytes(), previous)

    def test_each_install_failure_can_be_retried(self):
        real_run = self.run_command
        for target_name in ('showallopts-ssh', 'config.json'):
            for path in (bootstrap.DEST/'showallopts-ssh', bootstrap.CONFIG):
                path.unlink(missing_ok=True)
            self.auth.write_text('old-key')
            def fail(*args, **kwargs):
                if args[:3] == ('sudo', '-n', 'install') and '-d' not in args and target_name in args[-1]:
                    raise subprocess.CalledProcessError(1, 'install')
                return real_run(*args, **kwargs)
            with patch.object(bootstrap, 'run', fail), self.assertRaises(subprocess.CalledProcessError):
                bootstrap.main()
            self.assertEqual(self.auth.read_text(), 'old-key')
            bootstrap.main()
            self.assertIn('old-key\n', self.auth.read_text())

    def test_post_install_check_failure_can_be_retried(self):
        with patch.object(bootstrap.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'check')):
            with self.assertRaises(subprocess.CalledProcessError):
                bootstrap.main()
        self.assertEqual(self.auth.read_text(), 'existing-key-without-final-newline')
        bootstrap.main()
        self.assertIn(bootstrap.KEY_PREFIX, self.auth.read_text())

    def test_rejects_private_malformed_and_multiple_keys(self):
        for value in ('-----BEGIN OPENSSH PRIVATE KEY-----', 'ssh-ed25519 AAAA', PUBLIC+'\n'+PUBLIC):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bootstrap.public_key(value)


class InstalledFileTests(unittest.TestCase):
    def test_existing_file_requires_exact_content_owner_and_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp)/'source', Path(temp)/'target'
            source.write_text('expected')
            self.assertFalse(bootstrap.installed_matches(source, target, '0644'))
            target.write_text('expected')
            metadata = SimpleNamespace(st_mode=0o100644, st_uid=0, st_gid=0)
            with patch.object(Path, 'lstat', return_value=metadata):
                self.assertTrue(bootstrap.installed_matches(source, target, '0644'))
                target.write_text('different')
                with self.assertRaises(ValueError):
                    bootstrap.installed_matches(source, target, '0644')
                target.write_text('expected')
                for field, value in (('st_uid', 1000), ('st_gid', 1000), ('st_mode', 0o100666), ('st_mode', 0o120644)):
                    original = getattr(metadata, field)
                    setattr(metadata, field, value)
                    with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                        bootstrap.installed_matches(source, target, '0644')
                    setattr(metadata, field, original)
