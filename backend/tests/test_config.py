import importlib.util
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core import config
from app.core.config import ConfigurationError, Settings


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    names = {name.upper() for name in Settings.model_fields} | {'APP_ENV_FILE'}
    for name in list(os.environ):
        if name.upper() in names:
            monkeypatch.delenv(name)
    monkeypatch.setattr(config, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(config, 'DEFAULT_ENV_FILE', tmp_path / 'local.env')


def test_default_file_and_environment_override(monkeypatch, tmp_path):
    (tmp_path / 'local.env').write_text('DB_HOST=local.example\nDB_PORT=3307\n', encoding='utf-8')
    monkeypatch.setenv('DB_HOST', 'environment.example')
    settings = Settings()
    assert settings.db_host == 'environment.example'
    assert settings.db_port == 3307
    assert settings.log_retention_days == 30


def test_external_file_replaces_local_and_resolves_from_project(monkeypatch, tmp_path):
    (tmp_path / 'local.env').write_text('DB_PASSWORD=local-only-secret\n', encoding='utf-8')
    external = tmp_path / 'config' / 'app.config'
    external.parent.mkdir()
    external.write_text('DB_HOST=external.example\n', encoding='utf-8')
    other = tmp_path / 'other'
    other.mkdir()
    monkeypatch.chdir(other)
    monkeypatch.setenv('APP_ENV_FILE', 'config/app.config')
    assert Settings().db_host == 'external.example'
    assert Settings().db_password.get_secret_value() == ''
    monkeypatch.setenv('DB_HOST', 'override.example')
    assert Settings().db_host == 'override.example'


def test_each_instance_resolves_file_again(monkeypatch, tmp_path):
    for name in ('first', 'second'):
        path = tmp_path / (name + '.env')
        path.write_text('DB_NAME=' + name, encoding='utf-8')
        monkeypatch.setenv('APP_ENV_FILE', str(path))
        assert Settings().db_name == name


@pytest.mark.parametrize('path', ['', '   ', 'missing.env', '.'])
def test_explicit_invalid_file_fails_without_local_fallback(monkeypatch, tmp_path, path):
    (tmp_path / 'local.env').write_text('DB_HOST=local.example', encoding='utf-8')
    monkeypatch.setenv('APP_ENV_FILE', path)
    with pytest.raises(ConfigurationError, match='APP_ENV_FILE'):
        Settings()


def test_unreadable_file_fails(monkeypatch, tmp_path):
    path = tmp_path / 'private.env'
    path.touch()
    monkeypatch.setenv('APP_ENV_FILE', str(path))
    original = Path.open
    def deny(selected, *args, **kwargs):
        if selected == path:
            raise PermissionError('denied')
        return original(selected, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', deny)
    with pytest.raises(ConfigurationError, match='unreadable'):
        Settings()


def test_explicit_test_override_bypasses_file_selection(monkeypatch, tmp_path):
    monkeypatch.setenv('APP_ENV_FILE', 'missing.env')
    assert Settings(_env_file=None).db_host == '127.0.0.1'
    path = tmp_path / 'test.env'
    path.write_text('DB_NAME=test_override', encoding='utf-8')
    assert Settings(_env_file=path).db_name == 'test_override'


def test_env_only_mode_without_default_file(monkeypatch):
    monkeypatch.setenv('DB_HOST', 'container.example')
    assert Settings().db_host == 'container.example'


def test_invalid_values_do_not_print_input(monkeypatch, tmp_path):
    path = tmp_path / 'invalid.env'
    path.write_text('DB_PORT=secret-misplaced-in-port', encoding='utf-8')
    monkeypatch.setenv('APP_ENV_FILE', str(path))
    with pytest.raises(ValidationError) as error:
        Settings()
    assert 'secret-misplaced-in-port' not in str(error.value)


@pytest.fixture
def initializer():
    source = Path(__file__).resolve().parents[2] / 'scripts' / 'configure_database.py'
    spec = importlib.util.spec_from_file_location('configure_database_test', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.initialize_configuration


def test_setup_external_preserved_without_local_copy(initializer, monkeypatch, tmp_path):
    path = tmp_path / 'external.env'
    content = 'DB_HOST=external.example\nDB_PASSWORD=fixture-secret\n'
    path.write_text(content, encoding='utf-8')
    monkeypatch.setenv('APP_ENV_FILE', str(path))
    initializer(tmp_path)
    assert not (tmp_path / 'backend' / '.env').exists()
    assert path.read_text(encoding='utf-8') == content


def test_setup_invalid_external_does_not_create_local(initializer, monkeypatch, tmp_path):
    monkeypatch.setenv('APP_ENV_FILE', 'missing.env')
    with pytest.raises(ConfigurationError):
        initializer(tmp_path)
    assert not (tmp_path / 'backend' / '.env').exists()


def test_setup_preserves_existing_local(initializer, tmp_path):
    target = tmp_path / 'backend' / '.env'
    target.parent.mkdir()
    target.write_bytes(b'DB_HOST=keep.example\r\n')
    initializer(tmp_path)
    assert target.read_bytes() == b'DB_HOST=keep.example\r\n'


def test_setup_fresh_clone_copies_template(initializer, tmp_path):
    root = tmp_path / 'project'
    (root / 'backend').mkdir(parents=True)
    content = 'DB_HOST=template.example\n'
    (root / 'backend' / '.env.example').write_text(content, encoding='utf-8')
    initializer(root)
    assert (root / 'backend' / '.env').read_text(encoding='utf-8') == content


def test_setup_legacy_source_roundtrip(initializer, tmp_path, capsys):
    (tmp_path / 'backend').mkdir()
    source = tmp_path / 'collector.py'
    source.write_text("class MysqlTrade:\n    host = 'legacy.example'\n    port = 3307\n    dbname = 'legacy'\n    dbuser = 'reader'\n    dbpassword = 'fake@pass#word'\n", encoding='utf-8')
    initializer(tmp_path, source)
    settings = Settings(_env_file=tmp_path / 'backend' / '.env')
    assert settings.db_host == 'legacy.example'
    assert settings.db_port == 3307
    assert settings.db_password.get_secret_value() == 'fake@pass#word'
    assert 'fake@pass#word' not in capsys.readouterr().out


@pytest.mark.parametrize('production', [False, True])
def test_setup_default_source_uses_migrated_private_script_without_execution(initializer, tmp_path, capsys, production):
    root = tmp_path / 'project'
    source = root / '脚本' / '本地专用' / '聚宽采集' / '全部策略批量采集_数据库版.py'
    source.parent.mkdir(parents=True)
    source.write_text(
        "DB_CONFIG = {\n"
        "    'host': 'migrated.example',\n"
        "    'port': 3308,\n"
        "    'dbname': 'migrated_fixture',\n"
        "    'dbuser': 'fixture_reader',\n"
        "    'dbpassword': 'fixture-only@password',\n"
        "}\n"
        "class MysqlTrade:\n"
        "    host = 1 / 0\n"
        "raise RuntimeError('The source must never be executed')\n",
        encoding='utf-8-sig',
    )
    initializer(root, production=production)
    target = root / 'config' / 'app.config' if production else root / 'backend' / '.env'
    settings = Settings(_env_file=target)
    assert settings.db_host == 'migrated.example'
    assert settings.db_port == 3308
    assert settings.db_name == 'migrated_fixture'
    assert settings.db_user == 'fixture_reader'
    assert settings.db_password.get_secret_value() == 'fixture-only@password'
    output = capsys.readouterr().out
    for value in ('migrated.example', 'migrated_fixture', 'fixture_reader', 'fixture-only@password'):
        assert value not in output


@pytest.mark.parametrize('missing', ['host', 'port', 'dbname', 'dbuser', 'dbpassword'])
def test_setup_db_config_requires_every_connection_field(initializer, tmp_path, missing):
    values = {'host': 'fixture.example', 'port': 3307, 'dbname': 'fixture_database',
              'dbuser': 'fixture_reader', 'dbpassword': 'fixture-only-secret'}
    del values[missing]
    source = tmp_path / 'collector.py'
    source.write_text(f'DB_CONFIG = {values!r}\n', encoding='utf-8')
    with pytest.raises(ConfigurationError, match='settings are missing'):
        initializer(tmp_path, source)
    assert not (tmp_path / 'backend' / '.env').exists()


def test_setup_db_config_rejects_expressions_without_execution(initializer, tmp_path, capsys):
    marker = tmp_path / 'must-not-exist'
    source = tmp_path / 'collector.py'
    source.write_text(
        "DB_CONFIG = {'host': 'fixture.example', 'port': 3306, 'dbname': 'fixture', "
        "'dbuser': 'reader', 'dbpassword': 'fixture-only-secret', "
        f"'extra': __import__('pathlib').Path({str(marker)!r}).touch()}}\n",
        encoding='utf-8',
    )
    with pytest.raises(ConfigurationError, match='literal values only') as error:
        initializer(tmp_path, source)
    assert not marker.exists()
    assert not (tmp_path / 'backend' / '.env').exists()
    assert 'fixture-only-secret' not in str(error.value) + capsys.readouterr().out


@pytest.mark.parametrize(('content', 'message'), [
    ("DB_CONFIG = ['fixture-only-secret']\n", 'literal dictionary'),
    ("def nested():\n    DB_CONFIG = {}\n", 'was not found'),
    ("DB_CONFIG = {'dbpassword': 'fixture-only-secret',\n", 'not valid Python'),
])
def test_setup_invalid_source_fails_without_exposing_content(initializer, tmp_path, capsys, content, message):
    source = tmp_path / 'collector.py'
    source.write_text(content, encoding='utf-8')
    with pytest.raises(ConfigurationError, match=message) as error:
        initializer(tmp_path, source)
    assert not (tmp_path / 'backend' / '.env').exists()
    assert 'fixture-only-secret' not in str(error.value) + capsys.readouterr().out


def test_setup_explicit_missing_source_fails(initializer, tmp_path):
    (tmp_path / 'backend').mkdir()
    with pytest.raises(ConfigurationError, match='source file'):
        initializer(tmp_path, tmp_path / 'missing.py')
    assert not (tmp_path / 'backend' / '.env').exists()


@pytest.mark.parametrize('existing', [True, False])
def test_production_initializer_preserves_or_migrates_local(initializer, tmp_path, existing):
    (tmp_path / 'backend').mkdir()
    local = tmp_path / 'backend' / '.env'
    local.write_text('DB_HOST=development.example\n', encoding='utf-8')
    target = tmp_path / 'config' / 'app.config'
    if existing:
        target.parent.mkdir()
        target.write_text('DB_HOST=production.example\n', encoding='utf-8')
    initializer(tmp_path, production=True)
    expected = 'production.example' if existing else 'development.example'
    assert Settings(_env_file=target).db_host == expected
    assert local.read_text(encoding='utf-8') == 'DB_HOST=development.example\n'


def test_production_initializer_uses_production_template(initializer, tmp_path):
    root = tmp_path / 'fresh'
    (root / 'config').mkdir(parents=True)
    (root / 'config' / 'app.config.example').write_text('DB_HOST=template.example\n', encoding='utf-8')
    initializer(root, production=True)
    assert Settings(_env_file=root / 'config' / 'app.config').db_host == 'template.example'
    assert not (root / 'backend' / '.env').exists()


@pytest.mark.parametrize('override', [None, 'custom/settings.config', ''])
def test_linux_launcher_selects_production_and_preserves_override(tmp_path, override):
    import shutil
    import subprocess
    bash = Path('D:/Program Files/Git/bin/bash.exe') if os.name == 'nt' else Path(shutil.which('bash') or '/missing-bash')
    if not bash.is_file():
        pytest.skip('Bash not available')
    root = tmp_path / 'project with spaces'
    python_stub = root / 'backend' / '.venv' / 'bin' / 'python'
    python_stub.parent.mkdir(parents=True)
    python_stub.write_text('#!/usr/bin/env bash\nprintf "%s" "$APP_ENV_FILE"\n', encoding='utf-8')
    python_stub.chmod(0o755)
    source = Path(__file__).resolve().parents[2] / 'start.sh'
    launcher = root / 'start.sh'
    launcher.write_bytes(source.read_bytes())
    environment = dict(os.environ)
    environment.pop('APP_ENV_FILE', None)
    if override is not None:
        environment['APP_ENV_FILE'] = override
    result = subprocess.run([str(bash), str(launcher)], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, check=True)
    assert result.stdout == ('config/app.config' if override is None else override)
