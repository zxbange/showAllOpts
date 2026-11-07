"""Offline page configuration and job/security regressions; never use live DB credentials."""
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import json
import threading
import time

import portalocker
import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from account_helpers import install_accounts, sign_in, PASSWORD
from app.api.routes.accounts import require_user
from app.api.routes.etf import router
from app.core.config import Settings
from app.services.etf_collection import ETFCollection
from app.services.etf_configuration import ConfigurationFailure
from test_etf_collection import worker, EVENING


def form(**changes):
    return dict(revision=0, username='etf_writer', password='private-test-password', enabled=True,
                lookback_days=300, batch_days=5, schedule=['18:30'], db_tls=False,
                **changes)


def save(w, changes=None):
    body = form()
    body.update(changes or {})
    # Deterministic tests enqueue/tick themselves instead of starting daemon workers.
    w.start = lambda: None
    return w.configure(body)


def test_page_config_encrypted_persistent_and_no_password_response(tmp_path):
    w, _, _ = worker(tmp_path)
    public = save(w)
    assert public['revision'] == 1 and public['password_set']
    assert 'password' not in public and public['source'] == 'page'
    assert 'private-test-password' not in (w.root / 'state.sqlite').read_bytes().decode(errors='ignore')
    assert (w.root / 'state.sqlite').stat().st_mode & 0o777 == 0o600
    restarted, _, _ = worker(tmp_path)
    assert restarted.configuration.public() == public
    assert restarted.configuration.load()['password'] == 'private-test-password'
    assert 'private-test-password' not in json.dumps(restarted.status())
    for file in (w.root.parent / 'short-validation-secrets').iterdir():
        assert file.stat().st_mode & 0o077 == 0


def test_password_retention_stale_revision_and_changed_user(tmp_path):
    w, _, _ = worker(tmp_path)
    save(w)
    body = form(); body.update(revision=1, password=None, enabled=False)
    w.configure(body)
    assert w.configuration.load()['password'] == 'private-test-password'
    with pytest.raises(ConfigurationFailure, match='其他页面'):
        w.configure(body)
    body.update(revision=2, username='other')
    with pytest.raises(ConfigurationFailure, match='重新输入密码'):
        w.configure(body)
    assert w.configuration.public()['revision'] == 2


def test_key_loss_and_corruption_fail_closed(tmp_path):
    w, _, _ = worker(tmp_path); save(w)
    key = w.root.parent / 'short-validation-secrets' / 'source.key'
    original = key.read_bytes(); key.unlink()
    with pytest.raises(ConfigurationFailure, match='原密钥缺失'):
        w.configuration.public()
    assert not key.exists()
    key.write_bytes(original); key.chmod(0o600)
    assert w.configuration.public()['revision'] == 1
    with w.connect() as db:
        db.execute("UPDATE configuration SET ciphertext='broken'")
    with pytest.raises(ConfigurationFailure):
        w.configuration.public()
    assert key.read_bytes() == original


def test_target_change_needs_fresh_password_and_displays_new_target(tmp_path):
    w, _, _ = worker(tmp_path); save(w)
    w.settings.db_host = 'new-db.example'
    assert w.configuration_issue == 'writer_target_mismatch'
    public = w.configuration.public()
    assert public['host'] == 'new-db.example' and not public['password_set'] and public['target_changed']
    body = form(); body.update(revision=1, password=None, db_tls=True)
    with pytest.raises(ConfigurationFailure, match='重新输入密码'):
        w.test_configuration(body)
    assert w.configuration.load()['host'] == '127.0.0.1'


def test_bad_legacy_config_can_be_repaired_on_page(tmp_path):
    from pydantic import SecretStr
    w, _, _ = worker(tmp_path)
    w.settings.etf_collection_writer_url = SecretStr('mysql+pymysql://bad:secret@elsewhere/wrong')
    assert w.configuration_issue == 'writer_target_mismatch'
    assert w.configuration.public()['host'] == '127.0.0.1'
    save(w)
    assert w.configured


