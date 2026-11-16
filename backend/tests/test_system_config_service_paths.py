"""Keep credential-write exceptions narrow and deployment instructions consistent."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def directives(text, name):
    return [line.split('=', 1)[1] for line in text.splitlines() if line.startswith(name + '=')]


def test_base_unit_allows_atomic_config_write_without_relaxing_hardening():
    unit = (ROOT / 'deploy/systemd/showallopts-api.service').read_text()
    assert directives(unit, 'ReadWritePaths') == ['/opt/showallopts/log /opt/showallopts/config']
    assert directives(unit, 'ProtectSystem') == ['strict']
    assert directives(unit, 'ProtectHome') == ['true']
    assert directives(unit, 'NoNewPrivileges') == ['true']


def test_existing_ubuntu_dropin_is_additive_config_directory_only():
    text = (ROOT / 'deploy/systemd/showallopts-config-write.conf').read_text()
    settings = [line for line in text.splitlines() if '=' in line and not line.startswith('#')]
    assert settings == ['ReadWritePaths=/home/ubuntu/showAllOpts/config']
    docs = (ROOT / 'docs/operations/linux-deployment.md').read_text()
    assert 'ReadWritePaths=/home/ubuntu/showAllOpts/log /home/ubuntu/showAllOpts/config' in docs
    assert 'ProtectHome=read-only' in docs
    assert 'zzz-config-write.conf' in docs
