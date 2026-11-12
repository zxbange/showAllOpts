import json
import multiprocessing
import os
import sqlite3
import stat
import pytest
from dotenv import dotenv_values
from app.core.config import Settings
from app.services.safe_env import EnvFileError, env_transaction
from app.services.system_configuration import SystemConfiguration, SystemConfigurationError

def configured(tmp_path):
    path = tmp_path / 'app.config'
    path.write_bytes(b"# keep this comment\r\nDB_HOST='original.example'\r\nDB_PORT=3306\r\nDB_NAME='research'\r\nDB_USER='app'\r\nDB_PASSWORD='old-secret' # database credential\r\nUNRELATED='unchanged'\r\n")
    path.chmod(0o640)
    return path, Settings(_env_file=path, log_dir=tmp_path / 'log')

def form(config):
    body = config.public()
    body['database']['password'] = None
    return body

def test_atomic_save_preserves_metadata_unrelated_content_and_restart(tmp_path):
    path, settings = configured(tmp_path)
    original = path.stat()
    config = SystemConfiguration(settings)
    body = form(config)
    body['database']['password'] = "new' # secret\\value"
    body['integrations'] = {'asharehub': {'token': 'ash-new'}, 'telegram': {'token': 'tg-new', 'chat_id': '-100123'}}
    saved = config.save(body)
    parsed = dotenv_values(path)
    assert parsed['DB_PASSWORD'] == "new' # secret\\value"
    assert parsed['ASHAREHUB_API_KEY'] == 'ash-new'
    assert parsed['TELEGRAM_BOT_TOKEN'] == 'tg-new'
    assert parsed['TELEGRAM_CHAT_ID'] == '-100123'
    assert b"UNRELATED='unchanged'\r\n" in path.read_bytes()
    assert b'# database credential\r\n' in path.read_bytes()
    after = path.stat()
    assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (original.st_uid, original.st_gid, 0o640)
    assert 'tg-new' not in json.dumps(saved)
    restarted = SystemConfiguration(Settings(_env_file=path, log_dir=settings.log_dir))
    assert restarted.load()['database']['password'] == parsed['DB_PASSWORD']
    assert restarted.load()['integrations']['telegram']['token'] == 'tg-new'

def test_external_file_change_wins_and_invalidates_form_across_workers(tmp_path):
    path, settings = configured(tmp_path)
    first = SystemConfiguration(settings)
    stale = form(first)
    path.write_bytes(path.read_bytes().replace(b'original.example', b'changed.example'))
    current = SystemConfiguration(settings).load()
    assert current['database']['host'] == 'changed.example'
    assert current['database_revision'] == 1
    assert first.load()['revision'] == current['revision']
    with pytest.raises(SystemConfigurationError, match='更新'):
        first.save(stale)
    assert b'changed.example' in path.read_bytes()

def test_environment_override_rejects_secret_write_without_echo(tmp_path, monkeypatch):
    path, settings = configured(tmp_path)
    monkeypatch.setenv('DB_PASSWORD', 'environment-secret')
    config = SystemConfiguration(settings)
    before = path.read_bytes()
    body = form(config)
    assert config.load()['database']['password'] == 'environment-secret'
    body['database']['password'] = 'replacement'
    with pytest.raises(SystemConfigurationError, match='DB_PASSWORD') as error:
        config.save(body)
    assert 'environment-secret' not in str(error.value)
    assert path.read_bytes() == before
    assert config.load()['revision'] == body['revision']

def test_replace_failure_rolls_back_encrypted_metadata(tmp_path, monkeypatch):
    path, settings = configured(tmp_path)
    config = SystemConfiguration(settings)
    body = form(config)
    body['database']['password'] = 'replacement'
    before = path.read_bytes()
    from app.services import safe_env
    monkeypatch.setattr(safe_env.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('failure')))
    with pytest.raises(SystemConfigurationError, match='安全替换'):
        config.save(body)
    assert path.read_bytes() == before
    assert config.load()['revision'] == body['revision']
    assert not list(path.parent.glob('.app-config-*'))

def test_transaction_rollback_preserves_external_writer(tmp_path):
    path, settings = configured(tmp_path)
    with pytest.raises(EnvFileError, match='外部变更'):
        with env_transaction(settings) as txn:
            txn.write({'DB_PASSWORD': 'replacement'})
            path.write_text("DB_PASSWORD='external-new'\n")
            raise RuntimeError('later failure')
    assert dotenv_values(path)['DB_PASSWORD'] == 'external-new'

def test_rollback_restores_file_when_sqlite_commit_fails(tmp_path, monkeypatch):
    path, settings = configured(tmp_path)
    config = SystemConfiguration(settings)
    body = form(config)
    body['database']['password'] = 'replacement'
    before = path.read_bytes()
    original_connect = sqlite3.connect
    class FailedCommit(sqlite3.Connection):
        def __exit__(self, *args):
            if self.in_transaction and b'replacement' in path.read_bytes():
                self.rollback()
                raise sqlite3.OperationalError('simulated commit failure')
            return super().__exit__(*args)
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: original_connect(*a, factory=FailedCommit, **kw))
    with pytest.raises(sqlite3.OperationalError):
        config.save(body)
    assert path.read_bytes() == before
    assert config.load()['revision'] == body['revision']

