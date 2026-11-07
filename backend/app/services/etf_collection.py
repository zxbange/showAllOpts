"""Low-frequency exchange collection. All state is local and durable.

Writer credentials are deliberately separate from the application's readonly DSN.
"""
from datetime import datetime, timedelta
from contextlib import contextmanager
from zoneinfo import ZoneInfo
import sqlite3
import threading
import logging
import json

from app.services.etf_configuration import ETFConfiguration, ConfigurationFailure

import httpx
import portalocker
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

LOG = logging.getLogger(__name__)
CHINA = ZoneInfo('Asia/Shanghai')
TIMES = ((9, 30), (11, 30), (18, 30))


class WriterFailure(RuntimeError):
    """Safe public classification only; original driver message is never retained."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def writer_error_code(exc):
    original = getattr(exc, 'orig', exc)
    args = getattr(original, 'args', ())
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
                     timezone='Asia/Shanghai', schedule=config['schedule'], target_sessions=config['lookback_days'],
                     historical_dates_per_run=config['batch_days'], coverage_is_target_not_completion=True,
                     manual=None, progress=None, runs=[], gaps=[], metadata={})
        if not (self.root / 'state.sqlite').exists():
            return state
        with self.connect() as db:
            # Keep responses bounded even at the maximum historical target.
            state['gaps'] = [dict(r) for r in db.execute("SELECT * FROM observations WHERE status != 'complete' ORDER BY day,code,kind LIMIT 500")]
            state['retry_exhausted_count'] = db.execute("SELECT COUNT(*) FROM observations WHERE status='retry_exhausted'").fetchone()[0]
            state['gap_count'] = db.execute("SELECT COUNT(*) FROM observations WHERE status != 'complete'").fetchone()[0]
            state['runs'] = [dict(r) for r in db.execute('SELECT * FROM slots ORDER BY slot DESC LIMIT 9')]
            state['metadata'] = dict(db.execute('SELECT key,value FROM metadata').fetchall())
            row = db.execute('SELECT * FROM manual_runs ORDER BY rowid DESC LIMIT 1').fetchone()
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
            raise ConfigurationFailure('采集配置尚未就绪，请先在页面保存独立写入账号')
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
                    db.execute("UPDATE manual_runs SET status='interrupted',error='service_interrupted' WHERE status='running'")
                    row = db.execute("SELECT * FROM manual_runs WHERE status='queued' LIMIT 1").fetchone()
                    if row:
                        if row['id'] == request_id:
                            return dict(row)
                        raise ConfigurationFailure('已有采集任务排队，请勿重复触发')
                    if retry_exhausted:
                        db.execute("UPDATE observations SET status='pending',attempts=0 WHERE status='retry_exhausted'")
                    db.execute('INSERT INTO manual_runs(id,status,requested_at,phase) VALUES (?,?,?,?)',
                               (request_id, 'queued', self.clock().isoformat(), 'queued'))
                    result = dict(db.execute('SELECT * FROM manual_runs WHERE id=?', (request_id,)).fetchone())
        except portalocker.exceptions.LockException:
            raise ConfigurationFailure('采集正在运行，请等待本轮完成') from None
        self.start()
        self.wake_event.set()
        return result

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
                        db.execute("UPDATE manual_runs SET status='interrupted',error='service_interrupted' WHERE status='running'")
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
                LOG.warning('ETF scheduler interrupted: %s', type(exc).__name__)

    def tick(self, moment):
        if not self.configured:
            return False
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with portalocker.Lock(str(self.root / 'collection.lock'), timeout=0):
                config = self.configuration.load()
                with self.connect() as db:
                    self._refresh(db)
                    db.execute("UPDATE manual_runs SET status='interrupted',error='service_interrupted' WHERE status='running'")
                    db.execute("DELETE FROM metadata WHERE key='progress'")
                    manual = db.execute("SELECT * FROM manual_runs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
                    if manual:
                        db.execute("UPDATE manual_runs SET status='running',phase='calendar' WHERE id=?", (manual['id'],))
                        db.commit()
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
        def progress(phase, completed=0, total=0):
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('progress',?)",
                       (json.dumps(dict(phase=phase, completed=completed, total=total)),))
            if manual_id:
                db.execute('UPDATE manual_runs SET phase=?,completed=?,total=? WHERE id=?',
                           (phase, completed, total, manual_id))
            db.commit()
        progress('calendar')
        try:
            complete = self._collect(db, slot, config=config, progress=progress)
            state, error = ('complete' if complete else 'partial'), None
        except Exception as exc:
            state, error = 'failed', exc.code if isinstance(exc, WriterFailure) else 'source_unavailable'
            if self.stop_event.is_set():
                state, error = 'interrupted', 'service_interrupted'
            if isinstance(exc, WriterFailure):
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('writer_error',?)", (exc.code,))
            complete = False
        if manual_id:
            db.execute('UPDATE manual_runs SET status=?,error=?,phase=? WHERE id=?', (state, error, state, manual_id))
        else:
            db.execute('UPDATE slots SET status=?,error=? WHERE slot=?', (state, error, key))
        db.execute("DELETE FROM metadata WHERE key='progress'")
        db.commit()
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
            LOG.warning('ETF cache publish failed: %s', type(exc).__name__)

    def _collect(self, db, slot, config=None, progress=None):
        config = config or self.configuration.load()
        progress = progress or (lambda *args: None)
        c = self.collector or collector_module()
        funds = c.catalog()
        with self.client_factory() as http:
            client = c.ExchangeClient(http)
            calendar = client.calendar()
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
            start, end = calendar.default_range(config['lookback_days'])
            cutoff = slot.date() if slot.hour >= 18 else slot.date() - timedelta(days=1)
            if end > cutoff:
                while end > cutoff:
                    end = calendar.previous(end)
                start = end
                for _ in range(config['lookback_days'] - 1):
                    start = calendar.previous(start)
            days = calendar.range(start, end)
            for day in days:
                for code in funds:
                    for kind in ('shares', 'prices'):
                        state = 'blocked_unverified_history' if kind == 'prices' and code.endswith('.XSHE') and day != end else 'pending'
                        db.execute('INSERT OR IGNORE INTO observations(kind,code,day,status) VALUES (?,?,?,?)',
                                   (kind, code, day.isoformat(), state))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('coverage_start',?)", (min(days).isoformat(),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('coverage_end',?)", (end.isoformat(),))
            # A missed Shenzhen same-day close cannot be recovered from a later intraday quote.
            db.execute("UPDATE observations SET status='blocked_unverified_history' WHERE kind='prices' AND code LIKE '%.XSHE' AND day<? AND status!='complete'", (end.isoformat(),))
            db.commit()
            rows = db.execute("SELECT DISTINCT day FROM observations WHERE status='pending' AND attempts<6 AND day<=? AND (kind='shares' OR ?=1) ORDER BY day LIMIT ?",
                              (end.isoformat(), int(slot.hour >= 18), config['batch_days'])).fetchall()
            selected = sorted({end, *(datetime.fromisoformat(r[0]).date() for r in rows)})
            shares, prices, attempted = [], [], set()
            completed = 0
            total = len(selected) * len(funds) * (2 if slot.hour >= 18 else 1)
            progress('fetching', completed, total)
            for day in selected:
                if self.stop_event.is_set():
                    raise RuntimeError('Collection stopped')
                for kind in ('shares', 'prices'):
                    if kind == 'prices' and slot.hour < 18:
                        continue
                    wanted = {code: name for code, name in funds.items() if
                              (kind == 'shares' and day == end) or
                              db.execute("SELECT 1 FROM observations WHERE kind=? AND code=? AND day=? AND status='pending' AND attempts<6",
                                         (kind, code, day.isoformat())).fetchone()}
                    if not wanted:
                        continue
                    for code, name in wanted.items():
                        completed += 1
                        progress('fetching', completed, total)
                        attempted.add((kind, code, day.isoformat()))
                        try:
                            s, p, _ = c.collect(client, day, day, funds={code: name}, calendar=calendar,
                                                include_prices=kind == 'prices', include_shares=kind == 'shares')
                        except Exception as exc:
                            # Isolate source failures to the exact observation. Other fetched
                            # observations still reach the writer; no quota is charged yet.
                            LOG.warning('ETF source observation failed: %s', type(exc).__name__)
                            continue
                        shares.extend(s)
                        prices.extend(p)
            engine = None
            try:
                progress('writing', completed, total)
                if config['source'] == 'environment':
                    engine = self.engine_factory(
                        self.settings.etf_collection_writer_url.get_secret_value(), pool_pre_ping=True,
                        connect_args={'connect_timeout': 10, 'read_timeout': 60, 'write_timeout': 60})
                else:
                    engine = self.configuration.engine(config)
                c.write_records(engine, shares, prices, calendar=calendar)
            except Exception as exc:
                raise WriterFailure(writer_error_code(exc)) from None
            finally:
                if engine is not None:
                    engine.dispose()
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('writer_error','')")
            # A crash after the DB commit only causes an idempotent replay, never a skipped gap.
            # Writer failure reaches none of these quota updates.
            for kind, code, day in attempted:
                db.execute('UPDATE observations SET attempts=attempts+1 WHERE kind=? AND code=? AND day=?',
                           (kind, code, day))
            for kind, records in (('shares', shares), ('prices', prices)):
                for record in records:
                    db.execute("UPDATE observations SET status='complete' WHERE kind=? AND code=? AND day=?",
                               (kind, record['fund_code'], record['trade_date'].isoformat()))
            db.execute("UPDATE observations SET status='retry_exhausted' WHERE status='pending' AND attempts>=6")
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('cache_pending','1')")
            db.commit()
            # Existing completed values survive a failed verification, but they cannot
            # stand in for a successful latest-session share read in this run.
            verified_latest = {r['fund_code'] for r in shares if r['trade_date'] == end}
            if verified_latest != set(funds):
                return False
            return not db.execute("SELECT 1 FROM observations WHERE day=? AND status!='complete' AND (kind='shares' OR ?=1) LIMIT 1",
                                  (end.isoformat(), int(slot.hour >= 18))).fetchone()
