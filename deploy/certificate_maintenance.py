"""Renew the site's IP certificate and recover a stale Nginx certificate safely.

Run by the supplied root-owned systemd timer. Never forces renewal, edits Nginx
configuration, opens firewall ports, or changes certificate validation rules.
"""
import argparse
import hashlib
import ipaddress
import logging
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import time

LOG = logging.getLogger('certificate-maintenance')


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if result.returncode:
        # Certbot's detailed diagnostic remains in /var/log/letsencrypt.
        raise RuntimeError(f'{Path(args[0]).name} failed (exit {result.returncode})')


def disk_fingerprint(path):
    pem = path.read_text(encoding='ascii').split('-----END CERTIFICATE-----', 1)[0] + '-----END CERTIFICATE-----\n'
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).digest()


def served_fingerprint(address):
    # Connect only to local Nginx; verify the public IP identity and trusted chain.
    with socket.create_connection(('127.0.0.1', 443), timeout=5) as sock:
        with ssl.create_default_context().wrap_socket(sock, server_hostname=address) as tls:
            return hashlib.sha256(tls.getpeercert(binary_form=True)).digest()


def maintain(address, cert_name, certbot, nginx, systemctl):
    ipaddress.ip_address(address)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', cert_name):
        raise ValueError('Invalid certificate name')
    failure = None
    try:
        command([certbot, 'renew', '--cert-name', cert_name, '--quiet'])
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        failure = error
        LOG.error('Renewal failed; next timer run retries. See /var/log/letsencrypt/letsencrypt.log')
    target = disk_fingerprint(Path('/etc/letsencrypt/live') / cert_name / 'fullchain.pem')
    try:
        current = served_fingerprint(address)
    except (OSError, ssl.SSLError):
        current = None
    if current != target:
        command([nginx, '-t'])  # Never reload a broken configuration.
        command([systemctl, 'reload', 'nginx'])
        # Nginx reload is asynchronous; allow old workers a short transition.
        for attempt in range(5):
            try:
                current = served_fingerprint(address)
            except (OSError, ssl.SSLError):
                current = None
            if current == target:
                break
            time.sleep(1)
        if current != target:
            raise RuntimeError('Nginx did not load the expected trusted certificate')
        LOG.info('Nginx certificate reloaded and verified')
    if failure:
        raise RuntimeError('Renewal failed; automatic retry is scheduled') from failure
    LOG.info('Certificate check completed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ip', required=True)
    parser.add_argument('--cert-name', default='showallopts-ip')
    options = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
    try:
        executables = [shutil.which(name) for name in ('certbot', 'nginx', 'systemctl')]
        if not all(executables):
            raise RuntimeError('certbot, nginx and systemctl must be installed')
        maintain(options.ip, options.cert_name, *executables)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        LOG.error('Certificate maintenance failed: %s', error)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
