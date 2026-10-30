from datetime import date
import json
import numpy as np
import pandas as pd
import portalocker
import pytest
from sqlalchemy import create_engine, event
from app.core.config import Settings
from app.services.short_validation import ValidationJobs, frame_json, frame_read
from app.services import short_statistics as stats
from test_ultra_short_statistics import database, prices

@pytest.fixture
def setup(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'source.sqlite3'))
    calendar=pd.bdate_range('2025-01-01',periods=240)
    closes=np.full(len(calendar),10.)
    closes[180:185]=[9.8,9.4,9.,8.9,9.5]
    database(engine,prices(closes,calendar))
    settings=Settings(_env_file=None,log_dir=tmp_path/'persist')
    return ValidationJobs(settings,engine),settings,engine,calendar

def create(jobs,calendar,methods=None):
    return jobs.create(methods or ['oversold'],calendar[170].date(),calendar[190].date(),calendar[195].date(), input_mode='frozen')

def finish(jobs,ident):
    for _ in range(15):
        job=jobs.get(ident)
        if job['status'] in ('completed','failed','cancelled'): return job
        jobs.tick()
    pytest.fail('job did not finish')

def test_float_lossless():
    frame=prices([10.,10.00000000004])
    restored=frame_read(frame_json(frame))
    assert restored.close.tolist()==frame.close.tolist()
    assert stats.forward_outcomes(restored).iloc[0].d1_win==1

def test_atomic_resume_matches_legacy_and_source_read_only(setup,monkeypatch):
    jobs,settings,engine,calendar=setup
    statements=[]
    event.listen(engine,'before_cursor_execute',lambda conn,cursor,sql,*args:statements.append(sql))
    job=create(jobs,calendar);jobs.tick();jobs.tick()
    assert jobs.get(job['id'])['captured']==1
    original=stats.stock_samples
    def fail(*a,**kw): raise RuntimeError('secret')
    monkeypatch.setattr(stats,'stock_samples',fail);jobs.tick()
    assert jobs.get(job['id'])['status']=='failed'
    assert 'secret' not in jobs.get(job['id'])['error']
    monkeypatch.setattr(stats,'stock_samples',original)
    resumed=ValidationJobs(settings,engine);resumed.action(job['id'],'retry')
    result=finish(resumed,job['id'])
    assert result['status']=='completed',result
    assert result['done']==1
    with resumed.db() as db: row=db.execute('SELECT bars,universe,details FROM stocks').fetchone()
    bars,universe,detail=map(frame_read,row)
    samples,_=original('000001.XSHE',bars,universe,pd.DatetimeIndex(pd.to_datetime(result['calendar'])),pd.Timestamp(result['start']),pd.Timestamp(result['end']),stats.PARAMS)
    reference=stats.select_signals(samples,stats.PARAMS,['超跌后止跌反弹'])
    expected=stats.summary_table(reference,['超跌后止跌反弹']).to_dict('records')
    for actual,wanted in zip(result['summary'],expected):
        for key,value in wanted.items():
            if isinstance(value,(int,float)): assert actual[key]==pytest.approx(value)
            else: assert actual[key]==value
    assert len(detail)==len(reference)>0
    assert all(s.lstrip().upper().startswith('SELECT') for s in statements)

def test_duplicate_lock_cancel_and_restart(setup):
    jobs,settings,engine,calendar=setup
    first=create(jobs,calendar)
    assert create(ValidationJobs(settings,engine),calendar)['id']==first['id']
    with portalocker.Lock(str(jobs.root/'worker.lock'),timeout=0):
        assert ValidationJobs(settings,engine).tick() is False
    jobs.tick();jobs.action(first['id'],'pause');assert not jobs.tick()
    jobs.action(first['id'],'retry');jobs.action(first['id'],'retry')
    jobs.tick();jobs.tick();assert jobs.get(first['id'])['done']==1
    restarted=ValidationJobs(settings,engine)
    assert finish(restarted,first['id'])['status']=='completed'
    restarted.action(first['id'],'cancel');restarted.action(first['id'],'retry')
    assert restarted.get(first['id'])['done']==1

