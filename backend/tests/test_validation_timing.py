from types import SimpleNamespace
import pytest
from app.services import validation_timing as timing
from app.services.short_validation import ValidationJobs
from test_short_validation import setup
from test_validation_stream import new

@pytest.fixture
def clock(monkeypatch):
    state=SimpleNamespace(value=100.0)
    monkeypatch.setattr(timing,'time',SimpleNamespace(monotonic=lambda:state.value))
    return state


def test_first_execution_persisted_once_and_waiting_not_counted(setup,clock,monkeypatch):
    jobs,settings,engine,calendar=setup;job=new(jobs,calendar)
    assert jobs.get(job['id'])['first_started_at'] is None
    def work(current,source):
        jobs.stage(current['id'],'mock',current['generation'])
        clock.value+=7
        jobs.execution_timer.flush()
        assert jobs.get(job['id'])['execution_seconds']==7
        jobs.action(job['id'],'pause')
        clock.value+=60  # in-flight work returning after pause is excluded
    monkeypatch.setattr(jobs,'work',work);jobs.tick()
    saved=jobs.get(job['id']);first=saved['first_started_at']
    assert first and saved['execution_seconds']==7 and 'execution_token' not in saved
    other=ValidationJobs(settings,engine);clock.value+=600
    other.action(job['id'],'retry')
    def finish(current,source):
        clock.value+=3
        with other.db() as db:
            value=other.get(job['id'],db);value['status']='completed';other.save(db,value)
    monkeypatch.setattr(other,'work',finish);other.tick()
    result=other.get(job['id'])
    assert result['execution_seconds']==10 and result['first_started_at']==first
    clock.value+=600;assert not other.tick()
    assert other.get(job['id'])['execution_seconds']==10


@pytest.mark.parametrize('action',['pause','cancel'])
def test_cross_worker_control_stops_timer_without_reviving_task(setup,clock,monkeypatch,action):
    jobs,settings,engine,calendar=setup;job=new(jobs,calendar);other=ValidationJobs(settings,engine)
    def work(current,source):
        clock.value+=2;other.action(job['id'],action);clock.value+=200
        jobs.execution_timer.flush()
    monkeypatch.setattr(jobs,'work',work);jobs.tick()
    current=jobs.get(job['id'])
    assert current['execution_seconds']==2
    assert current['status']=={'pause':'paused','cancel':'cancelled'}[action]


def test_legacy_history_is_never_fabricated(setup,clock,monkeypatch):
    jobs,_,_,calendar=setup;job=new(jobs,calendar)
    with jobs.db() as db:
        current=jobs.get(job['id'],db)
        for key in ('execution_seconds','execution_partial','first_started_at'):current.pop(key)
        current['stage_started_at']='2020-01-01T00:00:00+00:00';jobs.save(db,current)
    def work(*args):clock.value+=4
    monkeypatch.setattr(jobs,'work',work);jobs.tick();current=jobs.get(job['id'])
    assert current['execution_partial'] and current['execution_seconds']==4
    assert not current.get('first_started_at')


def test_orphan_recovery_preserves_saved_time_not_downtime(setup,clock):
    jobs,settings,engine,calendar=setup;job=new(jobs,calendar)
    with jobs.db() as db:
        current=jobs.get(job['id'],db);current.update(work_pending=True,status='running')
        timer=timing.ExecutionTimer(jobs,current,'2026-10-31T00:00:00+00:00');jobs.save(db,current)
    clock.value+=5;timer.flush()
    clock.value+=3600
    other=ValidationJobs(settings,engine);other.tick();current=other.get(job['id'])
    assert current['status']=='retry_wait' and current['execution_seconds']==5
    assert current['execution_approximate'] and 'execution_token' not in current
    assert current['first_started_at']=='2026-10-31T00:00:00+00:00'
    assert not other.tick() and other.get(job['id'])['execution_seconds']==5


def test_late_timer_cannot_overwrite_new_work_unit(setup,clock):
    jobs,_,_,calendar=setup;job=new(jobs,calendar)
    with jobs.db() as db:
        current=jobs.get(job['id'],db);old=timing.ExecutionTimer(jobs,current,'first');jobs.save(db,current)
    clock.value+=3;old.flush()
    with jobs.db() as db:
        current=jobs.get(job['id'],db);latest=timing.ExecutionTimer(jobs,current,'second');jobs.save(db,current)
    clock.value+=2;old.flush(final=True);latest.finish();current=jobs.get(job['id'])
    assert current['execution_seconds']==5 and current['first_started_at']=='first'


def test_timer_shutdown_and_retry_wait_do_not_count_idle(setup,clock,monkeypatch):
    from test_validation_auto_retry import transient
    jobs,_,_,calendar=setup;job=new(jobs,calendar)
    def fail(current,source):
        clock.value+=8;current['_phase']='source_read';raise transient()
    monkeypatch.setattr(jobs,'work',fail);jobs.tick()
    assert jobs.get(job['id'])['status']=='retry_wait'
    assert jobs.get(job['id'])['execution_seconds']==8
    clock.value+=50;assert not jobs.tick()
    assert jobs.get(job['id'])['execution_seconds']==8


def test_graceful_stop_records_only_before_stop(setup,clock,monkeypatch):
    jobs,_,_,calendar=setup;job=new(jobs,calendar)
    def work(*args):
        clock.value+=2;jobs.stop();clock.value+=60
    monkeypatch.setattr(jobs,'work',work);jobs.tick()
    assert jobs.get(job['id'])['execution_seconds']==2


def test_final_timing_save_failure_keeps_terminal_task_and_last_pulse(setup,clock,monkeypatch):
    import sqlite3
    jobs,_,_,calendar=setup;job=new(jobs,calendar);original=jobs.save;reads=[]
    def work(current,source):
        reads.append(True);clock.value+=5;jobs.execution_timer.flush();clock.value+=2
        with jobs.db() as db:
            value=jobs.get(job['id'],db);value['status']='completed';jobs.save(db,value)
    def broken(db,value):
        if value['status']=='completed' and not value.get('execution_token'):
            raise sqlite3.OperationalError('mock timer save failure')
        return original(db,value)
    monkeypatch.setattr(jobs,'work',work);monkeypatch.setattr(jobs,'save',broken)
    with pytest.raises(sqlite3.OperationalError):jobs.tick()
    current=jobs.get(job['id'])
    assert current['status']=='completed' and current['execution_seconds']==5
    assert current['execution_token']  # UI renders terminal residual tokens as approximate.
    clock.value+=1000;assert not jobs.tick();assert reads==[True]
