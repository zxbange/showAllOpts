"""Update an existing native Linux deployment; never writes database data/configuration."""
import argparse
from contextlib import contextmanager
import datetime as dt
import getpass
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger('deploy')


class DeployError(RuntimeError):
    pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Update an existing Linux systemd + Nginx deployment.')
    parser.add_argument('--web-root', default='/var/www/showallopts', help='Existing Nginx static root')
    parser.add_argument('--service', default='showallopts-api', help='Existing systemd API service')
    parser.add_argument('--remote', default='origin')
    parser.add_argument('--branch', default='main')
    parser.add_argument('--skip-pull', action='store_true', help='Deploy the current clean local commit without fetching')
    parser.add_argument('--api-url', default='http://127.0.0.1:8000', help='Direct local API URL')
    parser.add_argument('--web-url', help='Optional actual website URL, including custom HTTP/HTTPS port')
    return parser.parse_args(argv)


def run(args, *, capture=False, env=None, cwd=None):
    # Do not print command arguments/environment: systemd Environment may contain secrets.
    if capture:
        result = subprocess.run(args, cwd=cwd or ROOT, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise DeployError('Command failed: {} (exit {}).'.format(args[0], result.returncode))
        return result.stdout.strip()
    with subprocess.Popen(args, cwd=cwd or ROOT, env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, errors='replace') as process:
        for line in process.stdout:
            LOG.info('%s', line.rstrip())
        if process.wait():
            raise DeployError('Command failed: {} (exit {}).'.format(args[0], process.returncode))
    return ''


def property_value(service, name):
    return run(['systemctl', 'show', service, '--property='+name, '--value'], capture=True)


def validate_url(value, local=False):
    url = urlsplit(value)
    if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in ('', '/')
            or (local and url.hostname not in ('localhost', '127.0.0.1', '::1'))):
        raise DeployError('Use a site origin URL without credentials, path or query; --api-url must be loopback.')
    return value.rstrip('/')


def static_root(value):
    path = Path(value)
    if not path.is_absolute():
        raise DeployError('--web-root must be an absolute existing directory.')
    path = path.resolve()
    reserved = {'/', '/home', '/var', '/var/www', '/usr', '/usr/share', '/usr/share/nginx', '/etc', '/tmp'}
    if (str(path) in reserved or path == ROOT or path in ROOT.parents or ROOT in path.parents
            or not path.is_dir()):
        raise DeployError('--web-root must be the dedicated existing site directory, outside the source tree.')
    return path


