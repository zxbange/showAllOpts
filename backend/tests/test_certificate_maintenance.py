import importlib.util
from pathlib import Path
import ssl
from unittest.mock import Mock

import pytest

PATH = Path(__file__).resolve().parents[2] / 'deploy/certificate_maintenance.py'


@pytest.fixture
def maintenance(monkeypatch):
    spec = importlib.util.spec_from_file_location('certificate_maintenance', PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.command = Mock()
    module.disk_fingerprint = Mock(return_value=b'new')
    module.served_fingerprint = Mock(return_value=b'new')
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    return module


def run(module):
    module.maintain('119.28.106.127', 'showallopts-ip', 'certbot', 'nginx', 'systemctl')


def test_only_renews_when_due_no_force_or_unnecessary_reload(maintenance):
    run(maintenance)
    maintenance.command.assert_called_once_with(['certbot', 'renew', '--cert-name', 'showallopts-ip', '--quiet'])


@pytest.mark.parametrize('old', [b'old', ssl.SSLError('expired')])
def test_expired_or_stale_served_certificate_is_reloaded_and_verified(maintenance, old):
    maintenance.served_fingerprint.side_effect = [old, b'old', b'new']
    run(maintenance)
    assert maintenance.command.call_args_list[1].args[0] == ['nginx', '-t']
    assert maintenance.command.call_args_list[2].args[0] == ['systemctl', 'reload', 'nginx']


def test_bad_config_never_reloaded(maintenance):
    maintenance.served_fingerprint.return_value = b'old'
    maintenance.command.side_effect = [None, RuntimeError('bad nginx config')]
    with pytest.raises(RuntimeError): run(maintenance)
    assert maintenance.command.call_count == 2


def test_failed_renewal_does_not_report_success_but_can_recover_already_issued_cert(maintenance):
    maintenance.command.side_effect = [RuntimeError('network down'), None, None]
    maintenance.served_fingerprint.side_effect = [b'old', b'new']
    with pytest.raises(RuntimeError, match='automatic retry'): run(maintenance)
    assert maintenance.command.call_count == 3


def test_certificate_still_expired_after_reload_fails(maintenance):
    maintenance.served_fingerprint.side_effect = ssl.SSLError('expired')
    with pytest.raises(RuntimeError, match='expected trusted certificate'): run(maintenance)


def test_timer_is_hourly_persistent_and_runs_root_owned_copy():
    root = PATH.parent
    timer = (root / 'systemd/showallopts-certificate.timer').read_text()
    service = (root / 'systemd/showallopts-certificate.service').read_text()
    assert 'OnCalendar=hourly' in timer and 'Persistent=true' in timer
    assert '/usr/local/libexec/showallopts-certificate-maintenance' in service
    assert '/home/ubuntu/showAllOpts' not in service
