"""Date cursors for collectors whose normal runs only append within a partition.

The cursor lives in the published SQLite snapshot, never in independent state.json.
Legacy snapshots are rebuilt once: a local MAX alone cannot certify completeness.
Historical corrections/deletions inside the retained range require full=True.
"""
import logging
import shutil

from sqlalchemy import Column, Date, Integer, MetaData, String, Table, create_engine, func, select
from sqlalchemy.pool import NullPool

from app.core.cache_sync import transfer
from app.core.sync_progress import report

logger = logging.getLogger(__name__)
APPEND_PARTITIONS = {
    'industry-momentum': 'industry_type',
    'market-crowding': 'top_pct',
    'etf-shares': 'fund_code',
    'microcap-breadth': 'series_id',
}
_metadata = MetaData()
_cursor = Table('append_cache_cursor_v1', _metadata,
                Column('partition', String, primary_key=True),
                Column('first_date', Date, nullable=False),
                Column('successful_date', Date, nullable=False))
# A separate marker certifies even an empty snapshot.
_marker = Table('append_cache_version', _metadata,
                Column('version', Integer, primary_key=True),
                Column('total_rows', Integer, nullable=False))


def read_cursor(connection):
    names = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars().all()
    if _cursor.name not in names or _marker.name not in names:
        return None
    if connection.scalar(select(_marker.c.version)) != 1:
        return None
    return {row[0]: (row[1], row[2]) for row in connection.execute(select(_cursor))}


def copy_append(cache, module, path, previous, full=False):
    """Return (filename, count), reusing previous on a verified no-op.

    Bounds, payload and cursor are read in one repeatable-read source transaction;
    local changes + cursor commit together, and publication is owned by the caller.
    """
    from app.core.cache import MODULES, CacheUnavailable
    table = MODULES[module][1][0].__table__
    partition = table.c[APPEND_PARTITIONS[module]]
    day = table.c.trade_date
    old = None
    count = 0
    if previous is not None and not full:
        local = create_engine('sqlite:///file:' + previous.as_posix() + '?mode=ro&uri=true', poolclass=NullPool)
        try:
            with local.connect() as connection:
                old = read_cursor(connection)
                if old is not None:
                    count = connection.scalar(select(_marker.c.total_rows))
        finally:
            local.dispose()
    with cache.source.connect() as source:
        if source.dialect.name == 'mysql':
            source = source.execution_options(isolation_level='REPEATABLE READ')
        with source.begin():
            report('读取分类同步日期', percent=10)
            # Bounded metadata output (one row per category), not a PK/timestamp manifest.
            # On MySQL a (partition, trade_date) index supports this aggregate efficiently.
            bounds = list(source.execute(select(partition, func.min(day), func.max(day)).group_by(partition)))
            current = {str(key): (first, last) for key, first, last in bounds}
            if old is not None and old == current:
                logger.info('Cache append no-op module=%s partitions=%s copied_bytes=0', module, len(current))
                return previous.name, count
            destination = create_engine('sqlite:///' + path.as_posix(), poolclass=NullPool)
            try:
                if old is not None:
                    shutil.copyfile(previous, path)
                table.metadata.create_all(destination, tables=[table])
                _metadata.create_all(destination)
                with destination.begin() as target:
                    if old is not None:
                        # Removed categories are also retention, including a completely empty source.
                        for key in old.keys() - current.keys():
                            target.execute(table.delete().where(partition == key))
                    fetched = 0
                    for key, first, last in bounds:
                        prior = old.get(str(key)) if old is not None else None
                        condition = partition == key
                        # A regressed boundary means source reset/reseed; rebuild just this partition.
                        rebuild = prior is None or first < prior[0] or last < prior[1]
                        if rebuild:
                            target.execute(table.delete().where(condition))
                        else:
                            target.execute(table.delete().where(condition, day < first))
                            condition = condition & (day > prior[1])
                        if rebuild or last > prior[1]:
                            fetched += transfer(source, target, table, condition & (day >= first) & (day <= last))
                    target.execute(_cursor.delete())
                    if current:
                        target.execute(_cursor.insert(), [dict(partition=key, first_date=first, successful_date=last)
                                                        for key, (first, last) in current.items()])
                    target.execute(_marker.delete())
                    if module == 'industry-momentum':
                        target.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_local_momentum_type_date ON industry_momentum (industry_type, trade_date)')
                        target.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_local_momentum_history ON industry_momentum (industry_type, industry_code, trade_date)')
                    count = target.scalar(select(func.count()).select_from(table))
                    target.execute(_marker.insert().values(version=1, total_rows=count))
                report('校验本地快照', percent=95)
                with destination.connect() as target:
                    if target.exec_driver_sql('PRAGMA quick_check').scalar() != 'ok':
                        raise CacheUnavailable('Local snapshot validation failed')
                logger.info('Cache append sync module=%s partitions=%s downloaded=%s total=%s', module, len(current), fetched, count)
                return path.name, count
            except BaseException:
                destination.dispose()
                path.unlink(missing_ok=True)
                raise
            finally:
                destination.dispose()
