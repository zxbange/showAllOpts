"""Commit calculated daily summaries and their inputs to MySQL, then publish locally.

Retry reads the committed rows: a failed local write never requires deleting MySQL data.
Both writers serialize on the same result-journal row used by JoinQuant.
"""
from datetime import date
import json
import uuid

from sqlalchemy import Column, Date, DateTime, Integer, MetaData, Numeric, String, Table, Text, select
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.exc import IntegrityError

from app.core.database import build_engine
from app.core.history_sync import source_identity
from app.core.result_sync import HEAD, CHANGE
from app.services.index_valuation import checked, init_store, METRICS
from app.services.index_valuation_rules import METHOD, SOURCE, PUBLIC_SOURCE, summarize, normalized_statistics
from app.services.system_configuration import database_config
from app.services.valuation_ruler_model import ValuationError

META = MetaData()
def keys():
    return [Column('trade_date', Date, primary_key=True), Column('index_code', String(16), primary_key=True),
            Column('method_version', String(48), primary_key=True)]
def provenance():
    return [Column('source', String(48), nullable=False), Column('updated_at', DateTime, nullable=False)]
DAILY = Table('valuation_ruler_daily_v2', META, *keys(), Column('index_close', Numeric(24, 8), nullable=False),
    *(Column(field, Numeric(30, 12)) for field in METRICS), Column('member_count', Integer, nullable=False),
    Column('statistics', Text, nullable=False), Column('members_sha256', String(64), nullable=False), *provenance(),
    mysql_engine='InnoDB', mysql_charset='utf8mb4')
MEMBER = Table('valuation_ruler_member_v2', META, *keys(),
    Column('payload', Text().with_variant(LONGTEXT(), 'mysql'), nullable=False), *provenance(),
    mysql_engine='InnoDB', mysql_charset='utf8mb4')
MODULE = 'valuation-ruler-v2'


def initialize(engine):
    META.create_all(engine)
    HEAD.create(engine, checkfirst=True)
    CHANGE.create(engine, checkfirst=True)
    with engine.connect() as db:
        exists = db.execute(select(HEAD).where(HEAD.c.module == MODULE)).first()
    if exists is None:
        try:
            with engine.begin() as db:
                db.execute(HEAD.insert().values(module=MODULE, protocol=1, generation=uuid.uuid4().hex, revision=0))
        except IntegrityError:
            with engine.connect() as db:
                if db.execute(select(HEAD).where(HEAD.c.module == MODULE)).first() is None:
                    raise


def write_records(engine, records, check):
    initialize(engine)
    published, changes = [], []
    with engine.begin() as db:
        head = db.execute(select(HEAD).where(HEAD.c.module == MODULE).with_for_update()).mappings().one()
        if head['protocol'] != 1:
            raise ValuationError('估值结果日志版本不匹配。')
        for daily, raw in records:
            check()
            key = {k: daily[k] for k in ('trade_date', 'index_code', 'method_version')}
            old = db.execute(select(DAILY).filter_by(**key)).mappings().first()
            member = db.execute(select(MEMBER).filter_by(**key)).mappings().first()
            if bool(old) != bool(member):
                raise ValuationError('已有指数日的汇总或原始成分缺失，请核实；不会覆盖。')
            if old:
                if old['source'] not in (SOURCE, PUBLIC_SOURCE) or member['source'] != old['source']:
                    raise ValuationError('已有指数日来源不匹配。')
                computed, archive = summarize(old['trade_date'], old['index_close'], json.loads(member['payload']),
                                               old['index_code'], source=old['source'])
                if (archive['payload'] != member['payload'] or any((normalized_statistics(old[k]) != normalized_statistics(v) if k == 'statistics' else old[k] != v) for k, v in computed.items() if k != 'updated_at')):
                    raise ValuationError('已有指数日汇总与原值不一致，请核实。')
                # Preserve historical/JQ originals even when today's vendor revises its values.
                published.append(checked(old, daily['trade_date']))
                continue
            computed, archive = summarize(daily['trade_date'], daily['index_close'], json.loads(raw['payload']),
                                           daily['index_code'], source=PUBLIC_SOURCE)
            if any(daily[k] != v for k, v in computed.items() if k != 'updated_at') or archive['payload'] != raw['payload']:
                raise ValuationError('候选估值汇总与原始数据不一致。')
            db.execute(DAILY.insert().values(**daily))
            db.execute(MEMBER.insert().values(**raw))
            published.append(checked(daily, daily['trade_date']))
            changes.extend(dict(table=table.name, key=[str(key['trade_date']), key['index_code'], METHOD], deleted=False)
                           for table in (DAILY, MEMBER))
        if changes:
            revision = head['revision']+1
            db.execute(HEAD.update().where(HEAD.c.module == MODULE).values(revision=revision))
            db.execute(CHANGE.insert().values(module=MODULE, revision=revision, payload=json.dumps(changes)))
        check()
    return published


def collect(monitor, source, state, check, moment, cutoff):
    config = database_config(monitor.settings)
    state.update(phase='collecting', target_date=str(cutoff))
    monitor.write_state(state)
    records = source.collect_latest(cutoff, moment)
    check()
    state.update(phase='saving', target_date=str(max(row[0]['trade_date'] for row in records)))
    monitor.write_state(state)
    engine = build_engine(monitor.settings, configuration=config)
    try:
        published = write_records(engine, records, check)
        state['phase'] = 'publishing'
        # A config change must not mix an old database into the new target's local cache.
        if database_config(monitor.settings) != config:
            raise ValuationError('数据库配置已变化；原库写入已保留，请重新采集。')
        check()
        with monitor.db() as db:
            init_store(db)
            db.execute('CREATE TABLE IF NOT EXISTS history_cache_source_v1 (id INTEGER PRIMARY KEY,identity TEXT NOT NULL)')
            previous = db.execute('SELECT identity FROM history_cache_source_v1 WHERE id=1').fetchone()
            identity = source_identity(engine)
            if previous and previous[0] != identity:
                raise ValuationError('本地历史属于其他数据库，请先从数据同步更新当前库。')
            for point in published:
                db.execute('INSERT INTO index_points_v2 VALUES (?,?,?) ON CONFLICT(index_code,trade_date) '
                    'DO UPDATE SET payload=excluded.payload', (point['index_code'], point['trade_date'],
                                                            json.dumps(point, ensure_ascii=False, sort_keys=True)))
            for day in {point['trade_date'] for point in published}:
                db.execute('DELETE FROM index_rosters_v2 WHERE trade_date=?', (day,))
            db.execute('INSERT OR REPLACE INTO history_cache_source_v1 VALUES (1,?)', (identity,))
        state.update(completed_days=len({point['trade_date'] for point in published}), error=None)
    finally:
        engine.dispose()