@contextmanager
def deployment_lock():
    import fcntl  # Linux only; keep help and unit tests portable.
    runtime = ROOT / '.runtime'
    runtime.mkdir(exist_ok=True)
    with (runtime / 'deploy.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError('Another deployment is running in this project.') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def preflight(options):
    for name in ('git', 'node', 'npm', 'sudo', 'systemctl', 'rsync'):
        if not shutil.which(name):
            raise DeployError('Missing executable: {}. Load your Node.js/nvm environment first.'.format(name))
    for value in (options.service, options.remote, options.branch):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]*', value):
            raise DeployError('Invalid service, remote or branch name.')
    options.api_url = validate_url(options.api_url, local=True)
    if options.web_url:
        options.web_url = validate_url(options.web_url)
    web_root = static_root(options.web_root)
    if Path(run(['git', 'rev-parse', '--show-toplevel'], capture=True)).resolve() != ROOT:
        raise DeployError('Project must be its own Git checkout.')
    if run(['git', 'status', '--porcelain', '--untracked-files=normal'], capture=True):
        raise DeployError('Working tree has local changes/untracked files. Commit or move them first; ignored config/logs are preserved.')
    if run(['git', 'branch', '--show-current'], capture=True) != options.branch:
        raise DeployError('Checkout does not match --branch. Switch branches explicitly before deploying.')
    run(['node', '-e', 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a<22 || (a===22 && b<12) ? 1 : 0)'])
    run(['sudo', '-v'])
    if property_value(options.service, 'LoadState') != 'loaded':
        raise DeployError('Install and configure the systemd service first.')
    if property_value(options.service, 'User') != getpass.getuser():
        raise DeployError('Run as the systemd service user, without sudo on deploy.sh.')
    if Path(property_value(options.service, 'WorkingDirectory')).resolve() != ROOT / 'backend':
        raise DeployError('systemd WorkingDirectory does not match this checkout; fix its override first.')
    command = property_value(options.service, 'ExecStart')
    if str(ROOT / 'backend/.venv/bin/python') not in command or str(ROOT / 'backend/run.py') not in command:
        raise DeployError('systemd ExecStart does not use this checkout; fix its override first.')
    if property_value(options.service, 'EnvironmentFiles'):
        raise DeployError('Use the documented APP_ENV_FILE configuration, not systemd EnvironmentFile, before deploying.')
    service_env = dict(item.split('=', 1) for item in shlex.split(property_value(options.service, 'Environment')) if '=' in item)
    config = service_env.get('APP_ENV_FILE', '')
    if not config:
        raise DeployError('Set APP_ENV_FILE=config/app.config in the systemd override first.')
    config_path = Path(config)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    with config_path.open('rb'):
        pass  # Check readability without printing its contents or overwriting it.
    service_env['APP_ENV_FILE'] = str(config_path.resolve())
    # Match service variables, not interactive-shell DB_* overrides.
    check_env = {'PATH': os.defpath, 'HOME': str(Path.home()), 'LANG': 'C.UTF-8', **service_env}
    run(['sudo', 'nginx', '-t'])
    run(['systemctl', 'is-active', '--quiet', 'nginx'])
    return web_root, check_env


def health_check(base_url, attempts=20):
    opener = build_opener(ProxyHandler({}))  # Local checks must not use shell HTTP proxies.
    for attempt in range(attempts):
        try:
            with opener.open(base_url + '/api/v1/health', timeout=3) as response:
                payload = json.load(response)
            if isinstance(payload, dict) and payload.get('status') == 'ok' and payload.get('service') == 'showallopts-api':
                return
        except (URLError, OSError, ValueError):
            pass
        if attempt + 1 < attempts:
            time.sleep(2)
    raise DeployError('Health check failed. Check systemd, application logs and the configured URL/proxy.')


def publish_frontend(web_root):
    dist = ROOT / 'frontend/dist'
    if not (dist / 'index.html').is_file() or not (dist / 'assets').is_dir():
        raise DeployError('Frontend build output is incomplete.')
    # Copy assets first, retain old hashed assets, then atomically switch index.html.
    run(['sudo', 'rsync', '-rlt', '--chmod=D755,F644', '--exclude=/index.html', str(dist)+'/', str(web_root)+'/'])
    temporary_index = web_root / '.showallopts-index.next'
    run(['sudo', 'install', '-m', '0644', str(dist / 'index.html'), str(temporary_index)])
    if shutil.which('selinuxenabled') and subprocess.run(['selinuxenabled'], capture_output=True).returncode == 0:
        run(['sudo', 'restorecon', '-R', str(web_root)])
    run(['sudo', 'mv', '-f', str(temporary_index), str(web_root / 'index.html')])


def deploy(options):
    LOG.info('[1/7] Checking deployment user, working tree, configuration and Nginx')
    web_root, check_env = preflight(options)
    previous = run(['git', 'rev-parse', 'HEAD'], capture=True)
    LOG.info('Previous commit: %s', previous)
    LOG.info('[2/7] Updating source code')
    if not options.skip_pull:
        run(['git', 'fetch', options.remote, options.branch])
        # Reject local-only commits even when git pull --ff-only would report up-to-date.
        run(['git', 'merge-base', '--is-ancestor', 'HEAD', 'FETCH_HEAD'], capture=True)
        run(['git', 'merge', '--ff-only', 'FETCH_HEAD'])
    current = run(['git', 'rev-parse', 'HEAD'], capture=True)
    LOG.info('Target commit: %s', current)
    LOG.info('[3/7] Installing frontend dependencies and building')
    run(['npm', 'ci', '--no-fund', '--no-audit'], cwd=ROOT / 'frontend')
    run(['npm', 'run', 'build'], cwd=ROOT / 'frontend')
    if not (ROOT / 'frontend/dist/index.html').is_file():
        raise DeployError('Build did not produce index.html.')
    LOG.info('[4/7] Stopping API and installing backend dependencies (maintenance interval begins)')
    run(['sudo', 'systemctl', 'stop', options.service])
    run([sys.executable, '-m', 'pip', 'install', '-r', str(ROOT / 'backend/requirements.lock.txt')])
    LOG.info('[5/7] Checking database reads using the systemd configuration')
    # Capture errors to avoid logging validation errors or connection credentials.
    try:
        run([sys.executable, str(ROOT / 'scripts/check_database.py')], capture=True, env=check_env)
    except DeployError:
        raise DeployError('Database read check failed. Check the systemd-selected config, database network and industry_momentum read permission; API remains stopped.') from None
    LOG.info('[6/7] Restarting API and waiting for readiness')
    run(['sudo', 'systemctl', 'restart', options.service])
    health_check(options.api_url)
    run(['systemctl', 'is-active', '--quiet', options.service])
    LOG.info('[7/7] Publishing frontend assets and switching the entry page')
    publish_frontend(web_root)
    if options.web_url:
        health_check(options.web_url)
    else:
        LOG.info('Website proxy check skipped: pass --web-url with the actual website origin to enable it.')
    LOG.info('Deployment complete: %s. Refresh the browser and verify module data.', current)


def main(argv=None):
    options = parse_args(argv)
    if not sys.platform.startswith('linux') or os.geteuid() == 0:
        print('Run on Linux as the existing deployment/service user: bash deploy.sh (without sudo).', file=sys.stderr)
        return 1
    if sys.version_info < (3, 11):
        print('Python 3.11+ is required.', file=sys.stderr)
        return 1
    try:
        with deployment_lock():
            log_dir = ROOT / 'log'
            log_dir.mkdir(exist_ok=True)
            log_path = log_dir / ('deploy-' + dt.date.today().isoformat() + '.log')
            logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)-7s | deploy | %(message)s',
                                handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_path, encoding='utf-8')])
            LOG.info('Release log: %s', log_path)
            deploy(options)
    except (DeployError, OSError, ValueError, KeyboardInterrupt) as exc:
        # Never automatically reset Git, replace service/site configuration or revert the database.
        message = str(exc) if isinstance(exc, DeployError) else type(exc).__name__
        LOG.error('Deployment stopped: %s', message)
        LOG.error('No automatic rollback. Code/dependencies may already have changed; API may be stopped. Fix the failure and rerun. See log/deploy-YYYY-MM-DD.log and systemctl status.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
