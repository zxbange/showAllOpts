from datetime import date, datetime
import logging
import pytest
from sqlalchemy import select, event
from sqlalchemy.orm import sessionmaker
from app.core.cache import SnapshotCache, SyncCancelled
from app.core.config import Settings
from app.core.result_sync import CURSOR, HEAD
from app.core.etf_snapshot import validate_etf_snapshot
from app.models.etf_heat import Heat, Pool
from test_result_journal_sync import journal

DAY = date(2026,10,30)
MODULE = 'etf-momentum'

def row(code):
    return Heat(trade_date=DAY,code=code,name='mock',score=None,details={},updated_at=datetime(2026,10,30))

@pytest.fixture
def scenario(engine,tmp_path):
    writer=sessionmaker(bind=engine)
    journal._install_result_journal(writer,(Heat,Pool),MODULE)
    with writer.begin() as s:
        s.add(row('A'));s.add(Pool(id=1,as_of_date=DAY,rule_version='etf_close_momentum_v3',members=[{'code':'A'}],updated_at=datetime(2026,10,30)))
    cache=SnapshotCache(Settings(_env_file=None,cache_enabled=True,cache_auto_refresh=False,log_dir=tmp_path/'log'),engine)
    cache.refresh(MODULE,background=False)
    before=cache.read_state()['modules'][MODULE].copy()
    assert not before['error']
    return cache,writer,engine,before


def publish_missing(writer,engine,complete=True):
    if complete:
        with engine.begin() as c:c.execute(Heat.__table__.insert().values(trade_date=DAY,code='B',name='mock',details={},updated_at=datetime(2026,10,30)))
    with writer.begin() as s:s.get(Pool,1).members=[{'code':'A'},{'code':'B'}]


def entry(cache):return cache.read_state()['modules'][MODULE]


def assert_preserved(cache,before):
    assert entry(cache)['file']==before['file']
    with cache.session(MODULE) as s:
        assert s.scalar(select(CURSOR.c.revision))==1
        assert set(s.scalars(select(Heat.code)))=={'A'}
    assert len(list(cache.root.glob('etf-momentum-*.sqlite')))==1


def test_unjournalled_heat_recovers_once_and_future_incremental_works(scenario,caplog):
    cache,writer,engine,before=scenario;publish_missing(writer,engine)
    with caplog.at_level(logging.INFO):cache.refresh(MODULE,background=False)
    assert not entry(cache)['error'] and '自动重建' in entry(cache)['sync_note']
    assert 'mode=recovered-baseline' in caplog.text and 'missing=1' in caplog.text
    with cache.session(MODULE) as s:
        assert set(s.scalars(select(Heat.code)))=={'A','B'}
        assert s.scalar(select(CURSOR.c.revision))==2
    path=entry(cache)['file'];statements=[]
    def capture(c,cur,sql,*args):statements.append(sql)
    event.listen(engine,'before_cursor_execute',capture)
    try:cache.refresh(MODULE,background=False)
    finally:event.remove(engine,'before_cursor_execute',capture)
    assert entry(cache)['file']==path
    assert not any('FROM etf_momentum_' in sql for sql in statements)
    with writer.begin() as s:
        s.add(row('C'));s.get(Pool,1).members=[{'code':c} for c in 'ABC']
    cache.refresh(MODULE,background=False)
    assert not entry(cache)['error'] and entry(cache)['sync_note'] is None
    with cache.session(MODULE) as s:assert set(s.scalars(select(Heat.code)))==set('ABC')


def test_unjournalled_pool_difference_uses_source_pool(scenario):
    cache,writer,engine,before=scenario
    with writer.begin() as s:s.add(row('B'));s.get(Pool,1).members=[{'code':c} for c in 'AB']
    cache.refresh(MODULE,background=False)
    with writer.begin() as s:journal._result_bulk(s.query(Heat).filter_by(code='B'))
    with engine.begin() as c:c.execute(Pool.__table__.update().values(members=[{'code':'A'}]))
    cache.refresh(MODULE,background=False)
    assert not entry(cache)['error'] and entry(cache)['sync_note']
    with cache.session(MODULE) as s:assert s.get(Pool,1).members==[{'code':'A'}]


