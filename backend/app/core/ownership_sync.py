"""Ownership snapshots use transactional change keys instead of full history scans.

The collector owns these source tables. A local cursor lives inside the immutable
SQLite snapshot, so publication of data and cursor is atomic. Legacy collectors
without a journal fall back to the general reconciliation path.
"""
import logging
from app.core.sync_progress import report
import shutil
import uuid

from sqlalchemy import BigInteger, Column, Date, Integer, MetaData, String, Table, create_engine, inspect, select
from sqlalchemy.pool import NullPool

from app.core.cache_errors import SnapshotDataError
from app.core.cache_sync import chunks, key_filter, read_batches, transfer
from app.models.ownership import OwnershipStock, OwnershipReport, OwnershipHolders

logger = logging.getLogger(__name__)
metadata = MetaData()
HEAD = Table('ownership_sync_head', metadata,
             Column('id', String(16), primary_key=True), Column('generation', String(32), nullable=False),
             Column('revision', BigInteger, nullable=False))
CHANGE = Table('ownership_change', metadata,
               Column('table_name', String(32), primary_key=True), Column('code', String(16), primary_key=True),
               Column('record_date', Date, primary_key=True), Column('revision', BigInteger, nullable=False),
               Column('deleted', Integer, nullable=False))
CURSOR = Table('ownership_cache_cursor', metadata,
               Column('id', Integer, primary_key=True), Column('generation', String(32), nullable=False),
               Column('revision', BigInteger, nullable=False), Column('rows', BigInteger, nullable=False))
TABLES = [m.__table__ for m in (OwnershipStock, OwnershipReport, OwnershipHolders)]
TABLE_MAP = {t.name: t for t in TABLES}


def read_cursor(path):
    if path is None:
        return None
    local = create_engine('sqlite:///file:' + path.as_posix() + '?mode=ro&uri=true', poolclass=NullPool)
    try:
        with local.connect() as connection:
            if not inspect(connection).has_table(CURSOR.name):
                return None
            return connection.execute(select(CURSOR).where(CURSOR.c.id == 1)).mappings().first()
    finally:
        local.dispose()


def reconcile_changes(source, target, cursor, head):
    """Apply latest changed keys, including tombstones, from one consistent source snapshot."""
    count = cursor['rows']
    columns = list(CHANGE.columns)
    ordering = [CHANGE.c.revision, CHANGE.c.table_name, CHANGE.c.code, CHANGE.c.record_date]
    condition = (CHANGE.c.revision > cursor['revision']) & (CHANGE.c.revision <= head['revision'])
    downloaded = removed = seen = 0
    for page in read_batches(source, columns, ordering, condition):
        seen += len(page)
        groups = {}
        for record in page:
            row = record._mapping
            name = row['table_name']
            if name not in TABLE_MAP or row['deleted'] not in (0, 1):
                raise SnapshotDataError('机构持仓变更记录无效，请检查采集脚本并重新同步。')
            key = (row['code'],) if name == OwnershipStock.__tablename__ else (row['code'], row['record_date'])
            groups.setdefault(name, []).append((key, row['deleted']))
        for name, records in groups.items():
            table = TABLE_MAP[name]
            for batch in chunks(records):
                keys = [key for key, _ in batch]
                deleted = target.execute(table.delete().where(key_filter(table, keys))).rowcount
                live = [key for key, tombstone in batch if not tombstone]
                fetched = transfer(source, target, table, key_filter(table, live)) if live else 0
                if fetched != len(live):
                    raise SnapshotDataError('机构持仓变更记录与源数据不一致，请执行完整同步复核。')
                count += fetched - deleted
                downloaded += fetched
                removed += sum(tombstone for _, tombstone in batch)
    if not seen:
        raise SnapshotDataError('机构持仓版本已变化但变更记录缺失，请执行完整同步复核。')
    logger.info('Cache ownership journal from_revision=%s to_revision=%s downloaded=%s tombstones=%s rows=%s',
                cursor['revision'], head['revision'], downloaded, removed, count)
    return count


def copy_ownership(cache, full=False):
    """Return (filename, rows), or None when legacy metadata reconciliation is required."""
    previous = cache.snapshot_path('institution-ownership', cache.read_state()['modules'].get('institution-ownership', {}))
    cursor = read_cursor(previous) if not full else None
    with cache.source.connect() as source:
        if cache.source.dialect.name == 'mysql':
            source = source.execution_options(isolation_level='REPEATABLE READ')
        with source.begin():
            inspector = inspect(source)
            if not inspector.has_table(HEAD.name) or not inspector.has_table(CHANGE.name):
                logger.info('Cache ownership journal unavailable; using legacy reconciliation')
                return None
            head = source.execute(select(HEAD).where(HEAD.c.id == 'main')).mappings().first()
            if head is None:
                return None
            if head['revision'] < 0 or not head['generation']:
                raise SnapshotDataError('机构持仓同步版本信息无效。')
            incremental = (cursor is not None and cursor['generation'] == head['generation']
                           and 0 <= cursor['revision'] <= head['revision'])
            if incremental and cursor['revision'] == head['revision']:
                logger.info('Cache ownership unchanged revision=%s; keeping existing snapshot', head['revision'])
                return previous.name, cursor['rows']
            filename = 'institution-ownership-' + uuid.uuid4().hex + '.sqlite'
            path = cache.root / filename
            destination = create_engine('sqlite:///' + path.as_posix(), poolclass=NullPool)
            try:
                report('准备机构持仓本地快照', percent=5)
                if incremental:
                    shutil.copyfile(previous, path)
                TABLES[0].metadata.create_all(destination, tables=TABLES)
                CURSOR.create(destination, checkfirst=True)
                with destination.begin() as target:
                    report('同步机构持仓', percent=10)
                    if incremental:
                        count = reconcile_changes(source, target, cursor, head)
                    else:
                        count = 0
                        for index, table in enumerate(TABLES):
                            count += transfer(source, target, table)
                            report('数据表完成 ' + table.name, percent=10 + 80*(index+1)/len(TABLES))
                        logger.info('Cache ownership baseline downloaded=%s revision=%s', count, head['revision'])
                    report('提交机构持仓数据', percent=90)
                    target.execute(CURSOR.delete())
                    target.execute(CURSOR.insert().values(id=1, generation=head['generation'], revision=head['revision'], rows=count))
                report('校验机构持仓快照', percent=95)
                with destination.connect() as connection:
                    if connection.exec_driver_sql('PRAGMA quick_check').scalar() != 'ok':
                        raise SnapshotDataError('机构持仓本地快照校验失败。')
                return filename, count
            except BaseException:
                destination.dispose()
                path.unlink(missing_ok=True)
                raise
            finally:
                destination.dispose()
