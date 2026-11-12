import logging
from unittest.mock import Mock

import pytest
from uvicorn.supervisors import multiprocess

from app.core.worker_diagnostics import DiagnosticProcess, worker_diagnostics, WORKER_HEALTHCHECK_SECONDS


def process(alive=True, exitcode=None):
    p = object.__new__(DiagnosticProcess)
    p.process = Mock(pid=123, exitcode=exitcode)
    p.process.is_alive.return_value = alive
    p.ping = Mock(return_value=True)
    return p


def test_normal_is_quiet(caplog):
    p = process()
    with caplog.at_level(logging.INFO):
        assert p.is_alive(30)
    p.ping.assert_called_once_with(30)
    assert not caplog.records


def test_dead_process_not_pinged(caplog):
    p = process(False, -11)
    with caplog.at_level(logging.WARNING):
        assert not p.is_alive(30)
    p.ping.assert_not_called()
    assert 'alive_before=False exitcode_before=-11 heartbeat=not_sent' in caplog.text
    p.process.kill.assert_not_called()


def test_unanswered_ping_diagnostic_precedes_supervisor_kill(caplog):
    p = process()
    p.ping.return_value = False
    with caplog.at_level(logging.WARNING):
        assert not p.is_alive(30)
    assert 'alive_before=True exitcode_before=None heartbeat=no_response' in caplog.text
    assert 'timeout_seconds=30' in caplog.text
    p.process.kill.assert_not_called()


def test_exit_during_ping_reported(caplog):
    p = process()
    def ping(timeout):
        p.process.exitcode = 1
        p.process.is_alive.return_value = False
        return False
    p.ping.side_effect = ping
    with caplog.at_level(logging.WARNING):
        assert not p.is_alive()
    assert 'alive_after=False exitcode_after=1' in caplog.text


def test_join_logs_post_reap_code(caplog):
    p = process(False, -9)
    with caplog.at_level(logging.INFO):
        p.join()
    p.process.join.assert_called_once_with()
    assert 'Worker reaped pid=123 exitcode=-9' in caplog.text


def test_install_restore_even_on_error():
    original = multiprocess.Process
    with pytest.raises(RuntimeError):
        with worker_diagnostics():
            assert multiprocess.Process is DiagnosticProcess
            raise RuntimeError()
    assert multiprocess.Process is original
    assert WORKER_HEALTHCHECK_SECONDS == 30


@pytest.mark.parametrize('alive,responds', [(False, False), (True, False), (True, True)])
def test_supervisor_policy_unchanged(monkeypatch, alive, responds):
    """Exercise locked Uvicorn's actual supervisor with instrumented processes."""
    from uvicorn import Config
    p = process(alive, None if alive else 1)
    p.ping.return_value = responds
    p.parent_conn = Mock()
    p.child_conn = Mock()
    import threading
    supervisor = object.__new__(multiprocess.Multiprocess)
    supervisor.config = Config('app.main:app', workers=1, timeout_worker_healthcheck=30)
    supervisor.sockets = []
    supervisor.should_exit = threading.Event()
    supervisor.processes = [p]
    replacement = Mock()
    replacement_factory = Mock(return_value=replacement)
    monkeypatch.setattr(multiprocess, 'Process', replacement_factory)
    supervisor.keep_subprocess_alive()
    if alive and responds:
        p.process.kill.assert_not_called()
        replacement_factory.assert_not_called()
        assert supervisor.processes == [p]
    else:
        p.process.kill.assert_called_once()
        p.process.join.assert_called_once()
        replacement.start.assert_called_once()
        assert supervisor.processes == [replacement]


def test_entrypoint_installs_diagnostics_and_timeout(monkeypatch):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('tested_api_entrypoint', Path(__file__).parents[1] / 'run.py')
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    monkeypatch.setattr('sys.argv', ['run.py', '--workers', '2'])
    monkeypatch.setattr(entry, 'Settings', lambda: SimpleNamespace(log_path='logs'))
    monkeypatch.setattr(entry, 'configure_logging', Mock())
    monkeypatch.setattr(entry, 'logging_config', lambda settings: {})
    monkeypatch.setattr(entry.logging, 'shutdown', Mock())
    original = multiprocess.Process
    def run(app, **kwargs):
        assert app == 'app.main:app'
        assert kwargs['workers'] == 2
        assert kwargs['timeout_worker_healthcheck'] == 30
        assert multiprocess.Process is DiagnosticProcess
    monkeypatch.setattr(entry.uvicorn, 'run', run)
    entry.main()
    assert multiprocess.Process is original
