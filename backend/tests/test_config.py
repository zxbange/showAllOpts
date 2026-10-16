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


def test_setup_explicit_missing_source_fails(initializer, tmp_path):
    (tmp_path / 'backend').mkdir()
    with pytest.raises(ConfigurationError, match='source file'):
        initializer(tmp_path, tmp_path / 'missing.py')
    assert not (tmp_path / 'backend' / '.env').exists()
