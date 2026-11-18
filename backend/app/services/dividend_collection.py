from app.services.collection_outcomes import record_task
from app.services.collection_failure_isolation import quarantine_publication, restore_manual_publication
from time import monotonic
from app.services.collection_calendar import automatic_collection_allowed, manual_failure_isolated
"""One public collection and atomic publication for both dividend strategies.

The public source validates coverage before each publication. Existing JoinQuant
snapshots and the unified database-sync pipeline remain available.
"""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
import sqlite3
import threading
import uuid

import httpx
import portalocker
from sqlalchemy import delete, select, inspect, text
from app.core.database import build_engine, database_identity
from app.core.diagnostics import diagnostic_details
from app.services.dividend_limits import DIVIDEND_GROUP_LIMITS
from app.models.dividend import DividendSnapshot, DividendStock
from app.services.system_configuration import database_config
from app.services.system_runtime import source_client

LOG = logging.getLogger(__name__)
CHINA = timezone(timedelta(hours=8))
RULE_VERSION = 'dividend_dps_proposal_age3_annual_history_v10'
PUBLIC_SOURCE_READY = True
NOT_READY = '当前候选池与同日估值覆盖尚未完成验证，保留原结果。'


class DividendCollectionError(ValueError):
    pass


def validate_result(meta, records):
    """Validate both groups before opening a write transaction, including empty results."""
    expected = {'trade_date', 'rule_version', 'universe_count', 'eligible_count', 'value_candidates', 'value_count'}
    if set(meta) != expected or meta['rule_version'] not in (RULE_VERSION, 'dividend_dps_proposal_age3_v9') or type(meta['trade_date']) is not date:
        raise DividendCollectionError('Invalid dividend snapshot metadata')
    counts = [meta[key] for key in ('universe_count', 'eligible_count', 'value_candidates', 'value_count')]
    if any(type(value) is not int or value < 0 for value in counts) or not counts[0] or not counts[0] >= counts[1] >= counts[2] >= counts[3]:
        raise DividendCollectionError('Incomplete dividend universe counts')
    groups = {'value': [], 'bank': []}
    seen = set()
    columns = set(DividendStock.__table__.columns.keys())
    for record in records:
        if set(record) != columns or record['method'] not in groups or record['trade_date'] != meta['trade_date'] or record['code'] in seen:
            raise DividendCollectionError('Invalid dividend record identity')
        seen.add(record['code'])
        for field in ('dividend_yield_1y', 'dividend_per_share', 'pe_ttm', 'market_cap', 'close'):
            value = Decimal(str(record[field]))
            if not value.is_finite() or value <= 0:
                raise DividendCollectionError('Invalid dividend numeric value')
        if not Decimal(str(record['pe_ttm'])) < 12 or not Decimal(str(record['dividend_yield_1y'])) > 3:
            raise DividendCollectionError('Dividend selection rules not met')
        if record['pb'] is not None and not Decimal(str(record['pb'])).is_finite():
            raise DividendCollectionError('Invalid dividend PB')
        fiscal_year = record['dividend_fiscal_year']
        if type(fiscal_year) is not int or not 1990 <= fiscal_year <= meta['trade_date'].year:
            raise DividendCollectionError('Invalid dividend fiscal year')
        if record['dividend_cash_1y'] is not None:
            raise DividendCollectionError('Proposal DPS must not be reported as total cash')
        expected_yield = Decimal(str(record['dividend_per_share'])) / Decimal(str(record['close'])) * 100
        if abs(expected_yield - Decimal(str(record['dividend_yield_1y']))) > Decimal('0.000001'):
            raise DividendCollectionError('Dividend DPS and yield mismatch')
        history = record['dividend_yield_history']
        if not isinstance(history, list) or len(history) != 3:
            raise DividendCollectionError('Incomplete annual dividend history')
        for offset, point in enumerate(history):
            if point['fiscal_year'] != fiscal_year - offset:
                raise DividendCollectionError('Invalid annual dividend year')
            if offset > 0 and point['yield_pct'] is None:
                continue
            if not Decimal(str(point['yield_pct'])).is_finite() or Decimal(str(point['yield_pct'])) < 0:
                raise DividendCollectionError('Invalid annual dividend history')
        groups[record['method']].append(record)
    for method, limit in DIVIDEND_GROUP_LIMITS.items():
        ranks = sorted(row['rank'] for row in groups[method])
        if len(ranks) > limit or ranks != list(range(1, len(ranks) + 1)):
            raise DividendCollectionError('Invalid dividend ranks')
    if len(records) != meta['value_count']:
        raise DividendCollectionError('Incomplete dividend result')


