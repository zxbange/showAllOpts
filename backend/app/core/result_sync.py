"""Transaction-journal synchronization for collector-owned result tables.

Each revision is one committed payload of changed keys. Revisions are contiguous;
missing or malformed history must never silently advance the local cursor.
"""
from datetime import date
import json
import logging
import shutil
import uuid

from sqlalchemy import BigInteger, Column, Integer, MetaData, String, Table, Text, create_engine, inspect, select, func
from sqlalchemy.pool import NullPool

from app.core.cache_errors import SnapshotDataError
from app.core.cache_sync import chunks, key_filter, read_batches, transfer
from app.core.sync_progress import report

MODULE_IDS = {'market-sentiment', 'microcap-temperature', 'etf-momentum'}
LOG = logging.getLogger(__name__)
META = MetaData()
HEAD = Table('result_sync_head', META,
             Column('module', String(48), primary_key=True), Column('protocol', Integer, nullable=False),
             Column('generation', String(32), nullable=False), Column('revision', BigInteger, nullable=False))
CHANGE = Table('result_sync_change', META,
               Column('module', String(48), primary_key=True), Column('revision', BigInteger, primary_key=True),
               Column('payload', Text, nullable=False))
CURSOR = Table('result_sync_cursor_v1', MetaData(),
               Column('module', String(48), primary_key=True), Column('generation', String(32), nullable=False),
               Column('revision', BigInteger, nullable=False), Column('rows', BigInteger, nullable=False))
LEGACY_NOTE = '源库未启用新版变更日志，本次使用全量清单对账；升级独立及批量采集器后自动切换增量。'


def read_cursor(path, module):
    if path is None:
        return None
    local = create_engine('sqlite:///file:' + path.as_posix() + '?mode=ro&uri=true', poolclass=NullPool)
    try:
        with local.connect() as connection:
            if not inspect(connection).has_table(CURSOR.name):
                return None
            row = connection.execute(select(CURSOR).where(CURSOR.c.module == module)).mappings().first()
            return dict(row) if row else None
    finally:
        local.dispose()


def changed_keys(source, module, revision, head, tables):
    last = revision
    changed = {}
    condition = (CHANGE.c.module == module) & (CHANGE.c.revision > revision) & (CHANGE.c.revision <= head['revision'])
    for page in read_batches(source, list(CHANGE.columns), [CHANGE.c.module, CHANGE.c.revision], condition, page_size=20):
        for row in page:
            if row.revision != last + 1:
                raise SnapshotDataError('结果同步变更日志不连续，请完整同步建立新基线。')
            try:
                records = json.loads(row.payload)
                if not isinstance(records, list) or not records:
                    raise ValueError('Empty changes')
                for item in records:
                    table = tables[item['table']]
                    raw = item['key']
                    columns = list(table.primary_key.columns)
                    if not isinstance(raw, list) or len(raw) != len(columns) or type(item['deleted']) is not bool:
                        raise ValueError('Invalid key')
                    values = []
                    for value, column in zip(raw, columns):
                        kind = column.type.python_type
                        if kind is date:
                            value = date.fromisoformat(value)
                        elif type(value) is not kind:
                            raise ValueError('Invalid key type')
                        values.append(value)
                    changed[(table.name, tuple(values))] = item['deleted']
            except (ValueError, TypeError, KeyError) as exc:
                raise SnapshotDataError('结果同步变更日志无效，请检查采集器并完整同步。') from exc
            last = row.revision
        report('读取结果变更记录', rows=len(page))
    if last != head['revision']:
        raise SnapshotDataError('结果同步变更日志缺失，请完整同步建立新基线。')
    return changed


def copy_results(cache, module, full=False):
    from app.core.cache import MODULES
    cache.sync_notes[module] = None
    previous = cache.snapshot_path(module, cache.read_state()['modules'].get(module, {}))
    cursor = read_cursor(previous, module) if not full else None
    tables = [model.__table__ for model in MODULES[module][1]]
    with cache.source.connect() as source:
        if source.dialect.name == 'mysql':
            source = source.execution_options(isolation_level='REPEATABLE READ')
        with source.begin():
            inspector = inspect(source)
            if not inspector.has_table(HEAD.name) or not inspector.has_table(CHANGE.name):
                cache.sync_notes[module] = LEGACY_NOTE
                LOG.warning('Cache result journal unavailable module=%s; using legacy reconciliation', module)
                return None
            head = source.execute(select(HEAD).where(HEAD.c.module == module)).mappings().first()
            if head is None:
                cache.sync_notes[module] = LEGACY_NOTE
                LOG.warning('Cache result journal head unavailable module=%s; using legacy reconciliation', module)
                return None
            if head['protocol'] != 1 or head['revision'] < 0 or not head['generation']:
                raise SnapshotDataError('结果同步版本信息无效，请检查采集器。')
            incremental = bool(cursor and cursor['generation'] == head['generation'] and 0 <= cursor['revision'] <= head['revision'])
            if incremental and cursor['revision'] == head['revision']:
                LOG.info('Cache result journal unchanged module=%s revision=%s', module, head['revision'])
                return previous.name, cursor['rows']
            path = cache.root / (module + '-' + uuid.uuid4().hex + '.sqlite')
            local = create_engine('sqlite:///' + path.as_posix(), poolclass=NullPool)
            try:
                if incremental:
                    shutil.copyfile(previous, path)
                tables[0].metadata.create_all(local, tables=tables)
                CURSOR.create(local, checkfirst=True)
                with local.begin() as target:
                    if incremental:
                        changes = changed_keys(source, module, cursor['revision'], head, {t.name: t for t in tables})
                        for table in tables:
                            records = [(key, deleted) for (name, key), deleted in changes.items() if name == table.name]
                            for batch in chunks(records):
                                keys = [key for key, _ in batch]
                                target.execute(table.delete().where(key_filter(table, keys)))
                                live = [key for key, deleted in batch if not deleted]
                                fetched = transfer(source, target, table, key_filter(table, live)) if live else 0
                                if fetched != len(live):
                                    raise SnapshotDataError('结果变更记录与源数据不一致，请完整同步复核。')
                    else:
                        report('建立结果同步基线', percent=10)
                        for table in tables:
                            transfer(source, target, table)
                    cache.validate_module(module, target)
                    count = sum(target.scalar(select(func.count()).select_from(table)) for table in tables)
                    target.execute(CURSOR.delete())
                    target.execute(CURSOR.insert().values(module=module, generation=head['generation'], revision=head['revision'], rows=count))
                with local.connect() as target:
                    if target.exec_driver_sql('PRAGMA quick_check').scalar() != 'ok':
                        raise SnapshotDataError('结果同步快照校验失败。')
                LOG.info('Cache result journal complete module=%s mode=%s revision=%s rows=%s',
                         module, 'incremental' if incremental else 'baseline', head['revision'], count)
                return path.name, count
            except BaseException:
                local.dispose()
                path.unlink(missing_ok=True)
                raise
            finally:
                local.dispose()
