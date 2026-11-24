"""Low-frequency exchange collection. All state is local and durable.

ETF collection and display reads can share a page-configured database account.
"""
from app.services.collection_calendar import automatic_collection_allowed, latest_due_slot, manual_failure_isolated
from app.services.system_runtime import source_client
from app.services.system_configuration import pin_configuration
from app.services import collection_notifications
from datetime import date, datetime, timedelta
from contextlib import contextmanager
from zoneinfo import ZoneInfo
import sqlite3
import errno
import threading
import logging
import json
import time
import uuid

from app.core.diagnostics import diagnostic_details
from app.services.collection_errors import safe_error, sanitize_error

from app.services.etf_configuration import ETFConfiguration, ConfigurationFailure, DEFAULT_COLLECTION_TIME, target_key, reconcile_system_ledger
from app.services.etf_diagnostics import database_diagnostic, database_write_run, log_database_failure, log_database_retry
from app.services.etf_notifications import ETFRunSummary

import httpx
import portalocker
from sqlalchemy import create_engine

LOG = logging.getLogger(__name__)
CHINA = ZoneInfo('Asia/Shanghai')
TIMES = (tuple(map(int, DEFAULT_COLLECTION_TIME.split(':'))),)
_MANUAL_GAP_KEYS = "(key='manual_gap_floor' OR key GLOB 'manual_gap_floor_deferred:*')"


class CollectionInterrupted(RuntimeError):
    """Stop at a safe boundary without abandoning a database transaction."""


class CollectionCancelled(CollectionInterrupted):
    """A durable, run-specific manual cancellation was requested."""


class CancellableHTTPClient:
    def __init__(self, client, check):
        self.client, self.check = client, check

    def __getattr__(self, name):
        return getattr(self.client, name)

    def get(self, *args, **kwargs):
        self.check()
        result = self.client.get(*args, **kwargs)
        self.check()
        return result


class _ScopedSourceGuard:
    """One scoped source's release window and rate limit, confined to this run."""
    def __init__(self):
        self.rate_limited = False
        self.available_from = None

    def skip_reason(self, day):
        if self.rate_limited:
            return 'rate_limit'
        if self.available_from is not None and day < self.available_from:
            return 'archive'
        return None

    def observe(self, error_details, available_from):
        if error_details.get('http_status') == 429:
            self.rate_limited = True
        if available_from is not None and (self.available_from is None or available_from > self.available_from):
            self.available_from = available_from


def _observation_source(kind, code, day, today, guards):
    if kind == 'shares' and code.endswith('.XSHG'):
        return 'sse_scoped_shares', {'scoped_sse': True}, guards['sse_scoped_shares']
    if kind == 'prices' and code.endswith('.XSHE') and day < today:
        return 'szse_dated_close', {'dated_szse_prices': True}, guards['szse_dated_close']
    return 'exchanges', {}, None


def _observation_error(exc, day, guard):
    from app.services.etf_exchange import SZSEPriceArchiveRequired
    available_from = exc.available_from if isinstance(exc, SZSEPriceArchiveRequired) else None
    code = 'etf_szse_price_archived' if available_from is not None else None
    details = safe_error(exc, code=code, stage='fetch', trade_date=day)
    if guard is not None:
        guard.observe(details, available_from)
    return details


