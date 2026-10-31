"""Readiness metadata fixtures: no production /proc, credentials or service access."""
import tempfile
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from test_deploy import deploy


@unittest.skipUnless(os.name == 'posix', 'Linux readiness uses uid and proc metadata')
class ProcessReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = Path(self.temp.name)
        self.group = '/system.slice/showallopts-api.service'
        (self.proc/'self/ns').mkdir(parents=True)
        (self.proc/'self/ns/net').touch()
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
        (proc/'status').write_text('Uid:\t'+'\t'.join([str(os.getuid())]*4)+'\n')
        (proc/'ns').mkdir(exist_ok=True)
        (proc/'ns/net').unlink(missing_ok=True)
        os.link(self.proc/'self/ns/net', proc/'ns/net')

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


    def change_namespace(self, pid):
        namespace = self.proc/str(pid)/'ns/net'
        namespace.unlink()
        namespace.touch()

    def test_other_network_namespace_on_listener_or_ancestor_is_rejected(self):
        for pid in (100, 101, 102):
            with self.subTest(pid=pid):
                self.change_namespace(pid)
                with self.assertRaises(deploy.DeployError):
                    deploy.ready('service', 'http://127.0.0.1:8000')
                self.metadata(pid, pid-1 if pid != 100 else 1, pid-90)

    def test_network_namespace_change_during_final_socket_check_is_rejected(self):
        for pid in (100, 101, 102, 'self'):
            with self.subTest(pid=pid):
                calls = 0
                def listener(candidate, url):
                    nonlocal calls
                    if candidate == '102':
                        calls += 1
                        if calls == 2:
                            self.change_namespace(pid)
                        return True
                    return False
                with patch.object(deploy, 'owns_listener', listener), self.assertRaises(deploy.DeployError):
                    deploy.ready('service', 'http://127.0.0.1:8000')
                for current in (100, 101, 102):
                    self.metadata(current, current-1 if current != 100 else 1, current-90)

    def test_other_uid_on_listener_or_ancestor_is_rejected(self):
        for pid in (100, 101, 102):
            for uid_field in range(4):
                with self.subTest(pid=pid, uid_field=uid_field):
                    uids = [str(os.getuid())]*4
                    uids[uid_field] = str(os.getuid()+1)
                    (self.proc/str(pid)/'status').write_text('Uid:\t'+'\t'.join(uids)+'\n')
                    with self.assertRaises(deploy.DeployError):
                        deploy.ready('service', 'http://127.0.0.1:8000')
                    self.metadata(pid, pid-1 if pid != 100 else 1, pid-90)

    def test_invalid_start_time_fails_closed(self):
        for start in (0, -1, 'bad', 2**64):
            with self.subTest(start=start):
                self.metadata(102, 101, start)
                with self.assertRaises(deploy.DeployError):
                    deploy.ready('service', 'http://127.0.0.1:8000')

    def test_missing_namespace_or_uid_metadata_fails_closed(self):
        for name in ('ns/net', 'status'):
            with self.subTest(name=name):
                (self.proc/'102'/name).unlink()
                with self.assertRaises(deploy.DeployError):
                    deploy.ready('service', 'http://127.0.0.1:8000')
                self.metadata(102, 101, 12)


    def test_uid_change_during_final_socket_check_is_rejected(self):
        calls = 0
        def listener(pid, url):
            nonlocal calls
            if pid == '102':
                calls += 1
                if calls == 2:
                    (self.proc/'102/status').write_text('Uid:\t'+'\t'.join([str(os.getuid()+1)]*4)+'\n')
                return True
            return False
        with patch.object(deploy, 'owns_listener', listener), self.assertRaises(deploy.DeployError):
            deploy.ready('service', 'http://127.0.0.1:8000')
