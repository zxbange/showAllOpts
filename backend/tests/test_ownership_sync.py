from datetime import date, datetime
import pytest
from sqlalchemy import event, select
from app.core.cache import SnapshotCache
from app.core.config import Settings
from app.core.ownership_sync import HEAD, CHANGE, CURSOR, read_cursor
from app.models.ownership import OwnershipReport, OwnershipHolders
from test_ownership_collector import mod

STAMP = datetime(2026,10,21)
MODULE = 'institution-ownership'

@pytest.fixture
def journal(mod, engine, tmp_path):
    from sqlalchemy.orm import sessionmaker
    ns=mod._ownership_strategy({'finance':None,'query':None},mod.DB_CONFIG)
    ns.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine)
    ns.install_change_tracking(factory)
    with factory() as s:
        s.add(ns.OwnershipStock(code='600001.XSHG',name='示例',updated_at=STAMP))
        s.add(ns.OwnershipReport(code='600001.XSHG',report_date=date(2026,9,30),payload={'value':1},updated_at=STAMP))
        s.add(ns.OwnershipHolders(code='600001.XSHG',end_date=date(2026,6,30),pub_date=None,payload={'a_share_holders':100},updated_at=STAMP))
        s.commit()
    settings=Settings(_env_file=None,cache_enabled=True,cache_auto_refresh=False,cache_dir=tmp_path/'cache',log_dir=tmp_path/'log')
    cache=SnapshotCache(settings,engine)
    return ns,factory,cache

def entry(cache):
    return cache.read_state()['modules'][MODULE]

def cursor(cache):
    return read_cursor(cache.snapshot_path(MODULE,entry(cache)))

def test_unchanged_does_not_scan_history_copy_or_advance_data_timestamp(journal,engine,monkeypatch):
    _,factory,cache=journal
    assert cache.refresh(MODULE,background=False)
    before=entry(cache)
    assert before['rows']==3 and cursor(cache)['revision']==1
    calls=[]
    def capture(conn,cur,sql,*args):calls.append(sql)
    event.listen(engine,'before_cursor_execute',capture)
    monkeypatch.setattr('app.core.ownership_sync.shutil.copyfile',lambda *a:pytest.fail('unchanged snapshot was copied'))
    cache.refresh(MODULE,background=False)
    assert entry(cache)['file']==before['file'] and entry(cache)['updated_at']==before['updated_at']
    assert not entry(cache)['error']
    assert not any('FROM ownership_report' in s or 'FROM ownership_stock' in s or 'FROM ownership_holders' in s for s in calls)
    assert not any('FROM ownership_change' in s for s in calls)
    event.remove(engine,'before_cursor_execute',capture)

def test_changed_keys_only_including_same_timestamp_correction_backfill_delete(journal,engine,monkeypatch):
    ns,factory,cache=journal
    cache.refresh(MODULE,background=False)
    with factory() as s:
        r=s.query(ns.OwnershipReport).one();r.payload={'value':2} # intentionally same updated_at
        s.delete(s.query(ns.OwnershipHolders).one())
        for month in (1,2,3,4,5):
            s.add(ns.OwnershipHolders(code='600001.XSHG',end_date=date(2025,month,1),pub_date=None,payload={'a_share_holders':None},updated_at=STAMP))
        s.commit()
    calls=[]
    event.listen(engine,'before_cursor_execute',lambda conn,cur,sql,*args:calls.append(sql))
    monkeypatch.setattr('app.core.cache_sync.READ_PAGE_SIZE',2)
    cache.refresh(MODULE,background=False)
    assert not entry(cache)['error'] and entry(cache)['rows']==7
    assert cursor(cache)['revision']==2
    with cache.session(MODULE) as local:
        assert local.query(OwnershipReport).one().payload=={'value':2}
        assert local.query(OwnershipHolders).count()==5
        assert all(r.payload['a_share_holders'] is None for r in local.query(OwnershipHolders))
    business=[s for s in calls if s.lstrip().startswith('SELECT') and ('FROM ownership_report' in s or 'FROM ownership_holders' in s)]
    assert business and all(' IN ' in sql for sql in business)
    assert not any('FROM ownership_stock' in sql for sql in calls)

