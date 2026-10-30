from concurrent.futures import ProcessPoolExecutor
import stat
from cryptography.fernet import Fernet
from pydantic import SecretStr
import pytest
from app.services.validation_keys import KeyStorageError
from test_validation_sources import secure, config


def initialize(root):
    from pathlib import Path
    from app.services.validation_keys import persistent_cipher
    cipher = persistent_cipher(Path(root), '', lambda cipher: None)
    return cipher.encrypt(b'probe')


def test_initialization_cross_worker_and_restart(tmp_path):
    root = tmp_path / 'short-validation'
    with ProcessPoolExecutor(max_workers=4) as pool:
        tokens = list(pool.map(initialize, [str(root)] * 8))
    path = tmp_path / 'short-validation-secrets/source.key'
    key = path.read_bytes()
    cipher = Fernet(key)
    assert all(cipher.decrypt(token) == b'probe' for token in tokens)
    initialize(str(root))
    assert path.read_bytes() == key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_existing_config_migrates_without_environment(secure, config):
    jobs, sources, settings, _ = secure
    original = settings.validation_encryption_key.get_secret_value()
    ref = sources.save(config)
    path = jobs.root.parent / 'short-validation-secrets/source.key'
    assert path.read_text() == original
    settings.validation_encryption_key = SecretStr('')
    assert sources.get(ref['id'], ref['revision']) == config
    assert sources.save(config)['revision'] == 1


def test_missing_or_corrupt_key_does_not_replace_existing_data(secure, config):
    jobs, sources, settings, _ = secure
    ref = sources.save(config)
    path = jobs.root.parent / 'short-validation-secrets/source.key'
    saved = path.read_bytes()
    settings.validation_encryption_key = SecretStr('')
    path.unlink()
    before = (jobs.root / 'jobs.sqlite3').read_bytes()
    with pytest.raises(ValueError, match='原密钥缺失'):
        sources.cipher()
    assert not path.exists()
    assert (jobs.root / 'jobs.sqlite3').read_bytes() == before
    path.write_bytes(b'corrupt'); path.chmod(0o600)
    with pytest.raises(ValueError):
        sources.cipher()
    assert path.read_bytes() == b'corrupt'
    path.write_bytes(saved)
    assert sources.get(ref['id'], ref['revision']) == config


def test_first_use_without_config_and_permissions(secure, config):
    jobs, sources, settings, _ = secure
    settings.validation_encryption_key = SecretStr('')
    ref = sources.save(config)
    assert sources.get(ref['id'],ref['revision']) == config
    path = jobs.root.parent / 'short-validation-secrets/source.key'
    path.chmod(0o644)
    with pytest.raises(ValueError, match='权限'):
        sources.cipher()


def test_symlink_directory_and_file_rejected(tmp_path):
    outside = tmp_path / 'outside'; outside.mkdir(mode=0o700)
    directory = tmp_path / 'short-validation-secrets'
    directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(KeyStorageError):
        initialize(str(tmp_path / 'short-validation'))
    directory.unlink(); directory.mkdir(mode=0o700)
    key = outside / 'key'; key.write_bytes(Fernet.generate_key())
    (directory / 'source.key').symlink_to(key)
    before = key.read_bytes()
    with pytest.raises(KeyStorageError):
        initialize(str(tmp_path / 'short-validation'))
    assert key.read_bytes() == before


def test_legacy_database_import_and_public_access_has_no_side_effect(secure, config):
    jobs, sources, settings, client = secure
    directory = jobs.root.parent / 'short-validation-secrets'
    response = client.get('/short-term/validation/access')
    assert response.json()['encryption_automatic'] is True
    assert not directory.exists()
    ref = sources.save(config)
    path = directory / 'source.key'
    expected = path.read_bytes()
    path.unlink()
    with jobs.db() as db:
        db.execute('DELETE FROM validation_source_key')
    assert sources.get(ref['id'], ref['revision']) == config
    assert path.read_bytes() == expected


def test_non_posix_preserves_legacy_key_without_unsafe_file(tmp_path, monkeypatch):
    import app.services.validation_keys as module
    monkeypatch.setattr(module, 'POSIX_KEY_STORAGE', False)
    key = Fernet.generate_key()
    token = Fernet(key).encrypt(b'old')
    def validate(cipher):
        assert cipher.decrypt(token) == b'old'
    cipher = module.persistent_cipher(tmp_path / 'jobs', key.decode(), validate)
    assert cipher.decrypt(token) == b'old'
    assert not list(tmp_path.iterdir())
    with pytest.raises(KeyStorageError, match='Linux'):
        module.persistent_cipher(tmp_path / 'jobs', '', validate)


def test_access_reports_platform_capability(secure, monkeypatch):
    import app.services.validation_keys as module
    _, _, _, client = secure
    monkeypatch.setattr(module, 'POSIX_KEY_STORAGE', False)
    assert client.get('/short-term/validation/access').json()['encryption_automatic'] is False
