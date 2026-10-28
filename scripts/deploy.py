"""Update an existing native Linux deployment; never writes database data/configuration."""
import argparse
from contextlib import contextmanager
import datetime as dt
import getpass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import tempfile
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger('deploy')
NON_INTERACTIVE = False


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
    parser.add_argument('--commit', help='Exact tested 40-character commit SHA')
    parser.add_argument('--non-interactive', action='store_true', help='Never prompt; use existing sudo permissions only')
    return parser.parse_args(argv)


def run(args, *, capture=False, env=None, cwd=None, timeout=900):
    child_env = dict(os.environ if env is None else env)
    if NON_INTERACTIVE:
        child_env.update(GIT_TERMINAL_PROMPT='0', GIT_SSH_COMMAND='ssh -oBatchMode=yes',
                         PIP_NO_INPUT='1', CI='true')
    interactive_sudo = args[0] == 'sudo' and not NON_INTERACTIVE
    process = subprocess.Popen(args, cwd=cwd or ROOT, env=child_env, text=True,
                               stdin=None if interactive_sudo else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=not interactive_sudo)
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except BaseException:
        # Reap the entire command group before recovery can touch its files.
        handlers = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            try:
                if interactive_sudo:
                    process.terminate()  # sudo remains attached to the terminal and forwards signals.
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                process.communicate(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            # A leader can exit while a grandchild keeps running with closed pipes.
            try:
                if interactive_sudo:
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=5)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        raise
    if process.returncode:
        raise DeployError('Command failed: {} (exit {}).'.format(Path(args[0]).name, process.returncode))
    if not capture:
        LOG.info('Completed: %s', Path(args[0]).name)
    return stdout.strip()


def sudo(*args):
    return ['sudo'] + (['-n'] if NON_INTERACTIVE else []) + list(args)


def property_value(service, name):
    return run(['systemctl', 'show', service, '--property='+name, '--value'], capture=True, timeout=30)


def validate_url(value, local=False):
    url = urlsplit(value)
    if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in ('', '/')
            or (local and url.hostname not in ('localhost', '127.0.0.1', '::1'))):
        raise DeployError('Use a site origin URL without credentials, path or query; --api-url must be loopback.')
    if local and url.hostname == 'localhost':
        value = value.replace('://localhost', '://127.0.0.1', 1)
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
    if options.commit is not None and not re.fullmatch(r'[0-9a-fA-F]{40}', options.commit):
        raise DeployError('--commit requires a full 40-character SHA.')
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
    run(sudo('-v'), timeout=30)
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
    options.config_paths = []
    for candidate in (config_path, config_path.resolve()):
        if candidate.is_relative_to(ROOT):
            options.config_paths.append(candidate.relative_to(ROOT).as_posix())
    tracked = run(['git', 'ls-files'], capture=True).splitlines()
    if any(paths_overlap(path, config) for path in tracked for config in options.config_paths):
        raise DeployError('APP_ENV_FILE or its parent is Git-tracked; move private configuration outside tracked source.')
    with config_path.open('rb'):
        pass  # Check readability without printing its contents or overwriting it.
    service_env['APP_ENV_FILE'] = str(config_path.resolve())
    # Match service variables, not interactive-shell DB_* overrides.
    check_env = {'PATH': os.defpath, 'HOME': str(Path.home()), 'LANG': 'C.UTF-8', **service_env}
    run(sudo('nginx', '-t'), timeout=30)
    run(['systemctl', 'is-active', '--quiet', 'nginx'])
    return web_root, check_env


def health_check(base_url, attempts=20):
    opener = build_opener(ProxyHandler({}))  # Local checks must not use shell HTTP proxies.
    for attempt in range(attempts):
        try:
            with opener.open(base_url + '/api/v1/health', timeout=3) as response:
                if response.geturl() != base_url + '/api/v1/health':
                    raise DeployError('Health endpoint redirected; check the exact origin.')
                payload = json.load(response)
            if isinstance(payload, dict) and payload.get('status') == 'ok' and payload.get('service') == 'showallopts-api':
                return
        except (URLError, OSError, ValueError):
            pass
        if attempt + 1 < attempts:
            time.sleep(2)
    raise DeployError('Health check failed. Check systemd, application logs and the configured URL/proxy.')


