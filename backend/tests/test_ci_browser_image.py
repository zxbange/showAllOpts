"""Offline workflow contracts: image integrity, locked versions and full CI gates."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def test_frontend_uses_version_matched_digest_pinned_official_image():
    workflow = (ROOT / '.github/workflows/deploy.yml').read_text()
    frontend = workflow.split('\n  frontend:\n')[1].split('\n  deploy:\n')[0]
    lock = json.loads((ROOT / 'frontend/package-lock.json').read_text())
    version = lock['packages']['node_modules/@playwright/test']['version']
    for name in ('playwright', 'playwright-core'):
        assert lock['packages'][f'node_modules/{name}']['version'] == version
    assert re.search(r'  image: mcr\.microsoft\.com/playwright:v' + re.escape(version) + r'-noble@sha256:[0-9a-f]{64}\n', frontend)
    assert 'options: --init --shm-size=1g' in frontend
    assert 'node-version: \'22\'' in frontend
    assert 'apt-get' not in frontend and 'playwright install' not in frontend
    assert 'ci_ubuntu_apt.py' not in frontend
    assert '--privileged' not in frontend and '--cap-add' not in frontend


def test_browser_validation_and_full_tests_still_gate_deploy():
    workflow = (ROOT / '.github/workflows/deploy.yml').read_text()
    frontend = workflow.split('\n  frontend:\n')[1].split('\n  deploy:\n')[0]
    commands = ('npm ci --no-audit --no-fund', 'npm test', 'npm run build',
                'node --test scripts/verify-ci-browser.test.mjs',
                'node scripts/verify-ci-browser.mjs', 'npm run test:e2e')
    positions = [frontend.index(command) for command in commands]
    assert positions == sorted(positions)
    assert 'timeout-minutes: 2' in frontend
    assert 'continue-on-error' not in workflow and 'needs: [backend, frontend]' in workflow
    assert 'id: frontend_e2e' in frontend
    assert "steps.frontend_e2e.conclusion == 'failure'" in frontend
