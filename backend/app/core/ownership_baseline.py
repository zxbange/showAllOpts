"""Durable unpublished ownership baseline, repaired by the source change journal.

Each page and its seek key commit together. Across source snapshots, replay every
key changed since the baseline revision before publishing. All writers must use
the transactional journal (the same requirement as incremental synchronization).
"""
import logging
import shutil
import uuid

from sqlalchemy import BigInteger, Column, Date, Integer, MetaData, String, Table, create_engine, select
from sqlalchemy.pool import NullPool

from app.core.cache_errors import SnapshotDataError
from app.core.cache_sync import after_key, read_batches, transfer_plan
from app.core.ownership_sync import CURSOR, TABLES, reconcile_changes, read_cursor
from app.core.sync_progress import report
from app.core.mysql_payload import decode_rows

logger = logging.getLogger(__name__)
CHECKPOINT = Table('ownership_baseline_checkpoint', MetaData(),
    Column('id', Integer, primary_key=True),
    Column('generation', String(32), nullable=False),
    Column('revision', BigInteger, nullable=False),
    Column('anchor', String(160), nullable=False),
    Column('table_index', Integer, nullable=False),
    Column('last_code', String(16)), Column('last_date', Date),
    Column('rows', BigInteger, nullable=False))
PARTIAL_NAME = 'ownership-baseline.partial'


def resume_baseline(cache, source, head, previous):
    path = cache.root / PARTIAL_NAME
    destination = create_engine('sqlite:///' + path.as_posix(), pool_size=1, max_overflow=0)
    anchor = previous.name if previous else ''
    try:
        TABLES[0].metadata.create_all(destination, tables=TABLES)
        CHECKPOINT.create(destination, checkfirst=True)
        CURSOR.create(destination, checkfirst=True)
        with destination.begin() as target:
            saved = target.execute(select(CHECKPOINT)).mappings().first()
            reusable = (saved and saved['generation'] == head['generation']
                        and 0 <= saved['revision'] <= head['revision'] and saved['anchor'] == anchor)
            if not reusable:
                for table in TABLES:
                    target.execute(table.delete())
                target.execute(CURSOR.delete())
                target.execute(CHECKPOINT.delete())
                target.execute(CHECKPOINT.insert().values(id=1, generation=head['generation'],
                    revision=head['revision'], anchor=anchor, table_index=0, rows=0))
                logger.info('Cache ownership baseline checkpoint initialized revision=%s', head['revision'])
            checkpoint = dict(target.execute(select(CHECKPOINT)).mappings().one())
        logger.info('Cache ownership baseline resume table_index=%s saved_rows=%s base_revision=%s',
                    checkpoint['table_index'], checkpoint['rows'], checkpoint['revision'])
        report('续传机构持仓：已保存 {} 行'.format(checkpoint['rows']), percent=10)
        for index in range(checkpoint['table_index'], len(TABLES)):
            table = TABLES[index]
            keys = list(table.primary_key.columns)
            condition = None
            if checkpoint['last_code'] is not None:
                values = [checkpoint['last_code']]
                if len(keys) == 2:
                    values.append(checkpoint['last_date'])
                condition = after_key(keys, values)
            # Keep large reports bounded; compact stock/holder rows use fewer round trips.
            with destination.connect() as target:
                columns, statement, _, compressed = transfer_plan(source, target, table)
            for batch in read_batches(source, columns, keys, condition, page_size=500 if table.name == 'ownership_report' else 2000):
                if cache.stop_event.is_set():
                    raise InterruptedError('Ownership baseline paused')
                last = batch[-1]._mapping
                values = decode_rows(batch, (), compressed)
                count = checkpoint['rows'] + len(values)
                with destination.begin() as target:
                    target.execute(statement, values)
                    target.execute(CHECKPOINT.update().where(CHECKPOINT.c.id == 1).values(
                        last_code=last[keys[0]], last_date=last[keys[1]] if len(keys) == 2 else None, rows=count))
                checkpoint['rows'] = count
                report('已保存断点 ' + table.name + '（{} 行）'.format(count))
            # Exhaustion is durable too, including empty tables.
            with destination.begin() as target:
                target.execute(CHECKPOINT.update().where(CHECKPOINT.c.id == 1).values(
                    table_index=index+1, last_code=None, last_date=None))
            checkpoint.update(table_index=index+1, last_code=None, last_date=None)
            report('数据表完成 ' + table.name, percent=10 + 65*(index+1)/len(TABLES))
        if cache.stop_event.is_set():
            raise InterruptedError('Ownership baseline paused')
        report('核对续传期间的数据变更', percent=80)
        with destination.begin() as target:
            count = checkpoint['rows']
            if checkpoint['revision'] < head['revision']:
                count = reconcile_changes(source, target, checkpoint, head)
            target.execute(CURSOR.delete())
            target.execute(CURSOR.insert().values(id=1, generation=head['generation'], revision=head['revision'], rows=count))
            target.execute(CHECKPOINT.update().where(CHECKPOINT.c.id == 1).values(revision=head['revision'], rows=count))
        report('校验续传快照', percent=95)
        with destination.connect() as target:
            if target.exec_driver_sql('PRAGMA quick_check').scalar() != 'ok':
                raise SnapshotDataError('机构持仓续传文件校验失败，请检查本地磁盘。')
        # Preserve the checkpoint even if copying or publishing state.json fails.
        # A published snapshot is always a different immutable file.
        filename = 'institution-ownership-' + uuid.uuid4().hex + '.sqlite'
        published = cache.root / filename
        try:
            shutil.copyfile(path, published)
            final = create_engine('sqlite:///' + published.as_posix(), poolclass=NullPool)
            try:
                with final.begin() as target:
                    CHECKPOINT.drop(target)
            finally:
                final.dispose()
        except BaseException:
            published.unlink(missing_ok=True)
            raise
        logger.info('Cache ownership baseline complete rows=%s revision=%s', count, head['revision'])
        return filename, count
    finally:
        # Never discard a committed page on disconnect or process restart.
        destination.dispose()


def cleanup_completed_baseline(cache, state):
    """Only remove staging after a matching final snapshot is durably published."""
    path = cache.root / PARTIAL_NAME
    if not path.is_file():
        return
    published = cache.snapshot_path('institution-ownership', state['modules'].get('institution-ownership', {}))
    if published is None:
        return
    staging = create_engine('sqlite:///' + path.as_posix(), poolclass=NullPool)
    try:
        with staging.connect() as connection:
            checkpoint = connection.execute(select(CHECKPOINT)).mappings().one()
        current = read_cursor(published)
        complete = (current and checkpoint['table_index'] == len(TABLES)
                    and checkpoint['anchor'] != published.name
                    and checkpoint['generation'] == current['generation']
                    and checkpoint['revision'] == current['revision']
                    and checkpoint['rows'] == current['rows'])
        staging.dispose()
        if complete:
            path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning('Cache ownership checkpoint cleanup deferred error_type=%s', type(exc).__name__)
    finally:
        staging.dispose()
