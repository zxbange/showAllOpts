"""Readiness metadata fixtures: no production /proc, credentials or service access."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from test_deploy import deploy


class ProcessReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = Path(self.temp.name)
        self.group = '/system.slice/showallopts-api.service'
        self.metadata(100, 1, 10)
        self.metadata(101, 100, 11)
        self.metadata(102, 101, 12)
        for name, value in [('PROC', self.proc), ('health_check', lambda *a, **k: None),
                            ('identity', lambda _: ('active', '100', 'invocation-new')),
                            ('property_value', lambda *a: self.group),
                            ('owns_listener', lambda pid, _: pid == '102')]:
            p = patch.object(deploy, name, value)
            p.start()
            self.addCleanup(p.stop)

    def metadata(self, pid, parent, start, group=None):
        proc = self.proc/str(pid)
        proc.mkdir(exist_ok=True)
        fields = ['S', str(parent)]+['0']*17+[str(start)]
        (proc/'stat').write_text(str(pid)+' (name with ) spaces) '+' '.join(fields))
        (proc/'cgroup').write_text('0::'+(group or self.group)+'\n')

    def test_xvfb_wrapper_child_listener_is_ready(self):
        deploy.ready('service', 'http://127.0.0.1:8000', ('active', '90', 'invocation-old'))

    def test_external_listener_even_with_successful_http_is_rejected(self):
        self.metadata(102, 101, 12, '/system.slice/other.service')
        with self.assertRaises(deploy.DeployError):
            deploy.ready('service', 'http://127.0.0.1:8000')

    def test_same_cgroup_non_descendant_is_rejected(self):
        self.metadata(102, 1, 12)
        with self.assertRaises(deploy.DeployError):
            deploy.ready('service', 'http://127.0.0.1:8000')

    def test_pid_reuse_during_http_is_rejected(self):
        def health(*a, **kw):
            if kw.get('attempts') == 1:
                self.metadata(102, 101, 99)
        with patch.object(deploy, 'health_check', health), self.assertRaises(deploy.DeployError):
            deploy.ready('service', 'http://127.0.0.1:8000')

    def test_parent_pid_reuse_is_rejected(self):
        def health(*a, **kw):
            if kw.get('attempts') == 1:
                self.metadata(101, 100, 99)
        with patch.object(deploy, 'health_check', health), self.assertRaises(deploy.DeployError):
            deploy.ready('service', 'http://127.0.0.1:8000')

    def test_restart_with_same_pid_during_check_is_rejected(self):
        with patch.object(deploy, 'identity', side_effect=[('active','100','new'), ('active','100','newer')]), self.assertRaises(deploy.DeployError):
            deploy.ready('service', 'http://127.0.0.1:8000')

    def test_previous_invocation_is_rejected(self):
        with self.assertRaises(deploy.DeployError):
            deploy.ready('service', 'http://127.0.0.1:8000', ('active','90','invocation-new'))

    def test_missing_metadata_fails_closed(self):
        (self.proc/'100/stat').unlink()
        with self.assertRaises(deploy.DeployError):
            deploy.ready('service', 'http://127.0.0.1:8000')

    def test_listener_closing_during_check_is_rejected(self):
        with patch.object(deploy, 'owns_listener', side_effect=[False, False, True, False]), self.assertRaises(deploy.DeployError):
            deploy.ready('service', 'http://127.0.0.1:8000')

    def test_pid_reuse_during_final_socket_check_is_rejected(self):
        calls = 0
        def listener(pid, url):
            nonlocal calls
            if pid == '102':
                calls += 1
                if calls == 2:
                    self.metadata(102, 101, 99)
                return True
            return False
        with patch.object(deploy, 'owns_listener', listener), self.assertRaises(deploy.DeployError):
            deploy.ready('service', 'http://127.0.0.1:8000')

    def test_cgroup_change_during_final_socket_check_is_rejected(self):
        calls = 0
        def listener(pid, url):
            nonlocal calls
            if pid == '102':
                calls += 1
                if calls == 2:
                    self.metadata(102, 101, 12, '/system.slice/other.service')
                return True
            return False
        with patch.object(deploy, 'owns_listener', listener), self.assertRaises(deploy.DeployError):
            deploy.ready('service', 'http://127.0.0.1:8000')
