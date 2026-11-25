"""Shared external 3/5/10-year screen, published to MySQL and local SQLite.

Only complete, validated observations can qualify. Interrupted refreshes never
replace a published snapshot. All currency amounts are CNY; rates use percent.
"""

import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from time import monotonic

import httpx
import portalocker

from app.core.diagnostics import diagnostic_details
from app.services.cash_cow_import import PUBLIC_SOURCE, latest_cached, write_public
from app.services.cash_cow_rules import PERIODS, evaluate_year, finish_stock, screen_cash_cows
from app.services.collection_calendar import automatic_collection_allowed, manual_failure_isolated
from app.services.collection_errors import safe_error
from app.services.collection_failure_isolation import (
    quarantine_publication,
    restore_manual_publication,
)
from app.services.collection_lifecycle import (
    RunContext,
    RunRequest,
    cancellation_marker,
    is_running,
    request_cancel,
    start_worker,
    try_lease,
)
from app.services.collection_notifications import notify_collection
from app.services.collection_outcomes import record_task
from app.services.public_publication import PublicPublication
from app.services.system_runtime import source_client

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
        return try_lease(lock, portalocker.exceptions.LockException)

    def running(self):
        return is_running(self.acquire)

    def cancel_path(self, state):
        return cancellation_marker(self.root, state.get('run_id'))

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
            return request_cancel(self.root, state.get('run_id'))

    def refresh(self, background=True, *, moment=None, slot=None):
        from app.services.earnings_schedule import (
            EarningsNotDue,
            acquire_execution,
            earnings_due_day,
            earnings_not_due_message,
        )
        moment = moment or now()
        if moment.tzinfo is None:
            raise ValueError('Collection requires a timezone-aware datetime')
        moment = moment.astimezone(CHINA)
        if slot is not None and not automatic_collection_allowed(moment):
            return False
        if self.stop_event.is_set():
            return False
        lock = acquire_execution(self.settings, self.acquire)
        if lock is None:
            return False
        state = None
        initialized = False
        try:
            # The application uses a current-only universe. Dated standalone
            # adapters may be injected explicitly by callers.
            current_only = self.source_factory is None
            due_day = earnings_due_day(self.settings, moment) if current_only else moment.date()
            state = self.read_state()
            if state.get('run_isolated'):
                quarantine_publication(self, 'public_publication', state)
            state['run_isolated'] = slot is None and manual_failure_isolated(moment)
            restored = slot is None and restore_manual_publication(self, 'public_publication', state)
            with self.db() as db:
                pending = (db.execute(
                    "SELECT json_extract(payload, '$.\"3\".snapshot.as_of'), "
                    "json_extract(payload, '$.\"3\".snapshot.run_id') FROM public_publication WHERE id=1"
                ).fetchone() if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='public_publication'"
                ).fetchone() else None)
            if current_only and (due_day is None or (pending and (not isinstance(pending[0], str)
                                                                or pending[0] > due_day.isoformat()))
                                 or (not pending and due_day != moment.date())):
                if restored:
                    quarantine_publication(self, 'public_publication', state)
                raise EarningsNotDue(earnings_not_due_message(due_day))
            same_pending = bool(pending and pending[1] == state.get('pending_run_id'))
            state.update(run_id=uuid.uuid4().hex, active=True, stage='读取全A股股票池（统一更新3/5/10年）',
                         due_day=due_day.isoformat(),
                         target_date=pending[0] if pending else due_day.isoformat(),
                         database_committed=bool(same_pending and state.get('database_committed')),
                         database_commit_uncertain=bool(same_pending and state.get('database_commit_uncertain')),
                         database_phase=None, database_retry_attempt=1,
                         publication_pending=bool(pending), changed_count_unconfirmed=False,
                         phase='读取全A股股票池', processed=0, total=None, error=None,
                         last_attempt_at=moment.isoformat(), current_scheduled_slot=slot)
            if slot:
                state.update(scheduled_slot=slot, scheduled_attempt_at=moment.isoformat())
            self.write_state(state)
            initialized = True
            if background:
                start_worker(self._collect, args=(lock, state, moment), name='cash-cow-screen',
                             assign=lambda worker: setattr(self, 'worker', worker), factory=threading.Thread)
        except EarningsNotDue:
            lock.release()
            raise
        except BaseException as exc:
            LOG.error('Cash cow launch or cleanup failed details=%s', diagnostic_details(exc))
            try:
                if initialized:
                    state.update(active=False, stage='启动失败', error='未能启动筛选，请重试。')
                    self.write_state(state)
            finally:
                lock.release()
            raise
        if not background:
            try:
                self._collect(lock, state, moment)
            finally:
                lock.release()
        return True

    @notify_collection('cash_cow')
    def _collect(self, lock, state, moment):
        from app.services.cash_cow_combined import CombinedCashCowSource
        from app.services.cash_cow_source import CashCowSourceError
        marker = self.cancel_path(state)
        assert marker is not None
        slot = state.get('current_scheduled_slot')
        context = RunContext(
            RunRequest(moment, slot, 'scheduled' if slot is not None else 'manual'), marker,
            self.stop_event, automatic_collection_allowed, manual_failure_isolated,
            CollectionCancelled, monotonic,
        )
        published = False
        publication = None
        run_outcome = 'failed'
        run_error = None
        error_stage = 'database_write'
        LOG.info('Cash cow collection started source=asharehub+eastmoney+tushare periods=3,5,10')

        def check_cancel():
            if not state.get('run_isolated') and context.isolate_manual_failure(include_requested=False):
                state['run_isolated'] = True
                self.write_state(state)
            context.check_cancel()

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
            nonlocal error_stage
            # The remote writer returned only after a successful transaction or
            # a full canonical read. Failures after this point are local publication.
            error_stage = 'cache_publish'
            state.update(database_committed=True, database_commit_uncertain=False,
                         database_phase='local_publish', stage='发布本地3/5/10年结果')
            self.write_state(state)
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
            completed_state = dict(state, active=False, last_success_at=meta['updated_at'],
                previous_success_at=meta.get('previous_updated_at'),
                previous_previous_success_at=meta.get('previous_previous_updated_at'),
                processed=meta['universe_count'], total=meta['universe_count'],
                stage='3/5/10年筛选完成', error=None, result_date=meta['as_of'],
                target_date=meta['as_of'], publication_pending=False,
                database_commit_uncertain=False, changed_count_unconfirmed=False,
                pending_run_id=None, database_phase=None)
            if state.get('current_scheduled_slot') and meta['as_of'] >= state['current_scheduled_slot']:
                completed_state['scheduled_success_slot'] = state['current_scheduled_slot']
            # Mutate the live state only after the local transaction commits.
            # A cancelled/failed apply must not advance success timestamps.
            db.execute('UPDATE state SET payload=? WHERE id=1', (json.dumps(completed_state, ensure_ascii=False),))

        def database_progress(phase):
            stages = {'schema': '检查奶牛股数据库表结构', 'lock': '读取并锁定奶牛股数据库快照',
                      'write': '写入奶牛股数据库快照', 'commit': '提交奶牛股数据库事务'}
            state.update(database_phase=phase, stage=stages[phase])
            if phase == 'commit':
                # Record before COMMIT: process death can lose its acknowledgement
                # without running an exception handler. Reconciliation clears it.
                state['database_commit_uncertain'] = True
            self.write_state(state)

        def write_cash(engine, payload, check):
            nonlocal error_stage
            error_stage = 'database_write'
            meta = payload['3']['snapshot']
            if state.get('pending_run_id') != meta['run_id']:
                state.update(database_committed=False, database_commit_uncertain=False)
            state.update(target_date=meta['as_of'], pending_run_id=meta['run_id'],
                         database_phase=None, publication_pending=True)
            self.write_state(state)
            try:
                return write_public(engine, payload, check, progress=database_progress)
            except BaseException:
                # Keep an ambiguous acknowledgement across retries/restarts. A
                # successful full-snapshot reconciliation clears it in prepare().
                if state.get('database_phase') == 'commit':
                    state['database_commit_uncertain'] = True
                    self.write_state(state)
                raise

        def retrying(attempt, exc):
            state.update(database_retry_attempt=attempt, stage=f'奶牛股数据库重试 {attempt}/3')
            self.write_state(state)

        def resume_cash():
            restored = publication.resume(write_cash, apply, check_cancel, prepare,
                                         database_retry=True, on_retry=retrying)
            if restored:
                state.update(self.read_state())
            return restored

        def pending_status():
            with self.db() as db:
                pending = (db.execute('SELECT 1 FROM public_publication WHERE id=1').fetchone()
                           if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='public_publication'").fetchone()
                           else None)
            committed = bool(pending and (state.get('database_committed')
                             or (publication is not None and publication.database_committed)))
            uncertain = bool(pending and state.get('database_commit_uncertain'))
            state.update(publication_pending=bool(pending), database_committed=committed,
                         changed_count_unconfirmed=committed or uncertain)
            return committed or uncertain

        try:
            publication = PublicPublication(self)
            if resume_cash():
                with self.db() as db:
                    restored = json.loads(db.execute('SELECT payload FROM snapshots WHERE years=3').fetchone()[0])
                if (restored['snapshot']['as_of'] >= moment.date().isoformat()
                        or state.get('due_day', moment.date().isoformat()) != moment.date().isoformat()):
                    published, run_outcome = True, 'success'
                    return
                state.update(active=True, processed=0, total=None, stage='读取本期公开数据')
                self.write_state(state)
            error_stage = 'fetch'
            state.update(target_date=moment.date().isoformat(), database_committed=False,
                         database_commit_uncertain=False, publication_pending=False,
                         changed_count_unconfirmed=False, pending_run_id=None,
                         database_phase=None, database_retry_attempt=1)
            self.write_state(state)
            end_year = moment.astimezone(CHINA).year - 1
            with self.client_factory() as client:
                source = (self.source_factory or (lambda client, **kw: CombinedCashCowSource(client, settings=self.settings, **kw)))(client, check_cancel=check_cancel,
                    as_of=moment.astimezone(CHINA).date(), progress=progress,
                    cache_dir=self.root / 'source-cache')
                error_stage = 'fetch'
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
                error_stage = 'database_write'
                state['stage'] = '写入数据库并发布本地结果'
                self.write_state(state)
                publication.stage(payloads)
                # Remote I/O does not hold the UI/cancel publication lock.
                resume_cash()
                published, run_outcome = True, 'success'
                for period in PERIODS:
                    LOG.info('Cash cow period complete years=%s matched=%s excluded=%s insufficient=%s',
                             period, len(items[period]),
                             sum(value == 'excluded' for value in outcomes[period].values()),
                             sum(value == 'insufficient' for value in outcomes[period].values()))
        except CollectionCancelled:
            run_outcome = 'cancelled'
            pending_status()
            state.update(stage='已取消，保留上次结果')
        except Exception as exc:
            run_outcome = 'publication_pending' if pending_status() else 'failed'
            run_error = safe_error(exc, stage=error_stage, operation=state.get('database_phase'),
                                   trade_date=state.get('target_date'))
            LOG.error('Cash cow screen failed details=%s', diagnostic_details(exc))
            state.update(stage='更新失败，保留上次结果',
                         error=str(exc) if isinstance(exc, CashCowSourceError) else '筛选暂未完成，请稍后重试。')
        finally:
            state['active'] = False
            try:
                if not published and context.isolate_manual_failure():
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
                run_error = safe_error(exc, stage='cache_publish')
                LOG.error('Cash cow state finalization failed details=%s', diagnostic_details(exc))
                raise
            finally:
                try:
                    marker.unlink(missing_ok=True)
                except BaseException as exc:
                    run_outcome = 'failed'
                    run_error = safe_error(exc, stage='collection')
                    LOG.error('Cash cow marker cleanup failed details=%s', diagnostic_details(exc))
                    raise
                finally:
                    try:
                        try:
                            record_task(self.settings, 'cash_cow', outcome=run_outcome, scheduled=state.get('current_scheduled_slot') is not None, run_key=state['run_id'], error_details=run_error if run_outcome != 'success' else None)
                        finally:
                            lock.release()
                    except BaseException as exc:
                        run_outcome = 'failed'
                        LOG.error('Collection lock release failed details=%s', diagnostic_details(exc))
                        raise
                    finally:
                        LOG.info('Cash cow collection finished outcome=%s processed=%s total=%s published=%s duration_seconds=%.3f',
                                 run_outcome, state.get('processed', 0), state.get('total'), published, context.elapsed)

    def stop(self):
        self.stop_event.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=30)
