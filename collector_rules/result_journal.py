"""Standalone collector journal; embedded copies are checked by the sync script."""
# BEGIN SHARED RESULT JOURNAL
import json as _journal_json
import uuid as _journal_uuid
from sqlalchemy import BigInteger, Column, Integer, MetaData, String, Table, Text
from sqlalchemy import event as _journal_event, inspect as _journal_inspect
from sqlalchemy.exc import IntegrityError as _JournalIntegrityError
from sqlalchemy.dialects.mysql import LONGTEXT as _JournalLongText
from sqlalchemy.schema import CreateTable as _JournalCreateTable
from sqlalchemy import text as _journal_text

def _result_journal_tables():
    meta = MetaData()
    head = Table('result_sync_head', meta,
                 Column('module', String(48), primary_key=True),
                 Column('protocol', Integer, nullable=False),
                 Column('generation', String(32), nullable=False),
                 Column('revision', BigInteger, nullable=False))
    changes = Table('result_sync_change', meta,
                    Column('module', String(48), primary_key=True),
                    Column('revision', BigInteger, primary_key=True),
                    Column('payload', Text().with_variant(_JournalLongText(), 'mysql'), nullable=False))
    for table in meta.tables.values():
        table.dialect_options['mysql']['engine'] = 'InnoDB'
        table.dialect_options['mysql']['charset'] = 'utf8mb4'
    return meta, head, changes


def _install_result_journal(Session, models, module):
    """Journal every ORM write and explicitly wrapped bulk DML in the same transaction."""
    if getattr(Session, '_result_journal_module', None) == module:
        return
    meta, head, changes = _result_journal_tables()
    engine = Session.kw['bind']
    # Compile explicitly for SQLAlchemy versions predating CreateTable(if_not_exists=True).
    # Database-side IF NOT EXISTS avoids the check-then-create race across collectors.
    with engine.begin() as connection:
        for table in meta.sorted_tables:
            ddl = str(_JournalCreateTable(table).compile(dialect=engine.dialect))
            connection.execute(_journal_text(ddl.replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS', 1)))
    # Initialization races are harmless; never reset an existing generation/revision.
    with engine.begin() as connection:
        exists = connection.execute(head.select().where(head.c.module == module)).first()
    if exists is None:
        try:
            with engine.begin() as connection:
                connection.execute(head.insert().values(module=module, protocol=1,
                                   generation=_journal_uuid.uuid4().hex, revision=0))
        except _JournalIntegrityError:
            with engine.begin() as connection:
                if connection.execute(head.select().where(head.c.module == module)).first() is None:
                    raise
    tracked = tuple(models)
    info_key = 'result_journal:' + module

    def lock(session):
        info = session.info.get(info_key)
        if info is None:
            with session.no_autoflush:
                row = session.execute(head.select().where(head.c.module == module).with_for_update()).first()
            if row is None or row.protocol != 1:
                raise ValueError('结果同步日志版本无效，请检查采集版本。')
            info = {'revision': row.revision, 'records': {}, 'written': False}
            session.info[info_key] = info
        return info

    def record(session, table, keys, deleted):
        if not keys:
            return
        info = lock(session)
        for key in keys:
            values = [value.isoformat() if hasattr(value, 'isoformat') else value for value in key]
            identity = (table.name, _journal_json.dumps(values, ensure_ascii=True, separators=(',', ':')))
            info['records'][identity] = {'table': table.name, 'key': values, 'deleted': bool(deleted)}
        payload = _journal_json.dumps(list(info['records'].values()), ensure_ascii=True, separators=(',', ':'))
        with session.no_autoflush:
            if not info['written']:
                info['revision'] += 1
                session.execute(head.update().where(head.c.module == module).values(revision=info['revision']))
                session.execute(changes.insert().values(module=module, revision=info['revision'], payload=payload))
                info['written'] = True
            else:
                session.execute(changes.update().where((changes.c.module == module) &
                                (changes.c.revision == info['revision'])).values(payload=payload))

    def before_flush(session, context, instances):
        rows = [row for row in session.new if isinstance(row, tracked)]
        rows += [row for row in session.dirty if isinstance(row, tracked)
                 and session.is_modified(row, include_collections=False)]
        rows += [row for row in session.deleted if isinstance(row, tracked)]
        groups = {}
        for row in rows:
            state = _journal_inspect(row)
            keys = list(row.__table__.primary_key.columns)
            if state.persistent and state.identity != tuple(getattr(row, column.name) for column in keys):
                raise ValueError('结果同步不允许修改主键；请删除旧记录再插入新记录。')
            deleted = row in session.deleted
            group = (row.__table__, deleted)
            groups.setdefault(group, []).append(tuple(getattr(row, column.name) for column in keys))
        for (table, deleted), keys in groups.items():
            record(session, table, keys, deleted)

    def reset(session):
        session.info.pop(info_key, None)

    _journal_event.listen(Session, 'before_flush', before_flush)
    _journal_event.listen(Session, 'after_commit', reset)
    _journal_event.listen(Session, 'after_rollback', reset)
    # sessionmaker copies this info dictionary into each new Session.
    Session.configure(info=dict(Session.kw.get('info', {}), _result_journal_callbacks=(tracked, lock, record)))
    Session._result_journal_module = module


def _result_bulk(query, values=None):
    """Serialize before selecting affected keys, then journal the exact bulk write."""
    session = query.session
    model = query.column_descriptions[0]['entity']
    registry = session.info.get('_result_journal_callbacks')
    if registry is None or model not in registry[0]:
        raise ValueError('批量写入缺少结果同步日志，拒绝无日志修改。')
    tracked, lock, record = registry
    lock(session)
    session.flush()
    table = model.__table__
    keys = query.with_entities(*table.primary_key.columns).with_for_update().all()
    if not keys:
        return 0
    record(session, table, keys, values is None)
    if values is None:
        return query.delete(synchronize_session=False)
    return query.update(values, synchronize_session=False)

# END SHARED RESULT JOURNAL
