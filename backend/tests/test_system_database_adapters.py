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
    calls=[];service.engine_factory=lambda url,**kwargs:calls.append((url, kwargs)) or object()
    market.engine(old);service.configuration.engine(old)
    assert [url.host for url, _ in calls]==['db.example','db.example']
    assert 'pool_size' not in calls[0][1]  # Legacy market writer is not retuned.
    assert calls[1][1]['pool_size'] == 1 and calls[1][1]['max_overflow'] == 0


def test_etf_run_and_system_test_use_the_same_pinned_connection_policy(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import socket
    from types import SimpleNamespace
    from app.core import database
    from app.services import database_sources

    service = owner(tmp_path)
    settings = service.settings
    settings.validation_allowed_targets = ['db.example:3306']
    central = SystemConfiguration(settings)
    resolutions, sockets, driver_options, handshakes, engines, statements = [], [], [], [], [], []

    def resolve(host, port):
        resolutions.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.1.2.3', port))]

    def socket_connect(target, timeout):
        sock = SimpleNamespace(close=lambda: None)
        sockets.append((target, timeout, sock))
        return sock

    def driver(**kwargs):
        driver_options.append(kwargs)
        return SimpleNamespace(connect=lambda *, sock: handshakes.append(sock), close=lambda: None)

    def engine(url, **kwargs):
        engines.append((url, kwargs))
        @contextmanager
        def connect():
            kwargs['creator']()
            yield SimpleNamespace(execute=lambda stmt: statements.append(str(stmt)))
        return SimpleNamespace(connect=connect, dispose=lambda: None)

    monkeypatch.setattr(database_sources, 'resolve_addresses', resolve)
    monkeypatch.setattr(database.socket, 'create_connection', socket_connect)
    monkeypatch.setattr(database.pymysql, 'connect', driver)
    monkeypatch.setattr(database, 'create_engine', engine)
    service.engine_factory = engine
    candidate = central.public()
    candidate['database']['password'] = None  # The unedited form uses the saved password.
    assert central.test(candidate)['ok']
    with service.configuration.engine(service.configuration.load()).connect():
        pass

    assert resolutions == [('db.example', 3306)] * 2
    assert [(target, timeout) for target, timeout, _ in sockets] == [(('10.1.2.3', 3306), 60)] * 2
    assert handshakes == [item[2] for item in sockets]
    assert driver_options[0] == driver_options[1]
    assert (driver_options[0]['host'], driver_options[0]['port']) == ('db.example', 3306)
    assert all(driver_options[0][key] == 60 for key in ('connect_timeout', 'read_timeout', 'write_timeout'))
    assert driver_options[0]['ssl_disabled'] and not driver_options[0]['autocommit']
    assert driver_options[0]['defer_connect'] and 'ssl' not in driver_options[0]
    assert statements == ['SELECT 1']
    assert all(opts['pool_pre_ping'] and opts['hide_parameters'] for _, opts in engines)


def test_etf_shared_engine_enforces_configured_allowlist_before_socket(tmp_path, monkeypatch):
    from app.core import database
    from app.services import database_sources

    service = owner(tmp_path)
    service.settings.validation_allowed_targets = ['approved.example:3306']
    service.engine_factory = lambda url, **opts: opts['creator']
    for module, name in ((database_sources, 'resolve_addresses'),
                         (database.socket, 'create_connection'), (database.pymysql, 'connect')):
        monkeypatch.setattr(module, name, lambda *a, **k: pytest.fail('unapproved target reached network or DBAPI'))

    connect = service.configuration.engine(service.configuration.load())
    with pytest.raises(ConfigurationFailure) as caught:
        connect()
    assert caught.value.database_diagnostic.stage == 'validate_address'
    assert caught.value.database_diagnostic.reason == 'target_address_rejected'
    assert caught.value.database_error_number is None
    assert 'db.example' not in str(caught.value)


def test_etf_shared_creator_projects_connect_timeout_without_raw_context(tmp_path, monkeypatch):
    import errno
    from unittest.mock import Mock
    from app.core import database
    from app.services import database_sources
    from app.services.etf_diagnostics import database_diagnostic, database_phase

    service = owner(tmp_path)
    service.engine_factory = lambda url, **options: options['creator']
    driver, sock = Mock(), Mock()
    driver.close.side_effect = RuntimeError('raw driver close failure')
    sock.close.side_effect = RuntimeError('raw socket close failure')
    raw = database.pymysql.err.OperationalError(2013, 'raw SQL and private password')
    raw.original_exception = TimeoutError(errno.ETIMEDOUT, 'raw network detail')
    driver.connect.side_effect = raw
    monkeypatch.setattr(database_sources.DatabaseSources, 'allowed', lambda self, conf: '10.1.2.3')
    monkeypatch.setattr(database.socket, 'create_connection', lambda *args, **kwargs: sock)
    monkeypatch.setattr(database.pymysql, 'connect', lambda **kwargs: driver)

    connect = service.configuration.engine(service.configuration.load())
    with pytest.raises(ConfigurationFailure) as failure:
        with database_phase('connect'):
            connect()
    error = failure.value
    assert error.__context__ is None and error.__cause__ is None
    assert error.database_error_number == 2013
    diagnostic = database_diagnostic(error, 'load_historical_keys')
    assert diagnostic.stage == 'mysql_connect' and diagnostic.seconds is not None
    assert diagnostic.mysql_code == 2013 and diagnostic.os_errno == errno.ETIMEDOUT
    assert diagnostic.socket_timeout and diagnostic.reason == 'connection_lost'
    assert 'raw' not in str(error)
    driver.close.assert_called_once_with()
    sock.close.assert_called_once_with()


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