def _crash_after_file_replace(path, log_dir, body):
    settings = Settings(_env_file=path, log_dir=log_dir)
    original_connect = sqlite3.connect
    class CrashCommit(sqlite3.Connection):
        def __exit__(self, *args):
            if self.in_transaction and b'crash-new-secret' in path.read_bytes():
                os._exit(77)
            return super().__exit__(*args)
    sqlite3.connect = lambda *a, **kw: original_connect(*a, factory=CrashCommit, **kw)
    SystemConfiguration(settings).save(body)

def test_process_death_after_file_replace_recovers_file_authority(tmp_path):
    path, settings = configured(tmp_path)
    config = SystemConfiguration(settings)
    body = form(config)
    body['database']['password'] = 'crash-new-secret'
    process = multiprocessing.get_context('fork').Process(target=_crash_after_file_replace, args=(path, settings.log_dir, body))
    process.start()
    process.join(10)
    assert process.exitcode == 77
    recovered = SystemConfiguration(settings).load()
    assert recovered['database']['password'] == 'crash-new-secret'
    assert recovered['database_revision'] == 1
    assert recovered['revision'] == body['revision'] + 1
    assert SystemConfiguration(settings).load()['revision'] == recovered['revision']

def test_explicit_clear_survives_reload_and_no_secret_in_public(tmp_path):
    path, settings = configured(tmp_path)
    config = SystemConfiguration(settings)
    body = form(config)
    body['integrations'] = {'asharehub': {'token': 'key-123'}, 'telegram': {'token': 'tg-123', 'chat_id': 'chat-123'}}
    config.save(body)
    cleared = form(config)
    cleared['integrations'] = {'asharehub': {'token': ''}, 'telegram': {'token': '', 'chat_id': ''}}
    config.save(cleared)
    assert config.load()['integrations'] == {'asharehub': {'token': ''}, 'telegram': {'token': '', 'chat_id': ''}}
    assert 'key-123' not in json.dumps(config.public())
    assert dotenv_values(path)['TELEGRAM_CHAT_ID'] == ''

def test_directory_fsync_failure_after_replace_restores_old_file(tmp_path, monkeypatch):
    path, settings = configured(tmp_path)
    config = SystemConfiguration(settings); body = form(config)
    body['database']['password'] = 'replacement'; before = path.read_bytes()
    from app.services import safe_env
    real_fsync = safe_env.os.fsync; calls = []
    def failing_fsync(fd):
        calls.append(fd)
        if len(calls) == 2: raise OSError('directory sync failure')
        return real_fsync(fd)
    monkeypatch.setattr(safe_env.os, 'fsync', failing_fsync)
    with pytest.raises(SystemConfigurationError, match='安全替换'): config.save(body)
    assert len(calls) >= 4 and path.read_bytes() == before
    assert config.load()['revision'] == body['revision']

def test_case_insensitive_process_override_is_not_silently_ignored(tmp_path, monkeypatch):
    path, settings = configured(tmp_path); monkeypatch.setenv('db_host', 'override.example')
    config = SystemConfiguration(settings); body = form(config)
    assert body['database']['host'] == 'override.example'
    body['database'].update(host='new.example', password='new-secret')
    with pytest.raises(SystemConfigurationError, match='db_host'): config.save(body)
    assert b'original.example' in path.read_bytes()

def test_selected_symlink_is_preserved_while_target_is_updated(tmp_path):
    path, _ = configured(tmp_path); alias = tmp_path / 'selected.config'; alias.symlink_to(path)
    settings = Settings(_env_file=alias, log_dir=tmp_path / 'log'); config = SystemConfiguration(settings)
    body = form(config); body['database']['password'] = 'new-secret'; config.save(body)
    assert alias.is_symlink() and dotenv_values(path)['DB_PASSWORD'] == 'new-secret'

def test_same_content_external_metadata_change_blocks_publish(tmp_path):
    path, settings = configured(tmp_path)
    with env_transaction(settings) as txn:
        path.chmod(0o600)
        with pytest.raises(EnvFileError, match='其他操作'): txn.write({'DB_PASSWORD': 'replacement'})
    assert dotenv_values(path)['DB_PASSWORD'] == 'old-secret'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

def test_runtime_save_keeps_environment_unchanged_and_reloads(tmp_path):
    path, settings = configured(tmp_path)
    config = SystemConfiguration(settings)
    body = form(config)
    original = path.read_bytes()
    body['runtime']['logging']['level'] = 'WARNING'
    body['runtime']['sources']['tdx']['request_timeout_seconds'] = 500
    saved = config.save(body)
    assert path.read_bytes() == original
    assert saved['database_revision'] == 0
    assert SystemConfiguration(settings).load()['runtime'] == body['runtime']
    assert all(item['health'] == 'not_checked' for item in saved['data_sources'])
    assert next(item for item in saved['data_sources'] if item['id'] == 'tdx')['request_timeout_seconds'] == 500


def test_old_record_without_runtime_migrates_in_memory(tmp_path):
    path, settings = configured(tmp_path)
    config = SystemConfiguration(settings)
    value = config.load()
    value.pop('runtime')
    config._persist(value, config.cipher())
    assert config.public()['runtime']['sources']['tdx']['request_timeout_seconds'] == 600
