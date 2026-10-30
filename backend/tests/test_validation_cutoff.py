"""Hard-cutoff regressions using synthetic SQLite only."""
from datetime import date, timedelta
import hashlib
import json
import pandas as pd
import pytest
from sqlalchemy import event, text
from app.services.short_validation import ValidationJobs, VERSION, frame_read
from test_short_validation import setup, finish


@pytest.mark.parametrize('mode', ['static-batch-v1', 'frozen'])
@pytest.mark.parametrize('available', [0, 1, 2, 3])
def test_cutoff_limits_queries_horizons_and_windows(setup, available, mode):
    jobs, _, engine, calendar = setup
    end = calendar[184 + available].date()
    queries = []
    event.listen(engine, 'before_cursor_execute', lambda conn, cursor, sql, params, *args: queries.append((sql, params)))
    job = jobs.create(['oversold'], calendar[170].date(), end, calendar[195].date(), input_mode=mode)
    result = finish(jobs, job['id'])
    assert result['status'] == 'completed', result
    assert result['version'] == VERSION == 'four-close-v2'
    assert result['end'] == result['observed'] == result['calendar'][-1] == str(end)
    assert result['requested']['observed'] == str(end)
    assert pd.Timestamp(result['calendar'][0]) < calendar[170]
    for horizon, row in enumerate(result['summary'], 1):
        assert row['触发样本'] == 1
        assert row['有效样本'] == int(horizon <= available)
        assert row['待观察'] == int(horizon > available)
        assert (row['胜率(%)'] is None) == (horizon > available)
    assert result['windows'][0]['完整三日样本'] == int(available == 3)
    with jobs.db() as db:
        details = frame_read(db.execute('SELECT details FROM stocks WHERE job=?', (job['id'],)).fetchone()[0])
    assert details.signal_date.tolist() == [calendar[184]]
    dated = [(sql, params) for sql, params in queries if 'SELECT' in sql and 'WHERE 1=0' not in sql and any(t in sql for t in
             ('FROM ultra_research_bar', 'FROM ultra_research_universe', 'FROM ultra_research_calendar'))]
    assert dated
    for sql, params in dated:
        assert '<=' in sql or 'BETWEEN' in sql
        assert all(str(value)[:10] <= str(end) for value in params
                   if str(value)[:4] == '2025' and '-' in str(value))


@pytest.mark.parametrize('holiday', [False, True])
def test_nontrading_end_uses_previous_collected_close(setup, holiday):
    jobs, _, engine, calendar = setup
    if holiday:
        end = calendar[185].date()
        with engine.begin() as conn:
            for table in ('ultra_research_calendar', 'ultra_research_bar', 'ultra_research_universe'):
                conn.execute(text(f'DELETE FROM {table} WHERE trade_date=:day'), {'day': end})
        expected = calendar[184].date()
    else:
        friday = next(day for day in calendar[185:195] if day.weekday() == 4)
        end, expected = friday.date() + timedelta(days=2), friday.date()
    job = jobs.create(['oversold'], calendar[170].date(), end)
    result = finish(jobs, job['id'])
    assert result['status'] == 'completed', result
    assert result['end'] == str(end)
    assert result['observed'] == result['calendar'][-1] == str(expected)


def test_default_cutoff_fixed_across_restart_even_after_new_data(setup):
    jobs, settings, engine, calendar = setup
    job = jobs.create(['oversold'], calendar[170].date(), None, calendar[184].date())
    jobs.tick()
    original = jobs.get(job['id'])
    assert original['end'] == original['observed'] == str(calendar[-1].date())
    jobs.action(job['id'], 'pause')
    future = (calendar[-1] + pd.offsets.BDay()).date()
    with engine.begin() as conn:
        conn.execute(text('INSERT INTO ultra_research_calendar(trade_date) VALUES (:day)'), {'day': future})
        for table in ('ultra_research_bar', 'ultra_research_universe'):
            row = pd.read_sql_query(text(f'SELECT * FROM {table} WHERE trade_date=:day'), conn,
                                    params={'day': calendar[-1].date()})
            row['trade_date'] = future
            row.to_sql(table, conn, if_exists='append', index=False)
    restarted = ValidationJobs(settings, engine)
    restarted.action(job['id'], 'retry')
    result = finish(restarted, job['id'])
    assert result['status'] == 'completed', result
    for field in ('start', 'end', 'observed', 'calendar'):
        assert result[field] == original[field]
    fresh = jobs.create(['oversold'], calendar[170].date(), request_id='new-latest')
    jobs.tick()
    assert jobs.get(fresh['id'])['end'] == str(future)


@pytest.mark.parametrize('planned', [False, True])
@pytest.mark.parametrize('mode', ['static-batch-v1', 'frozen'])
def test_legacy_retry_preserves_separate_observation(setup, planned, mode):
    jobs, settings, engine, calendar = setup
    job = jobs.create(['oversold'], calendar[170].date(), calendar[184].date(), input_mode=mode)
    with jobs.db() as db:
        old = jobs.get(job['id'], db)
        old.update(version='four-close-v1', observed=str(calendar[195].date()))
        old['requested'].update(version='four-close-v1', observed=old['observed'])
        jobs.save(db, old)
        fingerprint = hashlib.sha256(json.dumps([old['requested'], str(date.today())], sort_keys=True).encode()).hexdigest()
        db.execute('UPDATE jobs SET fingerprint=? WHERE id=?', (fingerprint, old['id']))
    if planned:
        jobs.tick()
    jobs.action(job['id'], 'pause')
    restarted = ValidationJobs(settings, engine)
    restarted.action(job['id'], 'retry')
    result = finish(restarted, job['id'])
    assert result['status'] == 'completed', result
    assert result['version'] == 'four-close-v1'
    assert result['end'] == str(calendar[184].date())
    assert result['observed'] == result['calendar'][-1] == str(calendar[195].date())
    assert all(row['有效样本'] == 1 and row['待观察'] == 0 for row in result['summary'])
    assert result['windows'][0]['完整三日样本'] == 1
    fresh = restarted.create(['oversold'], calendar[170].date(), calendar[184].date(), calendar[195].date(), input_mode=mode)
    assert fresh['id'] != job['id'] and fresh['version'] == VERSION
    # Stale observation dates never affect new-task identity.
    assert restarted.create(['oversold'], calendar[170].date(), calendar[184].date(), calendar[200].date(), input_mode=mode)['id'] == fresh['id']


def test_real_signal_in_warmup_is_excluded(setup):
    jobs, _, _, calendar = setup
    job = jobs.create(['oversold'], calendar[185].date(), calendar[190].date())
    result = finish(jobs, job['id'])
    assert result['status'] == 'completed', result
    assert str(calendar[184].date()) in result['calendar']
    # The fixture's known oversold signal on184 is warmup-only for this job.
    assert all(row['触发样本'] == 0 for row in result['summary'])
    assert result['windows'][0]['触发样本'] == 0


def test_api_ignores_stale_third_date(setup):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routes.short_term import router, validation_admin
    jobs, _, _, calendar = setup
    app = FastAPI()
    app.state.short_validation = jobs
    app.include_router(router)
    app.dependency_overrides[validation_admin] = lambda: jobs
    with TestClient(app) as client:
        payload = dict(methods=['oversold'], start=str(calendar[170].date()),
                       end=str(calendar[184].date()), observed=str(calendar[195].date()))
        response = client.post('/short-term/validation/jobs', json=payload)
        assert response.status_code == 200
        result = finish(jobs, response.json()['id'])
        assert result['status'] == 'completed'
        assert all(row['待观察'] == 1 for row in result['summary'])
