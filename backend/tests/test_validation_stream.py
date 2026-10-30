from contextlib import contextmanager
import json
import sqlite3

import pandas as pd
import pytest
from sqlalchemy import event, text

from app.services import short_validation as module
from app.services.short_validation import ValidationJobs, STATIC_MODE
from app.services.validation_sources import ValidationSources
from test_short_validation import setup, finish
from test_validation_sources import config


def expand(engine, count):
    with engine.begin() as conn:
        for table in ('ultra_research_bar', 'ultra_research_universe'):
            original = pd.read_sql_query(text('SELECT * FROM ' + table), conn)
            for n in range(2, count + 1):
                clone = original.copy()
                clone['code'] = f'{n:06d}.XSHE'
                clone.to_sql(table, conn, if_exists='append', index=False)


def new(jobs, calendar, token='new', methods=None, mode=STATIC_MODE):
    return jobs.create(methods or ['oversold'], calendar[170].date(), calendar[190].date(),
                       calendar[195].date(), token, input_mode=mode)


def values(jobs, ident):
    with jobs.db() as db:
        return [db.execute(f'SELECT {columns} FROM {table} WHERE job=? ORDER BY code,day,strategy' +
                           (',horizon' if table == 'outcomes' else ''), (ident,)).fetchall()
                for table, columns in [('outcomes','code,day,strategy,horizon,value,status'),
                                       ('windows','code,day,strategy,best,worst,win')]]


def test_static_matches_frozen_and_single_batch_all_four_and_boundaries(setup, monkeypatch):
    jobs, _, engine, calendar = setup
    expand(engine, 3)
    # Missing target close and paused target still use the same T+n rules in both paths.
    with engine.begin() as conn:
        conn.execute(text('UPDATE ultra_research_bar SET paused=1 WHERE trade_date=:day'), {'day': calendar[193].date()})
        conn.execute(text('DELETE FROM ultra_research_bar WHERE trade_date=:day'), {'day': calendar[194].date()})
    methods = list(module.METHODS)
    # Preserve the v1 separate-observation contract: missing data during the
    # signal interval intentionally fails the three-strategy input validation.
    def legacy(token, mode=STATIC_MODE):
        job = new(jobs, calendar, token, methods, mode)
        with jobs.db() as db:
            job.update(version='four-close-v1', observed=str(calendar[195].date()))
            jobs.save(db, job)
        return job
    baseline = legacy('old', 'frozen')
    assert finish(jobs, baseline['id'])['status'] == 'completed'
    current = legacy('batch')
    assert finish(jobs, current['id'])['status'] == 'completed'
    monkeypatch.setattr(module, 'BATCH_SIZE', 1)
    single = legacy('single')
    assert finish(jobs, single['id'])['status'] == 'completed'
    for ident in (current['id'], single['id']):
        assert values(jobs, ident) == values(jobs, baseline['id'])
        assert jobs.get(ident)['summary'] == jobs.get(baseline['id'])['summary']
        assert jobs.get(ident)['windows'] == jobs.get(baseline['id'])['windows']
        with jobs.db() as db:
            assert db.execute('SELECT count(*) FROM stocks WHERE job=? AND (bars IS NOT NULL OR universe IS NOT NULL)', (ident,)).fetchone()[0] == 0


def test_batch_limits_columns_connection_reuse_and_incremental_progress(setup):
    jobs, _, engine, calendar = setup
    expand(engine, 23)
    job = new(jobs, calendar)
    jobs.tick()  # Persist immutable plan first.
    queries, checkouts = [], []
    event.listen(engine, 'before_cursor_execute', lambda conn,cursor,sql,parameters,*args: queries.append((sql, parameters)))
    event.listen(engine, 'checkout', lambda *args: checkouts.append(1))
    jobs.tick()
    current = jobs.get(job['id'])
    assert current['done'] == 20 and current['total'] == 23 and current['captured'] == 0
    assert len(queries) == 2 and len(checkouts) == 1
    assert all('SELECT *' not in q and 'LIMIT 80001' in q and len(p) == 22 for q,p in queries)
    assert {'connect','ultra_research_bar','ultra_research_universe','compute','commit'} <= current['timings'].keys()
    jobs.tick()
    assert jobs.get(job['id'])['done'] == 23
    assert len(queries) == 4 and len(checkouts) == 2
    assert all('LIMIT 12001' in q for q,_ in queries[2:])
    assert finish(jobs, job['id'])['status'] == 'completed'


@pytest.mark.parametrize('action', ['failure', 'pause', 'cancel', 'shutdown'])
def test_partial_batch_atomic_resume_and_cancel(setup, monkeypatch, action):
    jobs, settings, engine, calendar = setup
    expand(engine, 3)
    job = new(jobs, calendar)
    original = module.stats.stock_samples
    seen = []
    def interrupt(code, *args, **kwargs):
        seen.append(code)
        assert ValidationJobs(settings, engine).tick() is False
        if code == '000002.XSHE':
            if action == 'failure': raise RuntimeError('secret connection text')
            elif action == 'shutdown': jobs.stop_event.set()
            else: jobs.action(job['id'], action)
        return original(code, *args, **kwargs)
    monkeypatch.setattr(module.stats, 'stock_samples', interrupt)
    jobs.tick(); jobs.tick()
    assert jobs.get(job['id'])['done'] == 1
    assert 'secret' not in json.dumps(jobs.get(job['id']))
    with jobs.db() as db:
        assert db.execute('SELECT code FROM stocks WHERE job=?', (job['id'],)).fetchall() == [('000001.XSHE',)]
    monkeypatch.setattr(module.stats, 'stock_samples', original)
    restarted = ValidationJobs(settings, engine)
    if action == 'cancel':
        assert restarted.get(job['id'])['status'] == 'cancelled'
        assert not restarted.tick()
        return
    restarted.action(job['id'], 'retry')
    result = finish(restarted, job['id'])
    assert result['status'] == 'completed' and result['done'] == 3
    baseline = new(restarted, calendar, 'reference')
    assert finish(restarted, baseline['id'])['status'] == 'completed'
    assert values(restarted, job['id']) == values(restarted, baseline['id'])


