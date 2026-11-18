import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.services.collection_outcomes import (
    OutcomeUnavailable, database, outcome_history, record_outcome,
)


def test_latest_five_keep_failures_unknown_and_original_history(tmp_path):
    settings = SimpleNamespace(log_path=tmp_path)
    for index, result in enumerate(('success', 'failed', 'success', 'cancelled', 'unknown', 'failed', 'success')):
        assert record_outcome(settings, 'etf-shares', run_key=f'run-{index}', outcome=result,
                              trigger='manual' if index % 2 else 'scheduled',
                              finished_at=f'2026-11-18T10:00:0{index}+08:00')
    before = (tmp_path / 'collection-outcomes' / 'outcomes.sqlite3').read_bytes()
    rows = outcome_history(settings, 'etf-shares')['items']
    assert [row['run_key'] for row in rows] == ['run-6', 'run-5', 'run-4', 'run-3', 'run-2']
    assert [row['outcome'] for row in rows] == ['success', 'failed', 'unknown', 'cancelled', 'success']
    assert rows[1]['trigger'] == 'manual' and rows[0]['kind'] == 'collection'
    assert before == (tmp_path / 'collection-outcomes' / 'outcomes.sqlite3').read_bytes()
    with database(settings) as db:
        assert db.execute('SELECT count(*) FROM results').fetchone()[0] == 7


def test_order_uses_normalized_completion_not_insertion_or_success(tmp_path):
    settings = SimpleNamespace(log_path=tmp_path)
    for run, stamp, result in [('new-failure', '2026-11-18T10:00:00+08:00', 'failed'),
                               ('late-old-success', '2026-11-18T01:00:00+00:00', 'success'),
                               ('same-time-manual', '2026-11-18T02:00:00+00:00', 'success')]:
        record_outcome(settings, 'industry-momentum', run_key=run, outcome=result,
                       trigger='manual', finished_at=stamp)
    record_outcome(settings, 'etf-shares', run_key='other-module', outcome='failed', trigger='manual')
    rows = outcome_history(settings, 'industry-momentum', 2)['items']
    assert [row['run_key'] for row in rows] == ['same-time-manual', 'new-failure']
    assert all(row['kind'] == 'database_sync' for row in rows)
    assert not record_outcome(settings, 'industry-momentum', run_key='new-failure', outcome='success', trigger='scheduled')
    assert len(outcome_history(settings, 'industry-momentum')['items']) == 3


def test_no_history_does_not_create_ledger_or_invent_legacy_record(tmp_path):
    settings = SimpleNamespace(log_path=tmp_path)
    root = tmp_path / 'market-data' / 'commodities'
    root.mkdir(parents=True)
    with sqlite3.connect(root / 'history.sqlite3') as db:
        db.execute('CREATE TABLE state(id INTEGER PRIMARY KEY,payload TEXT)')
        db.execute('INSERT INTO state VALUES(1,?)', (json.dumps(dict(outcome='completed', checkedAt='2026-11-18T10:00:00+08:00')),))
    assert outcome_history(settings, 'commodities-a00') == {'items': []}
    assert not (tmp_path / 'collection-outcomes').exists()


@pytest.mark.parametrize('module,limit', [('invented', 5), ("etf-shares' OR 1=1 --", 5), ('etf-shares', 0), ('etf-shares', 6), ('etf-shares', True)])
def test_invalid_history_requests_do_not_access_storage(tmp_path, module, limit):
    with pytest.raises(ValueError):
        outcome_history(SimpleNamespace(log_path=tmp_path), module, limit)
    assert not list(tmp_path.iterdir())


def test_corrupt_history_is_unavailable_not_fake_empty_success(tmp_path):
    settings = SimpleNamespace(log_path=tmp_path)
    root = tmp_path / 'collection-outcomes'
    root.mkdir()
    (root / 'outcomes.sqlite3').write_text('damaged')
    with pytest.raises(OutcomeUnavailable):
        outcome_history(settings, 'etf-shares')


def test_history_api_auth_contract_validation_and_failure(client):
    url = '/api/v1/collection-outcomes/history'
    assert client.get(url).status_code == 422
    for query in ('module=bad', 'module=etf-shares&limit=0', 'module=etf-shares&limit=6'):
        assert client.get(url + '?' + query).status_code == 422
    settings = client.app.state.accounts.settings
    record_outcome(settings, 'etf-shares', run_key='terminal-failure', outcome='failed', trigger='manual')
    response = client.get(url + '?module=etf-shares')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert response.json()['items'][0]['run_key'] == 'terminal-failure'
    assert response.json()['items'][0]['outcome'] == 'failed'
    (settings.log_path / 'collection-outcomes' / 'outcomes.sqlite3').write_text('damaged')
    assert client.get(url + '?module=etf-shares').status_code == 503
    client.cookies.clear()
    assert client.get(url + '?module=etf-shares').status_code == 401
