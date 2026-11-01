"""Compare complete daily headers, replacing all details of changed days atomically."""
import json
from sqlalchemy import Column, Integer, MetaData, Table, inspect, select
from app.core.cache_sync import read_batches, transfer_plan
from app.core.mysql_payload import decode_rows
from app.services.short_term import METHODS

MARKER = Table('short_term_cache_version', MetaData(), Column('version', Integer, primary_key=True))


def sync_short_term(source, target, tables, incremental):
    head, stocks = tables
    migrated = incremental and inspect(target).has_table(MARKER.name) and target.scalar(select(MARKER.c.version)) == 2
    remote = {row['trade_date']: dict(row) for row in source.execute(select(head)).mappings()}
    for row in remote.values():
        meta = row['payload']
        row['payload'] = dict(meta, counts={m: meta['counts'][m] for m in METHODS if m in meta.get('counts', {})})
    if not migrated:
        # A one-time module-only rebuild also removes retired strategy memberships.
        for table in tables:
            target.execute(table.delete())
    local = {row['trade_date']: dict(row) for row in target.execute(select(head)).mappings()}
    for day in remote.keys() | local.keys():
        if remote.get(day) == local.get(day):
            continue
        for table in tables:
            target.execute(table.delete().where(table.c.trade_date == day))
        if day not in remote:
            continue
        columns, _, _, compressed = transfer_plan(source, target, stocks)
        for batch in read_batches(source, columns, list(stocks.primary_key.columns), stocks.c.trade_date == day):
            values = []
            for value in decode_rows(batch, [], compressed):
                if isinstance(value['payload'], str):
                    value['payload'] = json.loads(value['payload'])
                methods = [m for m in value['payload'].get('methods', []) if m in METHODS]
                if methods:
                    value['payload'] = dict(value['payload'], methods=methods)
                    values.append(value)
            if values:
                target.execute(stocks.insert(), values)
        target.execute(head.insert(), remote[day])
    MARKER.create(target, checkfirst=True)
    target.execute(MARKER.delete())
    target.execute(MARKER.insert().values(version=2))