def signature(rows):
    def normalize(value, key):
        return str(Decimal(str(value)).quantize(Decimal('0.00000001' if key == 'dividend_per_share' else '0.000001')))
    numeric = {'dividend_yield_1y', 'dividend_cash_1y', 'pe_ttm', 'pb', 'market_cap', 'close', 'dividend_per_share'}
    return sorted(json.dumps({key: normalize(value, key) if key in numeric and value is not None else value
                              for key, value in dict(row).items()}, sort_keys=True, default=str) for row in rows)

def dividend_database_engine(settings, *, configuration=None):
    # Explicit dividend budget; shared transport applies the common 60-second floor.
    return build_engine(settings, configuration=configuration,
                        read_timeout_seconds=60, write_timeout_seconds=60, connect_timeout_seconds=60)


@contextmanager
def publication_phase(stage, progress=None):
    started = monotonic()
    if progress is not None:
        progress(stage)
    LOG.info('Dividend database phase started stage=%s', stage)
    try:
        yield
    except Exception as exc:
        LOG.error('Dividend database phase failed stage=%s seconds=%.3f details=%s',
                  stage, monotonic() - started, diagnostic_details(exc))
        raise
    else:
        LOG.info('Dividend database phase complete stage=%s seconds=%.3f',
                 stage, monotonic() - started)


def ensure_rule_version_capacity(engine, progress=None):
    """Widen legacy MySQL metadata before its implicitly committing DDL can affect a write."""
    if engine.dialect.name != 'mysql':
        return
    with publication_phase('检查规则版本字段', progress):
        column = next(c for c in inspect(engine).get_columns(DividendSnapshot.__tablename__)
                      if c['name'] == 'rule_version')
    length = getattr(column['type'], 'length', None)
    if length is not None and length < DividendSnapshot.rule_version.type.length:
        with publication_phase('扩容规则版本字段', progress), engine.begin() as conn:
            conn.execute(text('ALTER TABLE dividend_selection_snapshot MODIFY COLUMN rule_version VARCHAR(64) NOT NULL'))


def write_result(engine, meta, records, check=lambda: None, *, progress=None):
    """Both groups replace together, or neither does. Never replace a newer date."""
    validate_result(meta, records)
    head, stocks = DividendSnapshot.__table__, DividendStock.__table__
    # Schema creation is outside publication (MySQL DDL implicitly commits).
    with publication_phase('检查数据库表结构', progress):
        head.create(engine, checkfirst=True)
        stocks.create(engine, checkfirst=True)
    if progress is None:
        ensure_rule_version_capacity(engine)
    else:
        ensure_rule_version_capacity(engine, progress=progress)
    with publication_phase('检查股票字段', progress):
        columns = {column['name']: column for column in inspect(engine).get_columns(stocks.name)}
    if engine.dialect.name == 'mysql':
        with publication_phase('迁移股票字段', progress), engine.begin() as conn:
            if 'dividend_per_share' not in columns:
                conn.execute(text('ALTER TABLE dividend_selection_stock ADD COLUMN dividend_per_share NUMERIC(24,8) NULL'))
            if 'dividend_fiscal_year' not in columns:
                conn.execute(text('ALTER TABLE dividend_selection_stock ADD COLUMN dividend_fiscal_year INTEGER NULL'))
            if not columns['dividend_cash_1y']['nullable']:
                conn.execute(text('ALTER TABLE dividend_selection_stock MODIFY COLUMN dividend_cash_1y NUMERIC(28,6) NULL'))
    elif not {'dividend_per_share', 'dividend_fiscal_year'}.issubset(columns):
        raise DividendCollectionError('Dividend database schema migration required')
    with publication_phase('数据库快照事务', progress), engine.begin() as conn:
        with publication_phase('锁定数据库快照', progress):
            previous = conn.execute(select(head).where(head.c.id == 1).with_for_update()).mappings().first()
        if previous and previous['trade_date'] > meta['trade_date']:
            raise DividendCollectionError('A newer dividend snapshot already exists')
        check()
        with publication_phase('读取现有股票记录', progress):
            existing = conn.execute(select(stocks)).mappings().all()
        if previous and all(previous[key] == value for key, value in meta.items()) and signature(existing) == signature(records):
            return 0
        values = dict(meta, updated_at=datetime.now(CHINA).replace(tzinfo=None))
        with publication_phase('写入两组股票快照', progress):
            if previous:
                conn.execute(head.update().where(head.c.id == 1).values(**values))
            else:
                # Insert before deleting protects against concurrent first publication.
                conn.execute(head.insert().values(id=1, **values))
            conn.execute(delete(stocks))
            if records:
                conn.execute(stocks.insert(), records)
        check()
        if progress is not None:
            progress('提交数据库事务')
    return max(len(records), 1)


