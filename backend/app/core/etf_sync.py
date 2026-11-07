"""ETF reconciliation accepts late prices and exchange same-date share revisions.

Keep the existing cache namespace and share schema. Old sources without the optional
price table remain readable, and old immutable snapshots are upgraded on refresh.
"""
import shutil

from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import NullPool

from app.core.cache_sync import chunks, key_filter, read_map, transfer
from app.core.sync_progress import report
from app.models.etf import ETFPrice, ETFShare, ETFTradingDay


def manifest(connection, table):
    # There are only 18 tracked ETFs. Compare actual values as well as timestamps:
    # MySQL timestamps can have only second precision and late prices are independent.
    keys = list(table.primary_key.columns)
    columns = keys + [c for c in table.columns if c not in keys]
    return read_map(connection, columns, len(keys))


def copy_etf(cache, path, previous, full=False):
    tables = [ETFShare.__table__, ETFPrice.__table__, ETFTradingDay.__table__]
    local = None
    local_maps = {}
    local_names = set()
    if previous is not None and not full:
        local = create_engine('sqlite:///file:' + previous.as_posix() + '?mode=ro&uri=true', poolclass=NullPool)
        try:
            with local.connect() as connection:
                local_names = set(inspect(connection).get_table_names())
                local_maps = {table.name: manifest(connection, table) if table.name in local_names else {}
                              for table in tables}
        finally:
            local.dispose()
    with cache.source.connect() as source:
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
            remote = {table.name: manifest(source, table) if table.name in source_tables else {}
                      for table in tables}
            count = sum(len(values) for values in remote.values())
            if not full and previous is not None and all(t.name in local_names for t in tables) and remote == local_maps:
                return previous.name, count
            destination = create_engine('sqlite:///' + path.as_posix(), poolclass=NullPool)
            try:
                if previous is not None and not full:
                    shutil.copyfile(previous, path)
                tables[0].metadata.create_all(destination, tables=tables)
                with destination.begin() as target:
                    for table in tables:
                        old, new = local_maps.get(table.name, {}), remote[table.name]
                        changed = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
                        for batch in chunks(changed):
                            target.execute(table.delete().where(key_filter(table, batch)))
                        for batch in chunks(changed & new.keys()):
                            transfer(source, target, table, key_filter(table, batch))
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
