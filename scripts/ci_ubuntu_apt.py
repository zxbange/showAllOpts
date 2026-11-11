"""Use Ubuntu's direct HTTPS archive on disposable GitHub-hosted CI only.

The runner image routes Ubuntu through mirror+file, preferring Azure. A failed
Azure DNS lookup can repeatedly stall that mirror method even during failover.
Only the known Ubuntu URI is changed; suites/components and Signed-By survive.
No repository keys, certificate checks, package versions or tests are changed.
"""
import os
from pathlib import Path
import platform
import re
import stat
import tempfile

SOURCE = Path('/etc/apt/sources.list.d/ubuntu.sources')
CONFIG = Path('/etc/apt/apt.conf.d/80showallopts-ci-downloads')
ARCHIVE = 'https://archive.ubuntu.com/ubuntu/'
SECURITY = 'https://security.ubuntu.com/ubuntu/'
KNOWN_URIS = {
    'mirror+file:/etc/apt/apt-mirrors.txt',
    'http://azure.archive.ubuntu.com/ubuntu',
    'http://azure.archive.ubuntu.com/ubuntu/',
    ARCHIVE, ARCHIVE.rstrip('/'), SECURITY, SECURITY.rstrip('/'),
}
DOWNLOAD_CONFIG = '''Acquire::Retries "2";
Acquire::http::Timeout "20";
Acquire::https::Timeout "20";
APT::Update::Error-Mode "any";
'''


def direct_sources(text):
    """Reject unknown image layouts rather than rewriting arbitrary sources."""
    blocks = re.split(r'(\n[ \t]*\n)', text)
    replaced = 0
    for index, block in enumerate(blocks):
        if not any(line.strip() and not line.lstrip().startswith('#') for line in block.splitlines()):
            continue
        if not re.search(r'^URIs:', block, re.M):
            raise ValueError('Unexpected Ubuntu source stanza')
        fields = {}
        for line in block.splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            match = re.fullmatch(r'([A-Za-z-]+):[ \t]*(.*)', line)
            if match is None or match[1] in fields:
                raise ValueError('Unexpected Ubuntu deb822 layout')
            fields[match[1]] = match[2].strip()
        if (fields.get('URIs') not in KNOWN_URIS
                or fields.get('Signed-By') != '/usr/share/keyrings/ubuntu-archive-keyring.gpg'
                or not fields.get('Suites')
                or not set(fields['Suites'].split()) <= {'noble', 'noble-updates', 'noble-backports', 'noble-security'}
                or any(key.lower() in {'trusted', 'allow-insecure', 'allow-weak', 'allow-downgrade-to-insecure'} for key in fields)):
            raise ValueError('Unexpected Ubuntu source or signature policy')
        uri = SECURITY if set(fields['Suites'].split()) == {'noble-security'} else ARCHIVE
        blocks[index] = re.sub(r'^URIs:[^\n]*', 'URIs: ' + uri, block, flags=re.M)
        replaced += 1
    if not replaced:
        raise ValueError('Ubuntu deb822 sources were not found')
    return ''.join(blocks)


def atomic_write(path, text):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('Expected a regular CI configuration file')
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, name = tempfile.mkstemp(prefix='.showallopts-ci-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    release = platform.freedesktop_os_release()
    if (os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'
            or os.geteuid() != 0 or platform.machine() != 'x86_64'
            or release.get('ID') != 'ubuntu' or release.get('VERSION_ID') != '24.04'):
        raise ValueError('This helper is limited to GitHub-hosted Ubuntu 24.04 x64 CI')
    if SOURCE.is_symlink() or not SOURCE.is_file():
        raise ValueError('Expected the runner Ubuntu deb822 source file')
    if CONFIG.is_symlink() or (CONFIG.exists() and CONFIG.read_text() != DOWNLOAD_CONFIG):
        raise ValueError('Refusing to replace an unexpected APT configuration')
    before = SOURCE.read_text()
    after = direct_sources(before)  # Validate completely before either write.
    if after != before:
        atomic_write(SOURCE, after)
    atomic_write(CONFIG, DOWNLOAD_CONFIG)
    print('CI Ubuntu sources use direct official HTTPS archives; signature policy preserved.')


if __name__ == '__main__':
    main()
