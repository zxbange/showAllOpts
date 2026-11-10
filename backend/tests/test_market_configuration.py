from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import sqlite3

import pytest

from app.core.config import Settings
from app.services.market_configuration import MarketConfiguration
from app.services.etf_configuration import ConfigurationFailure, ETFConfiguration


class Owner:
    def __init__(self, path, **settings):
        self.root = path / 'market-data' / 'market-collection'
        self.settings = Settings(_env_file=None, log_dir=path, **settings)
        self.clock = lambda: datetime(2026, 11, 10, 18, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.configuration = MarketConfiguration(self)

    @contextmanager
    def connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.root / 'state.sqlite')
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE IF NOT EXISTS configuration(id INTEGER PRIMARY KEY,revision INTEGER,ciphertext BLOB)')
        try:
            with db:
                yield db
        finally:
            db.close()


def form(**changes):
    return dict(revision=0, username='market_writer', password='test-secret-never-public', enabled=True) | changes


def test_no_readonly_fallback_and_encrypted_one_time_configuration(tmp_path):
    owner = Owner(tmp_path)
    assert owner.configuration.issue() == 'missing_writer_configuration'
    public = owner.configuration.save(form())
    assert public['password_set'] and public['schedule'] == ['18:00'] and public['source'] == 'page'
    assert 'password' not in public and 'test-secret-never-public' not in (owner.root / 'state.sqlite').read_bytes().decode(errors='ignore')
    restarted = Owner(tmp_path)
    assert restarted.configuration.public() == public
    assert restarted.configuration.load()['password'] == 'test-secret-never-public'
    assert restarted.configuration.issue() is None


def test_stale_revision_and_new_account_require_new_password(tmp_path):
    config = Owner(tmp_path).configuration
    config.save(form())
    with pytest.raises(ConfigurationFailure, match='更新'):
        config.save(form())
    with pytest.raises(ConfigurationFailure, match='密码'):
        config.save(form(revision=1, username='different', password=None))
    config.save(form(revision=1, password=None, enabled=False))
    assert not config.load()['enabled']


def test_same_database_only_for_shared_writer(tmp_path):
    owner = Owner(tmp_path)
    candidate = dict(host=owner.settings.db_host, port=owner.settings.db_port, database=owner.settings.db_name,
                     username='saved_writer', password='saved-secret')
    owner.shared_configuration = SimpleNamespace(load=lambda: candidate, url=ETFConfiguration.url)
    assert owner.configuration.load()['source'] == 'etf'
    candidate['database'] = 'a_different_database'
    assert owner.configuration.issue() == 'missing_writer_configuration'
    assert owner.configuration.load()['username'] == ''


def test_key_loss_does_not_replace_key_or_configuration(tmp_path):
    owner = Owner(tmp_path)
    owner.configuration.save(form())
    path = owner.root / 'short-validation-secrets' / 'source.key'
    path.unlink()
    with pytest.raises(ConfigurationFailure):
        owner.configuration.load()
    assert not path.exists()


def test_target_changed_requires_new_password(tmp_path):
    owner = Owner(tmp_path)
    owner.configuration.save(form())
    owner.settings.db_host = 'changed.example'
    public = owner.configuration.public()
    assert public['target_changed'] and not public['password_set']
    assert public['host'] == 'changed.example'
    with pytest.raises(ConfigurationFailure, match='密码'):
        owner.configuration.save(form(revision=1, password=None))


def test_first_market_configuration_never_recreates_a_missing_sibling_etf_key(tmp_path):
    from app.services.etf_collection import ETFCollection
    owner = Owner(tmp_path)
    etf = ETFCollection(owner.settings)
    etf.root.mkdir(parents=True, exist_ok=True)
    etf.configuration.save(dict(revision=0, username='etf_writer', password='synthetic-etf-secret',
                                enabled=True, lookback_days=30, batch_days=1, schedule=['09:00']))
    shared_key = etf.root.parent / 'short-validation-secrets' / 'source.key'
    previous_key = shared_key.read_bytes()
    shared_key.unlink()
    owner.shared_configuration = etf.configuration
    owner.configuration.save(form())
    assert not shared_key.exists()
    assert (owner.root / 'short-validation-secrets' / 'source.key').exists()
    assert owner.configuration.load()['password'] == 'test-secret-never-public'
    shared_key.write_bytes(previous_key)
    shared_key.chmod(0o600)
    assert etf.configuration.load()['password'] == 'synthetic-etf-secret'