def publish_frontend(web_root, dist):
    if not (dist / 'index.html').is_file() or not (dist / 'assets').is_dir():
        raise DeployError('Frontend build output is incomplete.')
    # Copy assets first, retain old hashed assets, then atomically switch index.html.
    run(sudo('rsync', '-rlt', '--checksum', '--chmod=D755,F644', '--exclude=/index.html', str(dist)+'/', str(web_root)+'/'), timeout=120)
    temporary_index = web_root / '.showallopts-index.next'
    run(sudo('install', '-m', '0644', str(dist / 'index.html'), str(temporary_index)), timeout=30)
    if shutil.which('selinuxenabled') and subprocess.run(['selinuxenabled'], capture_output=True).returncode == 0:
        run(sudo('restorecon', '-R', str(web_root)), timeout=120)
    run(sudo('mv', '-f', str(temporary_index), str(web_root / 'index.html')), timeout=30)


def clean_head(expected=None, branch=None):
    if run(['git', 'status', '--porcelain', '--untracked-files=normal'], capture=True):
        raise DeployError('Checkout changed; refusing to overwrite local files.')
    head = run(['git', 'rev-parse', 'HEAD'], capture=True)
    if expected is not None and head != expected:
        raise DeployError('Checkout HEAD changed during deployment; reconcile manually.')
    if branch is not None and run(['git', 'branch', '--show-current'], capture=True) != branch:
        raise DeployError('Checkout branch changed during deployment; reconcile manually.')
    return head


def paths_overlap(left, right):
    return left == right or left.startswith(right+'/') or right.startswith(left+'/')


def protected_target(target, config_paths=()):
    paths = run(['git', 'ls-tree', '-r', '--name-only', target], capture=True).splitlines()
    prefixes = ('.runtime', 'log', 'backend/.venv', 'backend/.env',
                'frontend/node_modules', 'frontend/dist', '.env')
    for path in paths:
        if (any(path == p or path.startswith(p+'/') for p in prefixes)
                or (path.startswith('config/') and path != 'config/app.config.example')
                or any(paths_overlap(path, config) for config in config_paths)):
            raise DeployError('Target tracks protected deployment configuration/runtime data.')


def target_commit(options, previous):
    if options.skip_pull:
        if options.commit and options.commit.lower() != previous:
            raise DeployError('--skip-pull --commit must exactly match current HEAD.')
        target = previous
    else:
        run(['git', 'fetch', options.remote, options.branch])
        fetched = run(['git', 'rev-parse', 'FETCH_HEAD^{commit}'], capture=True)
        target = options.commit.lower() if options.commit else fetched
        if run(['git', 'rev-parse', target+'^{commit}'], capture=True) != target:
            raise DeployError('Target commit did not resolve exactly.')
        run(['git', 'merge-base', '--is-ancestor', target, fetched], capture=True)
        run(['git', 'merge-base', '--is-ancestor', previous, target], capture=True)
    protected_target(target, getattr(options, 'config_paths', ()))
    return target


def identity(service):
    return tuple(property_value(service, name) for name in ('ActiveState', 'MainPID', 'InvocationID'))


def owns_listener(pid, base_url):
    """Tie HTTP readiness to the new MainPID's listening socket, not another API."""
    import ipaddress
    url = urlsplit(base_url)
    host = '127.0.0.1' if url.hostname == 'localhost' else url.hostname
    wanted = ipaddress.ip_address(host)
    port = url.port or (443 if url.scheme == 'https' else 80)
    proc = Path('/proc') / str(pid)
    inodes = set()
    try:
        for fd in (proc / 'fd').iterdir():
            try:
                link = os.readlink(fd)
                if link.startswith('socket:['):
                    inodes.add(link[8:-1])
            except FileNotFoundError:
                continue
        table = proc / 'net' / ('tcp' if wanted.version == 4 else 'tcp6')
        for line in table.read_text().splitlines()[1:]:
            fields = line.split()
            address, raw_port = fields[1].split(':')
            if fields[3] != '0A' or int(raw_port, 16) != port or fields[9] not in inodes:
                continue
            raw = bytes.fromhex(address)
            raw = b''.join(raw[i:i+4][::-1] for i in range(0, len(raw), 4))
            bound = ipaddress.ip_address(raw)
            if bound == wanted or bound.is_unspecified:
                return True
    except (OSError, ValueError, IndexError):
        pass
    return False


def ready(service, url, previous=None):
    health_check(url)
    now = identity(service)
    if now[0] != 'active' or not now[1].isdigit() or int(now[1]) <= 0 or not now[2]:
        raise DeployError('API is not an active systemd process.')
    if previous and (now[1] == previous[1] or now[2] == previous[2]):
        raise DeployError('Expected a new process and invocation after restart.')
    if not owns_listener(now[1], url):
        raise DeployError('The systemd MainPID does not own the API listening socket.')
    health_check(url, attempts=1)
    if identity(service) != now:
        raise DeployError('API process changed during readiness checks.')