@pytest.mark.parametrize('username', ['', '   '])
def test_empty_account_is_rejected(tmp_path, username):
    w, _, _ = worker(tmp_path)
    with pytest.raises(ConfigurationFailure):
        save(w, {'username': username})


def test_remote_plaintext_requires_explicit_ack_and_tls_does_not_fallback(tmp_path):
    w, _, _ = worker(tmp_path)
    w.settings.db_host = 'db.example'
    from pydantic import SecretStr
    w.settings.etf_collection_writer_url = SecretStr('')
    assert w.configuration.public()['db_tls'] is True
    with pytest.raises(ConfigurationFailure, match='TLS'):
        save(w)
    public = save(w, {'db_tls': True})
    received = []
    w.engine_factory = lambda url, **kwargs: received.append((url, kwargs)) or SimpleNamespace(dispose=lambda: None)
    w.configuration.engine(w.configuration.load())
    args = received[0][1]
    assert args['hide_parameters']
    assert args['connect_args']['ssl'].check_hostname
    assert 'ssl_disabled' not in args['connect_args']
    assert public['db_tls']


def test_manual_is_async_idempotent_and_works_with_auto_disabled(tmp_path):
    w, c, _ = worker(tmp_path); save(w, {'enabled': False})
    first = w.request_run('manual-request-0001')
    assert first['status'] == 'queued' and not c.calls
    assert w.request_run(first['id']) == first
    with pytest.raises(ConfigurationFailure, match='排队'):
        w.request_run('manual-request-0002')
    assert w.tick(EVENING)
    assert w.status()['manual']['status'] == 'complete'
    count = len(c.calls)
    assert w.request_run(first['id'])['status'] == 'complete'
    assert not w.tick(EVENING + timedelta(hours=1))
    assert len(c.calls) == count


def test_configure_rejects_queued_and_cross_process_lock(tmp_path):
    w, c, _ = worker(tmp_path); save(w)
    body = form(); body['revision'] = 1
    with portalocker.Lock(str(w.root / 'collection.lock'), timeout=0):
        with pytest.raises(ConfigurationFailure, match='正在运行'):
            w.configure(body)
        with pytest.raises(ConfigurationFailure, match='正在运行'):
            w.request_run('manual-request-lock')
        assert not w.tick(EVENING)
    w.request_run('manual-request-lock')
    with pytest.raises(ConfigurationFailure, match='排队'):
        w.configure(body)
    assert not c.calls


def test_manual_failure_redaction_and_preserves_prior_runs(tmp_path):
    w, c, _ = worker(tmp_path); save(w)
    c.write_failure = True
    w.request_run('manual-request-fail')
    assert not w.tick(EVENING)
    assert w.status()['manual']['error'] == 'writer_write_failed'
    assert 'secret' not in str(w.status())
    c.write_failure = False
    w.request_run('manual-request-good'); assert w.tick(EVENING)
    assert w.status()['manual']['status'] == 'complete'
    with w.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM manual_runs').fetchone()[0] == 2


def test_restart_marks_interrupted_and_clears_progress_without_network(tmp_path):
    w, c, _ = worker(tmp_path)
    save(w, {'enabled': False})
    with w.connect() as db:
        db.execute("INSERT INTO manual_runs(id,status,requested_at) VALUES('lost','running',?)", (EVENING.isoformat(),))
        db.execute("INSERT OR REPLACE INTO metadata VALUES('progress',?)", ('{"phase":"fetching","completed":1,"total":10}',))
    restarted, collector, _ = worker(tmp_path)
    restarted.start()
    try:
        assert restarted.status()['manual']['status'] == 'interrupted'
        assert restarted.status()['progress'] is None
        assert not collector.calls
    finally:
        restarted.stop()


