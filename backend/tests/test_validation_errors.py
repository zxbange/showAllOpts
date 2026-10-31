import json
import logging
import sqlite3
import pytest
from pymysql.err import OperationalError as MysqlError
from sqlalchemy.exc import OperationalError
from app.core.logging import configure_logging
from app.services import short_validation as module
from app.services.validation_errors import diagnostic
from test_short_validation import setup, finish

SECRET = 'PRIVATE-password-url-parameters-results'
def failure(code=2013):
    return OperationalError('SELECT ' + SECRET, {'password': SECRET}, MysqlError(code, SECRET))

@pytest.mark.parametrize('code', [1045, 1146, 2003, 2013, 2026, 9999])
def test_mysql_code_and_safe_chain(code):
    try:
        raise failure(code)
    except Exception as exc:
        result = diagnostic(exc, 'ultra_research_bar')
    assert result['code'] == f'MYSQL_{code}'
    assert 'Traceback' in result['trace']
    assert SECRET not in json.dumps(result)

@pytest.mark.parametrize('phase', ['source_plan', 'compute', 'local_store'])
def test_unknown_exceptions_never_echo_messages(phase):
    for error in (ValueError(SECRET), ValueError(2013), RuntimeError(SECRET), OperationalError(SECRET, {'data': SECRET}, Exception(SECRET))):
        result = diagnostic(error, phase)
        assert result['phase'] == phase
        assert SECRET not in json.dumps(result)
        assert result['code'] == 'unknown'

def test_query_error_files_and_retry(setup, monkeypatch):
    jobs, settings, _, calendar = setup
    job = jobs.create(['oversold'], calendar[170].date(), calendar[190].date())
    jobs.tick()
    configure_logging(settings)
    original = module.pd.read_sql_query
    def broken(*args, **kwargs):
        raise failure()
    monkeypatch.setattr(module.pd, 'read_sql_query', broken)
    try:
        jobs.tick()
        failed = jobs.get(job['id'])
        assert failed['status'] == 'failed' and failed['done'] == 0
        assert failed['error_code'] == 'MYSQL_2013'
        assert failed['error_phase'] == 'ultra_research_bar'
        assert '读取源库行情' in failed['error'] and '断点保留' in failed['error']
        assert SECRET not in json.dumps(failed)
        for filename in ('app.log', 'error.log'):
            content = (settings.log_path / filename).read_text()
            assert job['id'] in content and 'phase=ultra_research_bar' in content
            assert 'MYSQL_2013' in content and 'Traceback' in content
            assert SECRET not in content and 'SELECT ' not in content
        monkeypatch.setattr(module.pd, 'read_sql_query', original)
        jobs.action(job['id'], 'retry')
        assert 'error_code' not in jobs.get(job['id'])
        assert finish(jobs, job['id'])['status'] == 'completed'
    finally:
        logging.shutdown()

def test_schema_and_compute_phases(setup, monkeypatch):
    jobs, _, _, calendar = setup
    job = jobs.create(['oversold'], calendar[170].date(), calendar[190].date())
    original = module.stats.validate_schema
    monkeypatch.setattr(module.stats, 'validate_schema', lambda *a: (_ for _ in ()).throw(failure(1146)))
    jobs.tick()
    assert jobs.get(job['id'])['error_phase'] == 'source_schema'
    monkeypatch.setattr(module.stats, 'validate_schema', original)
    jobs.action(job['id'], 'retry'); jobs.tick()
    monkeypatch.setattr(module.stats, 'stock_samples', lambda *a, **kw: (_ for _ in ()).throw(ValueError(SECRET)))
    jobs.tick()
    result = jobs.get(job['id'])
    assert result['error_phase'] == 'compute'
    assert SECRET not in result['error'] and result['done'] == 0

def test_local_commit_error_rolls_back_and_retries(setup, monkeypatch):
    jobs, _, _, calendar = setup
    job = jobs.create(['oversold'], calendar[170].date(), calendar[190].date())
    jobs.tick()
    original = jobs.save
    def broken(db, current):
        if current.get('done') == 1:
            db.execute('SELECT * FROM nonexistent_local_table')
        return original(db, current)
    monkeypatch.setattr(jobs, 'save', broken)
    jobs.tick()
    result = jobs.get(job['id'])
    assert result['error_phase'] == 'local_store'
    assert result['error_code'] == 'SQLITE_1' and result['done'] == 0
    with jobs.db() as db:
        assert db.execute('SELECT count(*) FROM outcomes').fetchone()[0] == 0
    monkeypatch.setattr(jobs, 'save', original)
    jobs.action(job['id'], 'retry')
    assert finish(jobs, job['id'])['done'] == 1

def test_status_write_failure_logs_both_errors(setup, monkeypatch, caplog):
    jobs, _, _, calendar = setup
    job = jobs.create(['oversold'], calendar[170].date(), calendar[190].date())
    original = jobs.save
    def broken(db, current):
        if current.get('status') == 'failed':
            db.execute('SELECT * FROM nonexistent_local_table')
        return original(db, current)
    monkeypatch.setattr(jobs, 'save', broken)
    monkeypatch.setattr(module.stats, 'validate_schema', lambda *a: (_ for _ in ()).throw(failure(1045)))
    with caplog.at_level(logging.ERROR), pytest.raises(sqlite3.OperationalError):
        jobs.tick()
    records = [r.getMessage() for r in caplog.records if 'Validation failure' in r.getMessage()]
    assert any(job['id'] in line and 'MYSQL_1045' in line for line in records)
    assert any(job['id'] in line and 'phase=local_store' in line for line in records)
    assert all(SECRET not in line and 'nonexistent_local_table' not in line for line in records)
    assert jobs.get(job['id'])['done'] == 0

def test_worker_loop_survives(setup, monkeypatch, caplog):
    jobs, _, _, _ = setup
    def broken():
        jobs.stop_event.set()
        raise sqlite3.OperationalError(SECRET)
    monkeypatch.setattr(jobs, 'tick', broken)
    with caplog.at_level(logging.ERROR):
        jobs.loop()
    assert 'Validation failure job=unassigned' in caplog.text
    assert SECRET not in caplog.text

@pytest.mark.parametrize('phase', ['connect', 'ultra_research_universe'])
def test_timed_phase_diagnosis(setup, phase):
    jobs, _, _, _ = setup
    job = {'id': 'a' * 32}
    try:
        with jobs.timed(job, phase):
            raise failure(2003)
    except Exception as exc:
        result = diagnostic(exc, 'worker')
    assert result['phase'] == phase and result['code'] == 'MYSQL_2003'


def test_local_open_error_has_local_code(setup, monkeypatch):
    jobs, _, _, _ = setup
    original = sqlite3.connect
    def broken(*args, **kwargs):
        return original('/nonexistent-validation-directory/db.sqlite3')
    monkeypatch.setattr(module.sqlite3, 'connect', broken)
    with pytest.raises(sqlite3.OperationalError) as caught:
        with jobs.db():
            pass
    result = diagnostic(caught.value, 'source_plan')
    assert result['phase'] == 'local_store' and result['code'] == 'SQLITE_14'
    assert '/nonexistent' not in json.dumps(result)


def test_safe_validation_reason_and_new_task_advice(caplog):
    from app.services.validation_errors import report
    error = ValueError('任务算法版本已变化，请创建新任务')
    result = report(logging.getLogger('app.validation-test'), error, {'id': 'a'*32}, 'source_config')
    assert '请创建新任务' in result['message']
    assert '点击重试' not in result['message']