def frontend_check(url, expected):
    opener = build_opener(ProxyHandler({}))
    with opener.open(url + '/index.html', timeout=5) as response:
        if response.geturl() != url+'/index.html' or response.read() != expected.read_bytes():
            raise DeployError('Website did not serve the new index.html.')
    for asset in re.findall(r'(?:src|href)=["\'](/assets/[^"\']+)["\']', expected.read_text()):
        local = expected.parent / asset.lstrip('/')
        if not local.is_file() or expected.parent.resolve() not in local.resolve().parents:
            raise DeployError('Build references an invalid frontend asset.')
        with opener.open(url + asset, timeout=5) as response:
            if response.geturl() != url+asset or response.read() != local.read_bytes():
                raise DeployError('Website asset content does not match this release.')


def prepare_release(target, stage, check_env):
    source = stage / 'source'
    run(['git', 'worktree', 'add', '--detach', str(source), target])
    run(['npm', 'ci', '--no-fund', '--no-audit'], cwd=source / 'frontend')
    run(['npm', 'run', 'build'], cwd=source / 'frontend')
    dist = source / 'frontend/dist'
    if not (dist / 'index.html').is_file() or not (dist / 'assets').is_dir():
        raise DeployError('Frontend build output is incomplete.')
    venv = stage / 'venv'  # Permanent final path: generated shebangs remain valid.
    run([sys.executable, '-m', 'venv', str(venv)])
    python = str(venv / 'bin/python')
    run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(source / 'backend/requirements.lock.txt')])
    run([python, '-m', 'pip', 'check'], capture=True)
    run([python, str(source / 'scripts/check_database.py')], capture=True, env=check_env)
    run([python, '-c', 'import app.main'], capture=True, cwd=source / 'backend', env=check_env)
    if run(['git', 'status', '--porcelain', '--untracked-files=normal'], capture=True, cwd=source):
        raise DeployError('Build changed tracked source; refusing to publish.')
    return dist, venv


