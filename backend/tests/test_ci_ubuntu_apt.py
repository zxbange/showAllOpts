"""Offline CI setup tests. Never touch host APT, sudo, or network."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('ci_ubuntu_apt', ROOT / 'scripts/ci_ubuntu_apt.py')
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)

STANZA = '''# Ubuntu image source
Types: deb
URIs: mirror+file:/etc/apt/apt-mirrors.txt
Suites: noble noble-updates noble-backports
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
'''


def test_only_uri_changes_and_security_uses_official_security_archive():
    security = STANZA.replace('noble noble-updates noble-backports', 'noble-security')
    original = STANZA + '\n' + security
    expected = STANZA.replace('mirror+file:/etc/apt/apt-mirrors.txt', ci.ARCHIVE) + '\n' + security.replace(
        'mirror+file:/etc/apt/apt-mirrors.txt', ci.SECURITY)
    assert ci.direct_sources(original) == expected
    assert ci.direct_sources(expected) == expected


@pytest.mark.parametrize('old,new', [
    ('mirror+file:/etc/apt/apt-mirrors.txt', 'https://unreviewed.example/ubuntu'),
    ('mirror+file:/etc/apt/apt-mirrors.txt', ci.ARCHIVE + ' ' + ci.SECURITY),
    ('ubuntu-archive-keyring.gpg', 'different-keyring.gpg'),
    ('noble noble-updates noble-backports', 'jammy'),
    ('Types: deb', 'Types: deb\nTrusted: yes'),
    ('Types: deb', 'Types: deb\nAllow-Insecure: yes'),
    ('Types: deb', 'Types: deb\nURIs: ' + ci.ARCHIVE),
    ('URIs: mirror+file:/etc/apt/apt-mirrors.txt', 'URIs:\n mirror+file:/etc/apt/apt-mirrors.txt'),
])
def test_unexpected_layout_trust_or_destination_rejected(old, new):
    with pytest.raises(ValueError):
        ci.direct_sources(STANZA.replace(old, new))


def test_missing_ubuntu_sources_rejected():
    with pytest.raises(ValueError):
        ci.direct_sources('# Nothing to configure\n')


def fake_runner(monkeypatch, tmp_path):
    source, config = tmp_path / 'ubuntu.sources', tmp_path / 'ci-downloads'
    source.write_text(STANZA)
    source.chmod(0o644)
    monkeypatch.setattr(ci, 'SOURCE', source)
    monkeypatch.setattr(ci, 'CONFIG', config)
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('RUNNER_ENVIRONMENT', 'github-hosted')
    monkeypatch.setattr(ci.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(ci.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(ci.platform, 'freedesktop_os_release', lambda: {'ID': 'ubuntu', 'VERSION_ID': '24.04'})
    return source, config


def test_ephemeral_runner_configuration_is_atomic_idempotent_and_preserves_mode(monkeypatch, tmp_path):
    source, config = fake_runner(monkeypatch, tmp_path)
    ci.main(); ci.main()
    assert source.read_text() == ci.direct_sources(STANZA)
    assert config.read_text() == ci.DOWNLOAD_CONFIG
    assert source.stat().st_mode & 0o777 == config.stat().st_mode & 0o777 == 0o644
    assert not list(tmp_path.glob('.showallopts-ci-*'))
    assert 'Acquire::Retries "2";' in config.read_text()
    assert 'APT::Update::Error-Mode "any";' in config.read_text()


@pytest.mark.parametrize('failure', ['not_ci', 'self_hosted', 'not_root', 'wrong_os', 'wrong_arch', 'source_symlink', 'config_exists'])
def test_refuse_other_environments_or_existing_config_before_writes(monkeypatch, tmp_path, failure):
    source, config = fake_runner(monkeypatch, tmp_path)
    if failure == 'not_ci': monkeypatch.delenv('GITHUB_ACTIONS')
    elif failure == 'self_hosted': monkeypatch.setenv('RUNNER_ENVIRONMENT', 'self-hosted')
    elif failure == 'not_root': monkeypatch.setattr(ci.os, 'geteuid', lambda: 1000)
    elif failure == 'wrong_os': monkeypatch.setattr(ci.platform, 'freedesktop_os_release', lambda: {'ID': 'debian', 'VERSION_ID': '13'})
    elif failure == 'wrong_arch': monkeypatch.setattr(ci.platform, 'machine', lambda: 'aarch64')
    elif failure == 'source_symlink':
        target = tmp_path / 'original'; source.rename(target); source.symlink_to(target)
    else: config.write_text('operator configuration')
    with pytest.raises(ValueError): ci.main()
    assert source.read_text() == STANZA
    if failure == 'config_exists':
        assert config.read_text() == 'operator configuration'
    else:
        assert not config.exists()


def test_workflow_keeps_locked_browser_and_full_tests_with_bounded_install():
    workflow = (ROOT / '.github/workflows/deploy.yml').read_text()
    frontend = workflow.split('\n  frontend:\n')[1].split('\n  deploy:\n')[0]
    assert frontend.index('ci_ubuntu_apt.py') < frontend.index('playwright install --with-deps chromium')
    assert 'GITHUB_ACTIONS="$GITHUB_ACTIONS" RUNNER_ENVIRONMENT="$RUNNER_ENVIRONMENT"' in frontend
    assert 'timeout-minutes: 8' in frontend
    for command in ('npm ci --no-audit --no-fund', 'npm test', 'npm run build',
                    'npx --no-install playwright install --with-deps chromium', 'npm run test:e2e'):
        assert command in frontend
    assert 'continue-on-error' not in workflow and 'needs: [backend, frontend]' in workflow


@pytest.mark.parametrize('second', ['uris: https://archive.ubuntu.com/ubuntu/\n', 'Types: deb\nSuites: noble\n'])
def test_unexpected_second_stanza_is_not_silently_skipped(second):
    with pytest.raises(ValueError):
        ci.direct_sources(STANZA + '\n' + second)
