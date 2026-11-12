"""Shared database authority, snapshot pinning and non-destructive ledger migration."""
import json
import pytest
from app.core.config import Settings
from app.services.system_configuration import SystemConfiguration
from app.services.etf_collection import ETFCollection
from app.services.etf_configuration import ConfigurationFailure,reconcile_system_ledger,target_key
from app.services.market_configuration import MarketConfiguration

def owner(tmp_path):
    return ETFCollection(Settings(_env_file=None,log_dir=tmp_path,db_host='db.example',db_name='shared',db_user='shared_user',db_password='shared_secret'))
def update(settings,**changes):
    central=SystemConfiguration(settings);body=central.public();body['database'].pop('password_set',None)
    body['database'].update(changes);body['database'].setdefault('password',None)
    return central.save(body)
def test_both_modules_use_central_default_not_legacy_writers(tmp_path):
    service=owner(tmp_path)
    service.settings.etf_collection_writer_url='mysql+pymysql://wrong:wrong@wrong/other'
    service.settings.market_collection_writer_url='mysql+pymysql://wrong:wrong@wrong/other'
    for config in (service.configuration,MarketConfiguration(service)):
        assert config.load()['host']=='db.example' and config.load()['username']=='shared_user'
        assert 'password' not in config.public()
        with pytest.raises(ConfigurationFailure,match='系统配置'):config.save({})
def test_update_both_readers_and_keep_engine_snapshot(tmp_path):
    service=owner(tmp_path);market=MarketConfiguration(service);old=service.configuration.load()
    update(service.settings,host='new.example',password='new-secret')
    assert service.configuration.load()['host']==market.load()['host']=='new.example'
    calls=[];service.engine_factory=lambda url,**kwargs:calls.append(url) or object()
    market.engine(old);service.configuration.engine(old)
    assert [url.host for url in calls]==['db.example','db.example']
def test_ledger_target_switch_archives_and_preserves_queued_intent(tmp_path):
    service=owner(tmp_path)
    with service.connect() as db:
        reconcile_system_ledger(service.configuration,db,('observations','slots','metadata'),'first')
        db.execute("INSERT INTO observations VALUES('share','510300','2026-11-10','complete',1)")
        db.execute("INSERT INTO manual_runs(id,status,phase,requested_at) VALUES('queued','queued','queued','2026-11-10')")
    update(service.settings,database='other',password='new-secret')
    with service.connect() as db:
        reconcile_system_ledger(service.configuration,db,('observations','slots','metadata'),'second')
        assert db.execute('SELECT count(*) FROM observations').fetchone()[0]==0
        archived=db.execute("SELECT payload FROM system_target_archive WHERE table_name='observations'").fetchone()
        assert json.loads(archived[0])['code']=='510300'
        assert db.execute("SELECT status FROM manual_runs WHERE id='queued'").fetchone()[0]=='queued'
        count=db.execute('SELECT count(*) FROM system_target_archive').fetchone()[0]
        reconcile_system_ledger(service.configuration,db,('observations','slots','metadata'),'third')
        assert db.execute('SELECT count(*) FROM system_target_archive').fetchone()[0]==count
def test_first_adoption_detects_legacy_custom_target(tmp_path):
    service=owner(tmp_path);legacy=dict(revision=1,host='legacy.example',port=3306,database='legacy',username='old',password='old-secret')
    service.root.mkdir(parents=True,exist_ok=True)
    ciphertext=service.configuration.cipher().encrypt(json.dumps(legacy).encode())
    with service.connect() as db:
        db.execute('INSERT INTO configuration VALUES(1,1,?)',(ciphertext,));db.commit()
        db.execute("INSERT INTO observations VALUES('share','510300','2026-11-10','complete',1)")
        reconcile_system_ledger(service.configuration,db,('observations','slots','metadata'),'adoption')
        assert db.execute('SELECT count(*) FROM observations').fetchone()[0]==0
        assert db.execute('SELECT ciphertext FROM configuration').fetchone()[0]==ciphertext
        assert db.execute("SELECT target FROM system_target_archive WHERE table_name='observations'").fetchone()[0]==target_key(legacy)
def test_credentials_only_change_preserves_ledger(tmp_path):
    service=owner(tmp_path)
    with service.connect() as db:
        reconcile_system_ledger(service.configuration,db,('observations','slots','metadata'),'first')
        db.execute("INSERT INTO observations VALUES('share','510300','2026-11-10','complete',1)")
    update(service.settings,username='new_user',password='new-secret')
    with service.connect() as db:
        reconcile_system_ledger(service.configuration,db,('observations','slots','metadata'),'second')
        assert db.execute('SELECT count(*) FROM observations').fetchone()[0]==1

def test_corrupt_legacy_requires_explicit_central_save_then_archives(tmp_path):
    service=owner(tmp_path)
    with service.connect() as db:
        db.execute("INSERT INTO configuration VALUES(1,1,'broken')");db.commit()
        db.execute("INSERT INTO observations VALUES('share','510300','2026-11-10','complete',1)")
        with pytest.raises(ConfigurationFailure,match='系统配置'):
            reconcile_system_ledger(service.configuration,db,('observations','slots','metadata'),'before')
        assert db.execute('SELECT count(*) FROM observations').fetchone()[0]==1
    update(service.settings,password='explicit-secret')
    with service.connect() as db:
        reconcile_system_ledger(service.configuration,db,('observations','slots','metadata'),'after')
        assert db.execute('SELECT ciphertext FROM configuration').fetchone()[0]=='broken'
        assert db.execute('SELECT count(*) FROM observations').fetchone()[0]==0
        assert db.execute("SELECT target FROM system_target_archive WHERE table_name='observations'").fetchone()[0]=='unverified-legacy-target'
