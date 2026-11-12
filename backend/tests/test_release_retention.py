"""Exercise retention with real disposable git worktrees, never production paths."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('retention_deploy', Path(__file__).resolve().parents[2]/'scripts/deploy.py')
d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        (self.root/'.gitignore').write_text('.runtime/\nbackend/.venv\nfrontend/node_modules/\nlog/\n')
        (self.root/'app').write_text('test')
        self.git('add', '.')
        self.git('commit', '-qm', 'test')
        self.runtime = self.root/'.runtime/releases'
        self.runtime.mkdir(parents=True)
        (self.root/'backend').mkdir()
        self.proc = self.root/'proc'
        self.proc.mkdir()
        for key, value in [('ROOT', self.root), ('PROC', self.proc)]:
            p = patch.object(d, key, value); p.start(); self.addCleanup(p.stop)
        self.stages = []
        for i in range(6):
            stage = self.runtime/('release'+str(i)); stage.mkdir()
            self.git('worktree', 'add', '--detach', str(stage/'source'), 'HEAD')
            (stage/'venv/bin').mkdir(parents=True)
            (stage/'venv/bin/python').write_text('test')
            (stage/'venv/deps').write_text('rebuildable')
            (stage/'status.json').write_text(json.dumps({'status':'complete'}))
            if self.stages:
                (stage/'previous-venv').symlink_to(self.stages[-1]/'venv')
            self.stages.append(stage)
        (self.root/'backend/.venv').symlink_to(self.stages[-1]/'venv')

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root, stderr=subprocess.DEVNULL).decode()

    def test_retains_current_and_two_predecessors_and_unregisters(self):
        logs = self.stages[0]/'source/log'; logs.mkdir(); (logs/'run.log').write_text('keep me')
        with d.deployment_lock():
            d.cleanup_releases()
        self.assertEqual([p.exists() for p in self.stages], [False]*3+[True]*3)
        self.assertEqual((self.root/'log/release-archive/release0/run.log').read_text(), 'keep me')
        self.assertNotIn(str(self.stages[0]/'source'), self.git('worktree','list'))

    def test_dirty_release_protects_its_predecessors(self):
        (self.stages[2]/'source/app').write_text('edited')
        d.cleanup_releases()
        self.assertTrue(all(p.exists() for p in self.stages))

    def test_missing_old_venv_does_not_count_as_rollback(self):
        (self.stages[4]/'venv/bin/python').unlink()
        d.cleanup_releases()
        self.assertEqual([p.exists() for p in self.stages], [False]*2+[True]*4)

    def test_unknown_protects_dependency_chain(self):
        (self.stages[2]/'status.json').write_text('{broken')
        d.cleanup_releases()
        self.assertTrue(all(p.exists() for p in self.stages))

    def test_unknown_retained_predecessor_protects_full_chain(self):
        (self.stages[4]/'status.json').write_text('{"status":"preparing"}')
        d.cleanup_releases()
        self.assertTrue(all(p.exists() for p in self.stages))

    def test_active_old_release_protected(self):
        proc = self.proc/'123'; proc.mkdir(); (proc/'fd').mkdir()
        (proc/'cwd').symlink_to(self.stages[1]/'source')
        (proc/'cmdline').write_bytes(b'python')
        (proc/'maps').write_text('')
        d.cleanup_releases()
        self.assertTrue(self.stages[0].exists()); self.assertTrue(self.stages[1].exists())
        self.assertFalse(self.stages[2].exists())

    def test_current_process_reference_does_not_keep_all_history(self):
        proc = self.proc/'123'; proc.mkdir(); (proc/'fd').mkdir()
        (proc/'cwd').symlink_to(self.stages[5]/'source')
        (proc/'cmdline').write_bytes(b'python'); (proc/'maps').write_text('')
        d.cleanup_releases()
        self.assertEqual([p.exists() for p in self.stages], [False]*3+[True]*3)

    def test_dirty_or_unknown_files_preserved(self):
        (self.stages[0]/'source/app').write_text('edited')
        (self.stages[1]/'source/private.txt').write_text('user')
        (self.stages[2]/'private.db').write_text('user')
        d.cleanup_releases()
        self.assertTrue(all(p.exists() for p in self.stages))

    def test_failed_preparation_removed_recovery_failure_protected(self):
        (self.stages[0]/'status.json').write_text('{"status":"preparation-failed"}')
        (self.stages[2]/'status.json').write_text('{"status":"recovery-failed"}')
        (self.stages[2]/'previous-venv').unlink()
        d.cleanup_releases()
        self.assertFalse(self.stages[0].exists()); self.assertTrue(self.stages[2].exists())

    def test_unverifiable_live_skips(self):
        (self.root/'backend/.venv').unlink()
        d.cleanup_releases()
        self.assertTrue(all(p.exists() for p in self.stages))