class DividendCollection:
    def __init__(self, settings, *, cache=None, source_factory=None, engine_factory=None, client_factory=None):
        self.settings, self.cache = settings, cache
        self.source_factory = source_factory
        self.engine_factory = engine_factory or dividend_database_engine
        self.client_factory = client_factory or (lambda: source_client(settings, factory=httpx.Client, timeout=60, follow_redirects=False))
        self.root = settings.log_path / 'market-data' / 'dividend-collection'
        self.root.mkdir(parents=True, exist_ok=True)
        self.stopped = threading.Event()
        self.collection_thread = None
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute("INSERT OR IGNORE INTO state VALUES(1,'{}')")
            db.execute('CREATE TABLE IF NOT EXISTS pending(id INTEGER PRIMARY KEY CHECK(id=1), target TEXT NOT NULL, payload TEXT NOT NULL)')

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.root / 'state.sqlite3', timeout=15)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def read_state(self):
        with self.db() as db:
            return json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])

    def save_state(self, state):
        with self.db() as db:
            db.execute('UPDATE state SET payload=? WHERE id=1', (json.dumps(state),))

    def pending(self):
        with self.db() as db:
            row = db.execute('SELECT target,payload FROM pending WHERE id=1').fetchone()
        if row is None:
            return None
        value = json.loads(row[1])
        value['meta']['trade_date'] = date.fromisoformat(value['meta']['trade_date'])
        for record in value['records']:
            record['trade_date'] = date.fromisoformat(record['trade_date'])
            for field in ('pe_ttm', 'pb', 'market_cap', 'close', 'dividend_yield_1y', 'dividend_per_share'):
                if record[field] is not None:
                    record[field] = Decimal(record[field])
        return row[0], value

    def stage(self, target, meta, records):
        payload = json.dumps(dict(meta=meta, records=records), default=str, allow_nan=False)
        with self.db() as db:
            db.execute('INSERT INTO pending VALUES(1,?,?)', (target, payload))

    def sync_local(self, meta, records, *, progress=None):
        if self.cache is None:
            raise DividendCollectionError('Local cache is unavailable; database publication remains pending')
        with publication_phase('刷新本地高股息缓存', progress):
            if not self.cache.refresh('dividend-selection', background=False, reason='dividend-collection', full=True):
                raise DividendCollectionError('Local synchronization is busy; publication remains pending')
        with publication_phase('核验本地完整快照', progress), self.cache.session('dividend-selection') as session:
            head = session.execute(select(DividendSnapshot.__table__).where(DividendSnapshot.id == 1)).mappings().first()
            rows = session.execute(select(DividendStock.__table__)).mappings().all()
            meta_fields = sorted(key for key, value in meta.items() if not head or head[key] != value)
            records_match = signature(rows) == signature(records)
            if meta_fields or not records_match:
                # Field names/counts only: preserve the strict full-snapshot gate.
                LOG.error('Dividend local verification mismatch metadata_fields=%s records_match=%s '
                          'expected_stock_rows=%s actual_stock_rows=%s',
                          ','.join(meta_fields) or '-', records_match, len(records), len(rows))
                raise DividendCollectionError('Local synchronization did not verify the complete dividend snapshot')

    def acquire(self):
        lock = portalocker.Lock(str(self.root / 'collection.lock'), timeout=0, flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
        try:
            lock.acquire()
            return lock
        except portalocker.exceptions.LockException:
            return None

    def running(self):
        lock = self.acquire()
        if lock is None:
            return True
        lock.release()
        return False

    def status(self):
        state = self.read_state()
        running = self.running()
        pending = self.pending() is not None
        available = self.source_factory is not None or PUBLIC_SOURCE_READY or pending
        return dict(state, publication_pending=pending, running=running, available=available,
                    unavailable_reason=None if available else NOT_READY,
                    interrupted=bool(state.get('active') and not running))

    def refresh(self, *, background=True, moment=None, slot=None):
        if self.source_factory is None and not PUBLIC_SOURCE_READY and self.pending() is None:
            raise DividendCollectionError(NOT_READY)
        if self.stopped.is_set():
            return False
        moment = moment or datetime.now(CHINA)
        if moment.tzinfo is None:
            raise DividendCollectionError('Timezone-aware collection time required')
        moment = moment.astimezone(CHINA)
        if slot is not None and not automatic_collection_allowed(moment):
            return False
        if moment.hour < 16 and self.pending() is None:
            raise DividendCollectionError('当日数据须在16:00后采集；不使用实时状态回填历史。')
        from app.services.earnings_schedule import acquire_execution
        lock = acquire_execution(self.settings, self.acquire)
        if lock is None:
            return False
        try:
            state = self.read_state()
            if state.get('run_isolated'):
                quarantine_publication(self, 'pending', state)
            state['run_isolated'] = slot is None and manual_failure_isolated(moment)
            if slot is None:
                restore_manual_publication(self, 'pending', state)
            recovered_older = (state.get('stage') == 'complete' and
                               isinstance(state.get('result_date'), str) and
                               slot is not None and state['result_date'] < slot)
            if slot and state.get('scheduled_slot') == slot and not recovered_older:
                lock.release()
                return False
            state.update(run_id=uuid.uuid4().hex, active=True, stage='collecting', error=None,
                         processed=None, total=None, failed_stage=None, stage_started_at=datetime.now(CHINA).isoformat(),
                         last_attempt_at=moment.isoformat(), database_committed=False, current_slot=slot, scheduled_slot=slot or state.get('scheduled_slot'))
            self.save_state(state)
            if background:
                self.collection_thread = threading.Thread(target=self._collect, args=(lock, state, moment), daemon=True, name='dividend-collection')
                self.collection_thread.start()
            else:
                self._collect(lock, state, moment)
            return True
        except BaseException:
            lock.release()
            raise

    def cancel(self):
        state = self.read_state()
        if self.running() and state.get('active'):
            (self.root / ('cancel-' + state['run_id'])).touch()
            return True
        return False

    def _collect(self, lock, state, moment):
        marker = self.root / ('cancel-' + state['run_id'])
        engine = None
        started = monotonic()
        def check():
            if state.get('current_slot') is None and not state.get('run_isolated') and manual_failure_isolated(moment + timedelta(seconds=monotonic()-started)):
                state['run_isolated'] = True
                self.save_state(state)
            if (state.get('current_slot') is not None and not automatic_collection_allowed(moment + timedelta(seconds=monotonic()-started))) or self.stopped.is_set() or marker.exists():
                raise DividendCollectionError('采集已取消；未提交的结果不发布。')
        def progress(stage, processed=None, total=None):
            check()
            if stage != state.get('stage'):
                state['stage_started_at'] = datetime.now(CHINA).isoformat()
            state.update(stage=stage, processed=processed, total=total)
            self.save_state(state)
        try:
            config = database_config(self.settings)
            target = database_identity(config)
            pending = self.pending()
            if pending is not None:
                if pending[0] != target:
                    raise DividendCollectionError('待发布结果属于其他数据库，请恢复原配置后重试。')
                meta, records = pending[1]['meta'], pending[1]['records']
            else:
                with self.client_factory() as client:
                    from app.services.dividend_public_source import DividendPublicSource
                    source = (self.source_factory or DividendPublicSource)(client, settings=self.settings,
                        as_of=moment.date(), cache_dir=self.root / 'source-cache', check_cancel=check)
                    if isinstance(source, DividendPublicSource):
                        source.on_progress = progress
                    meta, records = source.collect()
                check()
                progress('validating')
                validate_result(meta, records)
                if meta['trade_date'] != moment.date():
                    raise DividendCollectionError('Source date does not match collection date')
                self.stage(target, meta, records)
            progress('publishing')
            state.update(publication_pending=True)
            self.save_state(state)
            if config != database_config(self.settings):
                raise DividendCollectionError('数据库配置已变化，请重新采集。')
            engine = self.engine_factory(self.settings, configuration=config)
            def commit_check():
                check()
                if config != database_config(self.settings):
                    raise DividendCollectionError('数据库配置已变化，请重新采集。')
            written = max(write_result(engine, meta, records, commit_check, progress=progress),
                          int(pending[1].get('changed_count', 0)) if pending is not None else 0)
            with self.db() as db:
                payload = json.dumps(dict(meta=meta, records=records, changed_count=written), default=str, allow_nan=False)
                db.execute('UPDATE pending SET payload=? WHERE id=1', (payload,))
            state.update(database_committed=True, publication_pending=True)
            progress('syncing_local')
            self.save_state(state)
            check()
            self.sync_local(meta, records, progress=progress)
            commit_check()
            state.update(stage='complete', processed=None, total=None, last_success_at=datetime.now(CHINA).isoformat(),
                         written=written, result_date=meta['trade_date'].isoformat(), error=None, publication_pending=False, cache_refresh_pending=False)
            # Clearing the durable payload and recording local verification happen
            # together. A crash earlier replays the same idempotent database write.
            with self.db() as db:
                db.execute('UPDATE state SET payload=? WHERE id=1', (json.dumps(state),))
                db.execute('DELETE FROM pending WHERE id=1')
            from app.services.collection_notifications import emit_collection_result
            emit_collection_result(self.settings, 'dividend_selection', scheduled=bool(state.get('current_slot')),
                run_key=state['run_id'], outcome='success', changed_count=max(written, 0),
                summary=['选股日期：' + meta['trade_date'].isoformat(),
                         '非银行：' + str(sum(row['method'] == 'value' for row in records)),
                         '银行：' + str(sum(row['method'] == 'bank' for row in records))])
            LOG.info('Dividend collection complete date=%s rows=%s run=%s', meta['trade_date'], written, state['run_id'])
        except Exception as exc:
            pending = self.pending() is not None
            state['failed_stage'] = state.get('stage')
            state.update(stage='publication_pending' if pending else 'failed', publication_pending=pending,
                         error='数据库/本地同步尚未全部确认，已保留待发布结果；重试将恢复同步。' if pending else '采集未完成；已保留上次成功结果。')
            LOG.error('Dividend collection failed run=%s details=%s', state['run_id'], diagnostic_details(exc))
        finally:
            state['active'] = False
            try:
                if state.get('stage') != 'complete' and state.get('current_slot') is None and (manual_failure_isolated(moment) or manual_failure_isolated(moment + timedelta(seconds=monotonic()-started))):
                    state['run_isolated'] = True
                    self.save_state(state)
                    quarantine_publication(self, 'pending', state)
                    state.update(manual_failure_isolated=True, publication_pending=False, stage='failed',
                                 error='休市日手动采集失败，已隔离，不会自动重试；已有结果保留，可手动重试。')
                self.save_state(state)
                record_task(self.settings, 'dividend_selection', outcome=state['stage'], scheduled=state.get('current_slot') is not None, run_key=state['run_id'])
            finally:
                try:
                    marker.unlink(missing_ok=True)
                finally:
                    try:
                        if engine is not None:
                            engine.dispose()
                    finally:
                        lock.release()

    def stop(self):
        self.stopped.set()
        for worker in (self.collection_thread,):
            if worker and worker is not threading.current_thread():
                worker.join(timeout=5)
