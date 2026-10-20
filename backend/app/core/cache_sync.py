"""Read-only source reconciliation into an unpublished local SQLite snapshot."""
import logging

from sqlalchemy import func, select, tuple_

logger = logging.getLogger(__name__)
OMIT = {'member_codes', 'analysis', 'analysis_basis'}
KEY_BATCH = 250  # At most 750 bound values for the current composite primary keys.


def chunks(values):
    values = list(values)
    for offset in range(0, len(values), KEY_BATCH):
        yield values[offset:offset + KEY_BATCH]


def key_filter(table, keys):
    return tuple_(*table.primary_key.columns).in_(keys)


def read_map(connection, columns, key_size):
    result = connection.execution_options(stream_results=True).execute(select(*columns))
    try:
        return {tuple(row[:key_size]): tuple(row[key_size:]) for row in result}
    finally:
        result.close()


def transfer(source, target, table, condition=None):
    columns = [c for c in table.columns if c.name not in OMIT]
    query = select(*columns)
    if condition is not None:
        query = query.where(condition)
    result = source.execution_options(stream_results=True).execute(query)
    count = 0
    try:
        for batch in result.mappings().partitions(2000):
            values = [dict(row, **{c.name: '' for c in table.columns if c.name in OMIT}) for row in batch]
            target.execute(table.insert(), values)
            count += len(values)
    finally:
        result.close()
    return count


def sync_history(source, target, table):
    """Compare keys/timestamps, including late backfills and removed source rows."""
    keys = list(table.primary_key.columns)
    columns = keys + [table.c.updated_at]
    logger.info('Cache metadata read started table=%s', table.name)
    remote = read_map(source, columns, len(keys))
    local = read_map(target, columns, len(keys))
    changed = {key for key, stamp in remote.items()
               if key not in local or stamp != local[key] or stamp == (None,)}
    removed = local.keys() - remote.keys()
    logger.info('Cache changes planned table=%s changed=%s deleted=%s total=%s',
                table.name, len(changed), len(removed), len(remote))
    for batch in chunks(changed | removed):
        target.execute(table.delete().where(key_filter(table, batch)))
    fetched = 0
    for batch in chunks(changed):
        fetched += transfer(source, target, table, key_filter(table, batch))
    logger.info('Cache table sync table=%s mode=incremental downloaded=%s deleted=%s total=%s',
                table.name, fetched, len(removed), len(remote))


def sync_earnings(source, target, tables):
    """Collector updates the quarter header; stock details have no updated_at."""
    head, stocks = tables
    # Compare every header field and stock membership; unchanged quarters keep their JSON locally.
    columns = [head.c.report_period] + [c for c in head.columns if c.name != 'report_period']
    remote = read_map(source, columns, 1)
    local = read_map(target, columns, 1)
    changed = {key[0] for key in remote.keys() | local.keys() if remote.get(key) != local.get(key)}
    keys = list(stocks.primary_key.columns)
    remote_keys = read_map(source, keys, len(keys))
    local_keys = read_map(target, keys, len(keys))
    changed.update(key[0] for key in remote_keys.keys() ^ local_keys.keys())
    fetched = 0
    for batch in chunks(changed):
        for table in tables:
            condition = table.c.report_period.in_(batch)
            target.execute(table.delete().where(condition))
            fetched += transfer(source, target, table, condition)
    logger.info('Cache quarter sync quarters=%s downloaded=%s', len(changed), fetched)


def sync_tables(source, target, tables, module, incremental):
    if not incremental or module == 'dividend-selection':
        for table in tables:
            target.execute(table.delete())
            fetched = transfer(source, target, table)
            logger.info('Cache table sync table=%s mode=full downloaded=%s', table.name, fetched)
    elif module == 'earnings-gap':
        sync_earnings(source, target, tables)
    else:
        for table in tables:
            sync_history(source, target, table)
    return sum(target.scalar(select(func.count()).select_from(table)) for table in tables)