def test_only_selected_formulas_and_multi_equals_single(setup,monkeypatch):
    jobs,_,_,calendar=setup
    monkeypatch.setattr(stats,'pattern_candidates',lambda *a:pytest.fail('unselected legacy patterns'))
    selector=stats.make_three_selector;called=[]
    def selected(keys): called.append(keys);return selector(keys)
    monkeypatch.setattr(stats,'make_three_selector',selected)
    one=finish(jobs,create(jobs,calendar)['id'])
    assert not called and one['status']=='completed',one
    multi=finish(jobs,create(jobs,calendar,['oversold','xuli'])['id'])
    assert called==[['xuli']]
    assert multi['status']=='completed',multi
    assert [r for r in multi['summary'] if r['策略']=='超跌后止跌反弹']==one['summary']

def test_missing_and_future(setup):
    jobs,_,engine,calendar=setup
    with engine.begin() as db: db.exec_driver_sql('UPDATE ultra_research_bar SET adj_close=NULL WHERE trade_date=?',(str(calendar[175].date()),))
    failed=finish(jobs,create(jobs,calendar,['xuli'])['id'])
    assert failed['status']=='failed' and not failed['summary'],failed
    with pytest.raises(ValueError):jobs.create(['oversold'],date(2024,1,1),date(2099,1,1),date(2099,1,1))
    with pytest.raises(ValueError):create(jobs,calendar,['unknown'])

def test_cancel_inflight_does_not_commit(setup,monkeypatch):
    jobs,_,_,calendar=setup;job=create(jobs,calendar);jobs.tick();jobs.tick()
    original=stats.stock_samples
    def cancel(*a,**kw):jobs.action(job['id'],'cancel');return original(*a,**kw)
    monkeypatch.setattr(stats,'stock_samples',cancel);jobs.tick()
    assert jobs.get(job['id'])['done']==0
    with jobs.db() as db:assert db.execute('SELECT details FROM stocks').fetchone()[0] is None

def test_empty_signals_not_zero(setup):
    jobs,_,_,calendar=setup
    job=jobs.create(['oversold'],calendar[200].date(),calendar[210].date(),calendar[215].date())
    result=finish(jobs,job['id']);assert result['status']=='completed',result
    assert all(r['胜率(%)'] is None and r['触发样本']==0 for r in result['summary'])

def test_optional_cutoff_and_fresh_task(setup):
    jobs,_,_,calendar=setup
    job=jobs.create(['oversold'],calendar[170].date(),None,None)
    result=finish(jobs,job['id']);assert result['status']=='completed',result
    assert result['end']==str(calendar[-1].date())
    fresh=jobs.create(['oversold'],calendar[170].date(),None,None,'fresh')
    assert fresh['id']!=job['id']

def test_api_validation_idempotency_and_no_credential_exposure(setup):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routes.short_term import router, validation_admin
    jobs,_,_,calendar=setup
    app=FastAPI();app.state.short_validation=jobs;app.include_router(router)
    app.dependency_overrides[validation_admin]=lambda: jobs
    with TestClient(app) as client:
        payload=dict(methods=['oversold'],start=str(calendar[170].date()),end=None,observed=None,request_id='one')
        first=client.post('/short-term/validation/jobs',json=payload)
        assert first.status_code==200
        assert client.post('/short-term/validation/jobs',json={**payload,'request_id':'two'}).json()['id']==first.json()['id']
        assert client.post('/short-term/validation/jobs',json={**payload,'methods':[]}).status_code==422
        ident=first.json()['id'];jobs.tick()
        listed=client.get('/short-term/validation/jobs').json()['jobs'][0]
        assert 'calendar' not in listed and 'codes' not in listed
        assert client.post(f'/short-term/validation/jobs/{ident}/pause').json()['status']=='paused'
        assert client.post(f'/short-term/validation/jobs/{ident}/retry').json()['status']=='queued'
        assert client.post('/short-term/validation/jobs/missing/retry').status_code==404

