from types import SimpleNamespace
import json
import sqlite3
import pytest
from app.services.collection_errors import safe_error, sanitize_error
from app.services.collection_outcomes import record_outcome, outcome_history, database
from app.services.collection_retention import retain_history, restore_history


def seed(tmp_path):
    settings = SimpleNamespace(log_path=tmp_path)
    for i in range(8):
        record_outcome(settings, 'etf-shares', run_key=str(i), outcome='failed' if i % 2 else 'success',
                       trigger='manual', finished_at=f'2026-11-18T00:00:0{i}+00:00',
                       error_details=safe_error(TimeoutError('password=secret'), stage='fetch'))
    return settings


def test_default_preserves_all_and_dry_run_does_not_write(tmp_path):
    settings = seed(tmp_path)
    path = tmp_path/'collection-outcomes/outcomes.sqlite3'
    before = path.read_bytes()
    assert retain_history(settings) == dict(candidates=3, archived=0, dry_run=True)
    assert before == path.read_bytes()
    with pytest.raises(ValueError):
        retain_history(settings, apply=True)
    with database(settings) as db:
        assert db.execute('SELECT count(*) FROM results').fetchone()[0] == 8


def test_archive_restore_keeps_content_and_order_and_never_touches_business(tmp_path):
    settings = seed(tmp_path)
    with database(settings) as db:
        db.execute('CREATE TABLE business (payload TEXT)')
        db.execute("INSERT INTO business VALUES('do-not-delete')")
        original = db.execute('SELECT module,run_key,outcome,finished_at,trigger FROM results ORDER BY rowid').fetchall()
    assert retain_history(settings, apply=True, confirmed=True)['archived'] == 3
    with database(settings) as db:
        assert db.execute('SELECT count(*) FROM results').fetchone()[0] == 5
        assert db.execute('SELECT count(*) FROM results_archive').fetchone()[0] == 3
        assert db.execute('SELECT payload FROM business').fetchone()[0] == 'do-not-delete'
    assert restore_history(settings, apply=True, confirmed=True)['restored'] == 3
    with database(settings) as db:
        restored = db.execute('SELECT module,run_key,outcome,finished_at,trigger FROM results ORDER BY rowid').fetchall()
        assert [tuple(r) for r in original] == [tuple(r) for r in restored]
        assert json.loads(db.execute("SELECT payload FROM result_details WHERE run_key='1'").fetchone()[0])['code'] == 'timeout'
    assert restore_history(settings, apply=True, confirmed=True)['restored'] == 0
    assert retain_history(settings, apply=True, confirmed=True)['archived'] == 3


def test_enabled_retention_archives_only_after_successful_record(tmp_path):
    settings = seed(tmp_path)
    settings.collection_history_retention_enabled = True
    record_outcome(settings, 'etf-shares', run_key='new', outcome='failed', trigger='scheduled')
    with database(settings) as db:
        assert db.execute('SELECT count(*) FROM results').fetchone()[0] == 5
        assert db.execute('SELECT count(*) FROM results_archive').fetchone()[0] == 4
    with pytest.raises(ValueError):
        restore_history(settings, apply=True, confirmed=True)


def test_error_allowlist_drops_secrets_and_survives_history_api(tmp_path):
    settings = seed(tmp_path)
    row = outcome_history(settings, 'etf-shares')['items'][0]
    assert row['error_details']['code'] == 'timeout'
    assert 'secret' not in json.dumps(row)
    detail = sanitize_error(dict(code='database_error', message='password=secret', stage='url-secret',
                                 sql='select secret', mysql_errno=2013, sqlite_errno='secret',
                                 start_date='2026-11-17', end_date='2026-99-99'))
    assert detail == dict(code='database_error',message='数据库操作失败，请检查数据库连接与服务状态。',mysql_errno=2013,start_date='2026-11-17')
    assert safe_error(ValueError('sql secret'), stage='validate')['code'] == 'invalid_data'


def test_conflicting_archive_rolls_back_without_losing_active_rows(tmp_path):
    settings = seed(tmp_path)
    retain_history(settings, apply=True, confirmed=True)
    restore_history(settings, apply=True, confirmed=True)
    with database(settings) as db:
        db.execute("UPDATE results_archive SET outcome='invalid' WHERE run_key='1'")
    with pytest.raises(ValueError, match='verification'):
        retain_history(settings, apply=True, confirmed=True)
    with database(settings) as db:
        assert db.execute('SELECT count(*) FROM results').fetchone()[0] == 8
        assert db.execute("SELECT count(*) FROM result_details WHERE run_key='1'").fetchone()[0] == 1


def test_safe_error_http_numeric_status_without_request_url_or_body():
    import httpx
    request = httpx.Request('GET', 'https://example.com/private?token=secret')
    response = httpx.Response(429, request=request, text='secret body')
    details = safe_error(httpx.HTTPStatusError('secret error', request=request, response=response), stage='fetch')
    assert details['code'] == 'network_error' and details['http_status'] == 429
    assert 'secret' not in json.dumps(details)
    assert 'http_status' not in sanitize_error(dict(code='unknown', http_status=700))


