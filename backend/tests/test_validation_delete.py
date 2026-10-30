from concurrent.futures import ThreadPoolExecutor
from datetime import date
import multiprocessing
import sqlite3
import threading

import portalocker
import pytest
from sqlalchemy import event

from app.services.short_validation import ValidationJobs, DeleteConflict
from test_short_validation import setup, create, finish
from test_validation_pairing import paired, login, BASE
from test_validation_sources import secure, config, auth


def rows(jobs):
    with jobs.db() as db:
        return {table: db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
                for table in ('jobs', 'stocks', 'outcomes', 'windows')}


def test_delete_all_owned_rows_preserves_other_job_source_and_shared_files(setup):
    jobs, settings, engine, calendar = setup
    first = create(jobs, calendar)
    assert finish(jobs, first['id'])['status'] == 'completed'
    second = jobs.create(['oversold'], calendar[170].date(), calendar[190].date(), calendar[195].date(), 'second')
    assert finish(jobs, second['id'])['status'] == 'completed'
    before = rows(jobs)
    assert all(before.values())
    files = {p.name: p.stat().st_ino for p in jobs.root.iterdir()}
    statements = []
    event.listen(engine, 'before_cursor_execute', lambda conn, cursor, sql, *args: statements.append(sql))
    jobs.delete(first['id'])
    after = rows(jobs)
    for table in before:
        assert after[table] == [row for row in before[table] if row[0] != first['id']]
    assert not statements  # Deletion never opens the source engine.
    assert {p.name: p.stat().st_ino for p in jobs.root.iterdir()} == files
    restarted = ValidationJobs(settings, engine)
    assert [job['id'] for job in restarted.list()] == [second['id']]
    assert not restarted.tick()
    restarted.delete(first['id'])  # Lost response / repeated request is harmless.
    assert rows(restarted) == after


@pytest.mark.parametrize('status', ['queued', 'paused', 'failed', 'completed', 'cancelled'])
def test_deletable_states_require_no_lock(setup, status):
    jobs, _, _, calendar = setup
    job = create(jobs, calendar)
    with jobs.db() as db:
        job.update(status=status, cancel_requested_at='test' if status == 'cancelled' else None)
        jobs.save(db, job)
    with portalocker.Lock(str(jobs.root / 'worker.lock'), timeout=0):
        with pytest.raises(DeleteConflict):
            jobs.delete(job['id'])
    assert jobs.get(job['id'])['status'] == status
    jobs.delete(job['id'])
    assert not jobs.list()


@pytest.mark.parametrize('status', ['running', 'cancelling', 'unknown'])
def test_running_must_cancel_even_without_worker(setup, status):
    jobs, _, _, calendar = setup
    job = create(jobs, calendar)
    with jobs.db() as db:
        job['status'] = status
        jobs.save(db, job)
    with pytest.raises(DeleteConflict):
        jobs.delete(job['id'])
    assert jobs.get(job['id'])['status'] == status


def hold_lock(path, ready, release):
    with portalocker.Lock(path, timeout=0):
        ready.set()
        release.wait(10)


def test_cross_process_lock(setup):
    jobs, _, _, calendar = setup
    job = create(jobs, calendar)
    ctx = multiprocessing.get_context('spawn')
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=hold_lock, args=(str(jobs.root / 'worker.lock'), ready, release))
    process.start()
    try:
        assert ready.wait(10)
        with pytest.raises(DeleteConflict):
            jobs.delete(job['id'])
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0
    jobs.delete(job['id'])


def test_cancel_inflight_delete_waits_and_worker_cannot_revive(setup, monkeypatch):
    jobs, settings, engine, calendar = setup
    other = ValidationJobs(settings, engine)
    job = create(jobs, calendar)
    entered, release = threading.Event(), threading.Event()
    def work(snapshot, source):
        assert jobs.stage(snapshot['id'], 'query', snapshot['generation'])
        entered.set()
        assert release.wait(10)
        assert not jobs.stage(snapshot['id'], 'stale result', snapshot['generation'])
    monkeypatch.setattr(jobs, 'work', work)
    with ThreadPoolExecutor() as pool:
        worker = pool.submit(jobs.tick)
        try:
            assert entered.wait(10)
            other.action(job['id'], 'pause')
            with pytest.raises(DeleteConflict): other.delete(job['id'])
            assert other.action(job['id'], 'cancel')['status'] == 'cancelling'
            with pytest.raises(DeleteConflict): other.delete(job['id'])
        finally:
            release.set()
        worker.result(timeout=10)
    assert other.get(job['id'])['status'] == 'cancelled'
    other.delete(job['id'])
    assert not jobs.tick()
    assert all(not value for value in rows(jobs).values())


def test_delete_rollback_and_retry(setup):
    jobs, _, _, calendar = setup
    job = create(jobs, calendar)
    finish(jobs, job['id'])
    before = rows(jobs)
    with jobs.db() as db:
        db.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON jobs BEGIN SELECT RAISE(ABORT, 'private error'); END")
    with pytest.raises(sqlite3.Error): jobs.delete(job['id'])
    assert rows(jobs) == before
    with jobs.db() as db: db.execute('DROP TRIGGER fail_delete')
    jobs.delete(job['id'])
    assert all(not value for value in rows(jobs).values())


def test_concurrent_delete_retry_is_serializable(setup):
    jobs, settings, engine, calendar = setup
    job = create(jobs, calendar)
    jobs.action(job['id'], 'pause')
    other = ValidationJobs(settings, engine)
    barrier = threading.Barrier(3)
    def attempt(kind):
        barrier.wait()
        try:
            if kind == 'retry': other.action(job['id'], 'retry')
            else: other.delete(job['id'])
        except (KeyError, DeleteConflict): pass
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(attempt, ['delete', 'delete', 'retry']))
    jobs.delete(job['id'])
    assert not jobs.tick()
    assert all(not value for value in rows(jobs).values())


def test_api_auth_csrf_origin_idempotence_and_redacted_error(paired, monkeypatch):
    jobs, _, access, client = paired
    path = BASE + '/jobs/example/delete'
    assert client.post(path, json={}).status_code in (401, 403)
    login(access, client)
    for headers in ({'X-Validation-CSRF': 'wrong'}, {'Origin': 'http://evil.example'}, {'Origin': ''}):
        assert client.post(path, json={}, headers=headers).status_code == 403
    for _ in range(2):
        response = client.post(path, json={})
        assert response.status_code == 200 and response.json() == {'deleted': True}
        assert response.headers['cache-control'] == 'no-store'
    def fail(ident): raise sqlite3.OperationalError('private database password')
    monkeypatch.setattr(jobs, 'delete', fail)
    response = client.post(path, json={})
    assert response.status_code == 503 and 'private' not in response.text


def test_api_conflict_and_shared_encrypted_config_survives(secure, config):
    jobs, sources, _, client = secure
    auth(client)
    ref = sources.save(config)
    job = jobs.create(['bukou'], date(2024, 1, 1), None, None, source={'id': ref['id'], 'revision': ref['revision']})
    path = '/short-term/validation/jobs/' + job['id'] + '/delete'
    with portalocker.Lock(str(jobs.root / 'worker.lock'), timeout=0):
        assert client.post(path, json={}).status_code == 409
    assert client.post(path, json={}).status_code == 200
    assert sources.get(ref['id'], ref['revision']) == config
    assert sources.list()[0]['id'] == ref['id']
