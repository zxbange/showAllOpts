from types import SimpleNamespace
from app.services.collection_outcomes import record_outcome, latest_outcomes, database, record_task


def test_completion_order_not_success_preference_and_idempotent(tmp_path):
    settings = SimpleNamespace(log_path=tmp_path)
    assert latest_outcomes(settings)['items']['etf-shares']['outcome'] == 'unknown'
    for run, result, stamp in [('old', 'success', '2026-11-18T10:00:00+08:00'),
                               ('new', 'failed', '2026-11-18T11:00:00+08:00'),
                               ('late-old', 'success', '2026-11-18T09:00:00+08:00')]:
        assert record_outcome(settings, 'etf-shares', run_key=run, outcome=result, trigger='manual', finished_at=stamp)
    assert latest_outcomes(settings)['items']['etf-shares']['outcome'] == 'failed'
    assert not record_outcome(settings, 'etf-shares', run_key='new', outcome='success', trigger='manual')
    assert not record_outcome(settings, 'etf-shares', run_key='running', outcome='running', trigger='manual')
    assert latest_outcomes(settings)['items']['etf-shares']['outcome'] == 'failed'
    assert record_outcome(settings, 'etf-shares', run_key='retry', outcome='complete', trigger='scheduled', finished_at='2026-11-18T12:00:00+08:00')
    assert latest_outcomes(settings)['items']['etf-shares']['outcome'] == 'success'
    with database(settings) as db:
        assert db.execute('select count(*) from results').fetchone()[0] == 4


def test_invalid_or_unknown_time_cannot_manufacture_success(tmp_path):
    settings = SimpleNamespace(log_path=tmp_path)
    assert not record_outcome(settings, 'etf-shares', run_key='x', outcome='success', trigger='manual', finished_at='2026-11-18T10:00:00')
    assert not record_outcome(settings, 'made-up', run_key='x', outcome='success', trigger='manual')
    assert latest_outcomes(settings)['items']['etf-shares']['outcome'] == 'unknown'


def test_storage_failure_is_safe_and_sync_kind_explicit(tmp_path):
    (tmp_path / 'collection-outcomes').write_text('blocked')
    settings = SimpleNamespace(log_path=tmp_path)
    assert not record_outcome(settings, 'etf-shares', run_key='x', outcome='failed', trigger='manual')
    assert latest_outcomes(settings)['items']['industry-momentum']['kind'] == 'database_sync'


def test_task_aliases_and_all_cash_cow_periods(tmp_path):
    settings = SimpleNamespace(log_path=tmp_path)
    record_task(settings, 'cash_cow', outcome='success', scheduled=False, run_key='one')
    items = latest_outcomes(settings)['items']
    assert all(items[key]['outcome'] == 'success' for key in ('cash-cow', 'cash-cow-3', 'cash-cow-5', 'cash-cow-10'))
    record_task(settings, 'cash_cow', outcome='failed', scheduled=True, run_key='two')
    assert latest_outcomes(settings)['items']['cash-cow']['outcome'] == 'failed'


def test_native_newer_failure_cannot_be_hidden_by_older_ledger_success(tmp_path):
    import sqlite3, json
    settings = SimpleNamespace(log_path=tmp_path)
    root = tmp_path / 'market-data' / 'commodities'
    root.mkdir(parents=True)
    with sqlite3.connect(root / 'history.sqlite3') as db:
        db.execute('CREATE TABLE state(id INTEGER PRIMARY KEY,payload TEXT)')
        db.execute('INSERT INTO state VALUES (1,?)', (json.dumps(dict(outcome='failed',checkedAt='2026-11-18T11:00:00+08:00',error='source unavailable')),))
    record_outcome(settings, 'commodities-a00', run_key='old', outcome='success', trigger='manual',finished_at='2026-11-18T10:00:00+08:00')
    assert latest_outcomes(settings)['items']['commodities-a00']['outcome'] == 'failed'


def test_old_last_success_is_not_a_finished_latest_failure(tmp_path):
    import sqlite3, json
    settings = SimpleNamespace(log_path=tmp_path)
    root = tmp_path / 'market-data' / 'dividend-collection'
    root.mkdir(parents=True)
    with sqlite3.connect(root / 'state.sqlite3') as db:
        db.execute('CREATE TABLE state(id INTEGER PRIMARY KEY,payload TEXT)')
        db.execute('INSERT INTO state VALUES (1,?)', (json.dumps(dict(stage='failed',last_success_at='2026-11-18T10:00:00+08:00',error='new failure')),))
    assert latest_outcomes(settings)['items']['dividend-selection']['outcome'] == 'unknown'


def test_corrupt_ledger_cannot_fall_back_to_stale_native_success(tmp_path):
    import pytest, sqlite3, json
    from app.services.collection_outcomes import OutcomeUnavailable
    settings = SimpleNamespace(log_path=tmp_path)
    root = tmp_path / 'market-data' / 'commodities'
    root.mkdir(parents=True)
    with sqlite3.connect(root / 'history.sqlite3') as db:
        db.execute('CREATE TABLE state(id INTEGER PRIMARY KEY,payload TEXT)')
        db.execute('INSERT INTO state VALUES (1,?)', (json.dumps(dict(outcome='completed',checkedAt='2026-11-18T11:00:00+08:00')),))
    ledger = tmp_path / 'collection-outcomes'
    ledger.mkdir()
    (ledger / 'outcomes.sqlite3').write_text('damaged')
    with pytest.raises(OutcomeUnavailable):
        latest_outcomes(settings)


def test_api_authenticated_no_store_and_unavailable_is_not_stale_green(client):
    settings = client.app.state.accounts.settings
    response = client.get('/api/v1/collection-outcomes')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert response.json()['items']['etf-shares']['outcome'] == 'unknown'
    record_outcome(settings, 'etf-shares', run_key='failed', outcome='failed', trigger='manual')
    assert client.get('/api/v1/collection-outcomes').json()['items']['etf-shares']['outcome'] == 'failed'
    (settings.log_path / 'collection-outcomes' / 'outcomes.sqlite3').write_text('damaged')
    assert client.get('/api/v1/collection-outcomes').status_code == 503
    client.cookies.clear()
    assert client.get('/api/v1/collection-outcomes').status_code == 401


def test_new_native_failure_without_finish_clock_masks_stale_ledger_success(tmp_path):
    import sqlite3, json
    settings = SimpleNamespace(log_path=tmp_path)
    root = tmp_path / 'market-data' / 'dividend-collection'
    root.mkdir(parents=True)
    with sqlite3.connect(root / 'state.sqlite3') as db:
        db.execute('CREATE TABLE state(id INTEGER PRIMARY KEY,payload TEXT)')
        db.execute('INSERT INTO state VALUES (1,?)', (json.dumps(dict(stage='failed',run_id='new',last_success_at='2026-11-18T10:00:00+08:00',error='new failure')),))
    record_outcome(settings, 'dividend-selection', run_key='old', outcome='success', trigger='manual')
    assert latest_outcomes(settings)['items']['dividend-selection']['outcome'] == 'unknown'
    record_outcome(settings, 'dividend-selection', run_key='new', outcome='failed', trigger='manual')
    assert latest_outcomes(settings)['items']['dividend-selection']['outcome'] == 'failed'
