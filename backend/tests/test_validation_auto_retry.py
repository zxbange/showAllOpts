"""Durable 60-second retry budget; use a clock, never wait real minutes."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import socket
import ssl
import threading
from unittest.mock import Mock

import pytest
from pymysql.err import OperationalError as MysqlError
from sqlalchemy.exc import OperationalError
from app.services import short_validation as module
from app.services.short_validation import ValidationJobs
from app.services.validation_errors import diagnostic, automatic_retry_allowed, mark_phase
from app.services.validation_sources import SourceError, temporary_dns
from test_short_validation import setup, finish
from test_validation_stream import expand, new, values

SECRET = 'private-password-host-query'

@pytest.fixture
def clock(monkeypatch):
    class Clock:
        value = datetime(2026, 10, 31, tzinfo=timezone.utc)
        def advance(self, seconds=60): self.value += timedelta(seconds=seconds)
    current = Clock()
    monkeypatch.setattr(module, 'now', lambda: current.value.isoformat())
    return current


def transient():
    return OperationalError('SELECT ' + SECRET, {'secret': SECRET}, MysqlError(2013, SECRET))


def fail_work(monkeypatch, calls, error=None):
    def broken(self, job, engine=None):
        calls.append(job['auto_retry_count'])
        job['_phase'] = 'source_read'
        raise error or transient()
    monkeypatch.setattr(ValidationJobs, 'work', broken)


def test_five_retries_sixty_seconds_restart_and_no_initial_charge(setup, clock, monkeypatch):
    jobs, settings, engine, calendar = setup
    job = new(jobs, calendar)
    calls = []; fail_work(monkeypatch, calls)
    for used in range(6):
        assert jobs.tick()
        current = jobs.get(job['id'])
        assert calls == list(range(used + 1))
        assert current['auto_retry_count'] == used
        assert SECRET not in json.dumps(current)
        if used == 5:
            assert current['status'] == 'failed' and current['retry_exhausted']
            assert current['next_retry_at'] is None and '5次已用尽' in current['error']
        else:
            assert current['status'] == 'retry_wait'
            assert datetime.fromisoformat(current['next_retry_at']) == clock.value + timedelta(seconds=60)
            # A new process retains both budget and deadline; no hot retries.
            jobs = ValidationJobs(settings, engine)
            assert not jobs.tick()
            clock.advance(59); assert not jobs.tick()
            clock.advance(1)
    clock.advance(600); assert not jobs.tick()
    jobs.action(job['id'], 'retry'); jobs.action(job['id'], 'retry')
    assert jobs.get(job['id'])['auto_retry_count'] == 0
    jobs.tick(); assert calls[-1] == 0  # explicit manual fresh budget


def test_waiting_does_not_block_other_jobs_and_deduplicates(setup, clock, monkeypatch):
    jobs, _, _, calendar = setup
    first = new(jobs, calendar, 'one')
    calls=[]; fail_work(monkeypatch, calls)
    jobs.tick()
    assert new(jobs, calendar, 'different-intent')['id'] == first['id']
    other=jobs.create(['bukou'], calendar[171].date(),calendar[190].date(),request_id='other')
    assert jobs.tick() and jobs.get(other['id'])['status']=='retry_wait'
    assert calls==[0,0] and not jobs.tick()


@pytest.mark.parametrize('action', ['pause', 'cancel', 'delete', 'retry'])
def test_waiting_controls_invalidate_due_work(setup, clock, monkeypatch, action):
    jobs, settings, engine, calendar=setup
    job=new(jobs,calendar); calls=[]; fail_work(monkeypatch,calls)
    jobs.tick()
    if action=='delete': jobs.delete(job['id']); jobs.delete(job['id'])
    else: jobs.action(job['id'],action); jobs.action(job['id'],action)
    restarted=ValidationJobs(settings,engine); clock.advance(120)
    if action=='retry':
        assert restarted.tick() and calls==[0,0]
    else:
        assert not restarted.tick() and calls==[0]
        if action!='delete': assert restarted.get(job['id'])['next_retry_at'] is None


@pytest.mark.parametrize('orphan', [False, True])
@pytest.mark.parametrize('action', ['pause', 'cancel', 'retry', None])
def test_due_claim_cross_worker_and_control_race(setup, clock, monkeypatch, action, orphan):
    jobs, settings, engine, calendar=setup
    job=new(jobs,calendar); calls=[]; fail_work(monkeypatch,calls)
    if orphan:
        with jobs.db() as db:
            current=jobs.get(job['id'],db);current['work_pending']=True;jobs.save(db,current)
    jobs.tick(); clock.advance()
    second=ValidationJobs(settings,engine)
    entered,release=threading.Event(),threading.Event()
    def inflight(self, work, source=None):
        assert work['auto_retry_count']==1
        entered.set(); assert release.wait(5)
        work['_phase']='source_read'; raise transient()
    monkeypatch.setattr(ValidationJobs,'work',inflight)
    errors=[]
    def execute():
        try: jobs.tick()
        except Exception as exc: errors.append(exc)
    thread=threading.Thread(target=execute); thread.start()
    assert entered.wait(5)
    try:
        assert second.tick() is False
        if action:
            if action=='retry': second.action(job['id'],'pause')
            second.action(job['id'],action)
    finally:
        release.set(); thread.join(5)
    assert not errors and not thread.is_alive()
    current=second.get(job['id'])
    assert current['status']=={'pause':'paused','cancel':'cancelled','retry':'queued',None:'retry_wait'}[action]
    if action: assert current['next_retry_at'] is None
    else: assert current['auto_retry_count']==1


@pytest.mark.parametrize('code', [1044,1045,1049,1054,1142,1146,2026,9999])
def test_permanent_mysql_errors_do_not_retry(setup, clock, monkeypatch, code):
    jobs,_,_,calendar=setup
    job=new(jobs,calendar); calls=[]; fail_work(monkeypatch,calls,MysqlError(code,SECRET))
    jobs.tick(); assert jobs.get(job['id'])['status']=='failed'
    clock.advance(600); assert not jobs.tick() and calls==[0]


@pytest.mark.parametrize('error,phase,allowed', [
    (MysqlError(2013,SECRET),'source_schema',True),
    (MysqlError(2013,SECRET),'compute',False),
    (OperationalError(SECRET,{},Exception(SECRET)),'source_read',False),
    (ValueError(SECRET),'source_read',False),
    (RuntimeError(SECRET),'compute',False),
    (TimeoutError(SECRET),'connect',True),
    (ConnectionResetError(104,SECRET),'connect',True),
    (ConnectionResetError(10054,SECRET),'source_read',True),
    (ConnectionAbortedError(SECRET),'connect',True),
    (ConnectionRefusedError(SECRET),'connect',True),
    (BrokenPipeError(SECRET),'source_read',True),
    (ConnectionResetError(SECRET),'compute',False),
    (ConnectionResetError(SECRET),'local_store',False),
    (PermissionError(13,SECRET),'connect',False),
    (socket.gaierror(socket.EAI_AGAIN,SECRET),'source_config',True),
    (socket.gaierror(socket.EAI_NONAME,SECRET),'source_config',False),
    (ssl.SSLError(1,SECRET),'connect',False),
    (temporary_dns('DNS解析繁忙'),'source_config',True),
    (SourceError(SECRET),'source_config',False),
])
def test_conservative_error_classification(error,phase,allowed):
    info=diagnostic(error,phase)
    assert automatic_retry_allowed(error,info) is allowed
    assert SECRET not in json.dumps(info)


def test_wrapped_connection_errors_keep_safe_classification():
    for code,allowed in [(2013,True),(1045,False),(2026,False),(9999,False)]:
        try:
            try: raise MysqlError(code,SECRET)
            except Exception: raise SourceError('连接失败') from None
        except SourceError as error:
            info=diagnostic(error,'connect')
            assert automatic_retry_allowed(error,info) is allowed
            assert info['code']==f'MYSQL_{code}' and SECRET not in json.dumps(info)


def set_count(jobs,ident,count):
    with jobs.db() as db:
        job=jobs.get(ident,db); job['auto_retry_count']=count; jobs.save(db,job)


@pytest.mark.parametrize('mode', ['static-batch-v1','frozen'])
def test_plan_never_resets_but_persisted_progress_does(setup,clock,mode):
    jobs,_,_,calendar=setup
    job=new(jobs,calendar,mode=mode); set_count(jobs,job['id'],4)
    jobs.tick(); assert jobs.get(job['id'])['auto_retry_count']==4
    jobs.tick(); current=jobs.get(job['id'])
    assert current['auto_retry_count']==0 and current['error'] is None
    assert (current['done'] if mode=='static-batch-v1' else current['captured'])==1


def test_partial_batch_uses_committed_reset_not_stale_budget(setup,clock,monkeypatch):
    jobs,_,engine,calendar=setup; expand(engine,2)
    job=new(jobs,calendar);jobs.tick();set_count(jobs,job['id'],4)
    original=jobs.compute_stock
    def fail_second(work,code,*args):
        if code=='000002.XSHE':
            error=sqlite3.OperationalError(SECRET); error.sqlite_errorcode=sqlite3.SQLITE_BUSY
            mark_phase(error,'local_store');raise error
        return original(work,code,*args)
    monkeypatch.setattr(jobs,'compute_stock',fail_second)
    jobs.tick(); current=jobs.get(job['id'])
    assert current['done']==1 and current['auto_retry_count']==0 and current['status']=='retry_wait'
    monkeypatch.setattr(jobs,'compute_stock',original); clock.advance(); jobs.tick()
    assert jobs.get(job['id'])['done']==2 and jobs.get(job['id'])['auto_retry_count']==0
    assert finish(jobs,job['id'])['status']=='completed'
    reference=new(jobs,calendar,'reference');finish(jobs,reference['id'])
    assert values(jobs,job['id'])==values(jobs,reference['id'])


def test_failed_commit_does_not_reset_budget_or_count_results(setup,clock,monkeypatch):
    jobs,_,_,calendar=setup;job=new(jobs,calendar);jobs.tick();set_count(jobs,job['id'],4)
    original=jobs.save
    def broken(db,current):
        if current['done']==1:
            error=sqlite3.OperationalError(SECRET);error.sqlite_errorcode=sqlite3.SQLITE_BUSY;raise error
        return original(db,current)
    monkeypatch.setattr(jobs,'save',broken);jobs.tick();current=jobs.get(job['id'])
    assert current['done']==0 and current['auto_retry_count']==4 and current['status']=='retry_wait'
    with jobs.db() as db: assert db.execute('SELECT count(*) FROM outcomes').fetchone()[0]==0


def test_failed_failure_state_write_never_hot_retries_or_loses_budget(setup,clock,monkeypatch):
    jobs,settings,engine,calendar=setup;job=new(jobs,calendar);set_count(jobs,job['id'],3)
    calls=[];fail_work(monkeypatch,calls)
    original=jobs.save
    def broken(db,current):
        if current['status']=='retry_wait': raise sqlite3.OperationalError(SECRET)
        return original(db,current)
    monkeypatch.setattr(jobs,'save',broken)
    with pytest.raises(sqlite3.OperationalError):jobs.tick()
    restarted=ValidationJobs(settings,engine);assert restarted.tick()
    current=restarted.get(job['id'])
    assert current['status']=='retry_wait' and current['error_code']=='WORK_INTERRUPTED'
    assert current['auto_retry_count']==3 and calls==[3]
    assert not restarted.tick() and calls==[3]
    clock.advance();assert restarted.tick() and calls==[3,4]


def test_crash_after_auto_claim_keeps_budget_and_cannot_execute_immediately(setup,clock,monkeypatch):
    jobs,settings,engine,calendar=setup;job=new(jobs,calendar)
    calls=[];fail_work(monkeypatch,calls);jobs.tick();clock.advance()
    def crash(self,work,engine=None):raise SystemExit()
    monkeypatch.setattr(ValidationJobs,'work',crash)
    with pytest.raises(SystemExit):jobs.tick()
    assert jobs.get(job['id'])['auto_retry_count']==1
    restarted=ValidationJobs(settings,engine);restarted.tick()
    current=restarted.get(job['id'])
    assert current['auto_retry_count']==1 and current['status']=='retry_wait'
    assert not restarted.tick()


@pytest.mark.parametrize('underlying', [ssl.SSLCertVerificationError(1,SECRET),
    socket.gaierror(socket.EAI_NONAME,SECRET), PermissionError(13,SECRET)])
def test_permanent_cause_vetoes_mysql_generic_connect_error(underlying):
    try:
        try:
            try: raise underlying
            except OSError: raise MysqlError(2003,SECRET)
        except Exception: raise SourceError('连接失败') from None
    except SourceError as error:
        info=diagnostic(error,'connect')
        assert info['code']=='MYSQL_2003'
        assert not automatic_retry_allowed(error,info)
        assert SECRET not in json.dumps(info)


def test_summary_retry_completion_clears_failure_display(setup,clock,monkeypatch):
    jobs,_,_,calendar=setup;job=new(jobs,calendar);jobs.tick();jobs.tick()
    assert jobs.get(job['id'])['done']==1
    original=jobs.summarize
    def broken(*args):
        error=sqlite3.OperationalError(SECRET);error.sqlite_errorcode=sqlite3.SQLITE_BUSY
        mark_phase(error,'local_store');raise error
    monkeypatch.setattr(jobs,'summarize',broken);jobs.tick()
    assert jobs.get(job['id'])['status']=='retry_wait'
    monkeypatch.setattr(jobs,'summarize',original);clock.advance();jobs.tick()
    current=jobs.get(job['id'])
    assert current['status']=='completed' and current['error'] is None
    assert current['next_retry_at'] is None and 'error_code' not in current
    assert not current['retry_exhausted'] and not current['auto_retry_allowed']


def test_repeated_process_replacement_cannot_reset_or_exceed_budget(setup, clock, monkeypatch):
    jobs,settings,engine,calendar=setup;job=new(jobs,calendar);calls=[]
    def crash(self, work, source=None):
        calls.append(work['auto_retry_count']);raise SystemExit()
    monkeypatch.setattr(ValidationJobs,'work',crash)
    for used in range(6):
        with pytest.raises(SystemExit): jobs.tick()
        assert calls==list(range(used+1))
        jobs=ValidationJobs(settings,engine);assert jobs.tick()
        current=jobs.get(job['id']);assert current['auto_retry_count']==used
        if used==5:
            assert current['status']=='failed' and current['retry_exhausted']
            clock.advance(600);assert not jobs.tick()
        else:
            assert current['status']=='retry_wait'
            deadline=current['next_retry_at']
            for _ in range(3):
                jobs=ValidationJobs(settings,engine);assert not jobs.tick()
                assert jobs.get(job['id'])['next_retry_at']==deadline
            clock.advance(59);assert not jobs.tick();clock.advance(1)


@pytest.mark.parametrize('shutdown', ['stop_timeout', 'sigkill'])
def test_real_process_exit_keeps_checkpoint_and_recovers(setup, clock, shutdown, monkeypatch):
    import multiprocessing
    import os
    import signal
    import time
    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip('Linux process lifecycle regression')
    jobs,settings,engine,calendar=setup;expand(engine,2)
    monkeypatch.setattr(module,'BATCH_SIZE',1)
    job=new(jobs,calendar);jobs.tick();jobs.tick()
    saved=values(jobs,job['id']);set_count(jobs,job['id'],2)
    context=multiprocessing.get_context('fork');receive,send=context.Pipe(duplex=False)
    def child():
        worker=ValidationJobs(settings,engine);entered=threading.Event()
        def blocked(*args):
            entered.set();threading.Event().wait(30)
        worker.work=blocked;worker.start();assert entered.wait(5);send.send('entered')
        if shutdown=='stop_timeout':
            before=time.monotonic();worker.stop()
            send.send((time.monotonic()-before,worker.thread.is_alive()))
            os._exit(0)
        threading.Event().wait(30)
    process=context.Process(target=child);process.start();send.close()
    try:
        assert receive.poll(10) and receive.recv()=='entered'
        if shutdown=='sigkill': os.kill(process.pid,signal.SIGKILL)
        else:
            assert receive.poll(10)
            elapsed,alive=receive.recv();assert elapsed>=1.9 and alive
        process.join(10);assert not process.is_alive()
        restarted=ValidationJobs(settings,engine)
        assert restarted.get(job['id'])['work_pending'];assert restarted.tick()
        current=restarted.get(job['id'])
        assert current['status']=='retry_wait' and current['auto_retry_count']==2
        assert current['done']==1 and values(restarted,job['id'])==saved
        clock.advance(59);assert not restarted.tick()
        clock.advance(1);assert restarted.tick()
        assert restarted.get(job['id'])['done']==2
        assert restarted.get(job['id'])['auto_retry_count']==0
        assert finish(restarted,job['id'])['status']=='completed'
        reference=new(restarted,calendar,'reference');finish(restarted,reference['id'])
        assert values(restarted,job['id'])==values(restarted,reference['id'])
    finally:
        if process.is_alive(): process.kill();process.join(5)
        receive.close()


@pytest.mark.parametrize('action', ['pause','cancel'])
@pytest.mark.parametrize('after_schedule', [False,True])
def test_orphan_recovery_controls_remain_authoritative(setup,clock,action,after_schedule):
    jobs,settings,engine,calendar=setup;job=new(jobs,calendar)
    with jobs.db() as db:
        current=jobs.get(job['id'],db);current['work_pending']=True;jobs.save(db,current)
    second=ValidationJobs(settings,engine)
    if after_schedule: assert second.tick()
    jobs.action(job['id'],action);clock.advance(600);assert not second.tick()
    assert second.get(job['id'])['status']=={'pause':'paused','cancel':'cancelled'}[action]


def test_orphan_schedule_storage_failure_never_runs_source(setup,clock,monkeypatch):
    jobs,settings,engine,calendar=setup;job=new(jobs,calendar)
    with jobs.db() as db:
        current=jobs.get(job['id'],db);current.update(work_pending=True,auto_retry_count=2);jobs.save(db,current)
    calls=[];fail_work(monkeypatch,calls);original=jobs.save
    monkeypatch.setattr(jobs,'save',Mock(side_effect=sqlite3.OperationalError(SECRET)))
    for _ in range(3):
        with pytest.raises(sqlite3.OperationalError):jobs.tick()
        clock.advance()
    assert not calls and jobs.get(job['id'])['auto_retry_count']==2
    monkeypatch.setattr(jobs,'save',original);assert jobs.tick() and not calls
    clock.advance();assert jobs.tick() and calls==[3]
