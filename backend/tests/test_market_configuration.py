"""Central authority and legacy credential safety for market writes."""
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import sqlite3
import pytest
from app.core.config import Settings
from app.services.market_configuration import MarketConfiguration
from app.services.etf_configuration import ConfigurationFailure,ETFConfiguration
from app.services.system_configuration import SystemConfiguration,SystemConfigurationError
class Owner:
    def __init__(self,path,**settings):
        self.root=path/'market-data'/'market-collection'
        self.settings=Settings(_env_file=None,log_dir=path,**settings)
        self.clock=lambda:datetime(2026,11,10,18,tzinfo=ZoneInfo('Asia/Shanghai'))
        self.configuration=MarketConfiguration(self)
    @contextmanager
    def connect(self):
        self.root.mkdir(parents=True,exist_ok=True);db=sqlite3.connect(self.root/'state.sqlite');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE IF NOT EXISTS configuration(id INTEGER PRIMARY KEY,revision INTEGER,ciphertext BLOB)')
        try:
            with db:yield db
        finally:db.close()
def save(owner,**changes):
    store=SystemConfiguration(owner.settings);body=store.public();body['database'].pop('password_set',None)
    body['database'].update(username='market_writer',password='test-secret-never-public');body['database'].update(changes)
    store.save(body);return store
def test_global_account_is_authoritative_and_encrypted_configuration(tmp_path):
    owner=Owner(tmp_path);assert owner.configuration.load()['username']==owner.settings.db_user
    store=save(owner);public=owner.configuration.public()
    assert public['source']=='system' and public['schedule']==['00:01'] and 'password' not in public
    assert b'test-secret-never-public' not in store.path.read_bytes()
    assert Owner(tmp_path).configuration.public()==public and owner.configuration.issue() is None
def test_stale_revision_and_new_account_require_new_password(tmp_path):
    owner=Owner(tmp_path);store=save(owner);body=store.public();body['database'].pop('password_set');body['database']['password']=None
    store.save(body)
    with pytest.raises(SystemConfigurationError):store.save(body)
    body=store.public();body['database'].pop('password_set');body['database'].update(username='different',password=None)
    with pytest.raises(SystemConfigurationError,match='密码'):store.save(body)
def test_old_etf_writer_cannot_override_global_database(tmp_path):
    owner=Owner(tmp_path);candidate=dict(host='different',port=3306,database='other',username='saved_writer',password='saved-secret')
    owner.shared_configuration=SimpleNamespace(load=lambda:candidate,url=ETFConfiguration.url)
    assert owner.configuration.load()['username']==owner.settings.db_user
    assert owner.configuration.load()['database']==owner.settings.db_name
    with pytest.raises(ConfigurationFailure,match='系统配置'):owner.configuration.save({})
def test_key_loss_does_not_replace_key_or_configuration(tmp_path):
    owner=Owner(tmp_path);store=save(owner);path=next(store.root.rglob('source.key'));path.unlink()
    with pytest.raises(SystemConfigurationError):owner.configuration.load()
    assert not path.exists()
def test_target_changed_requires_new_password(tmp_path):
    owner=Owner(tmp_path);store=save(owner);body=store.public();body['database'].pop('password_set');body['database'].update(host='changed.example',password=None)
    with pytest.raises(SystemConfigurationError,match='密码'):store.save(body)
    assert owner.configuration.load()['host']==owner.settings.db_host
def test_central_save_never_creates_sibling_legacy_keys(tmp_path):
    from app.services.etf_collection import ETFCollection
    owner=Owner(tmp_path);etf=ETFCollection(owner.settings);save(owner)
    assert not (etf.root.parent/'short-validation-secrets'/'source.key').exists()
    assert not (owner.root/'short-validation-secrets'/'source.key').exists()
    assert etf.configuration.load()['password']==owner.configuration.load()['password']