class WriterFailure(RuntimeError):
    """Safe public classification only; original driver message is never retained."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def writer_error_code(exc):
    original = getattr(exc, 'orig', exc)
    args = getattr(original, 'args', ())
    number = getattr(original, 'database_error_number', None)
    if number is None:
        number = args[0] if args and isinstance(args[0], int) else None
    if number in {1044, 1045, 1142, 1143, 1227, 1370}:
        return 'writer_permission_denied'
    if number == 1146:
        return 'writer_table_missing'
    if number == 1049:
        return 'writer_database_missing'
    if number in {2002, 2003, 2005, 2006, 2013, 2055}:
        return 'writer_connection_failed'
    return 'writer_write_failed'


# A writer retry is only safe at checkout, before any schema check,
# business DML or COMMIT. The separate historic probe has no writes at all.
_CHECKOUT_PHASES = frozenset({
    'connect', 'socket_connect', 'mysql_connect', 'connect_socket', 'authenticate',
})
_HISTORICAL_READ_PHASES = frozenset({'load_historical_keys', 'load_share_keys', 'load_price_keys'})
_TRANSIENT_MYSQL = frozenset({2006, 2013, 2055})
_TRANSIENT_SOCKET = frozenset({errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE, errno.ETIMEDOUT})


def _transient_pre_write_failure(diagnostic, operation_stage):
    safe_phase = (diagnostic.stage in _CHECKOUT_PHASES or
                  (operation_stage == 'load_historical_keys' and diagnostic.stage in _HISTORICAL_READ_PHASES))
    if not safe_phase:
        return False
    transient_socket = diagnostic.socket_timeout or diagnostic.os_errno in _TRANSIENT_SOCKET
    if diagnostic.mysql_code is not None:
        return (diagnostic.mysql_code in _TRANSIENT_MYSQL or
                (diagnostic.mysql_code == 2003 and transient_socket))
    return transient_socket


def _safe_database_error(exc, diagnostic):
    details = safe_error(exc, stage='database_write')
    # The shared connector can deliberately detach the raw driver exception
    # while retaining its numeric code and verified socket-timeout flag.
    if diagnostic.socket_timeout:
        details['code'] = 'timeout'
    elif diagnostic.mysql_code is not None and details['code'] in ('unknown', 'invalid_data'):
        details['code'] = 'database_error'
    if diagnostic.mysql_code is not None:
        details['mysql_errno'] = diagnostic.mysql_code
    return sanitize_error(details)


class _RunDatabase:
    """A lazy engine for one pinned config; no connection is held across HTTP."""
    def __init__(self, engine_factory, config, check_cancelled):
        self.engine_factory = engine_factory
        self.config = config
        self.check_cancelled = check_cancelled
        self.engine = None
        self.retry_used = False
        self.stage = 'engine_create'

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.engine is not None:
            try:
                self.engine.dispose()
            except Exception as exc:
                # Cleanup cannot hide the previous phase or negate a commit.
                LOG.warning('ETF writer engine cleanup failed %s', diagnostic_details(exc))

    def execute(self, operation, *, stage):
        self.check_cancelled()
        self.stage = 'engine_create'
        if self.engine is None:
            self.engine = self.engine_factory(self.config)
        self.stage = stage
        try:
            return operation(self.engine)
        except CollectionInterrupted:
            raise
        except Exception as exc:
            diagnostic = database_diagnostic(exc, stage)
            if self.retry_used or not _transient_pre_write_failure(diagnostic, stage):
                raise
            self.check_cancelled()
            self.retry_used = True
            try:
                # No checkout remains at this point. Replace the old pool
                # on the same pinned engine; next checkout opens a new socket.
                self.engine.dispose()
            except Exception as cleanup:
                LOG.warning('ETF writer retry cleanup failed %s', diagnostic_details(cleanup))
                replacement_failed = True
            else:
                replacement_failed = False
            if replacement_failed:
                raise
            log_database_retry(diagnostic)
        # Outside the first failure handler: a retry fault must not inherit
        # the first error's raw driver context or be classified by its phase.
        self.check_cancelled()
        return operation(self.engine)


def scheduled_slots(moment, schedule=None):
    if moment.tzinfo is None:
        raise ValueError('Timezone-aware time required')
    local = moment.astimezone(CHINA)
    return [local.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in (TIMES if schedule is None else [tuple(map(int, item.split(':'))) for item in schedule])]


def collector_module():
    from app.services import etf_exchange
    return etf_exchange


class ETFCollection:
    def __init__(self, settings, refresh=None, *, collector=None, engine_factory=create_engine,
                 client_factory=None, clock=None):
        self.settings, self.refresh = settings, refresh
        self.collector, self.engine_factory = collector, engine_factory
        self.client_factory = client_factory or (lambda: source_client(self.settings, factory=httpx.Client, timeout=30, trust_env=False))
        self.clock = clock or (lambda: datetime.now(CHINA))
        self.root = settings.log_path / 'market-data' / 'etf-collection'
        self.stop_event = threading.Event()
        self.worker = None
        self.floor = None
        self.wake_event = threading.Event()
        self.thread_lock = threading.Lock()
        self.configuration = ETFConfiguration(self)

    @property
    def configuration_issue(self):
        try:
            config = self.configuration.load()
            if config.get('legacy_error'):
                return config['legacy_error']
            if config['password'] is None or not config['username']:
                return 'missing_writer_configuration'
            if config.get('target_mode') != 'custom':
                self.configuration.validate_target(self.configuration.url(config))
        except ConfigurationFailure as error:
            return str(error) if str(error) in {'writer_target_mismatch', 'invalid_writer_configuration'} else 'configuration_unavailable'
        except Exception:
            return 'invalid_writer_configuration'
        return None

    @property
    def configured(self):
        return self.configuration_issue is None

    @contextmanager
    def connect(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.root / 'state.sqlite', timeout=10)
        db.row_factory = sqlite3.Row
        db.executescript('''
            CREATE TABLE IF NOT EXISTS configuration (id INTEGER PRIMARY KEY CHECK(id=1),
              revision INTEGER NOT NULL, ciphertext BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS manual_runs (id TEXT PRIMARY KEY, status TEXT NOT NULL,
              requested_at TEXT NOT NULL, error TEXT, phase TEXT, completed INTEGER DEFAULT 0,
              total INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS manual_run_scope (
              id TEXT PRIMARY KEY, schedule TEXT NOT NULL, retry_exhausted INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS slots (slot TEXT PRIMARY KEY, status TEXT NOT NULL,
              attempts INTEGER NOT NULL, attempted_at TEXT NOT NULL, error TEXT);
            CREATE TABLE IF NOT EXISTS run_history (
              id TEXT PRIMARY KEY, trigger TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
              requested_at TEXT NOT NULL, finished_at TEXT, slot TEXT, attempt INTEGER,
              time_basis TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS run_history_errors (id TEXT PRIMARY KEY, details TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (kind TEXT, code TEXT, day TEXT, status TEXT,
              attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind,code,day));
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
        ''')
        (self.root / 'state.sqlite').chmod(0o600)
        try:
            with db:
                self._migrate_run_history(db)
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _migrate_run_history(db):
        # Legacy state records only the most recent attempt for each automatic
        # slot, and no completion timestamp. Preserve what exists without
        # inventing previous attempts or their finish times.
        if db.execute("SELECT 1 FROM metadata WHERE key='run_history_migrated'").fetchone():
            return
        records = []
        terminal = {'complete', 'partial', 'failed', 'interrupted', 'cancelled', 'abandoned'}
        for row in db.execute('SELECT * FROM slots'):
            if row['status'] in terminal:
                records.append(('legacy-slot:' + row['slot'], 'scheduled', row['status'], row['error'],
                                row['attempted_at'], None, row['slot'], row['attempts'], 'legacy_request_time'))
        for row in db.execute('SELECT * FROM manual_runs'):
            if row['status'] in terminal:
                records.append(('legacy-manual:' + row['id'], 'manual', row['status'], row['error'],
                                row['requested_at'], None, None, None, 'legacy_request_time'))
        db.executemany('INSERT OR IGNORE INTO run_history VALUES (?,?,?,?,?,?,?,?,?)',
                       sorted(records, key=lambda row: row[4]))
        db.execute("INSERT OR IGNORE INTO metadata VALUES ('run_history_migrated','1')")

    @staticmethod
    def _write_history(db, statement, values):
        # Audit storage must not roll back the separately persisted scheduling
        # outcome or turn a committed MySQL publication into a retry failure.
        try:
            db.execute('SAVEPOINT etf_history_write')
            db.execute(statement, values)
            db.execute('RELEASE etf_history_write')
            return True
        except sqlite3.Error as exc:
            try:
                db.execute('ROLLBACK TO etf_history_write')
                db.execute('RELEASE etf_history_write')
            except sqlite3.Error:
                pass
            LOG.warning('ETF terminal history update unavailable error_type=%s', type(exc).__name__)
            return False

    def _reconcile_target_change(self, issue):
        if issue != 'writer_target_mismatch' or not (self.root / 'state.sqlite').exists():
            return
        try:
            with portalocker.Lock(str(self.root / 'collection.lock'), timeout=0):
                with self.connect() as db:
                    self._recover_manual_runs(db, reason='configuration_changed')
                    stamp = self.clock().isoformat()
                    interrupted = db.execute("UPDATE manual_runs SET status='interrupted',phase='interrupted',"
                                             "error='configuration_changed' WHERE status='queued' RETURNING id,requested_at").fetchall()
                    for row in interrupted:
                        self._write_history(db, 'INSERT OR IGNORE INTO run_history VALUES (?,?,?,?,?,?,?,?,?)',
                            ('manual:' + row['id'], 'manual', 'interrupted', 'configuration_changed',
                             row['requested_at'], stamp, None, None, 'recovered_at'))
                        db.execute('DELETE FROM manual_run_scope WHERE id=?', (row['id'],))
                    db.execute("DELETE FROM metadata WHERE key='progress'")
                    db.commit()
                    for row in interrupted:
                        self._notify_terminal('manual:' + row['id'], 'interrupted', stamp,
                                              error_details=safe_error(code='interrupted', stage='collection'))
        except portalocker.exceptions.LockException:
            pass  # An existing collector still owns this run; never steal it.

    def status(self):
        issue = self.configuration_issue
        self._reconcile_target_change(issue)
        try:
            config = self.configuration.load()
        except ConfigurationFailure:
            return dict(status=issue, ready=False, configured=False, disabled_reason=issue)
        state = dict(enabled=config['enabled'], configured=not issue, ready=not issue,
                     status=issue or ('configured' if config['enabled'] else 'disabled'),
                     disabled_reason=issue, worker_running=bool(self.worker and self.worker.is_alive()),
                     timezone='Asia/Shanghai', schedule=config['schedule'], target_sessions=1,
                     collection_scope='latest_completed_session', historical_backfill='joinquant_manual',
                     manual_collection_scope='known_trading_session_gaps', manual_gap_start=None,
                     historical_dates_per_run=0, coverage_is_target_not_completion=True,
                     manual=None, progress=None, runs=[], recent_runs=[], latest_result=None, gaps=[], metadata={})
        if not (self.root / 'state.sqlite').exists():
            return state
        with self.connect() as db:
            state['runs'] = [dict(r) for r in db.execute('SELECT * FROM slots ORDER BY slot DESC LIMIT 9')]
            state['recent_runs'] = [dict(r) for r in db.execute("SELECT * FROM run_history WHERE status NOT IN ('running','publishing') ORDER BY (finished_at IS NOT NULL) DESC,julianday(COALESCE(finished_at,requested_at)) DESC,rowid DESC LIMIT 20")]
            for item in state['recent_runs']:
                detail = db.execute('SELECT details FROM run_history_errors WHERE id=?', (item['id'],)).fetchone()
                item['error_details'] = json.loads(detail[0]) if detail else None
            state['latest_result'] = next(iter(state['recent_runs']), None)
            state['metadata'] = dict(db.execute('SELECT key,value FROM metadata').fetchall())
            latest = state['metadata'].get('coverage_end')
            gap = db.execute(f"SELECT min(value) FROM metadata WHERE {_MANUAL_GAP_KEYS} "
                             "AND (? IS NULL OR value<?)", (latest, latest)).fetchone()
            state['manual_gap_start'] = gap[0]
            where, values = ('day=?', (latest,)) if latest else ('0=1', ())
            counts = dict(db.execute(f'SELECT status,COUNT(*) FROM observations WHERE {where} GROUP BY status', values))
            state['pending_count'] = counts.get('pending', 0)
            state['blocked_count'] = counts.get('blocked_unverified_history', 0)
            state['retry_exhausted_count'] = counts.get('retry_exhausted', 0)
            state['gap_count'] = sum(count for status, count in counts.items() if status != 'complete')
            # Keep responses bounded; counts cover the complete current target window.
            state['gaps'] = [dict(r) for r in db.execute(
                f"SELECT * FROM observations WHERE {where} AND status!='complete' ORDER BY day,code,kind LIMIT 500", values)]
            manual_after = int(state['metadata'].get('manual_after_rowid', '0'))
            row = db.execute('SELECT * FROM manual_runs WHERE rowid>? ORDER BY rowid DESC LIMIT 1', (manual_after,)).fetchone()
            state['manual'] = dict(row) if row else None
        writer_issue = state['metadata'].get('writer_error') or None
        if writer_issue:
            state.update(status=writer_issue, ready=False, disabled_reason=writer_issue)
        state['cache_pending'] = state['metadata'].get('cache_pending') == '1'
        raw_progress = state['metadata'].pop('progress', None)
        if raw_progress:
            state['progress'] = json.loads(raw_progress)
        return state

    def configure(self, body):
        self._reconcile_target_change(self.configuration_issue)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with portalocker.Lock(str(self.root / 'collection.lock'), timeout=0):
                with self.connect() as db:
                    if db.execute("SELECT 1 FROM manual_runs WHERE status='queued'").fetchone():
                        raise ConfigurationFailure('采集任务已排队，请等待完成后修改配置')
                result = self.configuration.save(body)
                # Changes take effect for future slots, never launch past slots immediately.
                self.floor = self.clock()
        except portalocker.exceptions.LockException:
            raise ConfigurationFailure('采集正在运行，请等待完成后修改配置') from None
        self.start()
        return result

    def test_configuration(self, body):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with portalocker.Lock(str(self.root / 'collection.lock'), timeout=0):
                return self.configuration.test(body)
        except portalocker.exceptions.LockException:
            raise ConfigurationFailure('采集正在运行，请稍后测试连接') from None

    def request_run(self, request_id, retry_exhausted=False):
        requested_at = self.clock().isoformat()
        if self.configuration_issue:
            raise ConfigurationFailure('采集配置尚未就绪，请先在页面保存数据库连接')
        with self.connect() as db:
            row = db.execute('SELECT * FROM manual_runs WHERE id=?', (request_id,)).fetchone()
            if row:
                return dict(row)
        try:
            with portalocker.Lock(str(self.root / 'collection.lock'), timeout=0):
                with self.connect() as db:
                    row = db.execute('SELECT * FROM manual_runs WHERE id=?', (request_id,)).fetchone()
                    if row:
                        return dict(row)
                    # Acquiring the shared lock proves no running job still owns it.
                    self._recover_manual_runs(db)
                    row = db.execute("SELECT * FROM manual_runs WHERE status='queued' LIMIT 1").fetchone()
                    if row:
                        if row['id'] == request_id:
                            return dict(row)
                        raise ConfigurationFailure('已有采集任务排队，请勿重复触发')
                    schedule = self.configuration.load().get('schedule')
                    db.execute('INSERT INTO manual_runs(id,status,requested_at,phase) VALUES (?,?,?,?)',
                               (request_id, 'queued', requested_at, 'queued'))
                    db.execute('INSERT INTO manual_run_scope VALUES (?,?,?)', (
                        request_id, json.dumps(schedule), int(retry_exhausted)))
                    result = dict(db.execute('SELECT * FROM manual_runs WHERE id=?', (request_id,)).fetchone())
        except portalocker.exceptions.LockException:
            raise ConfigurationFailure('采集正在运行，请等待本轮完成') from None
        self.start()
        self.wake_event.set()
        return result

    def _notify_terminal(self, run_key, outcome, stamp, *, scheduled=False, changed=None,
                         summary=(), data_date=None, error_details=None):
        try:
            collection_notifications.emit_collection_result(
                self.settings, 'etf', scheduled=scheduled, run_key=run_key, outcome=outcome,
                changed_count=changed, summary=summary, collected_at=stamp, data_date=data_date,
                error_details=error_details)
        except Exception as exc:
            LOG.warning('ETF notification result unavailable error_type=%s', type(exc).__name__)

    def _recover_manual_runs(self, db, reason='service_interrupted'):
        # Call only while owning collection.lock: no other worker can still run.
        stamp = self.clock().isoformat()
        restarting = {r['id']: r['trigger'] for r in db.execute(
            "SELECT id,trigger FROM run_history WHERE status IN ('running','publishing')")}
        for row in db.execute("SELECT * FROM manual_runs WHERE status IN ('running','cancelling')").fetchall():
            restarting['manual:' + row['id']] = 'manual'
            outcome = 'cancelled' if row['status'] == 'cancelling' else 'interrupted'
            db.execute('INSERT OR IGNORE INTO run_history VALUES (?,?,?,?,?,?,?,?,?)',
                       ('manual:' + row['id'], 'manual', 'running', None, row['requested_at'], None,
                        None, None, 'started_at'))
            db.execute("UPDATE run_history SET status=?,error=?,finished_at=?,time_basis='recovered_at' "
                       "WHERE id=? AND status IN ('running','publishing')",
                       (outcome, None if outcome == 'cancelled' else reason, stamp, 'manual:' + row['id']))
        # Older versions did not create an attempt row when a scheduled run began.
        for row in db.execute("SELECT * FROM slots WHERE status='running'").fetchall():
            if not db.execute('SELECT 1 FROM run_history WHERE slot=? AND attempt=?',
                              (row['slot'], row['attempts'])).fetchone():
                run_key = 'recovered-slot:' + row['slot'] + ':' + str(row['attempts'])
                restarting[run_key] = 'scheduled'
                db.execute('INSERT INTO run_history VALUES (?,?,?,?,?,?,?,?,?)',
                           (run_key, 'scheduled',
                            'running', None, row['attempted_at'], None, row['slot'], row['attempts'], 'started_at'))
        db.execute("UPDATE run_history SET status='interrupted',error=?,finished_at=?,time_basis='recovered_at' "
                   "WHERE status IN ('running','publishing')", (reason, stamp))
        db.execute("UPDATE manual_runs SET status='cancelled',phase='cancelled',error=NULL WHERE status='cancelling'")
        db.execute("UPDATE manual_runs SET status='interrupted',phase='interrupted',error=? WHERE status='running'", (reason,))
        db.execute("DELETE FROM manual_run_scope WHERE id NOT IN (SELECT id FROM manual_runs WHERE status='queued')")
        db.commit()
        from app.services.collection_outcomes import record_outcome
        # Repair the observation gap if a process exited after committing its
        # terminal state but before calling the shared outcome observer. Unknown
        # legacy completion times never enter this indicator. The ledger ignores
        # duplicate run IDs, so recovery cannot rewrite old outcomes.
        latest = db.execute("SELECT * FROM run_history WHERE finished_at IS NOT NULL "
                            "AND status NOT IN ('running','publishing') "
                            "ORDER BY julianday(finished_at) DESC,rowid DESC LIMIT 1").fetchone()
        if latest:
            detail = db.execute('SELECT details FROM run_history_errors WHERE id=?', (latest['id'],)).fetchone()
            error_details = json.loads(detail[0]) if detail else (safe_error(code='interrupted', stage='collection') if latest['status'] == 'interrupted' else None)
            if error_details and not detail:
                self._write_history(db, 'INSERT OR REPLACE INTO run_history_errors VALUES (?,?)',
                                    (latest['id'], json.dumps(error_details)))
                db.commit()
            record_outcome(self.settings, 'etf-shares', run_key=latest['id'], outcome=latest['status'],
                           trigger=latest['trigger'], finished_at=latest['finished_at'], error_details=error_details)
        for run_key, trigger in sorted(restarting.items()):
            result = db.execute('SELECT status,finished_at FROM run_history WHERE id=?', (run_key,)).fetchone()
            if result and result['status'] in ('interrupted', 'cancelled'):
                self._notify_terminal(run_key, result['status'], result['finished_at'],
                    scheduled=trigger == 'scheduled',
                    error_details=safe_error(code=result['status'], stage='collection'))
        self._retain_history()

    def _retain_history(self):
        if getattr(self.settings, 'collection_history_retention_enabled', False):
            from app.services.etf_collection_history import prune_native_history
            try:
                prune_native_history(self.settings, apply=True, confirmed=True)
            except Exception as exc:
                LOG.warning('ETF history archival unavailable error_type=%s', type(exc).__name__)


    def request_cancel(self, request_id):
        # The worker owns collection.lock for its entire run. Do not wait for that
        # lock before recording cancellation: another API process must be able to
        # cancel while a network call or a MySQL transaction is still in flight.
        transitioned = False
        with self.connect() as db:
            transitioned = bool(db.execute("""UPDATE manual_runs SET
                phase=CASE WHEN status='queued' THEN 'cancelled' ELSE phase END,
                status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END,
                error=NULL WHERE id=? AND status IN ('queued','running')""", (request_id,)).rowcount)
            row = db.execute('SELECT * FROM manual_runs WHERE id=?', (request_id,)).fetchone()
            if row is None:
                raise ConfigurationFailure('未找到该手动采集任务，请刷新采集状态')
        try:
            with portalocker.Lock(str(self.root / 'collection.lock'), timeout=0):
                with self.connect() as db:
                    # Recover an already-dead worker immediately, without starting
                    # another collection or mistaking a completed run for cancelled.
                    changed = db.execute("UPDATE manual_runs SET status='cancelled',phase='cancelled',error=NULL WHERE id=? AND status='cancelling'",
                                         (request_id,)).rowcount
                    transitioned = transitioned or bool(changed)
                    if changed:
                        db.execute("DELETE FROM metadata WHERE key='progress'")
        except portalocker.exceptions.LockException:
            pass
        with self.connect() as db:
            result = dict(db.execute('SELECT * FROM manual_runs WHERE id=?', (request_id,)).fetchone())
            if result['status'] == 'cancelled':
                db.execute('DELETE FROM manual_run_scope WHERE id=?', (request_id,))
                # Repeating a cancellation from before history tracking began
                # cannot establish its unknown original completion time.
                current = db.execute('SELECT 1 FROM run_history WHERE id=?', ('manual:' + request_id,)).fetchone()
                legacy = db.execute('SELECT 1 FROM run_history WHERE id=?', ('legacy-manual:' + request_id,)).fetchone()
                if not current and (legacy or not transitioned):
                    return result
                stamp = self.clock().isoformat()
                db.execute('INSERT OR IGNORE INTO run_history VALUES (?,?,?,?,?,?,?,?,?)',
                           ('manual:' + request_id, 'manual', 'cancelled', None,
                            result['requested_at'], stamp, None, None, 'completed_at'))
                db.execute("UPDATE run_history SET status='cancelled',error=NULL,finished_at=?,time_basis='recovered_at' "
                           "WHERE id=? AND status IN ('running','publishing')", (stamp, 'manual:' + request_id))
                cancel_stamp = db.execute('SELECT finished_at FROM run_history WHERE id=?',
                                          ('manual:' + request_id,)).fetchone()['finished_at']
        if result['status'] == 'cancelled':
            from app.services.collection_outcomes import record_outcome
            record_outcome(self.settings, 'etf-shares', run_key='manual:' + request_id,
                           outcome='cancelled', trigger='manual', finished_at=cancel_stamp)
            if transitioned:
                self._notify_terminal('manual:' + request_id, 'cancelled', cancel_stamp,
                                      changed=0 if row['status'] == 'cancelled' else None)
            self._retain_history()
        return result

    def start(self):
        with self.thread_lock:
            if self.worker and self.worker.is_alive():
                return
            if not self.configured:
                self._reconcile_target_change(self.configuration_issue)
                LOG.warning('ETF collection not ready: %s', self.configuration_issue)
                # Stay idle until the administrator supplies the shared configuration.
            # Recover interrupted jobs without network I/O or stealing another worker's run.
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                with portalocker.Lock(str(self.root / 'collection.lock'), timeout=0):
                    with self.connect() as db:
                        self._recover_manual_runs(db)
                        db.execute("DELETE FROM metadata WHERE key='progress'")
            except portalocker.exceptions.LockException:
                pass
            self.stop_event.clear()
            self.floor = self.clock()
            self.worker = threading.Thread(target=self._loop, name='etf-collection', daemon=True)
            self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.worker:
            self.worker.join(timeout=35)

    def _loop(self):
        # No startup network I/O. Explicit manual requests wake the scheduler.
        while not self.stop_event.is_set():
            self.wake_event.wait(60)
            self.wake_event.clear()
            if self.stop_event.is_set():
                return
            try:
                self.tick(self.clock())
            except Exception as exc:
                LOG.warning('ETF scheduler interrupted %s', diagnostic_details(exc))

    def tick(self, moment):
        with pin_configuration(self.settings):
            return self._tick(moment)

    def _tick(self, moment):
        if not self.configured:
            return False
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with portalocker.Lock(str(self.root / 'collection.lock'), timeout=0):
                config = self.configuration.load()
                with self.connect() as db:
                    config = reconcile_system_ledger(self.configuration, db,
                        ('observations', 'slots', 'metadata'), moment.isoformat())
                    db.commit()
                    self._recover_manual_runs(db)
                    db.execute("DELETE FROM metadata WHERE key='progress'")
                    manual = db.execute("SELECT * FROM manual_runs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
                    if manual:
                        claimed = db.execute("UPDATE manual_runs SET status='running',phase='calendar' WHERE id=? AND status='queued'", (manual['id'],)).rowcount
                        db.commit()
                        if not claimed:
                            return False  # Cancellation won the queue-claim race.
                        return self._run(db, moment.astimezone(CHINA), config, manual_id=manual['id'])
                    # Republish already committed database rows independently
                    # of the exchange schedule, after reconciling the target
                    # and while still holding the collector lock. No ETF slot
                    # or exchange request is created by this refresh.
                    self._refresh(db)
                    if not config['enabled'] or not automatic_collection_allowed(moment):
                        return False
                    today = moment.astimezone(CHINA).date().isoformat()
                    db.execute("UPDATE slots SET status='abandoned' WHERE substr(slot,1,10)<? AND status IN ('running','failed','partial')", (today,))
                    db.commit()
                    recoverable = {r[0] for r in db.execute("SELECT slot FROM slots WHERE status IN ('running','failed','partial') AND attempts<2")}
                    effective_after = datetime.fromisoformat(config['effective_after']) if config.get('effective_after') else None
                    slots = [s for s in scheduled_slots(moment, config['schedule']) if s <= moment and
                             (self.floor is None or s > self.floor or s.isoformat() in recoverable) and
                             (effective_after is None or s > effective_after or s.isoformat() in recoverable)]
                    if not slots:
                        return False
                    slot = slots[-1]
                    key = slot.isoformat()
                    # Changing the daily clock or migrating an old multi-slot plan
                    # must not start a second automatic run on the same date.
                    prior = db.execute('SELECT slot FROM slots WHERE substr(slot,1,10)=? AND slot!=? LIMIT 1',
                                       (slot.date().isoformat(), key)).fetchone()
                    if prior:
                        return False
                    row = db.execute('SELECT * FROM slots WHERE slot=?', (key,)).fetchone()
                    if row and (row['status'] == 'complete' or row['error'] == 'writer_commit_unconfirmed'
                                or row['attempts'] >= 2
                                or moment - datetime.fromisoformat(row['attempted_at']) < timedelta(minutes=30)):
                        return False
                    attempts = row['attempts'] + 1 if row else 1
                    db.execute('INSERT OR REPLACE INTO slots VALUES (?,?,?,?,?)',
                               (key, 'running', attempts, moment.isoformat(), None))
                    db.commit()
                    return self._run(db, slot, config, key=key)
        except portalocker.exceptions.LockException:
            return False

    def _run(self, db, slot, config, *, key=None, manual_id=None):
        self._run_error_details = None
        try:
            notification_enabled = collection_notifications.enabled(self.settings, 'etf')
        except Exception as exc:
            LOG.warning('ETF notification configuration unavailable error_type=%s', type(exc).__name__)
            notification_enabled = False
        self._notification_report = ETFRunSummary(notification_enabled)
        started = time.monotonic()
        run = uuid.uuid4().hex[:12]
        history_id = 'manual:' + manual_id if manual_id else run
        manual_scope = None
        if manual_id:
            request = db.execute('SELECT requested_at FROM manual_runs WHERE id=?', (manual_id,)).fetchone()
            requested_at, attempt = request['requested_at'], None
            manual_scope = db.execute('SELECT schedule,retry_exhausted FROM manual_run_scope WHERE id=?',
                                      (manual_id,)).fetchone()
            db.execute('DELETE FROM manual_run_scope WHERE id=?', (manual_id,))
        else:
            request = db.execute('SELECT attempted_at,attempts FROM slots WHERE slot=?', (key,)).fetchone()
            requested_at, attempt = request['attempted_at'], request['attempts']
        self._write_history(db, 'INSERT INTO run_history VALUES (?,?,?,?,?,?,?,?,?)',
                            (history_id, 'manual' if manual_id else 'scheduled', 'running', None,
                             requested_at, None, key, attempt, 'started_at'))
        db.commit()
        phase = 'startup'
        last_completed = last_total = 0
        LOG.info('ETF collection start run=%s source=exchanges trigger=%s date=%s',
                 run, 'manual' if manual_id else 'scheduled', slot.date())

        def check_cancelled():
            if manual_id:
                row = db.execute('SELECT status FROM manual_runs WHERE id=?', (manual_id,)).fetchone()
                if row and row['status'] in ('cancelling', 'cancelled'):
                    raise CollectionCancelled()
            if self.stop_event.is_set() or (not manual_id and not automatic_collection_allowed(self.clock())):
                raise CollectionInterrupted()

        def progress(next_phase, completed=0, total=0):
            nonlocal phase, last_completed, last_total
            check_cancelled()
            last_completed, last_total = completed, total
            if next_phase != phase:
                phase = next_phase
                LOG.info('ETF collection stage run=%s phase=%s completed=%s total=%s seconds=%.3f',
                         run, phase, completed, total, time.monotonic() - started)
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('progress',?)",
                       (json.dumps(dict(phase=phase, completed=completed, total=total)),))
            if manual_id:
                db.execute('UPDATE manual_runs SET phase=?,completed=?,total=? WHERE id=?',
                           (phase, completed, total, manual_id))
            db.commit()
        try:
            progress('calendar')
            with database_write_run(run):
                reference = datetime.fromisoformat(requested_at) if manual_id else slot
                # A queued legacy row has no scope record and uses the trusted
                # current configuration; new requests pin only the schedule.
                collect_config = config | {'schedule': json.loads(manual_scope['schedule'])} if manual_scope else config
                complete = self._collect(db, reference, config=collect_config, progress=progress,
                                         check_cancelled=check_cancelled, manual=manual_id is not None,
                                         retry_exhausted=bool(manual_scope and manual_scope['retry_exhausted']))
            check_cancelled()
            state, error = ('complete' if complete else 'partial'), None
        except CollectionCancelled:
            state, error, complete = 'cancelled', None, False
        except Exception as exc:
            if self._run_error_details is None:
                self._run_error_details = safe_error(exc, stage={'writing': 'database_write', 'fetching': 'fetch', 'calendar': 'fetch'}.get(phase, 'collection'))
            state, error = 'failed', exc.code if isinstance(exc, WriterFailure) else 'source_unavailable'
            if isinstance(exc, CollectionInterrupted):
                state, error = 'interrupted', 'service_interrupted'
            if isinstance(exc, WriterFailure):
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('writer_error',?)", (exc.code,))
            complete = False
            LOG.log(logging.WARNING if state == 'interrupted' else logging.ERROR,
                    'ETF collection failed run=%s phase=%s outcome=%s %s',
                    run, phase, state, diagnostic_details(exc))
        if manual_id:
            # The single SQL update arbitrates a late cancel against completion.
            # A cancel accepted before this update must never be reported complete.
            db.execute("""UPDATE manual_runs SET
                phase=CASE WHEN status='cancelling' THEN 'cancelled' ELSE ? END,
                error=CASE WHEN status='cancelling' THEN NULL ELSE ? END,
                status=CASE WHEN status='cancelling' THEN 'cancelled' ELSE ? END
                WHERE id=? AND status IN ('running','cancelling')""", (state, error, state, manual_id))
            terminal = db.execute('SELECT status,error FROM manual_runs WHERE id=?', (manual_id,)).fetchone()
            state, error = terminal['status'], terminal['error']
            complete = complete and state == 'complete'
        else:
            db.execute('UPDATE slots SET status=?,error=? WHERE slot=?', (state, error, key))
        # Preserve the outcome with its scheduling state in the same transaction.
        # Successful acquisition still awaits local cache publication; a crash in
        # that window is recovered as interrupted rather than fabricated success.
        self._write_history(db, "UPDATE run_history SET status=?,error=?,finished_at=?,time_basis=? WHERE id=?",
                            ('publishing' if state == 'complete' else state, error,
                             None if state == 'complete' else self.clock().isoformat(),
                             'started_at' if state == 'complete' else 'completed_at', history_id))
        if state == 'cancelled':
            self._run_error_details = None
        if self._run_error_details:
            self._write_history(db, 'INSERT OR REPLACE INTO run_history_errors VALUES (?,?)',
                                (history_id, json.dumps(self._run_error_details)))
        db.execute("DELETE FROM metadata WHERE key='progress'")
        db.commit()
        LOG.info('ETF collection finish run=%s outcome=%s completed=%s total=%s seconds=%.3f',
                 run, state, last_completed, last_total, time.monotonic() - started)
        if manual_id or automatic_collection_allowed(self.clock()):
            self._refresh(db)
        pending = db.execute("SELECT value FROM metadata WHERE key='cache_pending'").fetchone()
        history_status = 'partial' if state == 'complete' and pending and pending[0] == '1' else state
        if (history_status == 'partial' and pending and pending[0] == '1'
                and (state == 'complete' or self._run_error_details is None)):
            self._run_error_details = safe_error(code='publication_pending', stage='cache_publish')
        if self._run_error_details:
            self._write_history(db, 'INSERT OR REPLACE INTO run_history_errors VALUES (?,?)',
                                (history_id, json.dumps(self._run_error_details)))
        finished_at = self.clock().isoformat()
        history_recorded = self._write_history(db,
            "INSERT INTO run_history VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "status=excluded.status,error=excluded.error,finished_at=excluded.finished_at,time_basis=excluded.time_basis",
            (history_id, 'manual' if manual_id else 'scheduled', history_status, error,
             requested_at, finished_at, key, attempt, 'completed_at'))
        db.commit()
        from app.services.collection_outcomes import record_outcome
        outcome = history_status if history_recorded else 'partial'
        record_outcome(self.settings, 'etf-shares', run_key=history_id, outcome=outcome,
                       trigger='manual' if manual_id else 'scheduled', finished_at=finished_at, error_details=self._run_error_details)
        self._retain_history()
        report = self._notification_report
        summary = ()
        if report.enabled:
            try:
                oldest = None
                if report.target is not None and report.historical_gaps is None:
                    gap = db.execute(f"SELECT min(value) FROM metadata WHERE {_MANUAL_GAP_KEYS} AND value<?",
                                     (report.target.isoformat(),)).fetchone()[0]
                    if gap:
                        oldest = date.fromisoformat(gap)
                summary = report.lines(known_history=oldest)
            except Exception as exc:
                LOG.warning('ETF notification summary unavailable error_type=%s', type(exc).__name__)
                summary = ('本轮份额对比未核验',)
        self._notify_terminal(history_id, outcome, finished_at, scheduled=manual_id is None,
            changed=None if report.commit_unconfirmed else report.committed_changes, summary=summary,
            data_date=report.target, error_details=self._run_error_details)
        return complete

    def _refresh(self, db):
        row = db.execute("SELECT value FROM metadata WHERE key='cache_pending'").fetchone()
        if not row or row[0] != '1' or not self.refresh:
            return
        last = db.execute("SELECT value FROM metadata WHERE key='cache_attempt'").fetchone()
        now = self.clock()
        if last and now - datetime.fromisoformat(last[0]) < timedelta(minutes=5):
            return
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('cache_attempt',?)", (now.isoformat(),))
        db.commit()
        try:
            repair = db.execute("SELECT value FROM metadata WHERE key='cache_repair_start'").fetchone()
            published = self.refresh(repair_start=date.fromisoformat(repair[0])) if repair else self.refresh()
            if published is not False:
                db.execute("DELETE FROM metadata WHERE key='cache_repair_start'")
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('cache_pending','0')")
                db.commit()
        except Exception as exc:
            LOG.warning('ETF cache publish failed %s', diagnostic_details(exc))

    def _write_day(self, c, config, writer, calendar, end, shares, prices, *, historical=False):
        report = getattr(self, '_notification_report', None)
        try:
            options = dict(calendar=calendar, calendar_target=end)
            if historical:
                options['insert_only'] = True
            elif report is not None and report.comparison is not None:
                options['share_comparison'] = report.comparison
            changed = writer.execute(lambda engine: c.write_records(engine, shares, prices, **options),
                                     stage='write_records')
            if report is not None and type(changed) is int and changed > 0:
                report.committed_changes += changed
        except CollectionInterrupted:
            raise
        except Exception as exc:
            diagnostic = database_diagnostic(exc, writer.stage)
            self._run_error_details = _safe_database_error(exc, diagnostic)
            code = writer_error_code(exc)
            commit_disconnected = diagnostic.stage == 'commit' and (
                diagnostic.reason == 'connection_lost' or diagnostic.socket_timeout)
            if commit_disconnected:
                # An absent COMMIT reply is neither a successful publication
                # nor proof of rollback. Do not automatically replay this slot.
                code = 'writer_commit_unconfirmed'
                if report is not None:
                    report.commit_unconfirmed = True
                self._run_error_details = sanitize_error(
                    {**self._run_error_details, 'code': 'etf_commit_unconfirmed'})
            log_database_failure(exc, operation='collection-write', stage=writer.stage,
                                 target=target_key(config)[:12])
            raise WriterFailure(code) from None

    @staticmethod
    def _manual_days(db, calendar, end, funds):
        expected = {(kind, code) for kind in ('shares', 'prices') for code in funds}
        observed = {}
        for row in db.execute("SELECT kind,code,day,status FROM observations WHERE day<=?",
                              (end.isoformat(),)):
            key = row['kind'], row['code']
            if key in expected:
                try:
                    day = date.fromisoformat(row['day'])
                except (TypeError, ValueError):
                    continue
                observed.setdefault(day, {})[key] = row['status']
        completed = sorted(day for day, rows in observed.items()
                           if all(rows.get(key) == 'complete' for key in expected))
        has_floor = db.execute(f"SELECT value FROM metadata WHERE {_MANUAL_GAP_KEYS} AND value<=? "
                               "ORDER BY value LIMIT 1", (end.isoformat(),)).fetchone()
        candidates = [completed[-1] if completed else end]
        if completed:
            # A later fully covered scheduled date does not erase a known
            # earlier gap, but old blocked history alone does not open a range.
            candidates.extend(day for day, rows in observed.items() if completed[0] < day
                              and any(value in ('pending', 'manual_only', 'retry_exhausted')
                                      for value in rows.values()))
        if has_floor:
            candidates.append(date.fromisoformat(has_floor[0]))
        available = getattr(calendar, 'days', ())
        if not completed and not has_floor:
            # Bootstrap checks only the latest three verified sessions,
            # including sessions that never ran. The first known date has no
            # proven predecessor and cannot be written as a repair target.
            recent = [day for day in available[1:] if day <= end][-3:]
            if recent:
                candidates.append(recent[0])
        start = min(candidates)
        if available:
            start = max(start, available[0])
        scope = (end,) if start >= end else tuple(dict.fromkeys(calendar.range(start, end)))
        # A new target without reliable coverage never walks unbounded prior history.
        required = {(kind, code, day) for day in scope if day < end
                    and (day != scope[0] or day not in completed) for kind, code in expected}
        unchecked = {(kind, code, day) for kind, code, day in required
                     if observed.get(day, {}).get((kind, code)) != 'complete'}
        return scope, required, unchecked

    def _repair_history(self, db, c, client, config, writer, calendar, end, funds, progress, check_cancelled, today, guards):
        scope, required, unchecked = self._manual_days(db, calendar, end, funds)
        if not required:
            db.execute(f"DELETE FROM metadata WHERE {_MANUAL_GAP_KEYS} AND value<=?", (end.isoformat(),))
            return scope, set(), 0, 0, {}
        floor = min(day for _, _, day in required)
        # A configuration cutoff can move backwards. Preserve an already
        # known but not-yet-due gap while persisting this run's repair intent.
        db.execute("INSERT OR IGNORE INTO metadata "
                   "SELECT 'manual_gap_floor_deferred:'||value,value FROM metadata "
                   "WHERE key='manual_gap_floor' AND value>?", (end.isoformat(),))
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('manual_gap_floor',?)", (floor.isoformat(),))
        db.executemany("INSERT OR IGNORE INTO observations(kind,code,day,status) VALUES (?,?,?,'manual_only')",
                       ((kind, code, day.isoformat()) for kind, code, day in sorted(unchecked)))
        db.commit()  # Persist intended repair scope before source I/O or publication.
        check_cancelled()
        try:
            present = writer.execute(lambda engine: c.existing_observations(engine, required),
                                     stage='load_historical_keys')
        except CollectionInterrupted:
            raise
        except Exception as exc:
            self._run_error_details = _safe_database_error(exc, database_diagnostic(exc, writer.stage))
            log_database_failure(exc, operation='collection-write', stage=writer.stage,
                                 target=target_key(config)[:12])
            raise WriterFailure(writer_error_code(exc)) from None

        pending = required - present
        # A once-complete local row can no longer hide a deleted MySQL row.
        db.executemany("UPDATE observations SET status='manual_only' WHERE kind=? AND code=? AND day=? AND status='complete'",
                       ((kind, code, day.isoformat()) for kind, code, day in sorted(pending - unchecked)))
        db.commit()
        completed, total = 0, len(pending)
        unresolved = set(pending)
        pending_by_day, verified_by_day = {}, {}
        for item in sorted(pending):
            pending_by_day.setdefault(item[2], []).append(item[:2])
        for item in present & unchecked:
            verified_by_day.setdefault(item[2], set()).add(item)
        failures, history_errors = 0, {}
        for day in sorted(set(pending_by_day) | set(verified_by_day)):
            day_need = pending_by_day.get(day, ())
            verified = verified_by_day.get(day, set())
            shares, prices = [], []
            scoped = {source: dict(attempted=0, found=0, failed=0, rate_limit=0, archive=0)
                      for source in guards}
            for kind, code in day_need:
                progress('fetching', completed, total + 2 * len(funds))
                source, options, guard = _observation_source(kind, code, day, today, guards)
                if guard is not None:
                    if reason := guard.skip_reason(day):
                        scoped[source][reason] += 1
                        if reason == 'archive' and day not in history_errors:
                            history_errors[day] = safe_error(code='etf_szse_price_archived', stage='fetch', trade_date=day)
                        completed += 1
                        continue
                    scoped[source]['attempted'] += 1
                try:
                    found_shares, found_prices, _ = c.collect(
                        client, day, day, funds={code: funds[code]}, calendar=calendar,
                        include_prices=kind == 'prices', include_shares=kind == 'shares', **options)
                    check_cancelled()
                except CollectionInterrupted:
                    raise
                except Exception as exc:
                    details = _observation_error(exc, day, guard)
                    if day not in history_errors:
                        history_errors[day] = details
                    failures += 1
                    if guard is not None:
                        scoped[source]['failed'] += 1
                    if failures <= 5:
                        LOG.warning('ETF historical source observation failed kind=%s code=%s date=%s source=%s %s',
                                    kind, code, day, source,
                                    diagnostic_details(exc))
                else:
                    shares.extend(found_shares)
                    prices.extend(found_prices)
                    if guard is not None and (found_shares or found_prices):
                        scoped[source]['found'] += 1
                completed += 1
            for source, counts in scoped.items():
                if counts['attempted'] or counts['rate_limit'] or counts['archive']:
                    label = 'SSE shares' if source == 'sse_scoped_shares' else 'SZSE closes'
                    LOG.info('ETF historical %s date=%s attempted=%s found=%s empty=%s failed=%s '
                             'skipped_rate_limit=%s skipped_archive=%s', label, day, counts['attempted'], counts['found'],
                             counts['attempted'] - counts['found'] - counts['failed'], counts['failed'],
                             counts['rate_limit'], counts['archive'])
            check_cancelled()
            # Preserve the earliest dirty date *before* attempting COMMIT.
            # A stop, ambiguous COMMIT or cache lock cannot lose this intent.
            if shares or prices or verified:
                earliest = calendar.previous(day).isoformat()
                db.execute("INSERT INTO metadata VALUES ('cache_repair_start',?) "
                           "ON CONFLICT(key) DO UPDATE SET value=min(value,excluded.value)", (earliest,))
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('cache_pending','1')")
                db.commit()
                progress('writing', completed, total + 2 * len(funds))
                self._write_day(c, config, writer, calendar, day, shares, prices, historical=True)
            done = set(verified)
            done.update((kind, row['fund_code'], row['trade_date']) for kind, records in
                        (('shares', shares), ('prices', prices)) for row in records)
            db.executemany("UPDATE observations SET status='complete' WHERE kind=? AND code=? AND day=?",
                           ((kind, code, target.isoformat()) for kind, code, target in done))
            unresolved -= done
            db.commit()
        db.execute(f"DELETE FROM metadata WHERE {_MANUAL_GAP_KEYS} AND value<=?", (end.isoformat(),))
        if unresolved:
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('manual_gap_floor',?)",
                       (min(day for _, _, day in unresolved).isoformat(),))
        return scope, unresolved, completed, total, history_errors

    def _collect(self, db, slot, config=None, progress=None, check_cancelled=None, manual=False, retry_exhausted=False):
        config = config or self.configuration.load()
        progress = progress or (lambda *args: None)
        check_cancelled = check_cancelled or (lambda: None)
        check_cancelled()
        isolated = manual and manual_failure_isolated(slot)
        c = self.collector or collector_module()
        funds = c.catalog()
        with self.client_factory() as http, _RunDatabase(self.configuration.engine, config, check_cancelled) as writer:
            client = c.ExchangeClient(CancellableHTTPClient(http, check_cancelled))
            guards = {source: _ScopedSourceGuard() for source in ('sse_scoped_shares', 'szse_dated_close')}
            calendar = client.calendar()
            today = (getattr(calendar, 'now', None) or self.clock()).astimezone(CHINA).date()
            check_cancelled()
            if hasattr(client, 'prices'):
                fetch_prices, price_cache, price_errors = client.prices, {}, {}
                def cached_prices(code):
                    if code in price_errors:
                        raise price_errors[code]
                    if code not in price_cache:
                        try:
                            price_cache[code] = fetch_prices(code)
                        except Exception as exc:
                            price_errors[code] = exc
                            raise
                    return price_cache[code]
                client.prices = cached_prices
            # Manual uses the latest matured configured slot at the original
            # request time, even if it was actually queued across a cutoff.
            # This does not claim/run that automatic slot.
            data_slot = slot
            if manual:
                try:
                    data_slot = latest_due_slot(slot, config.get('schedule'), is_open=calendar.official_days.get)
                except ValueError:
                    self._run_error_details = safe_error(code='etf_manual_schedule_invalid', stage='validate')
                    raise ValueError('Invalid configured ETF manual cutoff') from None
                if data_slot is None:
                    self._run_error_details = safe_error(code='etf_manual_slot_unverified', stage='validate')
                    raise ValueError('No verified due ETF collection slot')
            # Automatic jobs remain latest-only and retain their existing rule.
            _, end = calendar.default_range(1)
            cutoff = data_slot.date() if data_slot.hour >= 15 else data_slot.date() - timedelta(days=1)
            while end > cutoff:
                end = calendar.previous(end)
            report = getattr(self, '_notification_report', None)
            if report is not None:
                report.set_target(end, calendar)
            scope, historical_gaps, repaired, history_total, history_errors = (end,), set(), 0, 0, {}
            if manual:
                LOG.info('ETF manual collection cutoff requested_at=%s due_slot=%s date=%s',
                         slot.isoformat(), data_slot.isoformat(), end)
                scope, historical_gaps, repaired, history_total, history_errors = self._repair_history(
                    db, c, client, config, writer, calendar, end, funds, progress, check_cancelled, today, guards)
                if report is not None:
                    report.historical_gaps = historical_gaps
                base_progress = progress
                progress = lambda phase, completed, total: base_progress(
                    phase, completed + repaired, total + history_total)
            for code in funds:
                for kind in ('shares', 'prices'):
                    db.execute('INSERT OR IGNORE INTO observations(kind,code,day,status) VALUES (?,?,?,?)',
                               (kind, code, end.isoformat(), 'manual_only' if manual else 'pending'))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('coverage_start',?)", (scope[0].isoformat(),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('coverage_end',?)", (end.isoformat(),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('coverage_target_sessions',?)", (str(len(scope)),))
            db.commit()
            exhausted = {(row['kind'], row['code']) for row in db.execute(
                "SELECT kind,code FROM observations WHERE day=? AND status='retry_exhausted'", (end.isoformat(),))}
            targets = {(kind, code) for kind in ('shares', 'prices') for code in funds
                       if manual or (kind, code) not in exhausted}
            shares, prices, attempted = [], [], set()
            completed, total = 0, len(targets)
            failures = 0
            skipped = {source: dict(rate_limit=0, archive=0) for source in guards}
            LOG.info('ETF source plan source=exchanges date=%s funds=%s targets=%s exhausted=%s',
                     end, len(funds), total, len(exhausted))
            for kind in ('shares', 'prices'):
                for code, name in funds.items():
                    if (kind, code) not in targets:
                        continue
                    progress('fetching', completed, total)
                    source, options, guard = _observation_source(kind, code, end, today, guards)
                    if guard is not None and (reason := guard.skip_reason(end)):
                        skipped[source][reason] += 1
                        completed += 1
                        continue
                    attempted.add((kind, code, end.isoformat()))
                    try:
                        s, p, _ = c.collect(client, end, end, funds={code: name}, calendar=calendar,
                                            include_prices=kind == 'prices', include_shares=kind == 'shares', **options)
                        check_cancelled()
                    except CollectionInterrupted:
                        raise
                    except Exception as exc:
                        self._run_error_details = _observation_error(exc, end, guard)
                        failures += 1
                        if failures <= 5:
                            LOG.warning('ETF source observation failed kind=%s code=%s date=%s source=%s %s',
                                        kind, code, end, source, diagnostic_details(exc))
                    else:
                        shares.extend(s)
                        prices.extend(p)
                    completed += 1
                    progress('fetching', completed, total)
            verified_shares = {r['fund_code'] for r in shares if r['trade_date'] == end}
            verified_prices = {r['fund_code'] for r in prices if r['trade_date'] == end}
            missing_shares = set(funds) - verified_shares
            missing_prices = set(funds) - verified_prices
            if report is not None:
                report.coverage = (len(funds) - len(missing_shares), len(funds) - len(missing_prices), len(funds))
            LOG.info('ETF source finish source=exchanges date=%s attempted=%s failed=%s shares=%s prices=%s '
                     'suppressed_failures=%s missing_shares=%s missing_prices=%s skipped_sse_rate_limit=%s '
                     'skipped_szse_rate_limit=%s skipped_szse_archive=%s',
                     end, len(attempted), failures, len(shares), len(prices), max(0, failures - 5),
                     len(missing_shares), len(missing_prices), skipped['sse_scoped_shares']['rate_limit'],
                     skipped['szse_dated_close']['rate_limit'], skipped['szse_dated_close']['archive'])
            if failures == 0 and (missing_shares or missing_prices):
                code = ('etf_missing_shares_and_prices' if missing_shares and missing_prices else
                        'etf_missing_shares' if missing_shares else 'etf_missing_prices')
                self._run_error_details = safe_error(code=code, stage='fetch', trade_date=end)
            if historical_gaps:
                old = min(day for _, _, day in historical_gaps)
                kinds = {kind for kind, _, day in historical_gaps if day == old}
                code = ('etf_missing_shares_and_prices' if len(kinds) == 2 else
                        'etf_missing_shares' if 'shares' in kinds else 'etf_missing_prices')
                self._run_error_details = history_errors.get(old) or safe_error(code=code, stage='fetch', trade_date=old)
            progress('writing', completed, total)
            self._write_day(c, config, writer, calendar, end, shares, prices)
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('writer_error','')")
            # Charge only committed attempts. Preserve old values on an incomplete
            # source response, but do not mistake them for fresh verification.
            isolated = isolated or (manual and manual_failure_isolated(self.clock()))
            if not isolated:
                if manual and retry_exhausted:
                    # Apply the requested reset only to the verified, written
                    # target; the previous coverage_end can now be in the future.
                    db.execute("UPDATE observations SET status='pending',attempts=0 "
                               "WHERE day=? AND status='retry_exhausted'", (end.isoformat(),))
                for kind, code, day in attempted:
                    db.execute('UPDATE observations SET attempts=attempts+1 WHERE kind=? AND code=? AND day=?',
                               (kind, code, day))
            for kind, records in (('shares', shares), ('prices', prices)):
                for record in records:
                    db.execute("UPDATE observations SET status='complete' WHERE kind=? AND code=? AND day=?",
                               (kind, record['fund_code'], record['trade_date'].isoformat()))
            if not isolated:
                db.execute("UPDATE observations SET status='pending' WHERE day=? AND status IN ('blocked_unverified_history','manual_only')",
                           (end.isoformat(),))
            db.execute("UPDATE observations SET status='retry_exhausted' WHERE day=? AND status='pending' AND attempts>=6",
                       (end.isoformat(),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('cache_pending','1')")
            db.commit()
            return not historical_gaps and verified_shares == verified_prices == set(funds)
