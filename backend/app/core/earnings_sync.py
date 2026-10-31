"""Compare quarterly versions and scalar counts; fetch only changed quarter details."""
import logging
import re
from sqlalchemy import BigInteger, Column, Date, MetaData, String, Table, inspect, select, func
from app.core.cache_errors import SnapshotDataError
from app.core.cache_sync import chunks, key_filter, transfer

LOG = logging.getLogger(__name__)
VERSION = Table('earnings_sync_head', MetaData(),
                Column('report_period', Date, primary_key=True),
                Column('generation', String(32), nullable=False),
                Column('revision', BigInteger, nullable=False))


def sync_quarters(source, target, tables, incremental):
    """Return False for legacy sources; callers retain full reconciliation compatibility."""
    head, stocks = tables
    remote = {row['report_period']: dict(row) for row in source.execute(select(head)).mappings()}
    has_versions = inspect(source).has_table(VERSION.name)
    versions = {row['report_period']: dict(row) for row in source.execute(select(VERSION)).mappings()} if has_versions else {}
    if any(not re.fullmatch(r'[0-9a-f]{32}', v['generation'] or '') or v['revision'] < 0 for v in versions.values()):
        raise SnapshotDataError('季度同步版本无效，请检查采集器。')
    # A version is only useful if its declared snapshot is complete. Aggregate
    # small keys/counts, never historical JSON payloads; reject before any copy.
    actual = dict(source.execute(select(stocks.c.report_period, func.count()).group_by(stocks.c.report_period)).all())
    for period in sorted(remote.keys() | actual.keys()):
        meta = remote.get(period)
        count = actual.get(period, 0)
        if meta is None or meta['matched_count'] != count or meta['matched_count'] < 0 or meta['matched_count'] > meta['universe_count']:
            version = versions.get(period)
            raise SnapshotDataError('季度 {} 数据不完整：声明数量={}，实际数量={}，revision={}；保留上次成功数据，请核查该季度采集。'.format(
                period, meta['matched_count'] if meta else 'missing', count, version['revision'] if version else 'missing'))
    if not has_versions or not remote.keys() <= versions.keys():
        LOG.warning('Cache earnings versions unavailable/incomplete; using validated legacy reconciliation; upgrade both collectors')
        if inspect(target).has_table(VERSION.name):
            VERSION.drop(target)
        return False
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
                raise SnapshotDataError('季度 {} 新增版本包含删除：源数量={}，缓存数量={}，源revision={}，缓存revision={}；请核查采集器，不能按新增发布删除。'.format(period, meta['matched_count'], old[period]['matched_count'], current['revision'], prior['revision']))
            for batch in chunks(remote_keys - local_keys):
                fetched = transfer(source, target, stocks, key_filter(stocks, batch))
                if fetched != len(batch):
                    raise SnapshotDataError('季度新增股票与源数据不一致。')
                downloaded += fetched
        elif meta['matched_count'] != old[period]['matched_count']:
            raise SnapshotDataError('季度 {} 股票数量变化但版本未更新：源数量={}，缓存数量={}，generation={}，revision={}；请运行新版采集器的季度版本迁移，保留上次成功数据。'.format(period, meta['matched_count'], old[period]['matched_count'], current['generation'], current['revision']))
        if rebuild or old.get(period) != meta:
            target.execute(head.delete().where(head.c.report_period == period))
            target.execute(head.insert().values(**meta))
    target.execute(VERSION.delete())
    if versions:
        target.execute(VERSION.insert(), list(versions.values()))
    LOG.info('Cache earnings quarterly versions quarters=%s downloaded=%s', len(remote), downloaded)
    return True