def test_true_source_missing_preserves_snapshot_and_cursor(scenario,caplog):
    cache,writer,engine,before=scenario;publish_missing(writer,engine,False)
    with caplog.at_level(logging.WARNING):cache.refresh(MODULE,background=False)
    assert 'SOURCE_DATA_INCOMPLETE' in entry(cache)['error']
    assert 'recovery rejected' in caplog.text
    assert_preserved(cache,before)


@pytest.mark.parametrize('failure',[OSError('mock'),SyncCancelled(),ValueError('mock')])
def test_rebuild_failure_or_cancel_preserves_old_snapshot(scenario,monkeypatch,failure):
    import app.core.result_sync as sync
    cache,writer,engine,before=scenario;publish_missing(writer,engine)
    original=sync.transfer;rebuild=[]
    def broken(source,target,table,condition=None):
        if condition is None:
            rebuild.append(table.name)
            if table.name==Heat.__tablename__:raise failure
        return original(source,target,table,condition)
    monkeypatch.setattr(sync,'transfer',broken);cache.refresh(MODULE,background=False)
    assert rebuild==[Pool.__tablename__,Heat.__tablename__]
    assert_preserved(cache,before)
    monkeypatch.setattr(sync,'transfer',original);cache.refresh(MODULE,background=False)
    assert not entry(cache)['error']


@pytest.mark.parametrize('members',[[],{},[{'code':'A'},{'code':'A'}],[{}],None])
def test_invalid_source_pool_cannot_be_successfully_rebuilt(scenario,members):
    cache,writer,engine,before=scenario
    # Keep the candidate pool at A while logging A's removal. Source pool is
    # malformed out-of-journal, so only a strict source check can reject it.
    with writer.begin() as s:journal._result_bulk(s.query(Heat))
    with engine.begin() as c:
        if members is None:c.execute(Pool.__table__.delete())
        else:c.execute(Pool.__table__.update().values(members=members))
    cache.refresh(MODULE,background=False)
    assert entry(cache)['error'];assert_preserved(cache,before)


def test_other_incremental_error_never_triggers_rebuild(scenario,monkeypatch):
    import app.core.result_sync as sync
    cache,writer,engine,before=scenario;publish_missing(writer,engine)
    def broken(*args,**kwargs):raise OSError('mock')
    monkeypatch.setattr(sync,'changed_keys',broken)
    monkeypatch.setattr(sync,'validate_etf_snapshot',lambda *a,**k:pytest.fail('unexpected recovery'))
    cache.refresh(MODULE,background=False);assert_preserved(cache,before)


def test_cursor_write_failure_rolls_back_recovery(scenario):
    from sqlalchemy.engine import Engine
    cache,writer,engine,before=scenario;publish_missing(writer,engine)
    def fail_cursor(connection,cursor,statement,*args):
        if connection.dialect.name=='sqlite' and statement.startswith('INSERT INTO result_sync_cursor_v1'):
            raise OSError('mock cursor write failure')
    event.listen(Engine,'before_cursor_execute',fail_cursor)
    try:cache.refresh(MODULE,background=False)
    finally:event.remove(Engine,'before_cursor_execute',fail_cursor)
    assert entry(cache)['error'];assert_preserved(cache,before)
    cache.refresh(MODULE,background=False)
    assert not entry(cache)['error']


def test_rebuilt_snapshot_is_rechecked_without_recovery_loop(scenario,monkeypatch):
    import app.core.result_sync as sync
    cache,writer,engine,before=scenario;publish_missing(writer,engine)
    original=sync.transfer;rebuilt=[]
    def incomplete(source,target,table,condition=None):
        count=original(source,target,table,condition)
        if condition is None and table.name==Heat.__tablename__:
            rebuilt.append(True)
            target.execute(table.delete().where(table.c.code=='B'))
        return count
    monkeypatch.setattr(sync,'transfer',incomplete);cache.refresh(MODULE,background=False)
    assert rebuilt==[True] and entry(cache)['error'];assert_preserved(cache,before)