def test_actual_worker_wakes_on_manual_and_returns_before_finish(tmp_path):
    w, c, _ = worker(tmp_path)
    started, release = threading.Event(), threading.Event()
    original = w._collect
    def collect(*args, **kwargs):
        started.set(); assert release.wait(5)
        return original(*args, **kwargs)
    w._collect = collect
    try:
        result = w.request_run('manual-thread-0001')
        assert result['status'] == 'queued'
        assert started.wait(3)
        assert w.status()['manual']['status'] == 'running'
        release.set()
        for _ in range(100):
            if w.status()['manual']['status'] == 'complete': break
            time.sleep(.01)
        assert w.status()['manual']['status'] == 'complete'
    finally:
        release.set(); w.stop()


def test_page_schedule_and_progress(tmp_path):
    w, c, _ = worker(tmp_path); save(w, {'schedule': ['19:15']})
    w.floor = None
    assert not w.tick(EVENING)
    assert w.tick(EVENING.replace(hour=19, minute=15))
    assert w.status()['schedule'] == ['19:15']
    assert w.status()['progress'] is None


@pytest.fixture
def page_client(tmp_path):
    settings = Settings(_env_file=None, log_dir=tmp_path, auth_allow_http=True)
    app = FastAPI()
    install_accounts(app, settings)
    app.state.etf_collection = ETFCollection(settings)
    app.state.etf_collection.start = lambda: None
    app.include_router(router, prefix='/api/v1', dependencies=[Depends(require_user)])
    client = TestClient(app, base_url='https://testserver')
    sign_in(client)
    yield client
    client.close()


def test_routes_auth_csrf_viewer_and_safe_validation(page_client):
    c = page_client
    path = '/api/v1/etf-shares/collection-configuration'
    assert c.get(path).json()['transport_secure']
    data = form()
    bad = c.post(path, json=data | {'password': 'secret-do-not-echo', 'lookback_days': 'bad'})
    assert bad.status_code == 422 and 'secret-do-not-echo' not in bad.text
    assert c.post(path, json=data).status_code == 200
    assert 'private-test-password' not in c.get(path).text
    token = c.headers.pop('X-CSRF-Token')
    assert c.post('/api/v1/etf-shares/collection-run', json={'request_id':'new-request-1234'}).status_code == 403
    c.headers['X-CSRF-Token'] = token
    actor = c.get('/api/auth/session').json()['user']['id']
    c.app.state.accounts.create('reader', PASSWORD, 'viewer', actor)
    sign_in(c, 'reader')
    assert c.get(path).status_code == 403
    assert c.get('/api/v1/etf-shares/collection-status').status_code == 200
    assert c.post(path, json=data).status_code == 403
    c.cookies.clear()
    assert c.get(path).status_code == 401


def test_http_password_gate_even_with_http_login_allowed(page_client):
    app = page_client.app
    with TestClient(app, base_url='http://testserver') as client:
        sign_in(client)
        body = form()
        for endpoint in ('collection-configuration', 'collection-test'):
            response = client.post('/api/v1/etf-shares/' + endpoint, json=body)
            assert response.status_code == 403
            assert 'private-test-password' not in response.text
        config = client.get('/api/v1/etf-shares/collection-configuration').json()
        assert config['https_required'] and not config['allow_insecure_transport']


def test_test_route_read_only_and_manual_accepted(page_client, monkeypatch):
    c = page_client; w = c.app.state.etf_collection
    calls = []
    w.configuration.test = lambda body: calls.append(body) or {'ok':True, 'permissions_verified':False, 'message':'read only'}
    assert c.post('/api/v1/etf-shares/collection-test', json=form()).json()['permissions_verified'] is False
    assert not w.configured  # test did not save config
    assert c.post('/api/v1/etf-shares/collection-configuration', json=form()).status_code == 200
    response = c.post('/api/v1/etf-shares/collection-run', json={'request_id':'page-manual-request-1'})
    assert response.status_code == 202 and response.json()['status'] == 'queued'
    assert 'private-test-password' not in response.text


