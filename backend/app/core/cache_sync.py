"""Read-only source reconciliation into an unpublished local SQLite snapshot."""
import logging
import time
from app.core.sync_progress import report, stage_range
from app.core.mysql_payload import OPTION, compressed_column, decode_rows

from sqlalchemy import JSON, Text, and_, bindparam, func, or_, select, tuple_, type_coerce

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


def read_batches(connection, columns, keys, condition=None, *, timing=None, page_size=None):
    """Buffer a bounded page; no unbuffered cursor remains open during local writes."""
    last = None
    limit = min(page_size, READ_PAGE_SIZE) if page_size else READ_PAGE_SIZE
    while True:
        query = select(*columns).order_by(*keys).limit(limit)
        if condition is not None:
            query = query.where(condition)
        if last is not None:
            query = query.where(after_key(keys, last))
        report('读取 ' + keys[0].table.name)
        started = time.perf_counter()
        try:
            with connection.execution_options(stream_results=False).execute(query) as result:
                batch = result.all()
        except Exception:
            logger.warning('Cache read failed table=%s seconds=%.3f', keys[0].table.name, time.perf_counter()-started)
            raise
        elapsed = time.perf_counter() - started
        if timing is not None:
            timing['read_seconds'] += elapsed
            timing['queries'] += 1
        if elapsed >= 5:
            logger.info('Cache slow read table=%s rows=%s seconds=%.3f', keys[0].table.name, len(batch), elapsed)
        if not batch:
            return
        report('处理 ' + keys[0].table.name, len(batch))
        yield batch
        if len(batch) < limit:
            return
        last = tuple(batch[-1]._mapping[key] for key in keys)


def read_map(connection, columns, key_size):
    timing = {'read_seconds': 0.0, 'queries': 0}
    result = {tuple(row[:key_size]): tuple(row[key_size:])
              for batch in read_batches(connection, columns, columns[:key_size], timing=timing) for row in batch}
    logger.info('Cache manifest table=%s database=%s rows=%s queries=%s read_seconds=%.3f',
                columns[0].table.name, connection.dialect.name, len(result), timing['queries'], timing['read_seconds'])
    return result


def transfer_plan(source, target, table):
    # MySQL/PyMySQL and SQLite return JSON text. Avoid parsing and serializing
    # entire holdings documents merely to copy them; page reads still use JSON.
    raw_json = source.dialect.name in ('mysql', 'sqlite') and target.dialect.name == 'sqlite'
    json_names = {c.name for c in table.columns if isinstance(c.type, JSON) and c.name not in OMIT} if raw_json else set()
    compressed = json_names if source.get_execution_options().get(OPTION) else set()
    columns = [compressed_column(c) if c.name in compressed else type_coerce(c, Text).label(c.name) if c.name in json_names else c
               for c in table.columns if c.name not in OMIT]
    statement = table.insert()
    if json_names:
        statement = statement.values(**{name: bindparam(name, type_=Text) for name in json_names})
    return columns, statement, bool(json_names), compressed


def transfer(source, target, table, condition=None):
    columns, statement, raw_json, compressed = transfer_plan(source, target, table)
    keys = list(table.primary_key.columns)
    count = 0
    timing = {'read_seconds': 0.0, 'queries': 0}
    prepare_seconds = write_seconds = 0.0
    for batch in read_batches(source, columns, keys, condition, timing=timing):
        started = time.perf_counter()
        values = decode_rows(batch, [c.name for c in table.columns if c.name in OMIT], compressed)
        prepare_seconds += time.perf_counter() - started
        report('写入本地 ' + table.name)
        started = time.perf_counter()
        target.execute(statement, values)
        write_seconds += time.perf_counter() - started
        count += len(values)
    logger.info('Cache transfer table=%s rows=%s queries=%s read_seconds=%.3f prepare_seconds=%.3f write_seconds=%.3f raw_json=%s compressed=%s',
                table.name, count, timing['queries'], timing['read_seconds'], prepare_seconds, write_seconds, raw_json, bool(compressed))
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
    report('对比完成 ' + table.name, percent=20)
    for batch in chunks(changed | removed):
        target.execute(table.delete().where(key_filter(table, batch)))
    fetched = 0
    for batch in chunks(changed):
        fetched += transfer(source, target, table, key_filter(table, batch))
        report('更新本地 ' + table.name, percent=20 + 80 * fetched / max(1, len(changed)))
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
    report('同步数据表', percent=10)
    if incremental and module == 'earnings-gap':
        sync_earnings(source, target, tables)
    else:
        for index, table in enumerate(tables):
            with stage_range(10 + 80*index/len(tables), 10 + 80*(index+1)/len(tables)):
                report('同步 ' + table.name, percent=0)
                if not incremental or module in ('dividend-selection', 'high-growth'):
                    target.execute(table.delete())
                    fetched = transfer(source, target, table)
                    logger.info('Cache table sync table=%s mode=full downloaded=%s', table.name, fetched)
                else:
                    sync_history(source, target, table)
                report('数据表完成 ' + table.name, percent=100)
    report('提交本地数据', percent=90)
    return sum(target.scalar(select(func.count()).select_from(table)) for table in tables)