def test_cli_exit_code_for_missing_config(tmp_path):
    import subprocess,sys
    from pathlib import Path
    entry=Path(__file__).resolve().parents[2]/'脚本/本地统计/七策略胜率统计_本地版.py'
    result=subprocess.run([sys.executable,str(entry),'--config',str(tmp_path/'missing.json')],capture_output=True,text=True)
    assert result.returncode==1

def test_cancel_then_retry_does_not_receive_stale_failure(setup,monkeypatch):
    jobs,_,_,calendar=setup;job=create(jobs,calendar);jobs.tick();jobs.tick()
    original=stats.stock_samples
    def interrupt(*a,**kw):
        jobs.action(job['id'],'pause');jobs.action(job['id'],'retry')
        raise InterruptedError('old attempt')
    monkeypatch.setattr(stats,'stock_samples',interrupt);jobs.tick()
    assert jobs.get(job['id'])['status']=='queued'
    assert jobs.get(job['id'])['done']==0
    monkeypatch.setattr(stats,'stock_samples',original)
    assert finish(jobs,job['id'])['status']=='completed'

def test_multi_stock_retry_keeps_committed_stock_and_frozen_inputs(setup,monkeypatch):
    jobs,settings,engine,calendar=setup
    for table in ('ultra_research_bar','ultra_research_universe'):
        with engine.connect() as db: frame=pd.read_sql_query(f'SELECT * FROM {table}',db)
        frame['code']='000002.XSHE';frame.to_sql(table,engine,if_exists='append',index=False)
    job=create(jobs,calendar);jobs.tick();jobs.tick();jobs.tick()
    original=stats.stock_samples;seen=[]
    def fail_second(code,*a,**kw):
        seen.append(code)
        if code=='000002.XSHE':raise RuntimeError('injected')
        return original(code,*a,**kw)
    monkeypatch.setattr(stats,'stock_samples',fail_second)
    jobs.tick();jobs.tick()
    assert jobs.get(job['id'])['done']==1
    with engine.begin() as db:db.exec_driver_sql('DELETE FROM ultra_research_bar')
    restarted=ValidationJobs(settings,engine);restarted.action(job['id'],'retry')
    def success(code,*a,**kw):seen.append(code);return original(code,*a,**kw)
    monkeypatch.setattr(stats,'stock_samples',success)
    result=finish(restarted,job['id'])
    assert result['status']=='completed',result
    assert seen==['000001.XSHE','000002.XSHE','000002.XSHE']
    assert result['done']==2
    assert all(r['股票数']==2 for r in result['summary'])

def test_observation_boundary_does_not_read_future_rows(setup):
    jobs,_,_,calendar=setup
    job=jobs.create(['oversold'],calendar[170].date(),calendar[184].date(),calendar[184].date())
    result=finish(jobs,job['id']);assert result['status']=='completed',result
    assert all(row['待观察']==1 and row['有效样本']==0 for row in result['summary'])


def test_new_intents_after_terminal_and_uncertain_retry_keep_history(setup):
    jobs, _, _, calendar = setup
    args = (['oversold'], calendar[170].date(), calendar[190].date(), calendar[195].date())
    prior = []
    for index, status in enumerate(('completed', 'failed', 'cancelled')):
        current = jobs.create(*args, request_id=f'intent-{index}')
        assert current['id'] not in prior
        # A second intent while the first is active still deduplicates.
        assert jobs.create(*args, request_id=f'uncertain-{index}')['id'] == current['id']
        with jobs.db() as db:
            current['status'] = status
            jobs.save(db, current)
        # Unknown response retried with the same ID returns the terminal record.
        assert jobs.create(*args, request_id=f'intent-{index}')['id'] == current['id']
        assert jobs.create(*args, request_id=f'uncertain-{index}')['id'] == current['id']
        prior.append(current['id'])
    assert len(jobs.list()) == 3
