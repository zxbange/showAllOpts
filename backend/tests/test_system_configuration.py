from contextlib import contextmanager
from types import SimpleNamespace
import json
import sqlite3
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from app.core.config import Settings
from app.services.system_configuration import SystemConfiguration, SystemConfigurationError, database_config, task_config, pin_configuration
from app.api.routes.system_configuration import router, save_with_locks
from app.api.routes.accounts import require_admin

def service(tmp_path):
    return SystemConfiguration(Settings(_env_file=None, log_dir=tmp_path, db_password='initial-secret'))

def form(config):
    value = config.public()
    value['database'].pop('password_set')
    value['database']['password'] = None
    return value

def test_default_is_global_database_and_daily_schedules(tmp_path):
    config = service(tmp_path)
    value = config.public()
    assert value['database']['password_set'] and 'password' not in value['database']
    assert value['schedules']['market']['times'] == ['00:01']
    assert value['schedules']['holdings']['start'] == '09:25'
    assert value['schedules']['holdings']['interval_seconds'] == 15
    assert not config.path.exists()

def test_save_encrypted_revisions_and_pin(tmp_path):
    config = service(tmp_path)
    body = form(config)
    body['database']['password'] = 'replacement-secret'
    result = config.save(body)
    assert result['revision'] == result['database_revision'] == 1
    assert b'replacement-secret' not in config.path.read_bytes()
    assert config.path.stat().st_mode & 0o777 == 0o600
    with pin_configuration(config.settings):
        next_body = form(config)
        next_body['database']['password'] = 'third-secret'
        config.save(next_body)
        assert database_config(config.settings)['password'] == 'replacement-secret'
    assert database_config(config.settings)['password'] == 'third-secret'
    next_body = form(config)
    next_body['schedules']['market']['times'] = ['01:01']
    result = config.save(next_body)
    assert result['database_revision'] == 2 and result['revision'] == 3
    assert task_config(config.settings, 'market')['times'] == ['01:01']

def test_stale_and_password_retention_rules(tmp_path):
    config = service(tmp_path)
    body = form(config)
    config.save(body)
    with pytest.raises(SystemConfigurationError, match='更新'):
        config.save(body)
    body = form(config)
    body['database']['host'] = 'different.example'
    with pytest.raises(SystemConfigurationError, match='密码'):
        config.save(body)

def test_missing_key_cannot_replace_existing_configuration(tmp_path):
    config = service(tmp_path)
    config.save(form(config))
    key = config.root / 'short-validation-secrets/source.key'
    key.unlink()
    with pytest.raises(SystemConfigurationError):
        config.load()
    assert not key.exists()

@pytest.mark.parametrize('key,changes', [
    ('market', {'times': ['25:01']}), ('commodities', {'times': ['18:00', '11:00']}),
    ('holdings', {'enabled': True, 'table': ''}), ('social', {'min_interval_minutes': 16, 'max_interval_minutes': 10}),
])
def test_invalid_task_configuration(tmp_path, key, changes):
    config = service(tmp_path)
    body = form(config)
    body['schedules'][key].update(changes)
    with pytest.raises(SystemConfigurationError):
        config.save(body)

def test_routes_require_admin_and_never_echo_bad_body(tmp_path):
    app = FastAPI()
    app.state.accounts = SimpleNamespace(settings=service(tmp_path).settings)
    app.include_router(router)
    def reject():
        raise HTTPException(403, 'administrator required')
    app.dependency_overrides[require_admin] = reject
    client = TestClient(app, base_url='https://testserver')
    assert client.get('/system-configuration').status_code == 403
    assert client.put('/system-configuration', json={}).status_code == 403
    app.dependency_overrides[require_admin] = lambda: {'id': 1}
    response = client.get('/system-configuration')
    assert response.status_code == 200 and 'initial-secret' not in response.text
    response = client.put('/system-configuration', content='initial-secret not json')
    assert response.status_code == 422 and 'initial-secret' not in response.text

