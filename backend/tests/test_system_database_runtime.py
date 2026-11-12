from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool
from app.core.config import Settings
from app.core.database import VersionedDatabase, database_identity
from app.core.cache import SnapshotCache
from app.services.system_configuration import SystemConfiguration, database_config


def test_two_workers_observe_database_revision_but_schedule_only_keeps_pool(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, log_dir=tmp_path, db_name='first')
    created = []
    def build(_settings, *, for_cache=False, configuration=None):
        engine = create_engine('sqlite://', poolclass=QueuePool)
        created.append((engine, dict(configuration)))
        return engine
    monkeypatch.setattr('app.core.database.build_engine', build)
    first, second = VersionedDatabase(settings), VersionedDatabase(settings)
    a, b = first.current(), second.current()
    connection = a.connect()
    service = SystemConfiguration(settings)
    config = service.load()
    config['schedules']['market']['times'] = ['00:02']
    service.save(config)
    assert first.current() is a and second.current() is b
    config = service.load()
    config['database']['password'] = 'rotated-test-secret'
    service.save(config)
    assert first.current() is not a and second.current() is not b
    assert created[-1][1]['password'] == 'rotated-test-secret'
    assert connection.exec_driver_sql('SELECT 1').scalar() == 1
    connection.close()
    first.dispose(); second.dispose()


def test_database_target_fences_all_snapshots_and_namespace_survives_restart(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, log_dir=tmp_path, db_name='first')
    monkeypatch.setattr('app.core.database.build_engine', lambda *a, **k: create_engine('sqlite://', poolclass=QueuePool))
    cache = SnapshotCache(settings, VersionedDatabase(settings))
    cache.root.mkdir(parents=True)
    path = cache.root / 'industry-momentum-existing.sqlite'
    path.touch()
    legacy = {'file': path.name}
    assert cache.snapshot_path('industry-momentum', legacy) == path
    identity = cache.target_identity()
    service = SystemConfiguration(settings)
    config = service.load()
    config['database']['database'] = 'second'
    config['database']['password'] = 'new-target-test-secret'
    service.save(config)
    assert cache.target_identity() != identity
    assert cache.snapshot_path('industry-momentum', legacy) is None
    assert cache.snapshot_path('industry-momentum', dict(legacy, database_target=identity)) is None
    updated = dict(legacy, database_target=database_identity(database_config(settings)))
    assert cache.snapshot_path('industry-momentum', updated) == path
    restarted = SnapshotCache(settings, VersionedDatabase(settings))
    assert restarted.root == cache.root
    assert restarted.snapshot_path('industry-momentum', updated) == path


def test_file_target_change_and_settings_restart_preserve_namespace(tmp_path, monkeypatch):
    env = tmp_path / 'app.config'
    env.write_text("DB_HOST='db.example'\nDB_NAME='first'\n")
    settings = Settings(_env_file=env, log_dir=tmp_path / 'log')
    first = SnapshotCache(settings, VersionedDatabase(settings))
    initial = first.legacy_target_identity
    env.write_text("DB_HOST='db.example'\nDB_NAME='second'\n")
    restarted_settings = Settings(_env_file=env, log_dir=tmp_path / 'log')
    second = SnapshotCache(restarted_settings, VersionedDatabase(restarted_settings))
    assert second.root == first.root
    assert second.legacy_target_identity == initial
    assert second.target_identity() != initial


def test_asharehub_runtime_reads_file_changes_and_pins_current_run(tmp_path, monkeypatch):
    from app.services.market_collection import configured_asharehub_key
    from app.services.system_configuration import pin_configuration
    monkeypatch.delenv('ASHAREHUB_API_KEY', raising=False)
    env = tmp_path / 'app.config'
    env.write_text("DB_HOST='db.example'\nASHAREHUB_API_KEY='old-test-key'\n")
    settings = Settings(_env_file=env, log_dir=tmp_path / 'log')
    assert configured_asharehub_key(settings).get_secret_value() == 'old-test-key'
    with pin_configuration(settings):
        env.write_text("DB_HOST='db.example'\nASHAREHUB_API_KEY='new-test-key'\n")
        assert configured_asharehub_key(settings).get_secret_value() == 'old-test-key'
    assert configured_asharehub_key(settings).get_secret_value() == 'new-test-key'
    assert 'new-test-key' not in str(SystemConfiguration(settings).public())


def test_sync_timeout_rebuilds_pool_without_database_revision_change(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, log_dir=tmp_path)
    monkeypatch.setattr('app.core.database.build_engine', lambda *a, **k: create_engine('sqlite://', poolclass=QueuePool))
    pool = VersionedDatabase(settings, for_cache=True)
    first = pool.current()
    service = SystemConfiguration(settings)
    config = service.load()
    revision = config['database_revision']
    config['runtime']['database_sync']['read_timeout_seconds'] = 1800
    service.save(config)
    assert service.load()['database_revision'] == revision
    assert pool.current() is not first
