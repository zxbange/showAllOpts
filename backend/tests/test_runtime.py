"""Regression checks for cross-platform startup and subprocess cleanup."""
import importlib.util
from pathlib import Path
import signal
import subprocess
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('project_dev', ROOT / 'scripts' / 'dev.py')
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)


def test_bind_address_allows_lan_by_default_and_local_is_explicit():
    assert dev.parse_args([]).host == '0.0.0.0'
    assert dev.parse_args(['--host', '127.0.0.1']).host == '127.0.0.1'
    with pytest.raises(SystemExit):
        dev.parse_args(['--host', 'unexpected-host'])


def test_stop_reaps_exited_children_and_kills_only_after_timeout():
    finished = Mock()
    finished.poll.return_value = 0
    running = Mock()
    running.poll.return_value = None
    running.wait.side_effect = [subprocess.TimeoutExpired('server', 5), 0]
    dev.stop_processes([finished, running])
    finished.terminate.assert_not_called()
    finished.kill.assert_not_called()
    finished.wait.assert_called_once_with(timeout=5)
    running.terminate.assert_called_once()
    running.kill.assert_called_once()
    assert running.wait.call_count == 2


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(dev.shutil, 'which', lambda name: '/usr/bin/node')
    monkeypatch.setattr(dev.Path, 'exists', lambda self: True)
    probe = Mock()
    probe.connect_ex.return_value = 1
    socket_context = Mock()
    socket_context.__enter__ = Mock(return_value=probe)
    socket_context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(dev.socket, 'socket', Mock(return_value=socket_context))
    signal_setter = Mock(return_value=signal.SIG_DFL)
    monkeypatch.setattr(dev.signal, 'signal', signal_setter)
    return probe, signal_setter


def test_sigterm_stops_both_services_and_keeps_api_local(monkeypatch, runtime):
    backend, frontend = Mock(), Mock()
    backend.poll.return_value = frontend.poll.return_value = None
    spawn = Mock(side_effect=[backend, frontend])
    monkeypatch.setattr(dev.subprocess, 'Popen', spawn)
    monkeypatch.setattr(dev.time, 'sleep', lambda _: dev.request_stop(signal.SIGTERM, None))
    dev.main([])
    backend_args = spawn.call_args_list[0].args[0]
    frontend_args = spawn.call_args_list[1].args[0]
    assert backend_args[backend_args.index('--host') + 1] == '127.0.0.1'
    assert frontend_args[frontend_args.index('--host') + 1] == '0.0.0.0'
    for process in (backend, frontend):
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
    runtime[1].assert_called_with(signal.SIGTERM, signal.SIG_DFL)


def test_failed_frontend_spawn_cleans_up_started_backend(monkeypatch, runtime):
    backend = Mock()
    backend.poll.return_value = None
    monkeypatch.setattr(dev.subprocess, 'Popen', Mock(side_effect=[backend, OSError('spawn failed')]))
    with pytest.raises(OSError, match='spawn failed'):
        dev.main([])
    backend.terminate.assert_called_once()
    backend.wait.assert_called_once_with(timeout=5)


def test_busy_port_prevents_starting_any_process(monkeypatch, runtime):
    runtime[0].connect_ex.return_value = 0
    spawn = Mock()
    monkeypatch.setattr(dev.subprocess, 'Popen', spawn)
    with pytest.raises(SystemExit, match='already in use'):
        dev.main([])
    spawn.assert_not_called()
