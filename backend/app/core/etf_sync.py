"""ETF incremental sync rechecks only the last three cached trading sessions.

Keep the existing cache namespace and share schema. Old sources without the optional
price table remain readable, and old immutable snapshots are upgraded on refresh.
"""
import shutil

from sqlalchemy import create_engine, func, inspect, select, union
from sqlalchemy.pool import NullPool

from app.core.cache_sync import chunks, key_filter, read_batches, transfer
from app.core.sync_progress import report
from app.models.etf import ETFPrice, ETFShare, ETFTradingDay


def manifest(connection, table, since=None):
    # Compare values too: timestamps may have only second precision.
    keys = list(table.primary_key.columns)
    columns = keys + [c for c in table.columns if c not in keys]
    condition = table.c.trade_date >= since if since is not None else None
    return {tuple(row[:len(keys)]): tuple(row[len(keys):])
            for batch in read_batches(connection, columns, keys, condition) for row in batch}


def recent_start(connection, table, names):
    """Each table advances independently so late prices do not follow the share cursor."""
    latest = connection.scalar(select(func.max(table.c.trade_date)))
    if latest is None:
        return None
    dates = select(table.c.trade_date).where(table.c.trade_date <= latest)
    calendar = ETFTradingDay.__table__
    if calendar.name in names and table is not calendar:
        dates = union(dates, select(calendar.c.trade_date).where(calendar.c.trade_date <= latest))
    else:
        dates = dates.distinct()
    recent = list(connection.scalars(dates.order_by(table.c.trade_date.desc()).limit(3)))
    # An initial snapshot with fewer than three sessions has no established window yet.
    return recent[-1] if len(recent) == 3 else None


def copy_etf(cache, path, previous, full=False, *, source=None, identity=None):
    tables = [ETFShare.__table__, ETFPrice.__table__, ETFTradingDay.__table__]
    local = None
    local_maps = {}
    local_names = set()
    bounds = {}
    local_counts = {}
    if previous is not None and not full:
        local = create_engine('sqlite:///file:' + previous.as_posix() + '?mode=ro&uri=true', poolclass=NullPool)
        try:
            with local.connect() as connection:
                local_names = set(inspect(connection).get_table_names())
                for table in tables:
                    if table.name not in local_names:
                        continue
                    bounds[table.name] = recent_start(connection, table, local_names)
                    local_maps[table.name] = manifest(connection, table, bounds[table.name])
                    local_counts[table.name] = connection.scalar(select(func.count()).select_from(table))
        finally:
            local.dispose()
    with (source if source is not None else cache.source).connect() as source:
        if source.dialect.name == 'mysql':
            source = source.execution_options(isolation_level='REPEATABLE READ')
        with source.begin():
            inspector = inspect(source)
            price_exists = inspector.has_table(ETFPrice.__tablename__)
            source_tables = {ETFShare.__tablename__}
            if price_exists:
                source_tables.add(ETFPrice.__tablename__)
            if inspector.has_table(ETFTradingDay.__tablename__):
                source_tables.add(ETFTradingDay.__tablename__)
            cache.sync_notes['etf-shares'] = None if price_exists else '源库尚无 ETF 收盘价表，保留份额展示，市值暂不估算。'
            remote = {table.name: manifest(source, table, bounds.get(table.name)) if table.name in source_tables else {}
                      for table in tables}
            absent = {table.name for table in tables if table.name not in source_tables}
            # A missing optional table means an empty series for every date, not
            # just the incremental window. Counts must also remove the old history.
            count = sum(local_counts.values()) + sum(
                len(remote[t.name]) - (local_counts.get(t.name, 0) if t.name in absent
                                      else len(local_maps.get(t.name, {}))) for t in tables)
            missing_rows = any(local_counts.get(name, 0) for name in absent)
            if (not full and previous is not None and not missing_rows
                    and all(t.name in local_names for t in tables) and remote == local_maps):
                return previous.name, count
            destination = create_engine('sqlite:///' + path.as_posix(), poolclass=NullPool)
            try:
                if previous is not None and not full:
                    shutil.copyfile(previous, path)
                tables[0].metadata.create_all(destination, tables=tables)
                with destination.begin() as target:
                    for table in tables:
                        if table.name in absent:
                            # Only mutate the unpublished copy. An empty table also
                            # resets its own cursor, so restoration backfills history.
                            # Inspection/query errors still abort the whole refresh.
                            target.execute(table.delete())
                            continue
                        old, new = local_maps.get(table.name, {}), remote[table.name]
                        changed = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
                        for batch in chunks(changed):
                            target.execute(table.delete().where(key_filter(table, batch)))
                        for batch in chunks(changed & new.keys()):
                            transfer(source, target, table, key_filter(table, batch))
                    if identity is not None:
                        target.exec_driver_sql('CREATE TABLE IF NOT EXISTS etf_cache_source_v1 (id INTEGER PRIMARY KEY, identity TEXT NOT NULL)')
                        target.exec_driver_sql('DELETE FROM etf_cache_source_v1')
                        target.exec_driver_sql('INSERT INTO etf_cache_source_v1 (id, identity) VALUES (1, ?)', (identity,))
                    # Do not leave an obsolete append cursor certifying this module.
                    target.exec_driver_sql('DROP TABLE IF EXISTS append_cache_cursor_v1')
                    target.exec_driver_sql('DROP TABLE IF EXISTS append_cache_version')
                report('校验 ETF 份额和收盘价快照', percent=95)
                with destination.connect() as connection:
                    if connection.exec_driver_sql('PRAGMA quick_check').scalar() != 'ok':
                        raise RuntimeError('ETF snapshot validation failed')
                return path.name, count
            finally:
                destination.dispose()
