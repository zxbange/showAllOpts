"""Real Windows process-tree tests using isolated ephemeral ports, never production services."""
import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows Job Object integration')

SERVER = """
import json, os, socket, sys, time
from pathlib import Path
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
server.bind(('127.0.0.1', 0))
server.listen()
Path(sys.argv[1]).write_text(json.dumps({'pid': os.getpid(), 'port': server.getsockname()[1]}))
time.sleep(60)
"""

CHILD = """
import json, os, socket, subprocess, sys, time
from pathlib import Path
directory = Path(sys.argv[1])
subprocess.Popen([sys.executable, '-c', sys.argv[2], str(directory / 'grandchild.json')],
                 creationflags=subprocess.CREATE_NO_WINDOW,
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
server.bind(('127.0.0.1', 0))
server.listen()
(directory / 'child.json').write_text(json.dumps({'pid': os.getpid(), 'port': server.getsockname()[1]}))
time.sleep(60)
"""

LAUNCHER = """
import subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts.windows_job import protect_process_tree
assert protect_process_tree()
assert protect_process_tree()  # Repeated initialization must not create nested jobs.
subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[2], sys.argv[4]],
                 creationflags=subprocess.CREATE_NO_WINDOW,
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    sys.stdin.readline()
finally:
    (Path(sys.argv[2]) / 'finally.txt').write_text('normal exit')
"""


def wait_record(path, process):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                pass
        if process.poll() is not None:
            _, stderr = process.communicate(timeout=2)
            pytest.fail('Test launcher exited before ready: ' + (stderr or ''))
        time.sleep(.05)
    pytest.fail('Test listener did not become ready')


@pytest.mark.parametrize('exit_mode', ['normal', 'forced'])
def test_launcher_exit_releases_child_and_grandchild_ports_only(tmp_path, exit_mode):
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    handles = []
    launcher = None
    unrelated = subprocess.Popen(
        [sys.executable, '-c', SERVER, str(tmp_path / 'unrelated.json')],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_record(tmp_path / 'unrelated.json', unrelated)
        launcher = subprocess.Popen(
            [sys.executable, '-u', '-c', LAUNCHER, str(ROOT), str(tmp_path), CHILD, SERVER],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        records = [wait_record(tmp_path / name, launcher) for name in ('child.json', 'grandchild.json')]
        for record in records:
            handle = kernel.OpenProcess(0x00100001, False, record['pid'])  # SYNCHRONIZE | TERMINATE
            assert handle, ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
        if exit_mode == 'forced':
            launcher.kill()  # TerminateProcess: no Python finally or signal handlers.
        else:
            launcher.stdin.write(chr(10))
            launcher.stdin.flush()
        launcher.communicate(timeout=10)
        assert (tmp_path / 'finally.txt').exists() == (exit_mode == 'normal')
        for handle in handles:
            assert kernel.WaitForSingleObject(handle, 5000) == 0, 'Descendant survived launcher exit'
        for record in records:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(('127.0.0.1', record['port']))
                probe.listen()  # A new instance can immediately reuse the released port.
        assert unrelated.poll() is None, 'Cleanup must not touch other services'
    finally:
        if launcher is not None:
            if launcher.poll() is None:
                launcher.kill()
            launcher.communicate(timeout=5)
        for handle in handles:
            if kernel.WaitForSingleObject(handle, 0) != 0:
                kernel.TerminateProcess(handle, 1)
                kernel.WaitForSingleObject(handle, 5000)
            kernel.CloseHandle(handle)
        unrelated.terminate()
        unrelated.wait(timeout=5)
