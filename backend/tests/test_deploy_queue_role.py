"""Test the API-only deployment guard using only synthetic configuration."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    'deploy_queue_role', Path(__file__).resolve().parents[2] / 'scripts/deploy.py'
)
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


@pytest.fixture
def guard(tmp_path, monkeypatch):
    config = tmp_path / 'app.config'
    config.write_text('DB_PASSWORD=synthetic-private\n')
    env = {
        'APP_ENV_FILE': str(config),
        'PATH': os.defpath,
        'HOME': str(Path.home()),
        'LANG': 'C.UTF-8',
    }
    state = {'units': '', 'environment': f'APP_ENV_FILE={config}', 'files': ''}
    calls = []

    def run(args, **kwargs):
        calls.append(args[0])
        if args[0] == 'systemctl':
            return state['units']
        result = subprocess.run(
            [sys.executable, *args[1:]],
            env=kwargs['env'],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    monkeypatch.setattr(deploy, 'run', run)
    monkeypatch.setattr(
        deploy,
        'property_value',
        lambda service, field: state[
            {'Environment': 'environment', 'EnvironmentFiles': 'files'}[field]
        ],
    )
    return config, env, state, calls


@pytest.mark.parametrize('content', ['', 'collection_runtime_role=combined\n'])
def test_default_and_explicit_combined_allowed(guard, content):
    config, env, _, _ = guard
    config.write_text(content)
    deploy.require_combined_runtime(env)


@pytest.mark.parametrize('role', ['split-api', 'queue-worker', 'invalid', ''])
def test_role_from_dotenv_case_insensitive_is_fail_closed(guard, role):
    config, env, _, calls = guard
    config.write_text(f'CoLlEcTiOn_RuNtImE_RoLe="{role}"\n')
    with pytest.raises(deploy.DeployError, match='verified combined runtime'):
        deploy.require_combined_runtime(env)
    assert 'systemctl' not in calls


def test_environment_overrides_dotenv(guard):
    config, env, _, _ = guard
    config.write_text('COLLECTION_RUNTIME_ROLE=combined\n')
    env['collection_runtime_role'] = 'split-api'
    with pytest.raises(deploy.DeployError):
        deploy.require_combined_runtime(env)
    config.write_text('COLLECTION_RUNTIME_ROLE=split-api\n')
    env['collection_runtime_role'] = 'combined'
    deploy.require_combined_runtime(env)


@pytest.mark.parametrize('unit_state', ['active', 'activating', 'deactivating', 'reloading'])
def test_worker_not_stopped_blocks_even_combined(guard, unit_state):
    _, env, state, _ = guard
    state['units'] = f'showallopts-queue-worker.service loaded {unit_state} running description'
    with pytest.raises(deploy.DeployError, match='not stopped'):
        deploy.require_combined_runtime(env)


def test_stopped_worker_and_unchanged_service_env_allowed(guard):
    _, env, state, _ = guard
    state['units'] = 'showallopts-queue-worker.service loaded inactive dead description'
    deploy.require_combined_runtime(env, 'showallopts-api')
    state['environment'] += ' COLLECTION_RUNTIME_ROLE=split-api'
    with pytest.raises(deploy.DeployError, match='environment changed'):
        deploy.require_combined_runtime(env, 'showallopts-api')


def test_config_role_changed_while_building(guard):
    config, env, _, _ = guard
    deploy.require_combined_runtime(env)
    config.write_text('COLLECTION_RUNTIME_ROLE=split-api\n')
    with pytest.raises(deploy.DeployError):
        deploy.require_combined_runtime(env, 'showallopts-api')
