"""Read-only source reconciliation into an unpublished local SQLite snapshot."""
import logging
from app.core.sync_progress import report

from sqlalchemy import and_, func, or_, select, tuple_

logger = logging.getLogger(__name__)
OMIT = {'member_codes', 'analysis', 'analysis_basis'}
READ_PAGE_SIZE = 2000
KEY_BATCH = 250  # At most 750 bound values for the current composite primary keys.


def chunks(values):
    values = list(values)
    for offset in range(0, len(values), KEY_BATCH):
        yield values[offset:offset + KEY_BATCH]


def key_filter(table, keys):
    return tuple_(*table.primary_key.columns).in_(keys)


def after_key(columns, values):
    # Explicit lexicographic ranges also work with older MySQL composite indexes.
    return or_(*(and_(*(columns[j] == values[j] for j in range(i)), column > values[i])
                 for i, column in enumerate(columns)))


def read_batches(connection, columns, keys, condition=None):
    """Buffer a bounded page; no unbuffered cursor remains open during local writes."""
    last = None
    while True:
        query = select(*columns).order_by(*keys).limit(READ_PAGE_SIZE)
        if condition is not None:
            query = query.where(condition)
        if last is not None:
            query = query.where(after_key(keys, last))
        report('读取 ' + keys[0].table.name)
        with connection.execution_options(stream_results=False).execute(query) as result:
            batch = result.all()
        if not batch:
            return
        report('处理 ' + keys[0].table.name, len(batch))
        yield batch
        if len(batch) < READ_PAGE_SIZE:
            return
        last = tuple(batch[-1]._mapping[key] for key in keys)


def read_map(connection, columns, key_size):
    return {tuple(row[:key_size]): tuple(row[key_size:])
            for batch in read_batches(connection, columns, columns[:key_size]) for row in batch}


def transfer(source, target, table, condition=None):
    columns = [c for c in table.columns if c.name not in OMIT]
    keys = list(table.primary_key.columns)
    count = 0
    for batch in read_batches(source, columns, keys, condition):
        values = [dict(row._mapping, **{c.name: '' for c in table.columns if c.name in OMIT})
                  for row in batch]
        target.execute(table.insert(), values)
        count += len(values)
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
    if not incremental or module in ('dividend-selection', 'high-growth'):
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