def test_archived_run_key_cannot_be_replayed_as_new_completion(tmp_path):
    settings = seed(tmp_path)
    retain_history(settings, apply=True, confirmed=True)
    before = outcome_history(settings, 'etf-shares')
    assert not record_outcome(settings, 'etf-shares', run_key='0', outcome='failed', trigger='manual',
                              error_details=safe_error(ValueError('different')))
    assert outcome_history(settings, 'etf-shares') == before
    with database(settings) as db:
        assert db.execute("SELECT outcome,details FROM results_archive WHERE run_key='0'").fetchone()[:] == ('success', None)
    for _ in range(3):
        assert restore_history(settings, apply=True, confirmed=True)['restored'] == 3
        assert retain_history(settings, apply=True, confirmed=True)['archived'] == 3
    assert outcome_history(settings, 'etf-shares') == before


def test_cli_previews_both_ledgers_without_writes(tmp_path, monkeypatch, capsys):
    import importlib.util
    import sys
    from pathlib import Path
    settings = seed(tmp_path)
    root = tmp_path/'market-data/etf-collection'
    root.mkdir(parents=True)
    native = root/'state.sqlite'
    with sqlite3.connect(native) as db:
        db.execute('CREATE TABLE run_history(id TEXT PRIMARY KEY, status TEXT, finished_at TEXT, requested_at TEXT)')
        db.executemany('INSERT INTO run_history VALUES(?,?,?,?)',
                       [(str(i), 'failed', f'2026-11-18T00:00:0{i}+00:00', None) for i in range(8)])
    paths = [native, tmp_path/'collection-outcomes/outcomes.sqlite3']
    before = [path.read_bytes() for path in paths]
    source = Path(__file__).resolve().parents[2]/'scripts/prune_collection_history.py'
    spec = importlib.util.spec_from_file_location('history_retention_cli_test', source)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, 'Settings', lambda: settings)
    monkeypatch.setattr(sys, 'argv', ['prune_collection_history'])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result['unified']['candidates'] == result['etf_native']['candidates'] == 3
    monkeypatch.setattr(sys, 'argv', ['prune_collection_history', '--restore'])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result['unified']['restored'] == result['etf_native']['restored'] == 0
    assert before == [path.read_bytes() for path in paths]
    monkeypatch.setattr(sys, 'argv', ['prune_collection_history', '--permanent'])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result['unified']['active_candidates'] == result['etf_native']['candidates'] == 3
    assert before == [path.read_bytes() for path in paths]
    monkeypatch.setattr(sys, 'argv', ['prune_collection_history', '--permanent', '--apply', '--confirm-archive-history'])
    with pytest.raises(SystemExit) as denied:
        cli.main()
    assert denied.value.code == 2
    assert before == [path.read_bytes() for path in paths]
    monkeypatch.setattr(sys, 'argv', ['prune_collection_history', '--help'])
    with pytest.raises(SystemExit) as done:
        cli.main()
    assert done.value.code == 0
    assert before == [path.read_bytes() for path in paths]


def test_permanent_purge_preview_confirmation_exact_union_keep_and_no_resurrection(tmp_path):
    from app.services.collection_retention import purge_history
    settings = seed(tmp_path)
    retain_history(settings, apply=True, confirmed=True)
    # An unusual newer archived completion must be protected by union ordering.
    with database(settings) as db:
        db.execute("UPDATE results_archive SET finished_at='2026-11-19T00:00:00+00:00' WHERE run_key='1'")
        db.execute('CREATE TABLE operational_slots (value TEXT)')
        db.execute("INSERT INTO operational_slots VALUES('keep-me')")
    path = tmp_path/'collection-outcomes/outcomes.sqlite3'
    before = path.read_bytes()
    preview = purge_history(settings)
    assert preview['active_candidates'] == 1 and preview['archive_candidates'] == 2
    assert path.read_bytes() == before
    with pytest.raises(ValueError):
        purge_history(settings, apply=True)
    applied = purge_history(settings, apply=True, confirmed=True)
    assert applied['deleted_active'] == 1 and applied['deleted_archive'] == 2
    with database(settings) as db:
        assert {r[0] for r in db.execute('SELECT run_key FROM results UNION SELECT run_key FROM results_archive')} == {'1','4','5','6','7'}
        assert db.execute('SELECT value FROM operational_slots').fetchone()[0] == 'keep-me'
        assert [r[1] for r in db.execute('PRAGMA table_info(result_tombstones)')] == ['module','run_key']
        assert db.execute('SELECT count(*) FROM result_tombstones').fetchone()[0] == 3
    assert not record_outcome(settings, 'etf-shares', run_key='0', outcome='success', trigger='manual')
    assert purge_history(settings, apply=True, confirmed=True)['deleted_active'] == 0
    assert restore_history(settings, apply=True, confirmed=True)['restored'] == 1
    with database(settings) as db:
        assert db.execute("SELECT count(*) FROM results WHERE run_key IN ('0','2','3')").fetchone()[0] == 0


def test_permanent_purge_is_per_module_and_preserves_active_archive_duplicates(tmp_path):
    from app.services.collection_retention import purge_history
    settings = seed(tmp_path)
    retain_history(settings, apply=True, confirmed=True)
    restore_history(settings, apply=True, confirmed=True)
    for i in range(6):
        record_outcome(settings, 'social-weibo', run_key=str(i), outcome='failed', trigger='manual',
                       finished_at=f'2026-11-18T01:00:0{i}+00:00')
    purge_history(settings, apply=True, confirmed=True)
    with database(settings) as db:
        counts = dict(db.execute('SELECT module,count(*) FROM results GROUP BY module'))
        assert counts == {'etf-shares':5, 'social-weibo':5}
        assert db.execute('SELECT count(*) FROM results_archive').fetchone()[0] == 0
