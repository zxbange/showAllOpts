"""Offline protocol repair: both collectors, history, migration and cache safety."""
from datetime import date
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine, select, event
from sqlalchemy.orm import sessionmaker
from app.core.earnings_sync import sync_quarters, VERSION
from app.core.cache_errors import SnapshotDataError
from app.models.earnings import EarningsRun, EarningsStock
from test_earnings_collector import c, record
from test_collector_batch import batch, CONFIG

@pytest.fixture(params=['standalone', 'batch'])
def subject(request, c, batch, tmp_path):
    if request.param == 'standalone':
        yield c
    else:
        ns = batch.STRATEGIES['净利润断层']({}, CONFIG)
        env = vars(ns).copy()
        for key in ('_migrate_earnings_versions', '_earnings_version_table', '_earnings_migration_table'):
            env[key] = getattr(batch, key)
        engine = create_engine('sqlite:///' + str(tmp_path / 'batch-source.sqlite'))
        ns.Base.metadata.create_all(engine)
        yield SimpleNamespace(env=env, engine=engine, Session=sessionmaker(bind=engine))
        engine.dispose()


def save(c, items, period=None):
    p = period or date(2026,9,30)
    return c.env['save_results'](c.Session,p,date(2026,10,16),6000,items,skip_existing=True)


def version(c, p=date(2026,9,30)):
    with c.engine.connect() as db:
        return dict(db.execute(select(VERSION).where(VERSION.c.report_period==p)).mappings().one())


def add_unversioned(c, item):
    with c.Session() as db:
        db.add(c.env['EarningsStock'](**c.env['database_record'](item)))
        db.commit()


def test_count_only_repair_is_generation_change_and_noop_is_stable(subject):
    c=subject; a=record(c); save(c,[a]); before=version(c)
    add_unversioned(c, record(c,code='000001.XSHE'))
    assert save(c,[]) == []
    after=version(c)
    assert after['generation'] != before['generation'] and after['revision']==before['revision']+1
    assert save(c,[]) == [] and version(c)==after
    with c.Session() as db:
        assert db.get(c.env['EarningsRun'],a['report_period']).matched_count==2


def test_missing_rows_cannot_be_hidden_by_lowering_count(subject):
    c=subject; a=record(c); save(c,[a]); before=version(c)
    with c.Session() as db:
        db.query(c.env['EarningsStock']).delete();db.commit()
    with pytest.raises(ValueError, match='拒绝调低'):
        save(c,[])
    assert version(c)==before
    with c.Session() as db:
        assert db.get(c.env['EarningsRun'],a['report_period']).matched_count==1
    with pytest.raises(ValueError, match='数据不完整'):
        c.env['_migrate_earnings_versions'](c.Session)
    assert version(c)==before


def test_existing_corrected_counts_and_old_quarters_migrate_once_and_sync(subject):
    c=subject; periods=[date(2026,6,30),date(2026,9,30)]
    target=create_engine('sqlite://')
    for t in (EarningsRun.__table__,EarningsStock.__table__):t.create(target)
    def sync(full=False):
        with c.engine.connect() as src,target.begin() as dst:
            sync_quarters(src,dst,[EarningsRun.__table__,EarningsStock.__table__],not full)
    for p in periods:save(c,[record(c,period=p)],p)
    sync(True)
    # Reproduce old collectors that already corrected counts but not versions.
    for p in periods:
        add_unversioned(c,record(c,period=p,code='000001.XSHE'))
        with c.Session() as db:
            db.get(c.env['EarningsRun'],p).matched_count=2;db.commit()
    with pytest.raises(SnapshotDataError, match='2026-06-30.*源数量=2.*缓存数量=1'):
        sync()
    before={p:version(c,p) for p in periods}
    c.env['_migrate_earnings_versions'](c.Session)
    after={p:version(c,p) for p in periods}
    assert all(after[p]['generation']!=before[p]['generation'] for p in periods)
    sync();sync()
    with target.connect() as db:
        assert len(db.execute(select(EarningsStock.code)).all())==4
    writes=[]
    def capture(conn,cursor,sql,*args):
        if sql.lstrip().startswith(('INSERT','UPDATE','DELETE')):writes.append(sql)
    event.listen(c.engine,'before_cursor_execute',capture)
    c.env['_migrate_earnings_versions'](sessionmaker(bind=c.engine))
    event.remove(c.engine,'before_cursor_execute',capture)
    assert not writes and {p:version(c,p) for p in periods}==after
    # Ordinary append remains append-only after migration.
    save(c,[record(c,code='000002.XSHE')]);sync()
    assert version(c)['generation']==after[periods[-1]]['generation']
    target.dispose()