def test_page_retry_exhausted_is_explicit_preserves_completed_and_blocked(tmp_path):
    w, _, _ = worker(tmp_path); save(w)
    with w.connect() as db:
        for code, state in [('a', 'retry_exhausted'), ('b', 'complete'), ('c', 'blocked_unverified_history')]:
            db.execute('INSERT INTO observations VALUES (?,?,?,?,?)', ('shares', code, '2026-11-01', state, 6))
    assert w.status()['retry_exhausted_count'] == 1
    w.request_run('manual-retry-exhausted', retry_exhausted=True)
    with w.connect() as db:
        rows = [tuple(row) for row in db.execute('SELECT code,status,attempts FROM observations ORDER BY code')]
    assert rows == [('a','pending',0), ('b','complete',6), ('c','blocked_unverified_history',6)]
    # Repeating an already accepted request must not reset attempts again.
    with w.connect() as db:
        db.execute("UPDATE observations SET status='retry_exhausted',attempts=6 WHERE code='a'")
    w.request_run('manual-retry-exhausted', retry_exhausted=True)
    assert w.status()['retry_exhausted_count'] == 1


def test_connection_probe_only_reads_fixed_tables(tmp_path):
    from sqlalchemy import event, text
    import sqlite3
    w, _, _ = worker(tmp_path)
    db_path = tmp_path / 'probe.sqlite'
    engine = create_engine('sqlite:///' + str(db_path))
    with engine.begin() as connection:
        for name in ('etf_share_daily','etf_price_daily','etf_trading_day'):
            connection.execute(text(f'CREATE TABLE {name} (id INTEGER)'))
    engine.dispose()
    queries = []
    @event.listens_for(engine, 'connect')
    def restrict(connection, _):
        def authorize(action, one, two, database, trigger):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                          sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        connection.set_authorizer(authorize)
    @event.listens_for(engine, 'before_cursor_execute')
    def observe(connection, cursor, statement, parameters, context, executemany):
        queries.append(statement)
    w.configuration.engine = lambda config: engine
    result = w.test_configuration(form())
    assert result['ok'] and not result['permissions_verified']
    assert queries == [f'SELECT 1 FROM `{name}` LIMIT 0' for name in
                       ('etf_share_daily','etf_price_daily','etf_trading_day')]
    assert w.configuration.public()['source'] == 'environment'


def test_tls_driver_requires_encryption_before_authentication(tmp_path):
    import pymysql
    w, _, _ = worker(tmp_path)
    received = []
    w.engine_factory = lambda url, **kwargs: received.append(kwargs) or None
    config = w.configuration.load() | {'db_tls': True}
    w.configuration.engine(config)
    # defer_connect guarantees this is a pure offline driver check.
    connection = pymysql.connections.Connection(defer_connect=True, **received[0]['connect_args'])
    assert connection.ssl and connection._ssl_required
    assert connection.ctx.check_hostname
    connection.server_version = '8.0.0'
    connection.server_capabilities = 0
    with pytest.raises(pymysql.err.OperationalError, match='server doesn.t support'):
        connection._request_authentication()


def test_configuration_activation_floor_is_shared_across_processes(tmp_path):
    first, _, _ = worker(tmp_path)
    other, collector, _ = worker(tmp_path)
    other.floor = EVENING - timedelta(hours=1)
    save(first)
    assert not other.tick(EVENING + timedelta(minutes=1))
    assert not collector.calls  # A second process must not replay the newly saved past slot.
    assert other.tick(EVENING + timedelta(days=1))


def test_target_change_interrupts_stale_queue_so_page_can_repair(tmp_path):
    w, c, _ = worker(tmp_path); save(w)
    w.request_run('stale-target-queued')
    w.settings.db_host = 'new-db.example'
    state = w.status()
    assert state['status'] == 'writer_target_mismatch'
    assert state['manual']['status'] == 'interrupted'
    assert state['manual']['error'] == 'configuration_changed'
    assert not c.calls
    body = form(); body.update(revision=1, password='new-target-password', db_tls=True)
    w.configure(body)
    assert w.configuration.public()['host'] == 'new-db.example'
    assert w.configured


