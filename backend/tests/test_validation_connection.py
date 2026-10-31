from contextlib import contextmanager
from types import SimpleNamespace
from sqlalchemy import create_engine, event
import pytest
from app.services import short_validation as module
from app.services.short_validation import ValidationJobs
from test_short_validation import setup
from test_validation_stream import expand, new

@pytest.fixture
def connected(setup,monkeypatch):
    jobs,settings,source,calendar=setup;expand(source,3)
    monkeypatch.setattr(module,'BATCH_SIZE',1)
    connections=[];closed=[];engines=[];refs=[]
    @contextmanager
    def engine(ref):
        refs.append(dict(ref));db=create_engine(source.url,pool_size=1,max_overflow=0)
        engines.append(db)
        event.listen(db,'connect',lambda connection,record:connections.append(connection))
        event.listen(db,'close',lambda connection,record:closed.append(connection))
        try:yield db
        finally:db.dispose()
    jobs.sources=SimpleNamespace(engine=engine,freeze_service=lambda:{'id':'fixture','revision':1},get=lambda *a:{})
    return jobs,settings,source,calendar,connections,closed,engines,refs


def test_one_physical_connection_across_batches_no_read_transaction_during_compute(connected,monkeypatch):
    jobs,settings,source,calendar,connections,closed,engines,refs=connected
    job=new(jobs,calendar);original=jobs.compute_stock;seen=[]
    def compute(*args):
        lease=jobs.source_lease
        assert not lease.connection.in_transaction()
        seen.append(lease.connection)
        assert not ValidationJobs(settings,source).drain()
        return original(*args)
    monkeypatch.setattr(jobs,'compute_stock',compute)
    assert jobs.drain()
    assert jobs.get(job['id'])['status']=='completed'
    assert len(seen)==3 and all(c is seen[0] for c in seen)
    assert len(connections)==len(closed)==len(engines)==1
    assert jobs.source_lease is None


def test_different_tasks_and_revisions_never_share_engine(connected):
    jobs,_,_,calendar,connections,closed,engines,refs=connected
    first=jobs.create(['oversold'],calendar[170].date(),calendar[190].date(),source={'id':'one','revision':1})
    second=jobs.create(['oversold'],calendar[170].date(),calendar[190].date(),source={'id':'two','revision':2})
    jobs.drain();jobs.drain()
    assert jobs.get(first['id'])['status']==jobs.get(second['id'])['status']=='completed'
    assert refs==[{'id':'one','revision':1},{'id':'two','revision':2}]
    assert len(connections)==len(closed)==len(engines)==2 and connections[0] is not connections[1]


@pytest.mark.parametrize('action',['pause','cancel','stop','error'])
def test_control_or_failure_releases_connection(connected,monkeypatch,action):
    jobs,_,_,calendar,connections,closed,engines,refs=connected
    job=new(jobs,calendar);original=jobs.compute_stock;called=[]
    def compute(*args):
        result=original(*args)
        if not called:
            called.append(True)
            if action=='stop':jobs.stop()
            elif action=='error':raise ValueError('deterministic mock')
            else:jobs.action(job['id'],action)
        return result
    monkeypatch.setattr(jobs,'compute_stock',compute);jobs.drain()
    assert len(connections)==len(closed)==1 and jobs.source_lease is None
    assert jobs.get(job['id'])['done']==1
    expected={'pause':'paused','cancel':'cancelled','error':'failed','stop':'running'}
    assert jobs.get(job['id'])['status']==expected[action]


def test_idle_invalidated_connection_waits_for_budgeted_retry(connected,monkeypatch):
    from datetime import datetime,timedelta,timezone
    jobs,_,_,calendar,connections,closed,engines,refs=connected
    stamp=[datetime(2026,10,31,tzinfo=timezone.utc)]
    monkeypatch.setattr(module,'now',lambda:stamp[0].isoformat())
    job=new(jobs,calendar);original=jobs.compute_stock;invalidated=[]
    def compute(*args):
        result=original(*args)
        if not invalidated:
            invalidated.append(True);jobs.source_lease.connection.invalidate()
        return result
    monkeypatch.setattr(jobs,'compute_stock',compute);jobs.drain()
    current=jobs.get(job['id'])
    assert current['status']=='retry_wait' and current['done']==1
    assert current['auto_retry_count']==0 and len(connections)==len(closed)==1
    assert jobs.source_lease is None and not jobs.drain()
    stamp[0]+=timedelta(seconds=60);jobs.drain()
    current=jobs.get(job['id'])
    assert current['status']=='completed' and current['done']==3 and current['auto_retry_count']==0
    assert len(connections)==len(closed)==2