def test_migration_marker_and_generation_rollback_together(subject):
    c=subject;save(c,[record(c)]);before=version(c)
    def deny(conn,cursor,sql,*args):
        if sql.startswith('INSERT INTO earnings_sync_migration'):
            raise RuntimeError('synthetic failure')
    event.listen(c.engine,'before_cursor_execute',deny)
    with pytest.raises(RuntimeError):c.env['_migrate_earnings_versions'](c.Session)
    event.remove(c.engine,'before_cursor_execute',deny)
    assert version(c)==before
    with c.engine.connect() as db:
        assert db.execute(select(c.env['_earnings_migration_table']())).first() is None
    c.env['_migrate_earnings_versions'](c.Session)
    assert version(c)['generation']!=before['generation']


def test_invalid_existing_snapshot_not_promoted(subject):
    c=subject;save(c,[record(c)]);before=version(c)
    with c.Session() as db:
        db.query(c.env['EarningsStock']).update({'profit_yoy':-1});db.commit()
    with pytest.raises(ValueError,match='无效股票'):
        c.env['_migrate_earnings_versions'](c.Session)
    assert version(c)==before


def test_versioned_source_count_mismatch_blocks_even_full_sync(subject):
    c=subject;save(c,[record(c)])
    with c.Session() as db:
        db.get(c.env['EarningsRun'],date(2026,9,30)).matched_count=2;db.commit()
    target=create_engine('sqlite://')
    for t in (EarningsRun.__table__,EarningsStock.__table__):t.create(target)
    with c.engine.connect() as src,target.begin() as dst,pytest.raises(SnapshotDataError,match='声明数量=2，实际数量=1'):
        sync_quarters(src,dst,[EarningsRun.__table__,EarningsStock.__table__],False)
    target.dispose()


def test_legacy_unversioned_quarter_migrates_only_after_validation(subject):
    c=subject;save(c,[record(c)])
    marker=c.env['_earnings_migration_table']()
    with c.engine.begin() as db:
        db.execute(VERSION.delete());db.execute(marker.delete())
        db.execute(EarningsRun.__table__.update().values(matched_count=2))
    # DDL initialization does not publish an unverified version.
    factory=sessionmaker(bind=c.engine)
    with pytest.raises(ValueError,match='数据不完整'):
        c.env['_migrate_earnings_versions'](factory)
    with c.engine.connect() as db:
        assert db.execute(select(VERSION)).first() is None
        assert db.execute(select(marker)).first() is None
    add_unversioned(c,record(c,code='000001.XSHE'))
    c.env['_migrate_earnings_versions'](factory)
    first=version(c)
    assert first['revision']==1
    c.env['_migrate_earnings_versions'](factory)
    assert version(c)==first


def test_count_repair_rolls_back_without_partial_metadata_or_version(subject):
    c=subject;a=record(c);save(c,[a]);before=version(c)
    add_unversioned(c,record(c,code='000001.XSHE'))
    def deny(conn,cursor,sql,*args):
        if sql.startswith('UPDATE earnings_sync_head'):
            raise RuntimeError('synthetic version write failure')
    event.listen(c.engine,'before_cursor_execute',deny)
    with pytest.raises(RuntimeError):save(c,[])
    event.remove(c.engine,'before_cursor_execute',deny)
    assert version(c)==before
    with c.Session() as db:
        assert db.get(c.env['EarningsRun'],a['report_period']).matched_count==1
        assert db.query(c.env['EarningsStock']).count()==2
    save(c,[])
    assert version(c)['generation']!=before['generation']