@pytest.mark.parametrize('username', ['readonly', 'root', 'mysql.sys', 'mysql.session', 'mysql.infoschema'])
def test_application_account_can_save_probe_and_collect(tmp_path, username):
    w, c, _ = worker(tmp_path)
    w.settings.db_user = username
    body = form() | {'username': w.settings.db_user}
    seen = []
    class Connection:
        def execute(self, statement):
            seen.append(str(statement))
    w.engine_factory = lambda url, **kwargs: SimpleNamespace(
        connect=lambda: nullcontext(Connection()), dispose=lambda: None)
    assert w.test_configuration(body)['ok']
    assert len(seen) == 3 and all(query.startswith('SELECT ') for query in seen)
    public = save(w, {'username': w.settings.db_user, 'enabled': False})
    assert public['username'] == w.settings.db_user and public['password_set']
    restarted, _, _ = worker(tmp_path)
    assert restarted.configuration.public() == public
    w.request_run('same-account-collection')
    assert w.tick(EVENING) and c.writes
    assert w.status()['manual']['status'] == 'complete'


def test_default_time_and_legacy_multiple_times_migrate_without_rewriting_credentials(tmp_path):
    w = ETFCollection(Settings(_env_file=None, log_dir=tmp_path))
    assert w.configuration.public()['schedule'] == ['09:00']
    old, _, _ = worker(tmp_path)
    save(old)
    config = old.configuration.load() | {'schedule': ['09:30', '11:30', '18:30']}
    ciphertext = old.configuration.cipher().encrypt(json.dumps(config).encode())
    with old.connect() as db:
        db.execute('UPDATE configuration SET ciphertext=?', (ciphertext,))
    public = w.configuration.public()
    assert public['schedule'] == ['09:00'] and public['schedule_migrated']
    assert w.configuration.load()['password'] == 'private-test-password'
    assert w.configuration.record()['ciphertext'] == ciphertext
    w.start = lambda: None
    w.configure(form() | {'revision': 1, 'password': None, 'schedule': ['10:15']})
    assert w.configuration.public()['schedule'] == ['10:15']
    assert not w.configuration.public().get('schedule_migrated')


@pytest.mark.parametrize('schedule', [[], ['09:00', '18:30'], ['25:00'], ['9:00']])
def test_only_one_valid_daily_time_can_be_saved(tmp_path, schedule):
    w, _, _ = worker(tmp_path)
    with pytest.raises(ConfigurationFailure, match='一个每日采集时间'):
        save(w, {'schedule': schedule})


def test_custom_target_roundtrip_and_no_service_target_or_password_fallback(tmp_path):
    w, _, _ = worker(tmp_path)
    public = save(w, {'host': 'new.example', 'port': 3307, 'database': 'etf_market',
                      'username': w.settings.db_user, 'db_tls': True})
    assert (public['host'], public['port'], public['database']) == ('new.example', 3307, 'etf_market')
    assert w.configured
    assert w.settings.db_host == '127.0.0.1' and w.settings.db_port == 3306
    restarted, _, _ = worker(tmp_path)
    restarted.settings.db_host = 'unrelated-app.example'
    assert restarted.configuration.public() == public
    assert restarted.configuration.reader_configuration()['host'] == 'new.example'
    assert restarted.configured
    for changed in ({'host': 'other.example'}, {'port': 3308}, {'database': 'other'}, {'username': 'other'}):
        body = form() | {'revision': 1, 'host': 'new.example', 'port': 3307, 'database': 'etf_market',
                         'username': w.settings.db_user, 'db_tls': True, 'password': None} | changed
        with pytest.raises(ConfigurationFailure, match='重新输入密码'):
            w.test_configuration(body)
    retained = w.configuration.validate(form() | {'revision': 1, 'password': None,
        'username': w.settings.db_user, 'db_tls': True})
    assert retained['host'] == 'new.example' and retained['password'] == 'private-test-password'