def test_database_save_refuses_active_cache_and_preserves_config(tmp_path):
    config = service(tmp_path)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(accounts=SimpleNamespace(settings=config.settings), personal_holdings=SimpleNamespace(root=tmp_path / 'personal-holdings/data'), cache=SimpleNamespace(acquire=lambda: None))))
    body = form(config)
    body['database']['password'] = 'new-secret'
    with pytest.raises(SystemConfigurationError, match='同步'):
        save_with_locks(request, body)
    assert config.load()['revision'] == 0

@pytest.mark.parametrize('change', ['database', 'table', 'enabled'])
def test_save_rejects_inflight_holdings_publication(tmp_path, change):
    import portalocker
    config = service(tmp_path)
    initial = form(config)
    initial['schedules']['holdings']['table'] = 'positions'
    config.save(initial)
    root = tmp_path / 'personal-holdings/data'
    root.mkdir(parents=True)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(accounts=SimpleNamespace(settings=config.settings), personal_holdings=SimpleNamespace(root=root))))
    body = form(config)
    if change == 'database': body['database']['password'] = 'changed-secret'
    elif change == 'table': body['schedules']['holdings']['table'] = 'positions_new'
    else: body['schedules']['holdings']['enabled'] = True
    with portalocker.Lock(str(root / 'configuration.lock'), timeout=0):
        with pytest.raises(SystemConfigurationError, match='持仓'):
            save_with_locks(request, body)
    assert config.load()['revision'] == 1
    with portalocker.Lock(str(root / 'configuration.lock'), timeout=0): pass

def test_holdings_save_ignores_long_lived_worker_leader_lock(tmp_path):
    import portalocker
    config = service(tmp_path)
    root = tmp_path / 'personal-holdings/data'
    root.mkdir(parents=True)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(accounts=SimpleNamespace(settings=config.settings), personal_holdings=SimpleNamespace(root=root))))
    body = form(config)
    body['schedules']['holdings'].update(table='positions', enabled=True)
    with portalocker.Lock(str(root / 'worker.lock'), timeout=0): saved = save_with_locks(request, body)
    assert saved['revision'] == 1 and saved['database_revision'] == 0

def test_database_save_releases_all_locks_on_revision_conflict(tmp_path):
    import portalocker
    config = service(tmp_path)
    config.save(form(config))
    roots = [tmp_path / name for name in ('holdings', 'etf', 'market')]
    for root in roots: root.mkdir()
    @contextmanager
    def connect():
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE manual_runs(status TEXT)')
        try: yield db
        finally: db.close()
    order = []
    def acquire_market():
        order.append('market')
        lock = portalocker.Lock(str(roots[2] / 'collection.lock'), timeout=0)
        lock.acquire()
        return lock
    class CacheLock:
        def release(self): order.append('cache_release')
    def acquire_cache():
        order.append('cache')
        newer = form(config)
        newer['schedules']['margin']['times'] = ['09:10']
        config.save(newer)
        return CacheLock()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(accounts=SimpleNamespace(settings=config.settings), personal_holdings=SimpleNamespace(root=roots[0]), cache=SimpleNamespace(acquire=acquire_cache), etf_collection=SimpleNamespace(root=roots[1], connect=connect), market_collection=SimpleNamespace(_acquire=acquire_market, connect=connect))))
    body = form(config)
    body['database']['password'] = 'would-be-dirty-secret'
    with pytest.raises(SystemConfigurationError, match='更新'): save_with_locks(request, body)
    assert config.load()['revision'] == 2 and config.load()['database']['password'] == 'initial-secret'
    assert order == ['cache', 'market', 'cache_release']
    for root, name in zip(roots, ('configuration.lock','collection.lock','collection.lock')):
        with portalocker.Lock(str(root / name), timeout=0): pass

def test_unpersisted_holdings_system_reference_keeps_migrated_table(tmp_path):
    config = service(tmp_path)
    root = tmp_path / 'personal-holdings/data'
    root.mkdir(parents=True)
    state = dict(enabled=True, source=dict(id='system:abc:positions', revision=0), system_table='positions')
    with sqlite3.connect(root / 'monitor.sqlite3') as db:
        db.execute('CREATE TABLE holdings_state(id INTEGER,payload TEXT)')
        db.execute('INSERT INTO holdings_state VALUES(1,?)', (json.dumps(state),))
    for _ in range(2):
        assert task_config(config.settings, 'holdings')['table'] == 'positions'
        assert task_config(config.settings, 'holdings')['enabled']
    assert not config.path.exists()

