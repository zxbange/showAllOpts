"""Run the independent API and Vite servers on Windows or Linux."""
import argparse
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', choices=('127.0.0.1', '0.0.0.0'), default='0.0.0.0',
                        help='Frontend binding; defaults to all IPv4 interfaces. Use 127.0.0.1 for local access only.')
    return parser.parse_args(argv)


def stop_processes(processes):
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def request_stop(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    args = parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    vite = root / 'frontend' / 'node_modules' / 'vite' / 'bin' / 'vite.js'
    node = shutil.which('node')
    if not node or not vite.exists():
        setup = 'scripts/setup.ps1' if os.name == 'nt' else 'bash scripts/setup.sh'
        raise SystemExit(f'Frontend dependencies are missing. Run {setup} first.')
    for port in (8000, 5173):
        with socket.socket() as probe:
            probe.settimeout(0.3)
            if probe.connect_ex(('127.0.0.1', port)) == 0:
                raise SystemExit(f'Port {port} is already in use. Stop the existing server first.')
    processes = []
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    try:
        processes.append(subprocess.Popen(
            [sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000'],
            cwd=root / 'backend', creationflags=flags,
        ))
        processes.append(subprocess.Popen(
            [node, str(vite), '--host', args.host], cwd=root / 'frontend', creationflags=flags,
        ))
        print('\nShowAllOpts: http://127.0.0.1:5173\nAPI docs: http://127.0.0.1:8000/docs\nCtrl+C stops both services.\n', flush=True)
        if args.host == '0.0.0.0':
            print('LAN access: http://<server-ip>:5173 (frontend and /api proxy).', flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        failed = next(process for process in processes if process.poll() is not None)
        raise SystemExit(f'A service exited with code {failed.returncode}.')
    except KeyboardInterrupt:
        print('\nStopping services...')
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            stop_processes(processes)
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == '__main__':
    main()
