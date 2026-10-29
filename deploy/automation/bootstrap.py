#!/usr/bin/python3 -I
"""One-time installer run by ubuntu from an archived, reviewed commit; no key generation."""
import argparse
import base64
import json
import importlib.util
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import tempfile

ROOT = Path('/home/ubuntu/showAllOpts')
DEST = Path('/usr/local/libexec')
CONFIG = Path('/etc/showallopts-deploy.json')
SSH = Path('/home/ubuntu/.ssh')
NODE = '/home/ubuntu/.nvm/versions/node/v22.23.3/bin'
KEY_PREFIX = 'restrict,command="/usr/local/libexec/showallopts-ssh" '


def run(*args, capture=False):
    result = subprocess.run(args, check=True, text=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.strip() if capture else None


def public_key(value):
    parts = value.strip().split()
    if len(parts) < 2 or parts[0] != 'ssh-ed25519' or '\n' in value.strip():
        raise ValueError('Supply one dedicated Ed25519 PUBLIC key, never a private key.')
    blob = base64.b64decode(parts[1], validate=True)
    if len(blob) != 51 or blob[:19] != b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20':
        raise ValueError('Invalid Ed25519 public key.')
    return 'ssh-ed25519 '+parts[1]+' showallopts-actions'


def safe_parent(path):
    for parent in path.parents:
        if not parent.exists():
            continue
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('Installation parents must be root-owned, non-writable real directories.')


def installed_matches(source, target, mode):
    if not target.exists() and not target.is_symlink():
        return False
    info = target.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != int(mode, 8) or target.read_bytes() != source.read_bytes()):
        raise ValueError('Existing installation differs; review before replacing '+str(target))
    return True


def authorized_content(previous, key):
    desired = (KEY_PREFIX+key).encode()
    key_blob = key.split()[1].encode()
    matches = [line for line in previous.splitlines() if key_blob in line.split()]
    if matches:
        if matches == [desired]:
            return previous
        raise ValueError('This key already has a different entry; review it before continuing.')
    return previous+(b'\n' if previous and not previous.endswith(b'\n') else b'')+desired+b'\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-key', required=True)
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    key = public_key(args.public_key)
    if not re.fullmatch(r'[0-9a-f]{40}', args.commit):
        raise ValueError('Use the reviewed full lowercase commit SHA.')
    if os.geteuid() == 0 or pwd.getpwuid(os.getuid()).pw_name != 'ubuntu':
        raise ValueError('Run as ubuntu without sudo; installer uses existing sudo only where needed.')
    bundle = Path(__file__).resolve().parents[2]
    targets = [(bundle/'deploy/automation/showallopts-ssh', DEST/'showallopts-ssh', '0755')]
    for source, target, _ in targets:
        if not source.is_file():
            raise ValueError('Incomplete reviewed installation bundle.')
    for target in [t for _, t, _ in targets]+[CONFIG]:
        safe_parent(target)
        if target.is_symlink():
            raise ValueError('Installation targets must not be symlinks.')
    os.chdir(ROOT)
    if run('git', 'status', '--porcelain', capture=True) or run('git', 'branch', '--show-current', capture=True) != 'main':
        raise ValueError('Server checkout must be clean main; installation never switches its code.')
    remote = run('git', 'remote', 'get-url', 'origin', capture=True)
    if remote not in ('https://github.com/zxbange/showAllOpts.git', 'https://github.com/zxbange/showAllOpts',
                      'git@github.com:zxbange/showAllOpts.git'):
        raise ValueError('Unexpected origin repository.')
    run('sudo', '-n', 'true')
    os.environ['PATH'] = NODE+':/usr/local/bin:/usr/bin:/bin'
    spec = importlib.util.spec_from_file_location('bootstrap_deploy', bundle/'scripts/deploy.py')
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    deploy.ROOT = ROOT
    deploy.NON_INTERACTIVE = True
    options = deploy.parse_args(['--non-interactive', '--web-url', 'http://119.28.106.127:8081'])
    deploy.preflight(options)
    deploy.ready(options.service, options.api_url)
    deploy.health_check(options.web_url)
    # Verify existing permissions; do not install sudoers or alter the service/static tree.
    ssh = SSH
    auth = ssh/'authorized_keys'
    for path in (ssh, auth):
        if path.is_symlink() or (path.exists() and path.stat().st_uid != os.getuid()):
            raise ValueError('SSH directory/key file must belong to ubuntu and not be symlinks.')
    authorized_content(auth.read_bytes() if auth.exists() else b'', key)
    with tempfile.TemporaryDirectory(prefix='showallopts-bootstrap-') as temporary:
        config = Path(temporary)/'config.json'
        config.write_text(json.dumps({'DEPLOY_WEB_URL': 'http://119.28.106.127:8081', 'NODE_BIN': NODE})+'\n')
        all_targets = targets+[(config, CONFIG, '0644')]
        # Reject mismatched existing files before any installation. Identical files permit
        # retry after interruption; upgrades to this small trusted entry require review.
        for source, target, mode in all_targets:
            installed_matches(source, target, mode)
        run('sudo', '-n', 'install', '-d', '-o', 'root', '-g', 'root', '-m', '0755', str(DEST))
        for source, target, mode in all_targets:
            if not installed_matches(source, target, mode):
                # Write beside the destination, then publish atomically without replacing it.
                staged = target.with_name('.'+target.name+'.'+Path(temporary).name)
                try:
                    run('sudo', '-n', 'install', '-o', 'root', '-g', 'root', '-m', mode, str(source), str(staged))
                    run('sudo', '-n', 'mv', '-nT', str(staged), str(target))
                    if not installed_matches(source, target, mode):
                        raise ValueError('Installation was not published.')
                finally:
                    run('sudo', '-n', 'rm', '-f', str(staged))
    # No release/build/restart; failed check leaves retryable files, not a new key.
    subprocess.run([str(DEST/'showallopts-ssh')], check=True,
                   env={**os.environ, 'SSH_ORIGINAL_COMMAND': 'check '+args.commit})
    ssh.mkdir(mode=0o700, exist_ok=True)
    ssh.chmod(0o700)
    previous = auth.read_bytes() if auth.exists() else b''
    content = authorized_content(previous, key)
    if content != previous:
        fd, temporary_key = tempfile.mkstemp(prefix='.showallopts-keys-', dir=ssh)
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if (auth.read_bytes() if auth.exists() else b'') != previous:
                raise ValueError('authorized_keys changed during installation; retry without overwriting it.')
            os.replace(temporary_key, auth)
        finally:
            Path(temporary_key).unlink(missing_ok=True)
    auth.chmod(0o600)
    print('Installed frozen entry and dedicated public key; preflight passed. No release attempted.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Subprocess stderr and arguments could contain site configuration; never echo them.
        raise SystemExit(str(exc) if isinstance(exc, ValueError) else 'Installation failed; inspect locally. No deployment attempted.')
