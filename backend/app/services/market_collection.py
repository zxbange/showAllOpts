"""Durable, paced Sina snapshots and verified JoinQuant-method observations.

One process owns collection.lock for a run. SQLite commits the universe before
the first per-stock request, and each observation before the next request. A
restart therefore resumes pending stocks without rebuilding a historical pool.
"""
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time as monotonic_time
import uuid
from zoneinfo import ZoneInfo

import httpx
import portalocker
from sqlalchemy import create_engine

from app.core.diagnostics import diagnostic_details
from app.services.etf_configuration import ConfigurationFailure
from app.services.growth_dividend_source import fetch_calendar
from app.services.market_configuration import MarketConfiguration
from app.services.market_methodology import JOINQUANT_HEAT_METHOD, LEGACY_SINA_HEAT_METHOD
from app.services.market_sina import (
    SinaDataError, SinaMarketProvider, SinaRateLimitError, aggregate_crowding, aggregate_heat,
)
from app.services.market_public import MarketDependencyError, PublicMarketProvider
from app.services.market_writer import read_market_dates, write_market_results


LOG = logging.getLogger(__name__)
CHINA = ZoneInfo('Asia/Shanghai')
SCHEDULE = ['18:30']
DATED_PRICE_METHOD = 'sina_dated_raw_snapshot_v1'
RESOLVED = ('current', 'insufficient_history', 'paused')
PUBLISHED = ('written', 'existing_preserved')
METRIC_DONE = (*PUBLISHED, 'outside_window')
# A scheduled batch is the latest Shanghai 18:30 slot. Polling/restarts keep
# its durable budget; only a new slot or explicit manual request starts over.
MAX_PASSES_PER_RUN = 4
RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=6))
SAFE_ERRORS = {
    'source_unavailable', 'source_not_ready', 'source_rate_limited', 'source_forbidden',
    'calendar_unavailable', 'calendar_incomplete', 'calendar_regressed',
    'snapshot_invalid', 'historical_snapshot_missing', 'quote_unavailable',
    'quote_mismatch', 'unknown_missing_ma', 'quote_stale', 'source_partial',
    'service_interrupted', 'cancelled', 'missing_writer_configuration',
    'writer_permission_denied', 'writer_table_missing', 'writer_database_missing',
    'writer_connection_failed', 'writer_write_failed', 'writer_journal_incomplete',
    'writer_value_mismatch', 'writer_result_invalid', 'before_collection_time',
    'worker_disabled', 'universe_coverage_unverified', 'universe_incomplete',
    'adjustment_unavailable', 'adjustment_precision_unverified', 'paused_unknown', 'invalid_tdx_archive',
    'historical_count_unavailable', 'historical_count_mismatch', 'historical_identity_conflict', 'historical_category_unknown',
    'unsupported_historical_schema',
    'heat_methodology_unverified', 'heat_methodology_mismatch',
    'historical_snapshot_unverified', 'writer_methodology_mismatch',
    'snapshot_quote_mismatch',
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _stamp(moment):
    if moment.tzinfo is None:
        raise ValueError('Timezone-aware time required')
    return moment.astimezone(CHINA).isoformat()


def _number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('invalid numeric observation') from None
    if not result.is_finite():
        raise ValueError('invalid numeric observation')
    return result


class CollectionInterrupted(Exception):
    stop_collection = True


class CollectionCancelled(CollectionInterrupted):
    pass


class CollectionBusy(ConfigurationFailure):
    """This admission was rejected; another request may still be in flight."""

    def __init__(self, active_run=None):
        self.active_run = dict(active_run) if active_run is not None else None
        if self.active_run and self.active_run['status'] == 'queued':
            message = '已有采集任务排队，请等待完成后刷新状态'
        elif self.active_run:
            message = '已有采集任务正在执行，请等待完成后刷新状态'
        else:
            message = '采集服务正忙，请稍后刷新状态并重试'
        super().__init__(message)

    def detail(self):
        return {'code': 'collection_busy', 'message': str(self), 'active_run': self.active_run}


class SourceFailure(Exception):
    def __init__(self, code):
        self.code = code if code in SAFE_ERRORS else 'source_unavailable'
        super().__init__(self.code)


class PacedHTTPClient:
    """Pace actual HTTP requests, including universe pagination and MA fallbacks."""

    def __init__(self, client, owner, check):
        self.client, self.owner, self.check = client, owner, check
        with owner.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='last_http_at'").fetchone()
        delay = 0.0
        if row:
            try:
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds()
                delay = max(0.0, min(1.0, 1.0 - elapsed))
            except (ValueError, TypeError):
                pass
        self.next_request = monotonic_time.monotonic() + delay

    def __getattr__(self, name):
        return getattr(self.client, name)

    def _before_request(self):
        while True:
            self.check()
            delay = self.next_request - monotonic_time.monotonic()
            if delay <= 0:
                break
            self.owner.stop_event.wait(min(delay, 0.2))
        self.next_request = monotonic_time.monotonic() + 1.0
        with self.owner.connect() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('last_http_at',?)",
                       (datetime.now(timezone.utc).isoformat(),))

    def _check_response(self, response):
        self.check()
        if response.status_code in (403, 429):
            raise SinaRateLimitError(response.status_code)
        return response

    def get(self, *args, **kwargs):
        self._before_request()
        return self._check_response(self.client.get(*args, **kwargs))

    @contextmanager
    def stream(self, *args, **kwargs):
        self._before_request()
        with self.client.stream(*args, **kwargs) as response:
            self._check_response(response)
            yield CheckedStreamResponse(response, self.check)
            self.check()


class CheckedStreamResponse:
    """Keep streaming bounded and cancellable without buffering archive bodies."""

    def __init__(self, response, check):
        self.response, self.check = response, check

    def __getattr__(self, name):
        return getattr(self.response, name)

    def _iterate(self, iterator):
        while True:
            self.check()
            try:
                block = next(iterator)
            except StopIteration:
                return
            self.check()
            yield block

    def iter_bytes(self, *args, **kwargs):
        return self._iterate(iter(self.response.iter_bytes(*args, **kwargs)))

    def iter_raw(self, *args, **kwargs):
        return self._iterate(iter(self.response.iter_raw(*args, **kwargs)))

    def iter_lines(self, *args, **kwargs):
        return self._iterate(iter(self.response.iter_lines(*args, **kwargs)))

    def iter_text(self, *args, **kwargs):
        return self._iterate(iter(self.response.iter_text(*args, **kwargs)))