def test_transaction_revision_coalescing_expunge_and_rollback(journal,engine):
    ns,factory,cache=journal
    with factory() as s:
        row=s.query(ns.OwnershipReport).one();row.payload={'value':2};s.flush();s.expunge_all()
        row=s.query(ns.OwnershipReport).one();row.payload={'value':3};s.flush();s.commit()
        assert s.query(ns.SyncHead).one().revision==2
        change=s.query(ns.Change).filter_by(table_name='ownership_report').one()
        assert change.revision==2 and change.deleted==0
        row.payload={'value':4};s.flush();s.rollback()
        assert s.query(ns.SyncHead).one().revision==2
        assert s.query(ns.OwnershipReport).one().payload=={'value':3}
        assert s.query(ns.Change).filter_by(table_name='ownership_report').one().revision==2
        row=s.query(ns.OwnershipReport).one();s.delete(row);s.flush();s.commit()
        assert s.query(ns.SyncHead).one().revision==3
        assert s.query(ns.Change).filter_by(table_name='ownership_report').one().deleted==1
        assert s.query(ns.Change).count()==3 # latest key state, not unbounded append-only events

def test_generation_reset_and_forced_full_rebuild(journal,engine):
    ns,factory,cache=journal
    cache.refresh(MODULE,background=False);before=entry(cache)['file']
    with engine.begin() as c:
        c.execute(HEAD.update().values(generation='reset',revision=0))
        c.execute(OwnershipReport.__table__.update().values(payload={'value':99}))
    cache.refresh(MODULE,background=False)
    assert entry(cache)['file']!=before and cursor(cache)['generation']=='reset'
    with cache.session(MODULE) as local: assert local.query(OwnershipReport).one().payload=={'value':99}
    before=entry(cache)['file'];cache.refresh(MODULE,background=False,full=True)
    assert entry(cache)['file']!=before and not entry(cache)['error']

def test_bad_change_keeps_old_snapshot_and_cursor(journal,engine):
    ns,factory,cache=journal
    cache.refresh(MODULE,background=False);before=entry(cache).copy()
    with engine.begin() as c:
        c.execute(HEAD.update().values(revision=2))
        c.execute(CHANGE.insert().values(table_name='ownership_report',code='missing',record_date=date(2026,9,30),revision=2,deleted=0))
    cache.refresh(MODULE,background=False)
    assert entry(cache)['error'] and entry(cache)['file']==before['file']
    assert cursor(cache)['revision']==1

def test_legacy_upgrade_and_journal_loss_invalidates_cursor(journal,engine):
    ns,factory,cache=journal
    # Simulate old collector first: no head, normal key/timestamp reconciliation.
    with engine.begin() as c:c.execute(HEAD.delete())
    cache.refresh(MODULE,background=False)
    assert not entry(cache)['error'] and cursor(cache) is None
    with engine.begin() as c:c.execute(HEAD.insert().values(id='main',generation='new',revision=1))
    cache.refresh(MODULE,background=False)
    assert cursor(cache)['revision']==1
    # If metadata is lost, a fallback snapshot must not inherit a certified cursor.
    with engine.begin() as c:c.execute(HEAD.delete())
    cache.refresh(MODULE,background=False)
    assert not entry(cache)['error'] and cursor(cache) is None


def test_missing_journal_does_not_silently_advance_cursor(journal,engine):
    _,_,cache=journal
    cache.refresh(MODULE,background=False);before=entry(cache)['file']
    with engine.begin() as c:c.execute(HEAD.update().values(revision=2))
    cache.refresh(MODULE,background=False)
    assert entry(cache)['error'] and entry(cache)['file']==before
    assert cursor(cache)['revision']==1
