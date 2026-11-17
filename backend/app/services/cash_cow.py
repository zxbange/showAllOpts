from app.services.collection_failure_isolation import quarantine_publication, restore_manual_publication
from app.services.collection_calendar import automatic_collection_allowed, manual_failure_isolated
"""Shared external 3/5/10-year screen, published to MySQL and local SQLite.

Only complete, validated observations can qualify. Interrupted refreshes never
replace a published snapshot. All currency amounts are CNY; rates use percent.
"""
from app.services.system_runtime import source_client
from app.services.collection_notifications import notify_collection
from app.services.cash_cow_rules import PERIODS, evaluate_year, finish_stock, screen_cash_cows
from app.services.cash_cow_import import latest_cached, write_public, PUBLIC_SOURCE
from app.services.public_publication import PublicPublication
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
from time import monotonic

import sqlite3
import threading
import uuid

import httpx
import portalocker

from app.core.diagnostics import diagnostic_details

LOG = logging.getLogger(__name__)
CHINA = timezone(timedelta(hours=8))
RULE_VERSION = 'cash_cow_payout100_v3'


def now():
    return datetime.now(CHINA)


class CollectionCancelled(Exception):
    pass


class CashCowScreen:
    """A period-specific view of one cross-process collection and publication."""
    def __init__(self, settings, *, years=10, source_factory=None, client_factory=None, cache=None):
        if years not in PERIODS:
            raise ValueError('仅支持近3年、5年或10年')
        self.settings = settings
        self.cache = cache
        self.years = years
        self.schedule_enabled = settings.cash_cow_worker_enabled
        self.root = settings.log_path / 'market-data' / 'cash-cow'
        self.root.mkdir(parents=True, exist_ok=True)
        self.source_factory = source_factory
        self.client_factory = client_factory or (lambda: source_client(self.settings, factory=httpx.Client,
            timeout=httpx.Timeout(25, connect=10), trust_env=True,
            follow_redirects=False, headers={'User-Agent': 'Mozilla/5.0 ShowAllOpts/1.0'}))
        self.stop_event = threading.Event()
        self.worker = None
        with self.publication_lock(), self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
                INSERT OR IGNORE INTO state VALUES (1, '{}');
                CREATE TABLE IF NOT EXISTS snapshots (years INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            ''')
            # Upgrade without dropping an existing period's successful snapshot.
            # Legacy files remain untouched; only missing unified rows are seeded.
            legacy_success = {}
            for period in PERIODS:
                path = self.root / str(period) / 'screen.sqlite3'
                if (path.is_file() and not db.execute(
                        'SELECT 1 FROM snapshots WHERE years=?', (period,)).fetchone()):
                    legacy = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)
                    try:
                        row = legacy.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
                        if row:
                            db.execute('INSERT INTO snapshots VALUES (?,?)', (period, row[0]))
                            if legacy.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='state'").fetchone():
                                old_state = legacy.execute('SELECT payload FROM state WHERE id=1').fetchone()
                                if old_state:
                                    legacy_success[period] = json.loads(old_state[0]).get('scheduled_success_slot')
                    finally:
                        legacy.close()
            # A completely successful legacy month should not be re-collected on
            # upgrade. Partial legacy completion still requires one unified run.
            slots = set(legacy_success.values())
            if len(legacy_success) == len(PERIODS) and len(slots) == 1 and None not in slots:
                saved = db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0]
                if json.loads(saved) == {}:
                    db.execute('UPDATE state SET payload=? WHERE id=1',
                               (json.dumps(dict(scheduled_success_slot=slots.pop())),))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / 'screen.sqlite3', timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def read_state(self):
        with self.db() as db:
            return json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])

    def write_state(self, state):
        with self.db() as db:
            db.execute('UPDATE state SET payload=? WHERE id=1', (json.dumps(state, ensure_ascii=False),))

    def acquire(self):
        lock = portalocker.Lock(str(self.root / 'collection.lock'), timeout=0,
                                flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
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

    def cancel_path(self, state):
        identifier = state.get('run_id', '')
        if len(identifier) == 32 and all(c in '0123456789abcdef' for c in identifier):
            return self.root / ('cancel-' + identifier)
        return None

    def _status(self, state, result):
        from app.services.earnings_schedule import daily_next_run
        from app.services.system_configuration import task_config
        schedule = task_config(self.settings, 'earnings_selection')
        running = self.running()
        next_run = daily_next_run(at=schedule['times'][0]) if schedule['enabled'] else None
        marker = self.cancel_path(state)
        snapshot = result.get('snapshot') or {}
        return dict(years=self.years, periods=list(PERIODS), collection_years=max(PERIODS),
                    run_id=state.get('run_id'),
                    next_run_at=next_run.isoformat() if next_run else None,
                    schedule_time=schedule['times'][0], running=running, cancelling=bool(running and marker and marker.exists()),
                    stage=('上次更新中断，请重新更新' if not running and state.get('active')
                           else state.get('stage', '尚未筛选')),
                    processed=state.get('processed', 0), total=state.get('total'),
                    last_attempt_at=state.get('last_attempt_at'),
                    last_success_at=snapshot.get('updated_at'),
                    previous_success_at=snapshot.get('previous_updated_at'),
                    previous_previous_success_at=snapshot.get('previous_previous_updated_at'),
                    error=state.get('error'))

    def _read_result(self, db):
        row = db.execute('SELECT payload FROM snapshots WHERE years=?', (self.years,)).fetchone()
        result = json.loads(row[0]) if row else dict(snapshot=None, items=[])
        if result.get('snapshot') and result['snapshot'].get('source') not in (PUBLIC_SOURCE, '东方财富公开数据接口'):
            result = dict(snapshot=None, items=[])
        if self.cache is not None:
            imported = latest_cached(self.cache, self.years)
            def freshness(value):
                snapshot = value.get('snapshot') or {}
                return snapshot.get('as_of', ''), snapshot.get('updated_at', '')
            if imported is not None and freshness(imported) > freshness(result):
                result = imported
        # Keep the last successful screening, but never relabel its old TTM value.
        if result.get('snapshot', {}) and result['snapshot'].get('rule_version') != RULE_VERSION:
            result['rule_outdated'] = True
            for item in result['items']:
                item.update(current_yield=None, dividend_yield_history=None)
        return result

    def status(self):
        with self.publication_lock(), self.db() as db:
            state = json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
            return self._status(state, self._read_result(db))

    def latest(self):
        # Read snapshot and state under the publication lock: the response never
        # combines a new completion status with the previous run's snapshot.
        with self.publication_lock(), self.db() as db:
            state = json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
            result = self._read_result(db)
            return dict(result, status=self._status(state, result), years=self.years)

    def publication_lock(self):
        return portalocker.Lock(str(self.root / 'publication.lock'), timeout=5)

    def cancel(self):
        with self.publication_lock():
            state = self.read_state()
            if not self.running() or not state.get('active'):
                return False
            marker = self.cancel_path(state)
            if marker is None:
                return False
            marker.touch()
            return True

    def refresh(self, background=True, *, moment=None, slot=None):
        from app.services.earnings_schedule import acquire_execution
        moment = moment or now()
        if slot is not None and not automatic_collection_allowed(moment):
            return False
        if self.stop_event.is_set():
            return False
        lock = acquire_execution(self.settings, self.acquire)
        if lock is None:
            return False
        state = None
        try:
            moment = moment or now()
            if moment.tzinfo is None:
                raise ValueError('Collection requires a timezone-aware datetime')
            state = self.read_state()
            if state.get('run_isolated'):
                quarantine_publication(self, 'public_publication', state)
            state['run_isolated'] = slot is None and manual_failure_isolated(moment)
            if slot is None:
                restore_manual_publication(self, 'public_publication', state)
            state.update(run_id=uuid.uuid4().hex, active=True, stage='读取全A股股票池（统一更新3/5/10年）',
                         phase='读取全A股股票池', processed=0, total=None, error=None,
                         last_attempt_at=moment.isoformat(), current_scheduled_slot=slot)
            if slot:
                state.update(scheduled_slot=slot, scheduled_attempt_at=moment.isoformat())
            self.write_state(state)
            if background:
                self.worker = threading.Thread(target=self._collect, args=(lock, state, moment),
                                               name='cash-cow-screen', daemon=True)
                self.worker.start()
            else:
                self._collect(lock, state, moment)
        except BaseException as exc:
            LOG.error('Cash cow launch or cleanup failed details=%s', diagnostic_details(exc))
            try:
                if state is not None:
                    state.update(active=False, stage='启动失败', error='未能启动筛选，请重试。')
                    self.write_state(state)
            finally:
                lock.release()
            raise
        return True

    @notify_collection('cash_cow')
    def _collect(self, lock, state, moment):
        from app.services.cash_cow_source import CashCowSourceError
        from app.services.cash_cow_combined import CombinedCashCowSource
        marker = self.cancel_path(state)
        published = False
        publication = None
        started = monotonic()
        run_outcome = 'failed'
        LOG.info('Cash cow collection started source=asharehub+eastmoney+tushare periods=3,5,10')

        def check_cancel():
            if state.get('current_scheduled_slot') is None and not state.get('run_isolated') and manual_failure_isolated(moment + timedelta(seconds=monotonic()-started)):
                state['run_isolated'] = True
                self.write_state(state)
            if (state.get('current_scheduled_slot') is not None and not automatic_collection_allowed(moment + timedelta(seconds=monotonic()-started))) or self.stop_event.is_set() or marker.exists():
                raise CollectionCancelled()

        def progress(stage, processed=None, total=None):
            check_cancel()
            if processed is not None:
                state.update(phase=stage, stage=stage, processed=processed)
            else:
                state['stage'] = state.get('phase', '读取公开数据') + ' · ' + stage
            if total is not None:
                state['total'] = total
            self.write_state(state)

        def prepare(payloads):
            result = {}
            for period in PERIODS:
                check_cancel()
                result[str(period)] = json.dumps(payloads[str(period)], ensure_ascii=False, allow_nan=False)
            return result

        def apply(payloads, db):
            meta = json.loads(payloads['3'])['snapshot']
            for period in PERIODS:
                db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?)',
                    (period, payloads[str(period)]))
            state.update(active=False, last_success_at=meta['updated_at'],
                previous_success_at=meta.get('previous_updated_at'),
                previous_previous_success_at=meta.get('previous_previous_updated_at'),
                processed=meta['universe_count'], total=meta['universe_count'],
                stage='3/5/10年筛选完成', error=None)
            if state.get('current_scheduled_slot') and meta['as_of'] >= state['current_scheduled_slot']:
                state['scheduled_success_slot'] = state['current_scheduled_slot']
            db.execute('UPDATE state SET payload=? WHERE id=1', (json.dumps(state, ensure_ascii=False),))

        try:
            publication = PublicPublication(self)
            if publication.resume(write_public, apply, check_cancel, prepare):
                with self.db() as db:
                    restored = json.loads(db.execute('SELECT payload FROM snapshots WHERE years=3').fetchone()[0])
                if restored['snapshot']['as_of'] >= moment.astimezone(CHINA).date().isoformat():
                    published, run_outcome = True, 'success'
                    return
                state.update(active=True, processed=0, total=None, stage='读取本期公开数据')
                self.write_state(state)
            end_year = moment.astimezone(CHINA).year - 1
            with self.client_factory() as client:
                source = (self.source_factory or (lambda client, **kw: CombinedCashCowSource(client, settings=self.settings, **kw)))(client, check_cancel=check_cancel,
                    as_of=moment.astimezone(CHINA).date(), progress=progress,
                    cache_dir=self.root / 'source-cache')
                items, outcomes, stocks = screen_cash_cows(source, end_year, progress, check_cancel)
                check_cancel()
                updated_at = now().isoformat()
                # One SQLite transaction publishes all periods and shared state.
                # Prepare/serialize outside the publication lock so cancellation
                # can still be accepted during potentially expensive encoding.
                with self.db() as db:
                    previous = {period: json.loads(payload).get('snapshot') or {} for period, payload in
                                db.execute('SELECT years, payload FROM snapshots')}
                payloads = {}
                for period in PERIODS:
                    check_cancel()
                    if len(outcomes[period]) != len(stocks):
                        raise RuntimeError('Incomplete period classification')
                    items[period].sort(key=lambda row: (-row['average_yield'], row['code']))
                    excluded = sum(value == 'excluded' for value in outcomes[period].values())
                    insufficient = sum(value == 'insufficient' for value in outcomes[period].values())
                    old = previous.get(period, {})
                    snapshot = dict(as_of=moment.astimezone(CHINA).date().isoformat(), updated_at=updated_at,
                        run_id=state['run_id'], previous_updated_at=old.get('updated_at'),
                        previous_previous_updated_at=old.get('previous_updated_at'),
                        years=period, start_year=end_year - period + 1, end_year=end_year,
                        universe_count=len(stocks), evaluated_count=excluded + len(items[period]),
                        excluded_count=excluded, insufficient_count=insufficient, matched_count=len(items[period]),
                        source=PUBLIC_SOURCE, rule_version=RULE_VERSION)
                    payloads[str(period)] = dict(snapshot=snapshot, items=items[period])
                state['stage'] = '写入数据库并发布本地结果'
                self.write_state(state)
                publication.stage(payloads)
                # Remote I/O does not hold the UI/cancel publication lock.
                publication.resume(write_public, apply, check_cancel, prepare)
                published, run_outcome = True, 'success'
                for period in PERIODS:
                    LOG.info('Cash cow period complete years=%s matched=%s excluded=%s insufficient=%s',
                             period, len(items[period]),
                             sum(value == 'excluded' for value in outcomes[period].values()),
                             sum(value == 'insufficient' for value in outcomes[period].values()))
        except CollectionCancelled:
            run_outcome = 'cancelled'
            state.update(stage='已取消，保留上次结果')
        except Exception as exc:
            run_outcome = 'failed'
            LOG.error('Cash cow screen failed details=%s', diagnostic_details(exc))
            state.update(stage='更新失败，保留上次结果',
                         error=str(exc) if isinstance(exc, CashCowSourceError) else '筛选暂未完成，请稍后重试。')
        finally:
            state['active'] = False
            try:
                if not published and state.get('current_scheduled_slot') is None and (manual_failure_isolated(moment) or manual_failure_isolated(moment + timedelta(seconds=monotonic()-started))):
                    state['run_isolated'] = True
                    self.write_state(state)
                    quarantine_publication(self, 'public_publication', state)
                    state['manual_failure_isolated'] = True
                if publication is not None:
                    publication.close()
                if not published:
                    with self.publication_lock():
                        self.write_state(state)
            except BaseException as exc:
                run_outcome = 'failed'
                LOG.error('Cash cow state finalization failed details=%s', diagnostic_details(exc))
                raise
            finally:
                try:
                    marker.unlink(missing_ok=True)
                except BaseException as exc:
                    run_outcome = 'failed'
                    LOG.error('Cash cow marker cleanup failed details=%s', diagnostic_details(exc))
                    raise
                finally:
                    try:
                        lock.release()
                    except BaseException as exc:
                        run_outcome = 'failed'
                        LOG.error('Collection lock release failed details=%s', diagnostic_details(exc))
                        raise
                    finally:
                        LOG.info('Cash cow collection finished outcome=%s processed=%s total=%s published=%s duration_seconds=%.3f',
                                 run_outcome, state.get('processed', 0), state.get('total'), published, monotonic() - started)

    def stop(self):
        self.stop_event.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=30)
