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
from app.models.dividend import DividendSnapshot, DividendStock
from app.services.system_configuration import database_config
from app.services.system_runtime import source_client

LOG = logging.getLogger(__name__)
CHINA = timezone(timedelta(hours=8))
RULE_VERSION = 'dividend_dps_proposal_age3_v9'
PUBLIC_SOURCE_READY = True
NOT_READY = '当前候选池与同日估值覆盖尚未完成验证，保留原结果。'


class DividendCollectionError(ValueError):
    pass


def validate_result(meta, records):
    """Validate both groups before opening a write transaction, including empty results."""
    expected = {'trade_date', 'rule_version', 'universe_count', 'eligible_count', 'value_candidates', 'value_count'}
    if set(meta) != expected or meta['rule_version'] != RULE_VERSION or type(meta['trade_date']) is not date:
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
            if point['fiscal_year'] != fiscal_year - offset or not Decimal(str(point['yield_pct'])).is_finite() or Decimal(str(point['yield_pct'])) < 0:
                raise DividendCollectionError('Invalid annual dividend history')
        groups[record['method']].append(record)
    for method, limit in (('value', 25), ('bank', 10)):
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

def write_result(engine, meta, records, check=lambda: None):
    """Both groups replace together, or neither does. Never replace a newer date."""
    validate_result(meta, records)
    head, stocks = DividendSnapshot.__table__, DividendStock.__table__
    # Schema creation is outside publication (MySQL DDL implicitly commits).
    head.create(engine, checkfirst=True)
    stocks.create(engine, checkfirst=True)
    columns = {column['name']: column for column in inspect(engine).get_columns(stocks.name)}
    if engine.dialect.name == 'mysql':
        with engine.begin() as conn:
            if 'dividend_per_share' not in columns:
                conn.execute(text('ALTER TABLE dividend_selection_stock ADD COLUMN dividend_per_share NUMERIC(24,8) NULL'))
            if 'dividend_fiscal_year' not in columns:
                conn.execute(text('ALTER TABLE dividend_selection_stock ADD COLUMN dividend_fiscal_year INTEGER NULL'))
            if not columns['dividend_cash_1y']['nullable']:
                conn.execute(text('ALTER TABLE dividend_selection_stock MODIFY COLUMN dividend_cash_1y NUMERIC(28,6) NULL'))
    elif not {'dividend_per_share', 'dividend_fiscal_year'}.issubset(columns):
        raise DividendCollectionError('Dividend database schema migration required')
    with engine.begin() as conn:
        previous = conn.execute(select(head).where(head.c.id == 1).with_for_update()).mappings().first()
        if previous and previous['trade_date'] > meta['trade_date']:
            raise DividendCollectionError('A newer dividend snapshot already exists')
        check()
        existing = conn.execute(select(stocks)).mappings().all()
        if previous and all(previous[key] == value for key, value in meta.items()) and signature(existing) == signature(records):
            return 0
        values = dict(meta, updated_at=datetime.now(CHINA).replace(tzinfo=None))
        if previous:
            conn.execute(head.update().where(head.c.id == 1).values(**values))
        else:
            # Insert before deleting protects against concurrent first publication.
            conn.execute(head.insert().values(id=1, **values))
        conn.execute(delete(stocks))
        if records:
            conn.execute(stocks.insert(), records)
        check()
    return max(len(records), 1)


class DividendCollection:
    def __init__(self, settings, *, cache=None, source_factory=None, engine_factory=None, client_factory=None):
        self.settings, self.cache = settings, cache
        self.source_factory = source_factory
        self.engine_factory = engine_factory or build_engine
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

    def sync_local(self, meta, records):
        if self.cache is None:
            raise DividendCollectionError('Local cache is unavailable; database publication remains pending')
        if not self.cache.refresh('dividend-selection', background=False, reason='dividend-collection', full=True):
            raise DividendCollectionError('Local synchronization is busy; publication remains pending')
        with self.cache.session('dividend-selection') as session:
            head = session.execute(select(DividendSnapshot.__table__).where(DividendSnapshot.id == 1)).mappings().first()
            rows = session.execute(select(DividendStock.__table__)).mappings().all()
            if not head or any(head[key] != value for key, value in meta.items()) or signature(rows) != signature(records):
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
        if moment.hour < 16 and self.pending() is None:
            raise DividendCollectionError('当日数据须在16:00后采集；不使用实时状态回填历史。')
        from app.services.earnings_schedule import acquire_execution
        lock = acquire_execution(self.settings, self.acquire)
        if lock is None:
            return False
        try:
            state = self.read_state()
            recovered_older = (state.get('stage') == 'complete' and
                               isinstance(state.get('result_date'), str) and
                               slot is not None and state['result_date'] < slot)
            if slot and state.get('scheduled_slot') == slot and not recovered_older:
                lock.release()
                return False
            state.update(run_id=uuid.uuid4().hex, active=True, stage='collecting', error=None,
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
        def check():
            if self.stopped.is_set() or marker.exists():
                raise DividendCollectionError('采集已取消；未提交的结果不发布。')
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
                    meta, records = source.collect()
                check()
                validate_result(meta, records)
                if meta['trade_date'] != moment.date():
                    raise DividendCollectionError('Source date does not match collection date')
                self.stage(target, meta, records)
            state.update(stage='publishing', publication_pending=True)
            self.save_state(state)
            if config != database_config(self.settings):
                raise DividendCollectionError('数据库配置已变化，请重新采集。')
            engine = self.engine_factory(self.settings, configuration=config)
            def commit_check():
                check()
                if config != database_config(self.settings):
                    raise DividendCollectionError('数据库配置已变化，请重新采集。')
            written = max(write_result(engine, meta, records, commit_check),
                          int(pending[1].get('changed_count', 0)) if pending is not None else 0)
            with self.db() as db:
                payload = json.dumps(dict(meta=meta, records=records, changed_count=written), default=str, allow_nan=False)
                db.execute('UPDATE pending SET payload=? WHERE id=1', (payload,))
            state.update(database_committed=True, stage='syncing_local', publication_pending=True)
            self.save_state(state)
            check()
            self.sync_local(meta, records)
            commit_check()
            state.update(stage='complete', last_success_at=datetime.now(CHINA).isoformat(),
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
            state.update(stage='publication_pending' if pending else 'failed', publication_pending=pending,
                         error='数据库/本地同步尚未全部确认，已保留待发布结果；重试将恢复同步。' if pending else '采集未完成；已保留上次成功结果。')
            LOG.error('Dividend collection failed run=%s details=%s', state['run_id'], diagnostic_details(exc))
        finally:
            state['active'] = False
            try:
                self.save_state(state)
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