def test_switch_target_archives_collection_bookkeeping_without_touching_business_data(tmp_path):
    w, collector, _ = worker(tmp_path)
    save(w)
    w.request_run('old-target-manual-request')
    assert w.tick(EVENING)
    original_writes = list(collector.writes)
    w.configure(form() | {'revision': 1, 'host': 'new.example', 'port': 3307,
                          'database': 'new_market', 'db_tls': True})
    status = w.status()
    assert status['manual'] is None and not status['runs'] and not status['gaps']
    assert collector.writes == original_writes
    with w.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM manual_runs').fetchone()[0] == 1
        archive = json.loads(db.execute('SELECT state FROM target_archive').fetchone()[0])
        assert archive['observations'] and archive['metadata']
    w.request_run('new-target-manual-request')
    assert w.status()['manual']['id'] == 'new-target-manual-request'


def test_custom_target_routes_accept_target_and_same_application_account(page_client):
    path = '/api/v1/etf-shares/collection-configuration'
    settings = page_client.app.state.etf_collection.settings
    response = page_client.post(path, json=form() | {'host': 'new.example', 'port': 3307,
        'database': 'etf_only', 'username': settings.db_user, 'db_tls': True})
    assert response.status_code == 200, response.text
    assert response.json()['host'] == 'new.example' and response.json()['database'] == 'etf_only'
    assert 'private-test-password' not in response.text
    assert settings.db_host == '127.0.0.1'


def test_custom_engine_pins_dns_and_keeps_tls_hostname(tmp_path, monkeypatch):
    import socket
    import pymysql
    from app.services import database_sources
    w, _, _ = worker(tmp_path)
    save(w, {'host': 'new.example', 'port': 3307, 'database': 'etf_only', 'db_tls': True})
    captured = []
    w.engine_factory = lambda url, **kwargs: captured.append((url, kwargs))
    w.configuration.engine(w.configuration.load())
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('169.254.169.254', 3307))]
    monkeypatch.setattr(database_sources, 'resolve_addresses', lambda *a: addresses)
    sockets, connections = [], []
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: sockets.append((a, kw)))
    monkeypatch.setattr(pymysql, 'connect', lambda **kw: connections.append(kw))
    with pytest.raises(ConfigurationFailure):
        captured[0][1]['creator']()
    assert not sockets and not connections  # Metadata addresses are blocked before credentials.
    addresses[0] = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.1.2.3', 3307))
    class Connection:
        def connect(self, **kwargs):
            self.socket = kwargs['sock']
    connection = Connection()
    monkeypatch.setattr(pymysql, 'connect', lambda **kw: connections.append(kw) or connection)
    sock = object()
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: sockets.append((a, kw)) or sock)
    assert captured[0][1]['creator']() is connection
    assert sockets[0][0] == (('10.1.2.3', 3307),)
    assert connections[0]['host'] == 'new.example' and connections[0]['ssl'].check_hostname
    assert connection.socket is sock


def test_custom_connection_retains_safe_permission_classification(tmp_path, monkeypatch):
    import socket
    import pymysql
    from app.services import database_sources
    from app.services.etf_collection import writer_error_code
    w, _, _ = worker(tmp_path)
    save(w, {'host': 'new.example', 'port': 3307, 'database': 'etf_only', 'db_tls': True})
    captured = []
    w.engine_factory = lambda url, **kwargs: captured.append(kwargs)
    w.configuration.engine(w.configuration.load())
    monkeypatch.setattr(database_sources, 'resolve_addresses', lambda *args:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.1.2.3', 3307))])
    def denied(**kwargs):
        raise pymysql.err.OperationalError(1045, 'private password and raw DSN')
    monkeypatch.setattr(pymysql, 'connect', denied)
    with pytest.raises(ConfigurationFailure) as failure:
        captured[0]['creator']()
    assert writer_error_code(failure.value) == 'writer_permission_denied'
    assert 'private' not in str(failure.value)