@pytest.mark.parametrize('per_stock_only', [False, True])
def test_oversize_fails_without_partial_batch_commit(setup, monkeypatch, per_stock_only):
    jobs, _, engine, calendar = setup
    expand(engine, 2)
    job = new(jobs, calendar)
    jobs.tick()
    if per_stock_only:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM ultra_research_bar WHERE code='000002.XSHE'"))
    monkeypatch.setattr(module, 'MAX_ROWS', 100 if per_stock_only else 10)
    jobs.tick()
    assert jobs.get(job['id'])['status'] == 'failed'
    assert jobs.get(job['id'])['done'] == 0
    assert not values(jobs, job['id'])[0]


def test_failed_commit_rolls_back_stock_results_and_cursor(setup):
    jobs, _, _, calendar = setup
    job = new(jobs, calendar)
    jobs.tick()
    with jobs.db() as db:
        db.execute("CREATE TRIGGER fail_commit BEFORE INSERT ON windows BEGIN SELECT RAISE(ABORT, 'secret'); END")
    jobs.tick()
    assert jobs.get(job['id'])['status'] == 'failed' and jobs.get(job['id'])['done'] == 0
    with jobs.db() as db:
        assert db.execute('SELECT count(*) FROM stocks').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM outcomes').fetchone()[0] == 0
        db.execute('DROP TRIGGER fail_commit')
    jobs.action(job['id'], 'retry')
    assert finish(jobs, job['id'])['status'] == 'completed'


def test_legacy_missing_mode_resumes_frozen_inputs_without_source(setup):
    jobs, settings, engine, calendar = setup
    job = new(jobs, calendar, mode='frozen')
    jobs.tick(); jobs.tick()
    with jobs.db() as db:
        current = jobs.get(job['id'], db)
        del current['input_mode']
        jobs.save(db, current)
    restarted = ValidationJobs(settings, None)
    assert finish(restarted, job['id'])['status'] == 'completed'


def test_static_source_version_is_bound_and_no_unselected_strategies(setup, config, monkeypatch):
    jobs, settings, engine, calendar = setup
    expand(engine, 2)
    jobs.sources = ValidationSources(jobs, settings)
    ref = jobs.sources.save(config)
    seen = []
    @contextmanager
    def source(reference):
        seen.append(jobs.sources.get(reference['id'], reference['revision'])['database'])
        yield engine
    monkeypatch.setattr(jobs.sources, 'engine', source)
    monkeypatch.setattr(module.stats, 'make_three_selector', lambda *a: pytest.fail('unselected three strategies'))
    job = jobs.create(['oversold'], calendar[170].date(), calendar[190].date(), calendar[195].date(), source=ref)
    jobs.sources.save({**config, 'database':'changed'}, ref['id'], 1)
    assert finish(jobs, job['id'])['status'] == 'completed'
    assert seen and all(value == config['database'] for value in seen)


def test_query_failure_is_redacted_and_retry_rereads_uncommitted_batch(setup, monkeypatch):
    jobs, _, _, calendar = setup
    job = new(jobs, calendar)
    jobs.tick()
    original = pd.read_sql_query
    def fail(query, *args, **kwargs):
        if 'ultra_research_universe' in str(query):
            raise RuntimeError('private-db credentials')
        return original(query, *args, **kwargs)
    monkeypatch.setattr(pd, 'read_sql_query', fail)
    jobs.tick()
    current = jobs.get(job['id'])
    assert current['done'] == 0 and current['status'] == 'failed'
    assert 'private-db' not in json.dumps(current)
    assert 'ultra_research_universe' in current['timings']
    monkeypatch.setattr(pd, 'read_sql_query', original)
    jobs.action(job['id'], 'retry')
    assert finish(jobs, job['id'])['status'] == 'completed'


def test_static_future_rows_do_not_change_results_and_missing_data_fails(setup):
    jobs, _, engine, calendar = setup
    first = new(jobs, calendar, 'baseline')
    assert finish(jobs, first['id'])['status'] == 'completed'
    with engine.begin() as conn:
        conn.execute(text('UPDATE ultra_research_bar SET adj_close=99999 WHERE trade_date>:day'), {'day':calendar[195].date()})
    second = new(jobs, calendar, 'future')
    assert finish(jobs, second['id'])['status'] == 'completed'
    assert values(jobs, first['id']) == values(jobs, second['id'])
    with engine.begin() as conn:
        conn.execute(text('UPDATE ultra_research_bar SET adj_close=NULL WHERE trade_date=:day'), {'day':calendar[175].date()})
    third = new(jobs, calendar, 'missing', ['xuli'])
    result = finish(jobs, third['id'])
    assert result['status'] == 'failed' and not result['summary']


def test_static_empty_source_is_not_zero_win_rate(setup):
    jobs, _, engine, calendar = setup
    job = new(jobs, calendar)
    jobs.tick()
    with engine.begin() as conn: conn.execute(text('DELETE FROM ultra_research_bar'))
    result = finish(jobs, job['id'])
    assert result['status'] == 'failed' and not result['summary']
