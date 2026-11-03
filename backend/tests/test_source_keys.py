"""Shared encryption retains existing holdings disk format across retirement."""
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import stat
from cryptography.fernet import Fernet
import pytest
from app.services.source_keys import persistent_cipher, POSIX_KEY_STORAGE
from app.services.database_sources import SourceError
from test_personal_holdings import monitor


def initialize(root):
    return persistent_cipher(Path(root), '', lambda _: None).encrypt(b'probe')


@pytest.mark.skipif(not POSIX_KEY_STORAGE, reason='POSIX permissions')
def test_keys_concurrent_and_restart(tmp_path):
    root = tmp_path/'personal-holdings'/'data'
    root.mkdir(parents=True)
    with ProcessPoolExecutor(max_workers=4) as workers:
        tokens = list(workers.map(initialize, [str(root)]*8))
    path = root.parent/'short-validation-secrets'/'source.key'
    key = path.read_bytes()
    assert all(Fernet(key).decrypt(t) == b'probe' for t in tokens)
    initialize(root)
    assert path.read_bytes() == key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_existing_holdings_ciphertext_and_versions_remain_readable(monitor):
    from app.services.personal_holdings import PersonalHoldings
    job, _, _, config, *_ = monitor
    before = job.sources.list()
    ref = before[0]
    key = (job.root.parent/'short-validation-secrets'/'source.key').read_bytes()
    restarted = PersonalHoldings(job.settings)
    try:
        assert restarted.sources.list() == before
        assert restarted.sources.get(ref['id'],ref['revision'])['password'] == config['password']
        assert (job.root.parent/'short-validation-secrets'/'source.key').read_bytes() == key
        assert config['password'].encode() not in job.path.read_bytes()
    finally:
        restarted.stop()


@pytest.mark.skipif(not POSIX_KEY_STORAGE, reason='POSIX permissions')
def test_missing_corrupt_or_public_key_never_replaces_saved_credentials(monitor):
    job, *_ = monitor
    path = job.root.parent/'short-validation-secrets'/'source.key'
    saved = path.read_bytes()
    path.unlink()
    before = job.path.read_bytes()
    with pytest.raises(SourceError, match='原密钥缺失'):
        job.sources.cipher()
    assert not path.exists() and job.path.read_bytes() == before
    path.write_bytes(b'corrupt'); path.chmod(0o600)
    with pytest.raises(SourceError):
        job.sources.cipher()
    assert path.read_bytes() == b'corrupt'
    path.write_bytes(saved); path.chmod(0o644)
    with pytest.raises(SourceError, match='权限'):
        job.sources.cipher()
    path.chmod(0o600)


@pytest.mark.skipif(not POSIX_KEY_STORAGE, reason='POSIX permissions')
def test_symlink_key_directory_rejected(tmp_path):
    from app.services.source_keys import KeyStorageError
    outside = tmp_path/'outside'; outside.mkdir(mode=0o700)
    (tmp_path/'short-validation-secrets').symlink_to(outside, target_is_directory=True)
    with pytest.raises(KeyStorageError):
        initialize(tmp_path/'data')
    assert not list(outside.iterdir())
