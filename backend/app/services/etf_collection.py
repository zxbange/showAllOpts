"""Low-frequency exchange collection. All state is local and durable.

ETF collection and display reads can share a page-configured database account.
"""
from datetime import datetime, timedelta
from contextlib import contextmanager
from zoneinfo import ZoneInfo
import sqlite3
import threading
import logging
import json
import time
import uuid

from app.core.diagnostics import diagnostic_details

from app.services.etf_configuration import ETFConfiguration, ConfigurationFailure, DEFAULT_COLLECTION_TIME, target_key
from app.services.etf_diagnostics import log_database_failure

import httpx
import portalocker
from sqlalchemy import create_engine

LOG = logging.getLogger(__name__)
CHINA = ZoneInfo('Asia/Shanghai')
TIMES = (tuple(map(int, DEFAULT_COLLECTION_TIME.split(':'))),)


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
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=30, trust_env=False))
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
            CREATE TABLE IF NOT EXISTS slots (slot TEXT PRIMARY KEY, status TEXT NOT NULL,
              attempts INTEGER NOT NULL, attempted_at TEXT NOT NULL, error TEXT);
            CREATE TABLE IF NOT EXISTS observations (kind TEXT, code TEXT, day TEXT, status TEXT,
              attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind,code,day));
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
        ''')
        (self.root / 'state.sqlite').chmod(0o600)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _reconcile_target_change(self, issue):
        if issue != 'writer_target_mismatch' or not (self.root / 'state.sqlite').exists():
            return
        try:
            with portalocker.Lock(str(self.root / 'collection.lock'), timeout=0):
                with self.connect() as db:
                    self._recover_manual_runs(db, reason='configuration_changed')
                    db.execute("UPDATE manual_runs SET status='interrupted',phase='interrupted',error='configuration_changed' WHERE status IN ('queued','running')")
                    db.execute("DELETE FROM metadata WHERE key='progress'")
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
                     historical_dates_per_run=0, coverage_is_target_not_completion=True,
                     manual=None, progress=None, runs=[], gaps=[], metadata={})
        if not (self.root / 'state.sqlite').exists():
            return state
        with self.connect() as db:
            state['runs'] = [dict(r) for r in db.execute('SELECT * FROM slots ORDER BY slot DESC LIMIT 9')]
            state['metadata'] = dict(db.execute('SELECT key,value FROM metadata').fetchall())
            latest = state['metadata'].get('coverage_end')
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
                    if retry_exhausted:
                        db.execute("UPDATE observations SET status='pending',attempts=0 WHERE status='retry_exhausted' AND day=(SELECT value FROM metadata WHERE key='coverage_end')")
                    db.execute('INSERT INTO manual_runs(id,status,requested_at,phase) VALUES (?,?,?,?)',
                               (request_id, 'queued', self.clock().isoformat(), 'queued'))
                    result = dict(db.execute('SELECT * FROM manual_runs WHERE id=?', (request_id,)).fetchone())
        except portalocker.exceptions.LockException:
            raise ConfigurationFailure('采集正在运行，请等待本轮完成') from None
        self.start()
        self.wake_event.set()
        return result

    @staticmethod
    def _recover_manual_runs(db, reason='service_interrupted'):
        # Call only while owning collection.lock: no other worker can still run.
        db.execute("UPDATE manual_runs SET status='cancelled',phase='cancelled',error=NULL WHERE status='cancelling'")
        db.execute("UPDATE manual_runs SET status='interrupted',phase='interrupted',error=? WHERE status='running'", (reason,))

    def request_cancel(self, request_id):
        # The worker owns collection.lock for its entire run. Do not wait for that
        # lock before recording cancellation: another API process must be able to
        # cancel while a network call or a MySQL transaction is still in flight.
        with self.connect() as db:
            db.execute("""UPDATE manual_runs SET
                phase=CASE WHEN status='queued' THEN 'cancelled' ELSE phase END,
                status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END,
                error=NULL WHERE id=? AND status IN ('queued','running')""", (request_id,))
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
                    if changed:
                        db.execute("DELETE FROM metadata WHERE key='progress'")
        except portalocker.exceptions.LockException:
            pass
        with self.connect() as db:
            return dict(db.execute('SELECT * FROM manual_runs WHERE id=?', (request_id,)).fetchone())

    def start(self):
        with self.thread_lock:
            if self.worker and self.worker.is_alive():
                return
            if not self.configured:
                self._reconcile_target_change(self.configuration_issue)
                LOG.warning('ETF collection not ready: %s', self.configuration_issue)
                return
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
        if not self.configured:
            return False
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with portalocker.Lock(str(self.root / 'collection.lock'), timeout=0):
                config = self.configuration.load()
                with self.connect() as db:
                    self._refresh(db)
                    self._recover_manual_runs(db)
                    db.execute("DELETE FROM metadata WHERE key='progress'")
                    manual = db.execute("SELECT * FROM manual_runs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
                    if manual:
                        claimed = db.execute("UPDATE manual_runs SET status='running',phase='calendar' WHERE id=? AND status='queued'", (manual['id'],)).rowcount
                        db.commit()
                        if not claimed:
                            return False  # Cancellation won the queue-claim race.
                        return self._run(db, moment.astimezone(CHINA), config, manual_id=manual['id'])
                    if not config['enabled']:
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
                    if row and (row['status'] == 'complete' or row['attempts'] >= 2 or
                                moment - datetime.fromisoformat(row['attempted_at']) < timedelta(minutes=30)):
                        return False
                    attempts = row['attempts'] + 1 if row else 1
                    db.execute('INSERT OR REPLACE INTO slots VALUES (?,?,?,?,?)',
                               (key, 'running', attempts, moment.isoformat(), None))
                    db.commit()
                    return self._run(db, slot, config, key=key)
        except portalocker.exceptions.LockException:
            return False

    def _run(self, db, slot, config, *, key=None, manual_id=None):
        started = time.monotonic()
        run = uuid.uuid4().hex[:12]
        phase = 'startup'
        last_completed = last_total = 0
        LOG.info('ETF collection start run=%s source=exchanges trigger=%s date=%s',
                 run, 'manual' if manual_id else 'scheduled', slot.date())

        def check_cancelled():
            if manual_id:
                row = db.execute('SELECT status FROM manual_runs WHERE id=?', (manual_id,)).fetchone()
                if row and row['status'] in ('cancelling', 'cancelled'):
                    raise CollectionCancelled()
            if self.stop_event.is_set():
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
            complete = self._collect(db, slot, config=config, progress=progress,
                                     check_cancelled=check_cancelled, manual=manual_id is not None)
            check_cancelled()
            state, error = ('complete' if complete else 'partial'), None
        except CollectionCancelled:
            state, error, complete = 'cancelled', None, False
        except Exception as exc:
            state, error = 'failed', exc.code if isinstance(exc, WriterFailure) else 'source_unavailable'
            if self.stop_event.is_set():
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
            terminal = db.execute('SELECT status FROM manual_runs WHERE id=?', (manual_id,)).fetchone()
            state = terminal['status']
            complete = complete and state == 'complete'
        else:
            db.execute('UPDATE slots SET status=?,error=? WHERE slot=?', (state, error, key))
        db.execute("DELETE FROM metadata WHERE key='progress'")
        db.commit()
        LOG.info('ETF collection finish run=%s outcome=%s completed=%s total=%s seconds=%.3f',
                 run, state, last_completed, last_total, time.monotonic() - started)
        self._refresh(db)
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
            if self.refresh() is not False:
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('cache_pending','0')")
                db.commit()
        except Exception as exc:
            LOG.warning('ETF cache publish failed %s', diagnostic_details(exc))

    def _collect(self, db, slot, config=None, progress=None, check_cancelled=None, manual=False):
        config = config or self.configuration.load()
        progress = progress or (lambda *args: None)
        check_cancelled = check_cancelled or (lambda: None)
        check_cancelled()
        c = self.collector or collector_module()
        funds = c.catalog()
        with self.client_factory() as http:
            client = c.ExchangeClient(CancellableHTTPClient(http, check_cancelled))
            calendar = client.calendar()
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
            if hasattr(client, 'sse_shares'):
                fetch_shares, share_cache, share_errors = client.sse_shares, {}, {}
                def cached_shares(day):
                    if day in share_errors:
                        raise share_errors[day]
                    if day not in share_cache:
                        try:
                            share_cache[day] = fetch_shares(day)
                        except Exception as exc:
                            share_errors[day] = exc
                            raise
                    return share_cache[day]
                client.sse_shares = cached_shares
            # Historical backfill belongs to the explicit JoinQuant script. The
            # daily exchange job only refreshes the latest confirmed closed session.
            _, end = calendar.default_range(1)
            cutoff = slot.date() if slot.hour >= 15 else slot.date() - timedelta(days=1)
            while end > cutoff:
                end = calendar.previous(end)
            for code in funds:
                for kind in ('shares', 'prices'):
                    db.execute('INSERT OR IGNORE INTO observations(kind,code,day,status) VALUES (?,?,?,?)',
                               (kind, code, end.isoformat(), 'pending'))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('coverage_start',?)", (end.isoformat(),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('coverage_end',?)", (end.isoformat(),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('coverage_target_sessions','1')")
            db.commit()
            exhausted = {(row['kind'], row['code']) for row in db.execute(
                "SELECT kind,code FROM observations WHERE day=? AND status='retry_exhausted'", (end.isoformat(),))}
            targets = {(kind, code) for kind in ('shares', 'prices') for code in funds
                       if manual or (kind, code) not in exhausted}
            shares, prices, attempted = [], [], set()
            completed, total = 0, len(targets)
            failures = 0
            LOG.info('ETF source plan source=exchanges date=%s funds=%s targets=%s exhausted=%s',
                     end, len(funds), total, len(exhausted))
            for kind in ('shares', 'prices'):
                for code, name in funds.items():
                    if (kind, code) not in targets:
                        continue
                    progress('fetching', completed, total)
                    attempted.add((kind, code, end.isoformat()))
                    try:
                        s, p, _ = c.collect(client, end, end, funds={code: name}, calendar=calendar,
                                            include_prices=kind == 'prices', include_shares=kind == 'shares')
                        check_cancelled()
                    except CollectionInterrupted:
                        raise
                    except Exception as exc:
                        failures += 1
                        if failures <= 5:
                            LOG.warning('ETF source observation failed kind=%s code=%s date=%s %s',
                                        kind, code, end, diagnostic_details(exc))
                    else:
                        shares.extend(s)
                        prices.extend(p)
                    completed += 1
                    progress('fetching', completed, total)
            LOG.info('ETF source finish source=exchanges date=%s attempted=%s failed=%s shares=%s prices=%s suppressed_failures=%s',
                     end, completed, failures, len(shares), len(prices), max(0, failures - 5))
            engine = None
            stage = 'engine_create'
            try:
                progress('writing', completed, total)
                engine = self.configuration.engine(config)
                check_cancelled()
                stage = 'write_records'
                c.write_records(engine, shares, prices, calendar=calendar)
            except CollectionInterrupted:
                raise
            except Exception as exc:
                log_database_failure(exc, operation='collection-write', stage=stage,
                                     target=target_key(config)[:12])
                raise WriterFailure(writer_error_code(exc)) from None
            finally:
                if engine is not None:
                    engine.dispose()
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('writer_error','')")
            # Charge only committed attempts. Preserve old values on an incomplete
            # source response, but do not mistake them for fresh verification.
            for kind, code, day in attempted:
                db.execute('UPDATE observations SET attempts=attempts+1 WHERE kind=? AND code=? AND day=?',
                           (kind, code, day))
            for kind, records in (('shares', shares), ('prices', prices)):
                for record in records:
                    db.execute("UPDATE observations SET status='complete' WHERE kind=? AND code=? AND day=?",
                               (kind, record['fund_code'], record['trade_date'].isoformat()))
            db.execute("UPDATE observations SET status='pending' WHERE day=? AND status='blocked_unverified_history'",
                       (end.isoformat(),))
            db.execute("UPDATE observations SET status='retry_exhausted' WHERE day=? AND status='pending' AND attempts>=6",
                       (end.isoformat(),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('cache_pending','1')")
            db.commit()
            verified_shares = {r['fund_code'] for r in shares if r['trade_date'] == end}
            verified_prices = {r['fund_code'] for r in prices if r['trade_date'] == end}
            return verified_shares == verified_prices == set(funds)
