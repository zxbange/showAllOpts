"""Compare complete daily headers, replacing all details of changed days atomically."""
from sqlalchemy import Column, Integer, MetaData, Table, inspect, select
from app.core.cache_sync import transfer

MARKER = Table('short_term_cache_version', MetaData(), Column('version', Integer, primary_key=True))


def sync_short_term(source, target, tables, incremental):
    head, stocks = tables
    migrated = incremental and inspect(target).has_table(MARKER.name) and target.scalar(select(MARKER.c.version)) == 1
    remote = {row['trade_date']: dict(row) for row in source.execute(select(head)).mappings()}
    if not migrated:
        # A one-time module-only rebuild repairs legacy head-new/details-old snapshots.
        for table in tables:
            target.execute(table.delete())
            transfer(source, target, table)
    else:
        local = {row['trade_date']: dict(row) for row in target.execute(select(head)).mappings()}
        for day in remote.keys() | local.keys():
            if remote.get(day) == local.get(day):
                continue
            for table in tables:
                target.execute(table.delete().where(table.c.trade_date == day))
                transfer(source, target, table, table.c.trade_date == day)
    MARKER.create(target, checkfirst=True)
    target.execute(MARKER.delete())
    target.execute(MARKER.insert().values(version=1))