class MarketCollection:
    def __init__(self, settings, refresh=None, *, provider_factory=None, writer=None,
                 clock=None, client_factory=None, calendar_loader=None,
                 engine_factory=create_engine, heat_aggregator=None, source_dates_loader=None):
        self.settings, self.refresh = settings, refresh
        self.root = settings.log_path / 'market-data' / 'market-collection'
        self.clock = clock or (lambda: datetime.now(CHINA))
        self.client_factory = client_factory or (lambda: httpx.Client(
            timeout=httpx.Timeout(20, connect=8), trust_env=False, follow_redirects=False))
        self.provider_factory = provider_factory or (lambda client: PublicMarketProvider(client, cache_root=self.root / 'source-cache'))
        self.calendar_loader = calendar_loader or fetch_calendar
        self.writer = writer or write_market_results
        self.source_dates_loader = source_dates_loader or read_market_dates
        self.heat_aggregator = heat_aggregator or aggregate_heat
        self.engine_factory = engine_factory
        self.shared_configuration = None
        self.configuration = MarketConfiguration(self)
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread_lock = threading.Lock()
        self.worker = None

    @contextmanager
    def connect(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.root / 'state.sqlite', timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA busy_timeout=15000')
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS configuration (
                  id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
                  ciphertext BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS calendar (day TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS days (
                  trade_date TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
                  error TEXT, snapshot_json TEXT, snapshot_hash TEXT,
                  total INTEGER NOT NULL DEFAULT 0, heat_status TEXT NOT NULL DEFAULT 'pending',
                  crowding_status TEXT NOT NULL DEFAULT 'pending', heat_json TEXT, crowding_json TEXT,
                  heat_hash TEXT, crowding_hash TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                  attempt_date TEXT, attempts_today INTEGER NOT NULL DEFAULT 0,
                  next_attempt_at TEXT, cache_pending INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations (
                  day TEXT NOT NULL, symbol TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, error TEXT,
                  quote_json TEXT, valid INTEGER NOT NULL DEFAULT 0,
                  above INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                  PRIMARY KEY(day,symbol));
                CREATE TABLE IF NOT EXISTS manual_runs (
                  id TEXT PRIMARY KEY, status TEXT NOT NULL, day TEXT,
                  trigger TEXT NOT NULL DEFAULT 'manual', phase TEXT NOT NULL DEFAULT 'queued',
                  requested_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT,
                  cancel_requested INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS market_observations_pending ON observations(day,status);
                CREATE TABLE IF NOT EXISTS methodology_archive (
                  kind TEXT NOT NULL, day TEXT NOT NULL, symbol TEXT NOT NULL DEFAULT '',
                  methodology TEXT NOT NULL, payload_hash TEXT NOT NULL, payload TEXT NOT NULL,
                  archived_at TEXT NOT NULL, PRIMARY KEY(kind,day,symbol,methodology,payload_hash));
                CREATE TABLE IF NOT EXISTS hq_checkpoints (
                  day TEXT NOT NULL, node_snapshot_hash TEXT NOT NULL, symbol TEXT NOT NULL,
                  row_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                  PRIMARY KEY(day,node_snapshot_hash,symbol));
            ''')
            (self.root / 'state.sqlite').chmod(0o600)
            with connection:
                yield connection
        finally:
            connection.close()

    @property
    def configuration_issue(self):
        return self.configuration.issue()

    @property
    def configured(self):
        return self.configuration_issue is None

    def _acquire(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = portalocker.Lock(str(self.root / 'collection.lock'), timeout=0,
                                flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
        try:
            lock.acquire()
            return lock
        except portalocker.exceptions.LockException:
            return None

    def _meta(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def _put_meta(self, **values):
        with self.connect() as db:
            db.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                           [(key, str(value)) for key, value in values.items()])

    def _day(self, day):
        with self.connect() as db:
            row = db.execute('SELECT * FROM days WHERE trade_date=?', (str(day),)).fetchone()
        return dict(row) if row else None

    def _update_day(self, day, **values):
        values['updated_at'] = _stamp(self.clock())
        fields = ','.join(key + '=?' for key in values)
        with self.connect() as db:
            db.execute('UPDATE days SET ' + fields + ' WHERE trade_date=?',
                       (*values.values(), str(day)))

    def provenance(self, day):
        row = self._day(day)
        if row is None:
            return None
        return {key: row[key] for key in ('heat_hash', 'crowding_hash', 'snapshot_hash')} | {
            'heat_source': self._meta('source:heat:' + row['heat_hash'], 'sina') if row['heat_hash'] else None,
            'crowding_source': self._meta('source:crowding:' + row['crowding_hash'], 'sina') if row['crowding_hash'] else None,
            'heat_method': self._published_heat_method(row['heat_hash']),
            'crowding_method': 'amount_top5_full_universe_v1',
        }

    def _published_heat_method(self, digest):
        return self._meta('methodology:heat:' + digest, LEGACY_SINA_HEAT_METHOD) if digest else None

    @staticmethod
    def _heat_contract(heat):
        return (isinstance(heat, dict) and heat.get('methodology') == JOINQUANT_HEAT_METHOD
                and heat.get('price_basis') == 'qfq' and heat.get('denominator') == 'eligible')

    @staticmethod
    def _quote_method(quote):
        return quote.get('methodology', LEGACY_SINA_HEAT_METHOD) if isinstance(quote, dict) else LEGACY_SINA_HEAT_METHOD

    @classmethod
    def _verified_heat_quote(cls, quote):
        return (isinstance(quote, dict) and cls._quote_method(quote) == JOINQUANT_HEAT_METHOD
                and quote.get('verified') is True)

    @staticmethod
    def _capability(provider):
        check = getattr(provider, 'heat_capability', None)
        result = check() if callable(check) else {}
        verified = isinstance(result, dict) and result.get('methodology') == JOINQUANT_HEAT_METHOD and result.get('verified') is True
        return {'methodology': JOINQUANT_HEAT_METHOD, 'verified': verified,
                'error': None if verified else 'heat_methodology_unverified'}

    @staticmethod
    def _archive(db, kind, day, payload, stamp, *, symbol='', methodology=LEGACY_SINA_HEAT_METHOD):
        serialized = _json(payload)
        db.execute('INSERT OR IGNORE INTO methodology_archive VALUES (?,?,?,?,?,?,?)',
                   (kind, day, symbol, methodology, hashlib.sha256(serialized.encode()).hexdigest(), serialized, stamp))

    def _migrate_heat_methodology(self):
        """Retain old evidence/ownership, but never reuse its MA as qfq evidence."""
        with self.connect() as db:
            marker = db.execute("SELECT value FROM metadata WHERE key='heat_methodology'").fetchone()
            if marker and marker[0] == JOINQUANT_HEAT_METHOD:
                return
            stamp = _stamp(self.clock())
            for row in db.execute('SELECT * FROM days WHERE heat_json IS NOT NULL').fetchall():
                old = json.loads(row['heat_json'])
                self._archive(db, 'heat', row['trade_date'], dict(row), stamp,
                              methodology=old.get('methodology', LEGACY_SINA_HEAT_METHOD))
            for row in db.execute('SELECT * FROM observations WHERE quote_json IS NOT NULL').fetchall():
                quote = json.loads(row['quote_json'])
                self._archive(db, 'quote', row['day'], dict(row), stamp, symbol=row['symbol'],
                              methodology=self._quote_method(quote))
            # Ownership hashes and write intents deliberately survive: they still
            # identify the original row, with its original calculation contract.
            db.execute("""UPDATE days SET heat_json=NULL,heat_status='blocked',
                       status=CASE WHEN snapshot_json IS NOT NULL THEN 'pending' ELSE status END,
                       error=CASE WHEN snapshot_json IS NOT NULL THEN 'heat_methodology_unverified' ELSE error END,
                       next_attempt_at=NULL,attempt_date=NULL,attempts_today=0,updated_at=?""", (stamp,))
            db.execute("""UPDATE observations SET status='pending',quote_json=NULL,valid=0,above=0,
                       attempts=0,error=NULL,next_attempt_at=NULL,updated_at=?""", (stamp,))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('heat_methodology',?)", (JOINQUANT_HEAT_METHOD,))
            db.execute("DELETE FROM metadata WHERE key='session_checked_day'")

    @staticmethod
    def _counts(db, day):
        row = db.execute('''SELECT COUNT(*) AS total,
            COALESCE(SUM(status!='pending'),0) AS completed,
            COALESCE(SUM(status='pending'),0) AS pending,
            COALESCE(SUM(valid),0) AS valid_count, COALESCE(SUM(above),0) AS above_count,
            COALESCE(SUM(status='paused'),0) AS no_trade_count,
            COALESCE(SUM(status='insufficient_history'),0) AS insufficient_history_count,
            COALESCE(SUM(attempts),0) AS quote_attempts
            FROM observations WHERE day=?''', (day,)).fetchone()
        return dict(row)

    @staticmethod
    def _public_run(row, *, running):
        result = dict(row)
        if result['status'] == 'running' and not running:
            # A persisted running row without the execution lock is interrupted,
            # not a live worker. Recovery persists this on the next admission/tick.
            result.update(status='paused', phase='paused',
                          error='cancelled' if result['cancel_requested'] else 'service_interrupted')
        return result

    def _replay_run(self, row):
        if row['status'] != 'running':
            return dict(row)
        lock = self._acquire()
        if lock is None:
            return dict(row)
        try:
            # The owner may have finished between our first read and the probe.
            with self.connect() as db:
                current = db.execute('SELECT * FROM manual_runs WHERE id=?', (row['id'],)).fetchone()
            return self._public_run(current, running=False)
        finally:
            lock.release()

    def status(self):
        issue = self.configuration_issue
        worker_disabled = not getattr(self.settings, 'market_collection_worker_enabled', True)
        try:
            config = self.configuration.load()
        except Exception:
            config = {'enabled': False, 'schedule': SCHEDULE}
        state = dict(enabled=config['enabled'], configured=issue is None, ready=issue is None and not worker_disabled,
                     status='configuration_error' if issue else 'worker_disabled' if worker_disabled else 'idle' if config['enabled'] else 'disabled',
                     disabled_reason=issue or ('worker_disabled' if worker_disabled else None),
                     worker_disabled=worker_disabled, timezone='Asia/Shanghai', schedule=list(SCHEDULE),
                     provider='public_market', trade_date=None, total=0, completed=0, pending=0,
                     heat_methodology=JOINQUANT_HEAT_METHOD, price_basis='qfq', denominator='eligible',
                     heat_pending_count=0,
                     above_count=0, valid_count=0, no_trade_count=0, insufficient_history_count=0,
                     heat_status='pending', crowding_status='pending', cache_pending=False,
                     updated_at=None, error=issue, progress=None, rawprogress=None,
                     manual=None, runs=[], gaps=[], next_run_at=None,
                     worker_running=bool(self.worker and self.worker.is_alive()), running=False)
        if not (self.root / 'state.sqlite').exists():
            # The first owner can take the lock before creating its state DB.
            if (self.root / 'collection.lock').exists():
                lock = self._acquire()
                state['running'] = lock is None
                if lock is not None:
                    lock.release()
            return state
        with self.connect() as db:
            metadata = dict(db.execute('SELECT key,value FROM metadata'))
            active_day = metadata.get('active_day')
            selected = db.execute("SELECT * FROM days WHERE trade_date=? AND status!='superseded'", (active_day,)).fetchone() if active_day else None
            selected = selected or db.execute("SELECT * FROM days WHERE status!='superseded' ORDER BY trade_date DESC LIMIT 1").fetchone()
            state['cache_pending'] = bool(db.execute('SELECT 1 FROM days WHERE cache_pending=1 LIMIT 1').fetchone())
            state['source_latest'] = {kind: metadata.get('source_latest_' + kind) or None for kind in ('heat', 'crowding')}
            state['gaps'] = [dict(row) for row in db.execute("SELECT trade_date,status,error,heat_status,crowding_status FROM days WHERE status NOT IN ('complete','superseded') ORDER BY trade_date DESC LIMIT 50")]
            if selected:
                counts = self._counts(db, selected['trade_date'])
                counts['total'] = selected['total']
                counts['pending'] = max(0, selected['total'] - counts['completed'])
                heat = json.loads(selected['heat_json']) if selected['heat_json'] else None
                saved_snapshot = json.loads(selected['snapshot_json']) if selected['snapshot_json'] else {}
                node_hash = saved_snapshot.get('node_snapshot_hash', selected['snapshot_hash'])
                dated_rows = db.execute(
                    'SELECT COUNT(*) FROM hq_checkpoints WHERE day=? AND node_snapshot_hash=?',
                    (selected['trade_date'], node_hash)).fetchone()[0]
                if self._heat_contract(heat) and heat.get('error') == 'heat_methodology_unverified':
                    # Progress refers to saved dated raw prices, not completed
                    # qfq observations. Heat's pending count stays independent.
                    counts.update(completed=dated_rows, pending=max(0, selected['total'] - dated_rows))
                current_method = metadata.get('heat_methodology') == JOINQUANT_HEAT_METHOD
                if not current_method:
                    counts.update(valid_count=0, above_count=0, no_trade_count=0, insufficient_history_count=0)
                state.update(counts, trade_date=selected['trade_date'], status=selected['status'],
                             heat_status=selected['heat_status'], crowding_status=selected['crowding_status'],
                             updated_at=selected['updated_at'], error=selected['error'])
                state['heat_pending_count'] = (heat.get('pending_count', selected['total'])
                                               if self._heat_contract(heat) else selected['total'])
                if not current_method:
                    state.update(heat_status='blocked', error='heat_methodology_unverified')
                state['rawprogress'] = counts | {'snapshot_saved': selected['snapshot_json'] is not None,
                                                'next_attempt_at': selected['next_attempt_at'],
                                                'heat_pending_count': state['heat_pending_count']}
                state['rawprogress']['dated_rows_saved'] = dated_rows
                state['rawprogress']['price_verified_count'] = dated_rows
                if selected['crowding_json']:
                    state['rawprogress']['price_verified_count'] = json.loads(selected['crowding_json']).get('price_verified_count', dated_rows)
            now = self.clock().astimezone(CHINA)
            for row in db.execute('SELECT day FROM calendar WHERE day>=? ORDER BY day LIMIT 20', (str(now.date()),)):
                slot = datetime.combine(date.fromisoformat(row[0]), time(18, 30), CHINA)
                if slot > now:
                    state['next_run_at'] = slot.isoformat() if config['enabled'] else None
                    break
        lock = self._acquire()
        state['running'] = lock is None
        try:
            # Read task rows after probing the lock. When we own it, no worker
            # can start between the probe and this small status snapshot.
            with self.connect() as db:
                if lock is not None and selected and selected['status'] == 'running':
                    # Finishing between the day read and the lock probe is not
                    # an interruption. Prefer the newly committed day state.
                    current = db.execute('SELECT status,error,updated_at FROM days WHERE trade_date=?',
                                         (selected['trade_date'],)).fetchone()
                    state.update(dict(current))
                latest = db.execute('SELECT * FROM manual_runs ORDER BY rowid DESC LIMIT 1').fetchone()
                active = db.execute("SELECT * FROM manual_runs WHERE status='queued' OR (? AND status='running') ORDER BY rowid LIMIT 1",
                                    (state['running'],)).fetchone()
                state['manual'] = self._public_run(active or latest, running=state['running']) if (active or latest) else None
                if active or latest:
                    state['run_id'] = (active or latest)['id']
                state['runs'] = [self._public_run(row, running=state['running'])
                                 for row in db.execute('SELECT * FROM manual_runs ORDER BY rowid DESC LIMIT 10')]
            if active:
                state.update(status=active['status'], run_id=active['id'])
                state['progress'] = {'phase': active['phase'], 'completed': state['completed'],
                                     'total': state['total'], 'pending': state['pending']}
            elif not state['running'] and (state['status'] == 'running' or (latest and latest['status'] == 'running')):
                state.update(status='paused', error='service_interrupted')
                if latest and latest['status'] == 'running' and latest['cancel_requested']:
                    state['error'] = 'cancelled'
        finally:
            if lock is not None:
                lock.release()
        if issue:
            state.update(status='configuration_error', error=issue)
        elif worker_disabled:
            state.update(status='worker_disabled', error='worker_disabled')
        elif not active and not config['enabled'] and state['status'] == 'idle':
            state['status'] = 'disabled'
        if metadata.get('last_error') and not state['error']:
            state['error'] = metadata['last_error']
        return state

    def configure(self, body):
        lock = self._acquire()
        if lock is None:
            raise ConfigurationFailure('采集正在运行，请等待完成后修改配置')
        try:
            with self.connect() as db:
                if db.execute("SELECT 1 FROM manual_runs WHERE status='queued'").fetchone():
                    raise ConfigurationFailure('采集已排队，请等待完成后修改配置')
            result = self.configuration.save(body)
        finally:
            lock.release()
        self.start()
        self.wake_event.set()
        return result

    def test_configuration(self, body):
        lock = self._acquire()
        if lock is None:
            raise ConfigurationFailure('采集正在运行，请稍后测试连接')
        try:
            return self.configuration.test(body)
        finally:
            lock.release()

    def request_run(self, request_id):
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ConfigurationFailure('采集请求编号无效')
        with self.connect() as db:
            row = db.execute('SELECT * FROM manual_runs WHERE id=?', (request_id,)).fetchone()
        if row:
            return self._replay_run(row)
        if not getattr(self.settings, 'market_collection_worker_enabled', True):
            raise ConfigurationFailure('当前进程已停用市场采集后台任务，请先启用服务配置')
        if self.configuration_issue:
            raise ConfigurationFailure('采集配置尚未就绪，请先保存数据库写入账号')
        lock = self._acquire()
        if lock is None:
            with self.connect() as db:
                # A concurrent admission can commit this ID after the first read
                # and before taking the execution lock. Replay it, do not report
                # that already accepted request as a conflicting new task.
                row = db.execute('SELECT * FROM manual_runs WHERE id=?', (request_id,)).fetchone()
                if row:
                    return dict(row)
                active = db.execute("SELECT * FROM manual_runs WHERE status IN ('queued','running') ORDER BY rowid LIMIT 1").fetchone()
            raise CollectionBusy(active)
        try:
            self._migrate_heat_methodology()
            self._recover()
            stamp = _stamp(self.clock())
            with self.connect() as db:
                row = db.execute('SELECT * FROM manual_runs WHERE id=?', (request_id,)).fetchone()
                if row:
                    return dict(row)
                active = db.execute("SELECT * FROM manual_runs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
                if active:
                    raise CollectionBusy(active)
                db.execute('INSERT INTO manual_runs(id,status,requested_at,updated_at) VALUES(?,?,?,?)',
                           (request_id, 'queued', stamp, stamp))
                result = dict(db.execute('SELECT * FROM manual_runs WHERE id=?', (request_id,)).fetchone())
        finally:
            lock.release()
        self.start()
        self.wake_event.set()
        return result

    def request_cancel(self, request_id):
        with self.connect() as db:
            changed = db.execute("""UPDATE manual_runs SET cancel_requested=1,
                       status=CASE WHEN status='queued' THEN 'paused' ELSE status END,
                       error='cancelled',updated_at=? WHERE id=? AND status IN ('queued','running')""",
                       (_stamp(self.clock()), request_id)).rowcount
            row = db.execute('SELECT * FROM manual_runs WHERE id=?', (request_id,)).fetchone()
            if row is None:
                raise ConfigurationFailure('未找到采集任务，请刷新状态')
            if changed:
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('manual_paused_on',?)",
                           (str(self.clock().astimezone(CHINA).date()),))
        self.wake_event.set()
        return dict(row)

    def _recover(self):
        """Called only by the owner of collection.lock, never by another worker."""
        stamp = _stamp(self.clock())
        with self.connect() as db:
            db.execute("""UPDATE manual_runs SET status='paused',phase='paused',updated_at=?,
                       error=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'service_interrupted' END
                       WHERE status='running'""", (stamp,))
            db.execute("UPDATE days SET status='paused',error='service_interrupted',updated_at=? WHERE status='running'", (stamp,))

    def _check(self, run_id):
        if self.stop_event.is_set():
            raise CollectionInterrupted()
        if run_id:
            with self.connect() as db:
                row = db.execute('SELECT cancel_requested FROM manual_runs WHERE id=?', (run_id,)).fetchone()
            if row and row[0]:
                raise CollectionCancelled()

    def _phase(self, run_id, phase, day=None):
        self._check(run_id)
        LOG.info('Market collection phase=%s date=%s run=%s', phase, str(day) if day else '-',
                 hashlib.sha256(str(run_id).encode()).hexdigest()[:12])
        with self.connect() as db:
            db.execute('UPDATE manual_runs SET phase=?,day=COALESCE(?,day),updated_at=? WHERE id=?',
                       (phase, str(day) if day else None, _stamp(self.clock()), run_id))
            if day:
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('active_day',?)", (str(day),))

    def start(self):
        with self.thread_lock:
            if not getattr(self.settings, 'market_collection_worker_enabled', True):
                return
            if self.worker and self.worker.is_alive():
                return
            if not self.configured:
                return
            self.stop_event.clear()
            self.worker = threading.Thread(target=self._loop, name='market-collection', daemon=True)
            self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=2)

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick(self.clock())
            except Exception as error:
                LOG.warning('Market collection scheduler failed %s', diagnostic_details(error))
            self.wake_event.wait(30)
            self.wake_event.clear()

    def _calendar(self, client, moment):
        through = self._meta('calendar_through', '')
        checked = self._meta('calendar_checked_at')
        fresh = checked and moment - datetime.fromisoformat(checked) < timedelta(days=7)
        if through >= str(moment.date()) and fresh:
            return
        days = self.calendar_loader(client)
        days = [date.fromisoformat(str(day)) for day in days]
        if not days or days != sorted(set(days)):
            raise SourceFailure('calendar_unavailable')
        if str(days[-1]) < through:
            raise SourceFailure('calendar_regressed')
        with self.connect() as db:
            db.execute('DELETE FROM calendar')
            db.executemany('INSERT INTO calendar VALUES (?)', [(str(day),) for day in days])
            db.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                           [('calendar_through', str(days[-1])), ('calendar_checked_at', _stamp(moment))])
        if days[-1] < moment.date():
            raise SourceFailure('calendar_incomplete')

    def _ensure_days(self, moment):
        """Plan after each source MAX, retaining only explicitly planned gaps.

        Old untracked holes stay outside the window. A planned metric/date is
        durable before collection starts, so a later success cannot strand an
        earlier failure when the authoritative MAX advances.
        """
        today = moment.date()
        cutoff = today if moment.time() >= time(18, 30) else today - timedelta(days=1)
        source = self.source_dates_loader(self)
        existing = {kind: {str(day) for day in source[kind]} for kind in ('heat', 'crowding')}
        stamp = _stamp(moment)
        with self.connect() as db:
            calendar = [row[0] for row in db.execute('SELECT day FROM calendar WHERE day<=? ORDER BY day', (str(cutoff),))]
            latest = {kind: max(values, default=None) for kind, values in existing.items()}
            target = calendar[-1] if calendar else None
            needed = {kind: {day for day in calendar if day > latest[kind]} if latest[kind]
                      else ({target} if target else set()) for kind in existing}
            for kind in needed:
                needed[kind].update(row[0].removeprefix('gap:' + kind + ':') for row in db.execute(
                    "SELECT key FROM metadata WHERE key LIKE ?", ('gap:' + kind + ':%',)))
            if calendar and any(value and value < calendar[0] for value in latest.values()):
                raise SourceFailure('calendar_incomplete')
            planned = {day for day in set().union(*needed.values()) if day <= str(cutoff)}
            planned |= {value for value in latest.values() if value and value <= str(cutoff)}
            tracked = {row[0] for row in db.execute('SELECT trade_date FROM days WHERE trade_date<=?', (str(cutoff),))}
            LOG.info('Market gap plan target=%s heat_after=%s crowding_after=%s sessions=%d heat_existing=%d crowding_existing=%d heat_missing=%d crowding_missing=%d',
                     target or '-', latest['heat'] or '-', latest['crowding'] or '-', len(planned),
                     len(existing['heat']), len(existing['crowding']), len(needed['heat']), len(needed['crowding']))
            for kind in existing:
                db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                           ('source_latest_' + kind, latest[kind] or ''))
            for day in sorted(planned | tracked):
                db.execute("""INSERT OR IGNORE INTO days
                           (trade_date,status,created_at,updated_at) VALUES (?,'pending',?,?)""", (day, stamp, stamp))
                row = dict(db.execute('SELECT * FROM days WHERE trade_date=?', (day,)).fetchone())
                states = {}
                for kind, values in existing.items():
                    old = row[kind + '_status']
                    key = 'gap:' + kind + ':' + day
                    if day in values:
                        value = old if old in PUBLISHED else 'existing_preserved'
                        db.execute('DELETE FROM metadata WHERE key=?', (key,))
                    elif day in needed[kind]:
                        value = old if old in ('pending', 'blocked') else 'pending'
                        db.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)', (key, stamp))
                    elif old in PUBLISHED:
                        # MAX-only source reads do not enumerate prior successes.
                        value = old
                        db.execute('DELETE FROM metadata WHERE key=?', (key,))
                    else:
                        value = 'outside_window'
                    states[kind + '_status'] = value
                complete = all(value in METRIC_DONE for value in states.values())
                excluded = all(value == 'outside_window' for value in states.values())
                reset = row['status'] in ('complete', 'superseded') and not complete
                db.execute("""UPDATE days SET heat_status=?,crowding_status=?,status=?,error=?,
                           next_attempt_at=?,updated_at=? WHERE trade_date=?""",
                           (states['heat_status'], states['crowding_status'],
                            'superseded' if excluded else 'complete' if complete else 'pending' if reset else row['status'],
                            None if complete or reset else row['error'],
                            None if complete or reset else row['next_attempt_at'],
                            stamp if complete or reset else row['updated_at'], day))

    @staticmethod
    def _scheduled_scope(moment):
        slot = moment.date() if moment.time() >= time(18, 30) else moment.date() - timedelta(days=1)
        return 'scheduled:' + str(slot)

    def _pass_count(self, scope, day):
        return int(self._meta('pass-budget:' + scope + ':' + day, '0'))

    def _claim_pass(self, scope, day, *, scheduled_scope=None):
        # collection.lock serializes claim and work. Save before I/O so a crash
        # cannot reset the budget. Interrupted passes count conservatively.
        key = 'pass-budget:' + scope + ':' + day
        with self.connect() as db:
            saved = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            count = int(saved[0]) if saved else 0
            if count >= MAX_PASSES_PER_RUN:
                return False
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, str(count + 1)))
            if scheduled_scope and scheduled_scope != scope:
                # A manual run may replace an already-due automatic batch. Its
                # work also consumes that batch, so polling cannot add 4 more
                # attempts immediately after an explicitly requested run ends.
                automatic = 'pass-budget:' + scheduled_scope + ':' + day
                saved = db.execute('SELECT value FROM metadata WHERE key=?', (automatic,)).fetchone()
                used = int(saved[0]) if saved else 0
                db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                           (automatic, str(min(MAX_PASSES_PER_RUN, used + 1))))
        return True

    def _record_dependency_failure(self, scope, moment):
        scopes = {scope, self._scheduled_scope(moment)}
        with self.connect() as db:
            for budget_scope in scopes:
                key = 'dependency-budget:' + budget_scope
                saved = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
                count = int(saved[0]) if saved else 0
                db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                           (key, str(min(MAX_PASSES_PER_RUN, count + 1))))

    def _retryable_day(self, day):
        row = self._day(day)
        # Missing historical/capability proof cannot improve by repeating the
        # same calls immediately. Writer failures still retry independent of it.
        unresolved = {kind for kind in ('heat', 'crowding') if row[kind + '_status'] not in METRIC_DONE}
        if unresolved and unresolved <= getattr(self, '_blocked_writers', {}).keys():
            return False
        return row['status'] == 'partial' or (row['status'] == 'blocked'
                and isinstance(row['error'], str) and row['error'].startswith('writer_'))

    def _targets(self, moment, manual):
        with self.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM days WHERE status NOT IN ('complete','superseded') ORDER BY trade_date")]
        targets = []
        cutoff = moment.date() if moment.time() >= time(18, 30) else moment.date() - timedelta(days=1)
        for row in rows:
            if row['trade_date'] > str(cutoff):
                continue
            if manual:
                targets.append(row['trade_date'])
                continue
            if self._pass_count(self._scheduled_scope(moment), row['trade_date']) >= MAX_PASSES_PER_RUN:
                continue
            if (self._pass_count(self._scheduled_scope(moment), row['trade_date'])
                    and row['next_attempt_at'] and datetime.fromisoformat(row['next_attempt_at']) > moment):
                continue
            if (row['status'] == 'blocked' and not row['next_attempt_at']
                    and self._pass_count(self._scheduled_scope(moment), row['trade_date'])):
                delay = RETRY_DELAYS[min(max(row['attempts_today'] - 1, 0), len(RETRY_DELAYS) - 1)]
                if datetime.fromisoformat(row['updated_at']) + delay > moment:
                    continue
            targets.append(row['trade_date'])
        return targets

    def tick(self, moment=None):
        moment = (moment or self.clock())
        _stamp(moment)
        moment = moment.astimezone(CHINA)
        if self.stop_event.is_set() or not self.configured or not getattr(self.settings, 'market_collection_worker_enabled', True):
            return False
        lock = self._acquire()
        if lock is None:
            return False
        run_id, active_day, manual = None, None, False
        scope = self._scheduled_scope(moment)
        self._blocked_writers = {}
        try:
            self._migrate_heat_methodology()
            self._recover()
            self._refresh_pending()
            config = self.configuration.load()
            with self.connect() as db:
                queued = db.execute("SELECT * FROM manual_runs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
                manual = queued is not None
                pending_exists = db.execute("SELECT 1 FROM days WHERE status NOT IN ('complete','blocked','superseded') LIMIT 1").fetchone()
            if not manual and not config['enabled']:
                return False
            # Retain the activation audit marker; source-table boundaries, not
            # this legacy marker, determine the current backfill window.
            with self.connect() as db:
                db.execute("INSERT OR IGNORE INTO metadata VALUES ('activation_date',?)", (str(moment.date()),))
            if not manual and moment.time() < time(18, 30) and not pending_exists:
                return False
            paused_until = self._meta('source_paused_until')
            next_check = self._meta('scheduler_next_attempt_at')
            if not manual and self._meta('manual_paused_on') == str(moment.date()):
                return False
            if paused_until and datetime.fromisoformat(paused_until) > moment:
                if queued:
                    self._finish_run(queued['id'], 'paused', self._meta('last_error') or 'source_rate_limited')
                return False
            if not manual and next_check and datetime.fromisoformat(next_check) > moment:
                return False
            if not manual and int(self._meta('dependency-budget:' + scope, '0')) >= MAX_PASSES_PER_RUN:
                return False
            # A completed/non-trading day needs no repeated source calls every 30s.
            planned_scope = self._meta('planned_scope')
            if not manual and planned_scope == scope and not self._targets(moment, False):
                return False
            run_id = queued['id'] if queued else uuid.uuid4().hex
            scope = 'manual:' + run_id if manual else scope
            stamp = _stamp(moment)
            with self.connect() as db:
                if queued:
                    changed = db.execute("UPDATE manual_runs SET status='running',phase='calendar',updated_at=? WHERE id=? AND status='queued' AND cancel_requested=0",
                                         (stamp, run_id)).rowcount
                    if not changed:
                        return False
                else:
                    db.execute('INSERT INTO manual_runs(id,status,trigger,phase,requested_at,updated_at) VALUES(?,?,?,?,?,?)',
                               (run_id, 'running', 'scheduled', 'calendar', stamp, stamp))
            check = lambda: self._check(run_id)
            completed = False
            with self.client_factory() as client:
                paced = PacedHTTPClient(client, self, check)
                calendar_error = None
                try:
                    self._calendar(paced, moment)
                except (CollectionInterrupted, SinaRateLimitError):
                    raise
                except Exception as error:
                    calendar_error = error.code if isinstance(error, SourceFailure) else 'calendar_unavailable'
                    with self.connect() as db:
                        saved_gaps = db.execute("SELECT 1 FROM days WHERE snapshot_json IS NOT NULL AND status NOT IN ('complete','blocked','superseded') LIMIT 1").fetchone()
                    if not saved_gaps and self._meta('calendar_through', '') < str(moment.date()):
                        raise SourceFailure(calendar_error) from None
                    self._record_dependency_failure(scope, moment)
                self._ensure_days(moment)
                self._put_meta(last_error=calendar_error or '',
                               scheduler_next_attempt_at=_stamp(moment + timedelta(minutes=30)) if calendar_error else '')
                if not calendar_error:
                    self._put_meta(planned_scope=self._scheduled_scope(moment))
                    if moment.time() >= time(18, 30):
                        self._put_meta(session_checked_day=str(moment.date()))
                if manual:
                    self._put_meta(manual_paused_on='')
                targets = self._targets(moment, manual)
                with self.provider_factory(paced) as provider:
                    for pass_number in range(MAX_PASSES_PER_RUN):
                        remaining = []
                        for active_day in targets:
                            current = self._day(active_day)
                            if all(current[kind + '_status'] in METRIC_DONE for kind in ('heat', 'crowding')):
                                continue
                            self._check(run_id)
                            if not self._claim_pass(scope, active_day, scheduled_scope=self._scheduled_scope(moment)):
                                continue
                            LOG.info('Market collection pass=%d/%d date=%s run=%s',
                                     pass_number + 1, MAX_PASSES_PER_RUN, active_day, hashlib.sha256(run_id.encode()).hexdigest()[:12])
                            self._phase(run_id, 'snapshot', active_day)
                            if pass_number == 0:
                                self._begin_day(active_day, moment)
                            else:
                                self._update_day(active_day, status='running', error=None)
                            try:
                                completed = self._collect_day(provider, active_day, moment, run_id, manual) or completed
                            except (CollectionInterrupted, SinaRateLimitError, MarketDependencyError):
                                raise
                            except Exception as error:
                                if getattr(error, 'status_code', None) in (403, 429):
                                    raise SinaRateLimitError(error.status_code) from None
                                code = getattr(error, 'code', None)
                                code = code if isinstance(code, str) and code in SAFE_ERRORS else 'quote_unavailable'
                                self._defer_day(active_day, code, moment)
                                LOG.warning('Market snapshot failed date=%s reason=%s source_reason=%s %s',
                                            active_day, code, self._safe_source_reason(error), diagnostic_details(error))
                            if self._retryable_day(active_day):
                                remaining.append(active_day)
                        # Retry only after every candidate had its first chance.
                        targets = remaining
                        if not targets:
                            break
                cutoff = moment.date() if moment.time() >= time(18, 30) else moment.date() - timedelta(days=1)
                with self.connect() as db:
                    unfinished = db.execute("SELECT 1 FROM days WHERE trade_date<=? AND status NOT IN ('complete','superseded') LIMIT 1",
                                            (str(cutoff),)).fetchone() is not None
                    has_days = db.execute('SELECT 1 FROM days WHERE trade_date<=? LIMIT 1',
                                          (str(cutoff),)).fetchone() is not None
                if not has_days and manual and moment.time() < time(18, 30):
                    self._finish_run(run_id, 'blocked', 'before_collection_time')
                else:
                    self._finish_run(run_id, 'partial' if unfinished else 'complete')
            self._refresh_pending()
            return completed
        except SinaRateLimitError as error:
            code = 'source_forbidden' if getattr(error, 'status_code', None) == 403 else 'source_rate_limited'
            pause = datetime.combine(moment.date() + timedelta(days=1), time(18, 30), CHINA)
            self._put_meta(source_paused_until=_stamp(pause), last_error=code)
            if active_day:
                self._update_day(active_day, status='paused', error=code, next_attempt_at=_stamp(pause))
            if run_id:
                self._finish_run(run_id, 'paused', code)
            return False
        except CollectionInterrupted as error:
            code = 'cancelled' if isinstance(error, CollectionCancelled) else 'service_interrupted'
            if active_day:
                self._update_day(active_day, status='paused', error=code)
            if run_id:
                self._finish_run(run_id, 'paused', code)
            return False
        except Exception as error:
            code = getattr(error, 'code', 'calendar_unavailable' if active_day is None else 'source_unavailable')
            if not isinstance(code, str) or code not in SAFE_ERRORS:
                code = 'source_unavailable'
            self._record_dependency_failure(scope, moment)
            self._put_meta(last_error=code, scheduler_next_attempt_at=_stamp(moment + timedelta(minutes=30)))
            if active_day:
                self._defer_day(active_day, code, moment)
            if run_id:
                self._finish_run(run_id, 'failed', code)
            LOG.warning('Market collection failed date=%s reason=%s %s', active_day or '-', code, diagnostic_details(error))
            return False
        finally:
            lock.release()

    @staticmethod
    def _safe_source_reason(error):
        code = getattr(error, 'code', None)
        return code if isinstance(code, str) and code in SAFE_ERRORS else 'unclassified'

    def _begin_day(self, day, moment):
        row = self._day(day)
        attempts_today = row['attempts_today'] + 1 if row['attempt_date'] == str(moment.date()) else 1
        self._update_day(day, status='running', error=None, attempts=row['attempts'] + 1,
                         attempt_date=str(moment.date()), attempts_today=attempts_today)

    def _defer_day(self, day, error, moment):
        error = error if isinstance(error, str) and error in SAFE_ERRORS else 'quote_unavailable'
        row = self._day(day)
        LOG.warning('Market collection deferred date=%s reason=%s attempt=%d heat=%s crowding=%s',
                    day, error if error in SAFE_ERRORS else 'source_unavailable', row['attempts_today'],
                    row['heat_status'], row['crowding_status'])
        delay = RETRY_DELAYS[min(max(row['attempts_today'] - 1, 0), len(RETRY_DELAYS) - 1)]
        moment = max(moment, self.clock().astimezone(CHINA))
        self._update_day(day, status='partial', error=error, next_attempt_at=_stamp(moment + delay))

    def _finish_run(self, run_id, status, error=None):
        with self.connect() as db:
            db.execute("""UPDATE manual_runs SET
                       status=CASE WHEN cancel_requested=1 THEN 'paused' ELSE ? END,
                       phase=CASE WHEN cancel_requested=1 THEN 'paused' ELSE ? END,
                       error=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE ? END,
                       updated_at=? WHERE id=?""", (status, status, error, _stamp(self.clock()), run_id))

    @staticmethod
    def _validate_snapshot(snapshot, day):
        try:
            rows = snapshot['rows']
            symbols = [row['symbol'] for row in rows]
            if snapshot['date'] != day or not rows or snapshot['expected_count'] != len(rows) or len(set(symbols)) != len(rows):
                raise ValueError()
            for row in rows:
                if _number(row['amount']) < 0 or _number(row['volume']) < 0 or _number(row['close']) < 0:
                    raise ValueError()
            if sum((_number(row['amount']) for row in rows), Decimal(0)) <= 0:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise SourceFailure('snapshot_invalid') from None

    def _store_snapshot(self, day, snapshot):
        payload = _json(snapshot)
        stamp = _stamp(self.clock())
        with self.connect() as db:
            previous = db.execute('SELECT snapshot_json FROM days WHERE trade_date=?', (day,)).fetchone()
            if previous and previous[0] and previous[0] != payload:
                self._archive(db, 'snapshot', day, json.loads(previous[0]), stamp,
                              methodology=DATED_PRICE_METHOD)
                # A verified HQ snapshot replaces rounded node amounts. Keep
                # ownership, but recompute our aggregate using the new amounts.
                db.execute("UPDATE days SET crowding_json=NULL,crowding_status=CASE WHEN crowding_status IN ('written','existing_preserved','outside_window') THEN crowding_status ELSE 'pending' END WHERE trade_date=?", (day,))
                for old in db.execute('SELECT * FROM observations WHERE day=? AND quote_json IS NOT NULL', (day,)).fetchall():
                    self._archive(db, 'quote', day, dict(old), stamp, symbol=old['symbol'],
                                  methodology=self._quote_method(json.loads(old['quote_json'])))
                # Codes, prices and the current pool may have changed. A new
                # snapshot gets a new observation set without losing its audit.
                db.execute('DELETE FROM observations WHERE day=?', (day,))
            db.execute('UPDATE days SET snapshot_json=?,snapshot_hash=?,total=?,updated_at=? WHERE trade_date=?',
                       (payload, hashlib.sha256(payload.encode()).hexdigest(), len(snapshot['rows']), stamp, day))
            db.executemany('INSERT OR IGNORE INTO observations(day,symbol,updated_at) VALUES(?,?,?)',
                           [(day, row['symbol'], stamp) for row in snapshot['rows']])

    @classmethod
    def _dated_snapshot(cls, snapshot, day):
        try:
            cls._validate_snapshot(snapshot, day)
            return (snapshot.get('source_has_trading_date') is True
                    and snapshot.get('dated_snapshot_verified') is True
                    and snapshot.get('date_verified_by') == 'sina_hq'
                    and all(row.get('date') == day for row in snapshot['rows']))
        except (SourceFailure, AttributeError, TypeError):
            return False

    def _checkpoint_hq(self, day, digest, node_rows, rows, run_id):
        """Commit one verified HQ batch before permitting the next HTTP request."""
        stamp = _stamp(self.clock())
        symbols = set()
        try:
            for row in rows:
                symbol = row['symbol']
                node = node_rows[symbol]
                if (symbol in symbols or row['date'] != day
                        or _number(row['close']) != _number(node['close'])
                        or _number(row['volume']) != _number(node['volume'])
                        or _number(row['amount']).quantize(Decimal('1'), rounding=ROUND_HALF_UP) != _number(node['amount'])):
                    raise ValueError()
                symbols.add(symbol)
        except (KeyError, TypeError, ValueError):
            raise SourceFailure('snapshot_invalid') from None
        with self.connect() as db:
            db.executemany('INSERT OR REPLACE INTO hq_checkpoints VALUES(?,?,?,?,?)',
                           [(day, digest, row['symbol'], _json(row), stamp) for row in rows])
            db.execute('UPDATE days SET updated_at=? WHERE trade_date=?', (stamp, day))
        self._check(run_id)

    def _collect_without_verified_heat(self, provider, day, snapshot, moment, run_id):
        """Collect dated raw prices independently; unproved qfq heat stays blocked."""
        heat = aggregate_heat(snapshot, []) | {'error': 'heat_methodology_unverified'}
        current = self._day(day)
        self._update_day(day, heat_json=_json(heat),
                         heat_status=current['heat_status'] if current['heat_status'] in METRIC_DONE else 'blocked')
        if not self._dated_snapshot(snapshot, day):
            self._phase(run_id, 'verifying_snapshot', day)
            digest = self._day(day)['snapshot_hash']
            node_rows = {row['symbol']: row for row in snapshot['rows']}
            with self.connect() as db:
                cached = [json.loads(row[0]) for row in db.execute(
                    'SELECT row_json FROM hq_checkpoints WHERE day=? AND node_snapshot_hash=? ORDER BY symbol',
                    (day, digest))]
            complete_cache = (len(cached) == len(node_rows)
                              and {row.get('symbol') for row in cached} == set(node_rows))
            if day != str(self.clock().astimezone(CHINA).date()) and not complete_cache:
                row = self._day(day)
                self._update_day(day, status='blocked', error='historical_snapshot_unverified',
                                 crowding_status=row['crowding_status'] if row['crowding_status'] in METRIC_DONE else 'blocked',
                                 next_attempt_at=None)
                return False
            # A complete checkpoint set is re-parsed from saved hq_raw without
            # requesting any current quote, including after a midnight restart.
            try:
                snapshot = provider.fetch_dated_snapshot(
                    snapshot, date.fromisoformat(day), cached_rows=cached,
                    checkpoint=lambda rows: self._checkpoint_hq(day, digest, node_rows, rows, run_id))
            except SinaDataError as error:
                if error.code != 'snapshot_quote_mismatch':
                    raise
                # Only an explicit node/HQ mismatch can invalidate today's
                # pool. The next normal retry refreshes it within the same
                # request budget; transport failures keep all saved evidence.
                if day == str(self.clock().astimezone(CHINA).date()):
                    self._put_meta(**{'snapshot-refresh:' + day: digest})
                raise SourceFailure('snapshot_quote_mismatch') from None
            if (not self._dated_snapshot(snapshot, day)
                    or set(node_rows) != {row['symbol'] for row in snapshot['rows']}):
                raise SourceFailure('snapshot_invalid')
            self._check(run_id)
            snapshot['node_snapshot_hash'] = digest
            self._store_snapshot(day, snapshot)
        # These are dated raw-price observations only. They resolve crowding's
        # freshness gate but contain no verified historical paused/qfq evidence.
        stamp = _stamp(self.clock())
        with self.connect() as db:
            db.executemany("""UPDATE observations SET status='price_verified',quote_json=?,
                           valid=0,above=0,error=NULL,next_attempt_at=NULL,updated_at=?
                           WHERE day=? AND symbol=?""",
                           [(_json({'symbol': row['symbol'], 'day': day, 'date': row['date'],
                                    'close': row['close'], 'volume': row['volume'],
                                    'methodology': DATED_PRICE_METHOD, 'verified': False,
                                    'status': 'price_verified', 'raw': row.get('hq_raw')}),
                             stamp, day, row['symbol']) for row in snapshot['rows']])
        crowding = aggregate_crowding(snapshot) | {
            'price_verified_count': len(snapshot['rows']), 'prices_verified': True,
        }
        self._update_day(day, crowding_json=_json(crowding))
        self._phase(run_id, 'writing', day)
        writer_ok = self._publish(day, None, crowding if crowding.get('publishable') else None, snapshot)
        row = self._day(day)
        if not writer_ok:
            self._defer_day(day, row['error'] or 'writer_write_failed', moment)
        elif all(row[kind + '_status'] in METRIC_DONE for kind in ('heat', 'crowding')):
            self._update_day(day, status='complete', error=None, next_attempt_at=None)
            return True
        elif row['crowding_status'] in METRIC_DONE:
            self._update_day(day, status='blocked', error='heat_methodology_unverified', next_attempt_at=None)
        else:
            self._defer_day(day, 'source_partial', moment)
        return False

    def _save_quote(self, day, snapshot_row, quote, moment):
        source_status = quote.get('status') if isinstance(quote, dict) else None
        status, valid, above = 'pending', 0, 0
        error = {'stale': 'quote_stale', 'mismatch': 'quote_mismatch',
                 'unknown_missing_ma': 'unknown_missing_ma'}.get(source_status, 'quote_unavailable')
        try:
            verdict = self.heat_aggregator({'date': day, 'expected_count': 1, 'rows': [snapshot_row]}, [quote])
            if not self._verified_heat_quote(quote):
                supplied_error = quote.get('error') if isinstance(quote, dict) else None
                error = supplied_error if source_status not in RESOLVED and isinstance(supplied_error, str) and supplied_error in SAFE_ERRORS else 'heat_methodology_mismatch'
            elif (source_status in RESOLVED and self._heat_contract(verdict)
                  and verdict.get('pending_count') == 0 and not verdict.get('unexpected_symbols')):
                status, valid, above = source_status, verdict['valid_count'], verdict['above_count']
            elif source_status in RESOLVED:
                error = 'quote_mismatch'
        except (AttributeError, KeyError, ValueError, TypeError):
            error = 'quote_mismatch'
        if status != 'pending':
            error = None
        with self.connect() as db:
            attempts = db.execute('SELECT attempts FROM observations WHERE day=? AND symbol=?',
                                  (day, snapshot_row['symbol'])).fetchone()[0]
            retry = moment + RETRY_DELAYS[min(max(attempts - 1, 0), len(RETRY_DELAYS) - 1)]
            db.execute('''UPDATE observations SET status=?,quote_json=?,valid=?,above=?,error=?,
                       next_attempt_at=?,updated_at=? WHERE day=? AND symbol=?''',
                       (status, _json(quote), valid, above, error,
                        _stamp(retry) if status == 'pending' else None, _stamp(self.clock()), day, snapshot_row['symbol']))

    def _fetch_one(self, provider, day, snapshot_row):
        symbol = snapshot_row['symbol']
        with self.connect() as db:
            db.execute('UPDATE observations SET attempts=attempts+1,updated_at=? WHERE day=? AND symbol=?',
                       (_stamp(self.clock()), day, symbol))
        try:
            elapsed = (self.clock().astimezone(CHINA).date() - date.fromisoformat(day)).days
            quote = provider.fetch_quote(symbol, date.fromisoformat(day), snapshot=snapshot_row,
                                         datalen=min(1970, elapsed + 40) if elapsed > 0 else 1)
        except (CollectionInterrupted, SinaRateLimitError) as error:
            code = ('source_forbidden' if error.status_code == 403 else 'source_rate_limited') if isinstance(error, SinaRateLimitError) else (
                'cancelled' if isinstance(error, CollectionCancelled) else 'service_interrupted')
            with self.connect() as db:
                db.execute('UPDATE observations SET error=?,updated_at=? WHERE day=? AND symbol=?',
                           (code, _stamp(self.clock()), day, symbol))
            raise
        except MarketDependencyError as error:
            code = self._safe_source_reason(error)
            with self.connect() as db:
                db.execute('UPDATE observations SET error=?,updated_at=? WHERE day=? AND symbol=?',
                           (code if code in SAFE_ERRORS else 'source_unavailable', _stamp(self.clock()), day, symbol))
            raise
        except Exception as error:
            if getattr(error, 'status_code', None) in (403, 429):
                raise SinaRateLimitError(error.status_code) from None
            code = self._safe_source_reason(error)
            quote = dict(symbol=symbol, day=day, status='transient_failure',
                         error=code if code in SAFE_ERRORS else 'source_unavailable')
        if isinstance(quote, dict) and quote.get('error') in ('source_forbidden', 'source_rate_limited'):
            raise SinaRateLimitError(403 if quote['error'] == 'source_forbidden' else 429)
        if isinstance(quote, dict) and quote.get('error') in ('calendar_unavailable', 'calendar_incomplete', 'calendar_regressed'):
            raise MarketDependencyError(quote['error'])
        self._save_quote(day, snapshot_row, quote, self.clock().astimezone(CHINA))
        return quote

    @staticmethod
    def _fresh_quote(quote, snapshot_row, day):
        try:
            return (quote.get('status') in ('current', 'insufficient_history', 'unknown_missing_ma')
                    and quote['symbol'] == snapshot_row['symbol']
                    and quote['day'] == quote['date'] == day
                    and _number(quote['close']) == _number(snapshot_row['close']) > 0
                    and _number(quote['volume']) == _number(snapshot_row['volume']) > 0)
        except (AttributeError, KeyError, ValueError, TypeError):
            return False

    @classmethod
    def _price_verified(cls, quote, snapshot_row, day):
        if cls._fresh_quote(quote, snapshot_row, day):
            return True
        try:
            if (cls._verified_heat_quote(quote) and quote.get('status') == 'paused'
                    and quote.get('paused') == 1 and quote['symbol'] == snapshot_row['symbol']
                    and quote['day'] == quote['date'] == day
                    and _number(quote['close']) == _number(snapshot_row['close']) > 0
                    and _number(quote['volume']) == _number(snapshot_row['volume']) == _number(snapshot_row['amount']) == 0):
                return True
            return (quote.get('status') == 'no_trade'
                    and quote['symbol'] == snapshot_row['symbol'] and quote['day'] == day
                    and date.fromisoformat(quote['date']) < date.fromisoformat(day)
                    and _number(quote['close']) > 0
                    and _number(snapshot_row['volume']) == _number(snapshot_row['amount']) == 0)
        except (AttributeError, KeyError, ValueError, TypeError):
            return False

    def _verify_snapshot(self, provider, day, snapshot, run_id, manual, attempted):
        """The undated quote list needs dated, matching samples in all markets."""
        digest = self._day(day)['snapshot_hash']
        saved = self._meta('snapshot-verification:' + day)
        if saved:
            try:
                if json.loads(saved)['snapshot_hash'] == digest:
                    return True
            except (ValueError, KeyError, TypeError):
                pass
        samples = []
        for market in ('sh', 'sz', 'bj'):
            rows = [row for row in snapshot['rows'] if row['symbol'].startswith(market)
                    and _number(row['amount']) > 0 and _number(row['volume']) > 0 and _number(row['close']) > 0]
            if not rows:
                return False
            samples.append(max(rows, key=lambda row: (_number(row['amount']), row['symbol'])))
        verified = True
        for sample in samples:
            self._check(run_id)
            with self.connect() as db:
                stored = db.execute('SELECT * FROM observations WHERE day=? AND symbol=?',
                                    (day, sample['symbol'])).fetchone()
            quote = json.loads(stored['quote_json']) if stored['quote_json'] else None
            if self._fresh_quote(quote, sample, day):
                continue
            # The enclosing run owns the durable pass budget.
            quote = self._fetch_one(provider, day, sample)
            attempted.add(sample['symbol'])
            if not self._fresh_quote(quote, sample, day):
                verified = False
        if verified:
            self._put_meta(**{'snapshot-verification:' + day: _json({
                'snapshot_hash': digest, 'symbols': [row['symbol'] for row in samples],
                'verified_at': _stamp(self.clock()),
            })})
        return verified

    def _collect_day(self, provider, day, moment, run_id, manual):
        row = self._day(day)
        snapshot = json.loads(row['snapshot_json']) if row['snapshot_json'] else None
        source_id = getattr(provider, 'source_id', None)
        if snapshot is not None and source_id and snapshot.get('source') != source_id:
            # A saved legacy pool cannot acquire the new provider's scope or
            # evidence. A successful replacement archives the old raw snapshot.
            snapshot = None
        refresh_node = (day == str(self.clock().astimezone(CHINA).date())
                        and self._meta('snapshot-refresh:' + day) == row['snapshot_hash']
                        and row['snapshot_hash'] is not None)
        if snapshot is None or refresh_node:
            historical = day != str(self.clock().astimezone(CHINA).date())
            capability = getattr(provider, 'historical_capability', lambda: {})()
            if historical and not (isinstance(capability, dict) and capability.get('verified') is True):
                self._update_day(day, status='blocked', error='historical_snapshot_missing',
                                 heat_status=row['heat_status'] if row['heat_status'] in METRIC_DONE else 'blocked',
                                 crowding_status=row['crowding_status'] if row['crowding_status'] in METRIC_DONE else 'blocked')
                return False
            if not historical and provider.latest_session_day() != date.fromisoformat(day):
                raise SourceFailure('source_not_ready')
            self._check(run_id)
            snapshot = provider.fetch_universe(date.fromisoformat(day))
            self._validate_snapshot(snapshot, day)
            self._check(run_id)
            self._store_snapshot(day, snapshot)
            LOG.info('Market snapshot saved date=%s source=%s securities=%d dated=%s', day,
                     'public_market' if snapshot.get('source') == 'public_market' else 'sina',
                     len(snapshot['rows']), snapshot.get('dated_snapshot_verified') is True)
            with self.connect() as db:
                db.execute('DELETE FROM metadata WHERE key=?', ('snapshot-refresh:' + day,))
        self._validate_snapshot(snapshot, day)
        prepare = getattr(provider, 'prepare_snapshot', None)
        if callable(prepare):
            prepare(snapshot)
        dated_prices = (snapshot.get('source_has_trading_date') is True
                        and snapshot.get('dated_snapshot_verified') is True
                        and all(item.get('date') == day for item in snapshot['rows']))
        crowding = aggregate_crowding(snapshot)
        crowding_write_error = None
        if dated_prices:
            # Exact-day raw prices/amounts are independent of adjusted MA/factor
            # availability. Commit crowding before potentially failing heat I/O.
            crowding |= {'price_verified_count': len(snapshot['rows']), 'prices_verified': True}
            self._update_day(day, crowding_json=_json(crowding))
            self._phase(run_id, 'writing', day)
            if not self._publish(day, None, crowding if crowding.get('publishable') else None, snapshot):
                # A table-specific failure must not prevent heat publication.
                # Do not retry crowding in the later heat transaction this round.
                crowding_write_error = self._day(day)['error'] or 'writer_write_failed'
        current = self._day(day)
        if current['heat_status'] in METRIC_DONE and current['crowding_status'] in METRIC_DONE:
            self._update_day(day, status='complete', error=None, next_attempt_at=None)
            self._put_meta(last_success_at=_stamp(self.clock()),
                           last_success_date=max(self._meta('last_success_date', ''), day))
            return True
        if dated_prices and current['heat_status'] in METRIC_DONE:
            self._defer_day(day, crowding_write_error or 'source_partial', moment)
            return False
        if not self._capability(provider)['verified']:
            if not dated_prices:
                return self._collect_without_verified_heat(provider, day, snapshot, moment, run_id)
            heat = aggregate_heat(snapshot, []) | {'error': 'heat_methodology_unverified'}
            current = self._day(day)
            complete = current['heat_status'] in METRIC_DONE and current['crowding_status'] in METRIC_DONE
            self._update_day(day, heat_json=_json(heat),
                             heat_status=current['heat_status'] if current['heat_status'] in METRIC_DONE else 'blocked',
                             status='complete' if complete else 'blocked',
                             error=None if complete else crowding_write_error or 'heat_methodology_unverified')
            return complete
        if self._day(day)['heat_status'] not in METRIC_DONE:
            self._update_day(day, heat_status='pending')
        self._update_day(day, crowding_json=_json(crowding))
        attempted = set()
        self._phase(run_id, 'verifying_snapshot', day)
        verified = dated_prices or self._verify_snapshot(provider, day, snapshot, run_id, manual, attempted)
        self._phase(run_id, 'ma20', day)
        snapshot_rows = {item['symbol']: item for item in snapshot['rows']}
        with self.connect() as db:
            pending = [dict(item) for item in db.execute('SELECT * FROM observations WHERE day=? ORDER BY symbol', (day,))
                       if item['status'] == 'pending' or not self._verified_heat_quote(json.loads(item['quote_json']) if item['quote_json'] else None)]
        for item in pending:
            self._check(run_id)
            if item['symbol'] in attempted:
                continue
            symbol = item['symbol']
            self._fetch_one(provider, day, snapshot_rows[symbol])
        self._check(run_id)
        with self.connect() as db:
            quotes = [json.loads(item[0]) for item in db.execute('SELECT quote_json FROM observations WHERE day=? AND quote_json IS NOT NULL', (day,))]
        heat = self.heat_aggregator(snapshot, quotes)
        LOG.info('Market heat validation date=%s total=%d valid=%d above=%d paused=%d new_listing=%d pending=%d',
                 day, len(snapshot['rows']), heat.get('valid_count', 0), heat.get('above_count', 0),
                 heat.get('paused_count', 0), heat.get('insufficient_history_count', 0), heat.get('pending_count', 0))
        reasons = {}
        for quote in quotes:
            reason = quote.get('error')
            if reason is not None:
                reason = reason if isinstance(reason, str) and reason in SAFE_ERRORS else 'quote_unavailable'
                reasons[reason] = reasons.get(reason, 0) + 1
        if reasons:
            LOG.warning('Market heat unresolved date=%s reasons=%s', day, _json(dict(sorted(reasons.items()))))
        by_symbol = {quote.get('symbol'): quote for quote in quotes}
        price_count = (len(snapshot['rows']) if dated_prices else
                       sum(self._price_verified(by_symbol.get(item['symbol']), item, day)
                           for item in snapshot['rows']))
        prices_ready = verified and price_count == len(snapshot['rows'])
        crowding = crowding | {'price_verified_count': price_count, 'prices_verified': prices_ready}
        self._update_day(day, crowding_json=_json(crowding))
        self._update_day(day, heat_json=_json(heat))
        self._phase(run_id, 'writing', day)
        # The list itself has no date. Every stock must be reconciled before its
        # amounts can become a dated published aggregate, even if three samples
        # already matched. A missing MA alone does not invalidate verified prices.
        writer_ok = self._publish(day, heat if self._heat_contract(heat) and heat.get('publishable') and prices_ready else None,
                                  crowding if not dated_prices and crowding.get('publishable') and prices_ready else None, snapshot)
        row = self._day(day)
        complete = row['heat_status'] in METRIC_DONE and row['crowding_status'] in METRIC_DONE
        if complete:
            self._update_day(day, status='complete', error=None, next_attempt_at=None)
            self._put_meta(last_success_at=_stamp(self.clock()),
                           last_success_date=max(self._meta('last_success_date', ''), day))
        elif not writer_ok:
            self._defer_day(day, row['error'] or 'writer_write_failed', moment)
        else:
            reasons = {quote.get('error') for quote in quotes if isinstance(quote, dict)
                       and isinstance(quote.get('error'), str) and quote.get('error') in SAFE_ERRORS}
            reason = next((code for code in ('adjustment_unavailable', 'adjustment_precision_unverified', 'paused_unknown') if code in reasons), 'source_partial')
            self._defer_day(day, crowding_write_error or reason, moment)
        return complete

    def _publish(self, day, heat, crowding, snapshot):
        if heat is not None and not self._heat_contract(heat):
            self._update_day(day, error='heat_methodology_mismatch')
            return False
        row = self._day(day)
        heat = heat if row['heat_status'] not in METRIC_DONE else None
        crowding = crowding if row['crowding_status'] not in METRIC_DONE else None
        blocked = getattr(self, '_blocked_writers', {})
        blocked_error = next((blocked[kind] for kind, value in (('heat', heat), ('crowding', crowding))
                              if value is not None and kind in blocked), None)
        heat = None if 'heat' in blocked else heat
        crowding = None if 'crowding' in blocked else crowding
        if blocked_error:
            self._update_day(day, error=blocked_error)
        if heat is None and crowding is None:
            return blocked_error is None
        try:
            result = self.writer(self, date.fromisoformat(day), heat, crowding, snapshot)
            updates = {}
            for kind, supplied in (('heat', heat), ('crowding', crowding)):
                if supplied is None:
                    continue
                status = result.get(kind + '_status')
                if status not in (*PUBLISHED, 'pending'):
                    raise ValueError()
                digest = result.get(kind + '_hash')
                if status == 'written' and (not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)):
                    raise ValueError()
                updates[kind + '_status'] = status
                updates[kind + '_hash'] = digest if status == 'written' else None
            if any(value in PUBLISHED for key, value in updates.items() if key.endswith('_status')):
                updates['cache_pending'] = 1
            self._update_day(day, **updates)
            with self.connect() as db:
                db.executemany('DELETE FROM metadata WHERE key=?',
                               [('gap:' + kind + ':' + day,) for kind in ('heat', 'crowding')
                                if updates.get(kind + '_status') in PUBLISHED])
            LOG.info('Market publication date=%s heat=%s crowding=%s cache_pending=%s', day,
                     updates.get('heat_status', 'skipped'), updates.get('crowding_status', 'skipped'),
                     bool(updates.get('cache_pending')))
            return not blocked_error and all(updates.get(kind + '_status') in PUBLISHED for kind, supplied in (('heat', heat), ('crowding', crowding)) if supplied is not None)
        except (CollectionInterrupted, SinaRateLimitError, MarketDependencyError):
            raise
        except Exception as error:
            code = getattr(error, 'code', 'writer_write_failed')
            if not isinstance(code, str) or code not in SAFE_ERRORS:
                code = 'writer_write_failed'
            if code in ('writer_permission_denied', 'writer_table_missing', 'writer_database_missing',
                        'missing_writer_configuration', 'writer_journal_incomplete'):
                for kind, supplied in (('heat', heat), ('crowding', crowding)):
                    if supplied is not None:
                        blocked[kind] = code
                self._blocked_writers = blocked
            self._update_day(day, error=code)
            LOG.warning('Market aggregate write failed date=%s reason=%s %s', day, code, diagnostic_details(error))
            return False

    def _refresh_pending(self):
        if not self.refresh:
            return
        with self.connect() as db:
            days = [row[0] for row in db.execute('SELECT trade_date FROM days WHERE cache_pending=1')]
        if not days:
            return
        moment = self.clock().astimezone(CHINA)
        last = self._meta('cache_attempt_at')
        if last and moment - datetime.fromisoformat(last) < timedelta(minutes=5):
            return
        self._put_meta(cache_attempt_at=_stamp(moment))
        try:
            if self.refresh() is not False:
                with self.connect() as db:
                    db.executemany('UPDATE days SET cache_pending=0 WHERE trade_date=?', [(day,) for day in days])
        except Exception as error:
            LOG.warning('Market cache publish failed pending_days=%d %s', len(days), diagnostic_details(error))
