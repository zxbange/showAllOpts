"""Compare small quarterly heads, never scan all historical stock keys when versioned."""
import logging
from sqlalchemy import BigInteger, Column, Date, MetaData, String, Table, inspect, select
from app.core.cache_errors import SnapshotDataError
from app.core.cache_sync import chunks, key_filter, transfer

LOG = logging.getLogger(__name__)
VERSION = Table('earnings_sync_head', MetaData(),
                Column('report_period', Date, primary_key=True),
                Column('generation', String(32), nullable=False),
                Column('revision', BigInteger, nullable=False))


def sync_quarters(source, target, tables, incremental):
    """Return False for legacy sources; callers retain full reconciliation compatibility."""
    if not inspect(source).has_table(VERSION.name):
        LOG.warning('Cache earnings versions unavailable; using legacy reconciliation; upgrade both collectors')
        if inspect(target).has_table(VERSION.name):
            VERSION.drop(target)
        return False
    head, stocks = tables
    remote = {row['report_period']: dict(row) for row in source.execute(select(head)).mappings()}
    versions = {row['report_period']: dict(row) for row in source.execute(select(VERSION)).mappings()}
    if not remote.keys() <= versions.keys():
        LOG.warning('Cache earnings versions incomplete; using legacy reconciliation; upgrade both collectors')
        if inspect(target).has_table(VERSION.name):
            VERSION.drop(target)
        return False
    if any(not v['generation'] or v['revision'] < 0 for v in versions.values()):
        raise SnapshotDataError('季度同步版本无效，请检查采集器。')
    old = {row['report_period']: dict(row) for row in target.execute(select(head)).mappings()} if incremental else {}
    previous = {}
    if incremental and inspect(target).has_table(VERSION.name):
        previous = {row['report_period']: dict(row) for row in target.execute(select(VERSION)).mappings()}
    VERSION.create(target, checkfirst=True)
    downloaded = 0
    for period in old.keys() - remote.keys():
        for table in tables:
            target.execute(table.delete().where(table.c.report_period == period))
    for period, meta in remote.items():
        current, prior = versions[period], previous.get(period)
        rebuild = (not incremental or period not in old or prior is None
                   or current['generation'] != prior['generation'] or current['revision'] < prior['revision'])
        if rebuild:
            target.execute(stocks.delete().where(stocks.c.report_period == period))
            downloaded += transfer(source, target, stocks, stocks.c.report_period == period)
        elif current['revision'] != prior['revision']:
            columns = list(stocks.primary_key.columns)
            remote_keys = {tuple(row) for row in source.execute(select(*columns).where(stocks.c.report_period == period))}
            local_keys = {tuple(row) for row in target.execute(select(*columns).where(stocks.c.report_period == period))}
            if not local_keys <= remote_keys:
                raise SnapshotDataError('季度新增版本包含删除，请更新采集器或完整同步。')
            for batch in chunks(remote_keys - local_keys):
                fetched = transfer(source, target, stocks, key_filter(stocks, batch))
                if fetched != len(batch):
                    raise SnapshotDataError('季度新增股票与源数据不一致。')
                downloaded += fetched
        elif meta['matched_count'] != old[period]['matched_count']:
            raise SnapshotDataError('季度股票数量变化但版本未更新，请升级采集器并完整同步。')
        if rebuild or old.get(period) != meta:
            target.execute(head.delete().where(head.c.report_period == period))
            target.execute(head.insert().values(**meta))
    target.execute(VERSION.delete())
    if versions:
        target.execute(VERSION.insert(), list(versions.values()))
    LOG.info('Cache earnings quarterly versions quarters=%s downloaded=%s', len(remote), downloaded)
    return True