def write_status(stage, status, **details):
    temporary = stage / 'status.next'
    with temporary.open('w') as stream:
        json.dump({'status': status, **details}, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(stage / 'status.json')
    fd = os.open(stage, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def restore_frontend(web_root, backup):
    run(sudo('rsync', '-rlt', '--checksum', '--chmod=D755,F644', '--exclude=/index.html',
             str(backup)+'/', str(web_root)+'/'), timeout=120)
    if (backup / 'index.html').exists():
        temporary = web_root / '.showallopts-index.restore'
        run(sudo('install', '-m', '0644', str(backup / 'index.html'), str(temporary)), timeout=30)
        run(sudo('mv', '-f', str(temporary), str(web_root / 'index.html')), timeout=30)
    else:
        run(sudo('rm', '-f', str(web_root / 'index.html')), timeout=30)
    if shutil.which('selinuxenabled') and subprocess.run(['selinuxenabled'], capture_output=True).returncode == 0:
        run(sudo('restorecon', '-R', str(web_root)), timeout=120)
    for item in backup.rglob('*'):
        if item.is_file():
            restored = web_root / item.relative_to(backup)
            with item.open('rb') as old, restored.open('rb') as current:
                if hashlib.file_digest(old, 'sha256').digest() != hashlib.file_digest(current, 'sha256').digest():
                    raise DeployError('Restored static content did not match its backup.')
    if not (backup / 'index.html').exists() and (web_root / 'index.html').exists():
        raise DeployError('Initial deployment index was not removed during recovery.')


def deploy(options):
    web_root, check_env = preflight(options)
    previous = clean_head(branch=options.branch)
    before = identity(options.service)
    if before[0] not in ('active', 'inactive', 'failed'):
        raise DeployError('Service is changing state; wait for it to settle.')
    was_active = before[0] == 'active'
    if was_active:
        ready(options.service, options.api_url)
    runtime = ROOT / '.runtime/releases'
    runtime.mkdir(parents=True, exist_ok=True)
    for record in runtime.glob('*/status.json'):
        if json.loads(record.read_text()).get('status') in ('activating', 'recovery-failed'):
            raise DeployError('Unfinished deployment: inspect {} before another release.'.format(record))
    target = target_commit(options, previous)
    stage = Path(tempfile.mkdtemp(prefix=target[:12]+'-', dir=runtime))
    os.chmod(stage, 0o700)
    details = {'previous': previous, 'target': target, 'was_active': was_active,
               'branch': options.branch, 'service': options.service, 'web_root': str(web_root)}
    write_status(stage, 'preparing', **details)
    live, saved = ROOT / 'backend/.venv', stage / 'previous-venv'
    static_touched = activation_started = False
    try:
        LOG.info('Preparing commit %s; running service unchanged.', target)
        dist, venv = prepare_release(target, stage, check_env)
        for directory in (web_root, dist):
            for name in ('.showallopts-index.next', '.showallopts-index.restore'):
                if (directory / name).exists() or (directory / name).is_symlink():
                    raise DeployError('Reserved frontend temporary filename already exists; inspect it manually.')
        if any(p.is_symlink() for p in web_root.rglob('*')):
            raise DeployError('Static root contains symlinks; use an ordinary dedicated site directory.')
        backup = stage / 'previous-static'
        shutil.copytree(web_root, backup)
        if not live.exists():
            raise DeployError('Existing backend/.venv is missing.')
        clean_head(previous, branch=options.branch)
        if identity(options.service) != before:
            raise DeployError('Service changed during preparation; not stopping it.')
        write_status(stage, 'activating', **details)
        activation_started = True
        run(sudo('systemctl', 'stop', options.service), timeout=60)
        if property_value(options.service, 'ActiveState') not in ('inactive', 'failed'):
            raise DeployError('Cannot confirm API stopped; refusing activation.')
        clean_head(previous, branch=options.branch)
        run(['git', 'merge', '--ff-only', '--no-overwrite-ignore', target])
        clean_head(target, branch=options.branch)
        live.rename(saved)
        live.symlink_to(venv, target_is_directory=True)
        run(sudo('systemctl', 'restart', options.service), timeout=60)
        ready(options.service, options.api_url, previous=before if was_active else None)
        static_touched = True
        publish_frontend(web_root, dist)
        if options.web_url:
            health_check(options.web_url)
            frontend_check(options.web_url, dist / 'index.html')
        ready(options.service, options.api_url, previous=before if was_active else None)
        write_status(stage, 'complete', **details)
        LOG.info('Deployment complete: %s. Recovery files retained: %s', target, stage)
        if not options.web_url:
            LOG.warning('Website proxy/static checks skipped: supply --web-url.')
    except BaseException:
        if not activation_started:
            write_status(stage, 'preparation-failed', **details)
            raise
        handlers = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
        errors = []
        try:
            try:
                run(sudo('systemctl', 'stop', options.service), timeout=60)
                if property_value(options.service, 'ActiveState') not in ('inactive', 'failed'):
                    raise DeployError('Cannot confirm API stopped for recovery.')
                head = clean_head(branch=options.branch)
                if head not in (previous, target):
                    raise DeployError('Checkout changed; manual recovery required.')
                if head != previous:
                    run(['git', 'switch', '--detach', '--no-overwrite-ignore', previous])
                    clean_head(previous)
                if saved.exists() or saved.is_symlink():
                    if live.is_symlink() and live.resolve() == venv.resolve():
                        live.unlink()
                    elif live.exists() or live.is_symlink():
                        raise DeployError('Environment path changed; refusing to overwrite it.')
                    saved.rename(live)
                if was_active:
                    run(sudo('systemctl', 'restart', options.service), timeout=60)
                    ready(options.service, options.api_url)
            except Exception:
                errors.append('api')
                LOG.error('API recovery failed; retained code/environment backups require manual inspection.')
            if static_touched:
                try:
                    restore_frontend(web_root, backup)
                    if options.web_url and (backup / 'index.html').exists():
                        frontend_check(options.web_url, backup / 'index.html')
                        if was_active:
                            health_check(options.web_url)
                except Exception:
                    errors.append('static')
                    LOG.error('Static recovery failed; previous-static is retained.')
            write_status(stage, 'recovery-failed' if errors else 'recovered', **details)
            if errors:
                raise DeployError('Release failed AND recovery is incomplete. Inspect '+str(stage))
            LOG.error('Release failed; prior code/environment and old static files restored; extra new assets retained. '
                      'Service was %s. Git may be detached at %s; reconcile before redeploying. '
                      'No database/application-data rollback was attempted.', before[0], previous)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        raise


def interrupted(signum, frame):
    raise DeployError('Interrupted by signal {}; attempting recovery if activation began.'.format(signum))


def main(argv=None):
    global NON_INTERACTIVE
    options = parse_args(argv)
    NON_INTERACTIVE = options.non_interactive
    if not sys.platform.startswith('linux') or os.geteuid() == 0 or sys.version_info < (3, 11):
        print('Run on Linux with Python 3.11+ as the service user, without sudo.', file=sys.stderr)
        return 1
    handlers = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        with deployment_lock():
            log_dir = ROOT / 'log'
            log_dir.mkdir(exist_ok=True)
            logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | deploy | %(message)s',
                                handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(
                                    log_dir / ('deploy-'+dt.date.today().isoformat()+'.log'), encoding='utf-8')])
            deploy(options)
    except (DeployError, OSError, ValueError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        LOG.error('Deployment failed: %s', str(exc) if isinstance(exc, DeployError) else type(exc).__name__)
        return 1
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

