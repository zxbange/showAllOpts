"""Run independent API/Vite servers and collect frontend output into daily logs."""
import argparse
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'backend'))
from app.core.config import Settings, resolve_env_file
from app.core.logging import ANSI_ESCAPE, configure_logging
from scripts.windows_job import protect_process_tree

logger = logging.getLogger('launcher')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', choices=('127.0.0.1', '0.0.0.0'), default='0.0.0.0',
                        help='Frontend binding; defaults to all IPv4 interfaces.')
    return parser.parse_args(argv)


def stop_processes(processes):
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning('Service did not stop in time; killing pid=%s', process.pid)
            process.kill()
            process.wait()


def request_stop(signum, frame):
    raise KeyboardInterrupt


def collect_output(stream, source_pid, default_level):
    output_logger = logging.getLogger('frontend')
    try:
        for raw in stream:
            line = ANSI_ESCAPE.sub('', raw).rstrip()
            if not line:
                continue
            level = default_level
            if re.search(r'\b(?:error|failed|failure)\b', line, re.IGNORECASE):
                level = logging.ERROR
            elif re.search(r'\bwarn(?:ing)?\b', line, re.IGNORECASE):
                level = logging.WARNING
            output_logger.log(level, line, extra={'source_pid': source_pid})
    except (OSError, ValueError):
        logger.exception('Failed to read frontend output')
    finally:
        stream.close()


def relay_backend_output(stream, source_pid, default_level):
    """Backend already writes formatted records to disk; relay without duplicating files."""
    raw_logger = logging.getLogger('app.backend.output')
    try:
        for raw in stream:
            line = raw.rstrip()
            if not line:
                continue
            if re.match(r'^\d{4}-\d{2}-\d{2}T[^ ]+ \| (?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+\| pid=\d+ \| ', line):
                if sys.stderr is not None:
                    sys.stderr.write(line + '\n')
                    sys.stderr.flush()
            else:
                raw_logger.log(default_level, line, extra={'source_pid': source_pid})
    except (OSError, ValueError):
        logger.exception('Failed to read backend output')
    finally:
        stream.close()


def start_output_readers(process, backend=False):
    readers = []
    for stream, level in ((process.stdout, logging.INFO), (process.stderr, logging.ERROR)):
        target = relay_backend_output if backend else collect_output
        reader = threading.Thread(target=target, args=(stream, process.pid, level), daemon=True)
        reader.start()
        readers.append(reader)
    return readers


def main(argv=None):
    args = parse_args(argv)
    settings = Settings()
    configure_logging(settings)
    logger.info('Starting ShowAllOpts; log_dir=%s', settings.log_path)
    logger.info('Configuration file: %s; environment variables take precedence', resolve_env_file())
    processes = []
    readers = []
    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    try:
        vite = ROOT / 'frontend' / 'node_modules' / 'vite' / 'bin' / 'vite.js'
        node = shutil.which('node')
        if not node or not vite.exists():
            setup = 'scripts/setup.ps1' if os.name == 'nt' else 'bash scripts/setup.sh'
            raise SystemExit(f'Frontend dependencies are missing. Run {setup} first.')
        for port in (8000, 5173):
            with socket.socket() as probe:
                probe.settimeout(0.3)
                if probe.connect_ex(('127.0.0.1', port)) == 0:
                    raise SystemExit(f'Port {port} is already in use. Stop the existing server first.')
        if protect_process_tree():
            logger.info('Windows process-tree cleanup enabled; closing the terminal stops all services')
        environment = dict(os.environ, PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8', NO_COLOR='1')
        backend = subprocess.Popen(
            [sys.executable, '-u', str(ROOT / 'backend' / 'run.py'), '--host', '127.0.0.1', '--port', '8000'],
            cwd=ROOT / 'backend', creationflags=flags, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding='utf-8', errors='replace', bufsize=1,
        )
        processes.append(backend)
        readers.extend(start_output_readers(backend, backend=True))
        frontend = subprocess.Popen(
            [node, str(vite), '--host', args.host], cwd=ROOT / 'frontend', creationflags=flags,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding='utf-8', errors='replace', bufsize=1, env=environment,
        )
        processes.append(frontend)
        readers.extend(start_output_readers(frontend))
        logger.info('Web: http://127.0.0.1:5173; API docs: http://127.0.0.1:8000/docs')
        if args.host == '0.0.0.0':
            logger.info('LAN access: http://<server-ip>:5173; Ctrl+C stops both services')
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        failed = next(process for process in processes if process.poll() is not None)
        raise SystemExit(f'A service exited with code {failed.returncode}.')
    except KeyboardInterrupt:
        logger.info('Shutdown requested; stopping services')
    except SystemExit as exc:
        if exc.code:
            logger.error('%s', exc)
        raise
    except Exception:
        logger.exception('Failed to run services')
        raise
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            stop_processes(processes)
            for reader in readers:
                reader.join(timeout=5)
            logger.info('ShowAllOpts stopped')
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == '__main__':
    try:
        main()
    finally:
        logging.shutdown()