def test_initial_market_disabled_preference_survives_migration(tmp_path):
    from cryptography.fernet import Fernet
    config = service(tmp_path)
    root = tmp_path / 'market-data/market-collection'
    root.mkdir(parents=True)
    key_dir = root / 'short-validation-secrets'
    key_dir.mkdir(mode=0o700)
    key = Fernet.generate_key()
    key_path = key_dir / 'source.key'
    key_path.write_bytes(key); key_path.chmod(0o600)
    raw = Fernet(key).encrypt(json.dumps(dict(revision=4, enabled=False, schedule=['18:30'])).encode())
    with sqlite3.connect(root / 'state.sqlite') as db:
        db.execute('CREATE TABLE configuration(id INTEGER,revision INTEGER,ciphertext BLOB)')
        db.execute('INSERT INTO configuration VALUES(1,4,?)', (raw,))
    migrated = task_config(config.settings, 'market')
    assert not migrated['enabled'] and migrated['times'] == ['00:01']

def test_configured_times_and_windows(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.services.system_configuration import latest_task_slot, window_open, next_window_time
    config = service(tmp_path)
    body = form(config)
    body['schedules']['commodities']['times'] = ['10:15', '19:20']
    body['schedules']['social'].update(enabled=True, start='08:30', end='22:10')
    config.save(body)
    moment = datetime(2026, 11, 14, 19, 30, tzinfo=ZoneInfo('Asia/Shanghai'))
    assert latest_task_slot(config.settings, 'commodities', moment).strftime('%H:%M') == '19:20'
    assert window_open(config.settings, 'social', moment)
    assert not window_open(config.settings, 'social', moment.replace(hour=23))
    following = datetime.fromtimestamp(next_window_time(config.settings, 'social', moment.replace(hour=23)), moment.tzinfo)
    assert following.strftime('%Y-%m-%d %H:%M') == '2026-11-15 08:30'
    assert not window_open(config.settings, 'social_cubes', moment, weekdays=True)

def test_valuation_tick_uses_saved_time_and_can_enable_without_restart(tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.services.valuation_ruler import ValuationRuler
    config = service(tmp_path); config.settings.valuation_ruler_worker_enabled = False
    worker = ValuationRuler(config.settings); calls = []
    monkeypatch.setattr(worker, 'refresh', lambda **kw: calls.append(kw))
    moment = datetime(2026, 11, 12, 21, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
    worker.tick(moment); assert calls == []
    body = form(config); body['schedules']['valuation_ruler'].update(enabled=True, times=['21:00']); config.save(body)
    worker.tick(moment); assert calls[0]['slot'] == '2026-11-12T21:00:00+08:00'

def test_margin_tick_uses_saved_time(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from app.services.margin_guarantee import MarginGuarantee
    config = service(tmp_path); body = form(config)
    body['schedules']['margin'].update(enabled=True, times=['10:15']); config.save(body)
    worker = MarginGuarantee(config.settings); calls = []
    monkeypatch.setattr(worker, 'refresh', lambda **kw: calls.append(kw))
    moment = datetime(2026, 11, 12, 10, 15, tzinfo=ZoneInfo('Asia/Shanghai'))
    worker.tick(moment - timedelta(seconds=1)); worker.tick(moment)
    assert calls[0]['slot'] == '2026-11-12T10:15:00+08:00'

def test_social_runtime_uses_central_settings(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.services.social import SocialMonitor
    config = service(tmp_path); worker = SocialMonitor(config.settings)
    body = form(config); body['schedules']['social'].update(enabled=True, start='08:30', end='20:00', min_interval_minutes=3, max_interval_minutes=5); config.save(body)
    with worker.db() as db:
        assert worker.config(db)['enabled'] and worker.config(db)['interval_min'] == 180
    moment = datetime(2026, 11, 12, 8, 20, tzinfo=ZoneInfo('Asia/Shanghai'))
    assert worker.night_time(moment) and not worker.night_time(moment.replace(minute=30))
