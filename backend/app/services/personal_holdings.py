"""Private holdings monitoring: one leader, bounded reads, atomic snapshots/events."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from app.core.diagnostics import diagnostic_details
import re
import sqlite3
import threading
import time
import uuid
from zoneinfo import ZoneInfo

import portalocker
import pymysql
from sqlalchemy.exc import DBAPIError
from sqlalchemy import text

from app.services.holdings_snapshot import HoldingsError, MAX_ROWS, changes, normalize
from app.services.system_configuration import SystemConfiguration, task_config
from app.services.database_sources import DatabaseSources, SourceError

LOG = logging.getLogger(__name__)
SHANGHAI = ZoneInfo('Asia/Shanghai')
INTERVAL = 15


def failure_details(error):
    """Return types and a numeric MySQL code only, never exception messages."""
    original = error.orig if isinstance(error, DBAPIError) else error
    code = None
    if isinstance(original, pymysql.err.MySQLError) and original.args:
        value = original.args[0]
        if type(value) is int and 0 <= value <= 65535:
            code = value
    return type(error).__name__, type(original).__name__, code


def clock():
    return datetime.now(timezone.utc)


def in_window(moment, settings=None):
    local = moment.astimezone(SHANGHAI)
    plan = task_config(settings, 'holdings') if settings is not None else {'start': '09:25', 'end': '15:10'}
    return plan['start'] <= local.strftime('%H:%M') < plan['end']


def table_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', value):
        raise HoldingsError('表名须以英文字母或下划线开头，仅含字母、数字、下划线，最多64位')
    return value


class PersonalHoldings:
    def __init__(self, settings):
        self.settings = settings
        # Keep the existing private holdings database and separate key directory.
        self.root = settings.log_path / 'personal-holdings' / 'data'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'monitor.sqlite3'
        self.stop_event = threading.Event()
        self.thread = self.lock = self.engine = self.connection = self.source_ref = None
        self.cooldown = 0
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS holdings_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS holdings_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, epoch TEXT NOT NULL, version INTEGER NOT NULL,
                day TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL)''')
            db.execute('CREATE TABLE IF NOT EXISTS holdings_manual_requests (id TEXT PRIMARY KEY)')
            db.execute('CREATE INDEX IF NOT EXISTS holdings_events_day ON holdings_events(day,id)')
            db.execute('INSERT OR IGNORE INTO holdings_state VALUES(1,?)', (json.dumps(dict(
                enabled=False, source=None, epoch=uuid.uuid4().hex, baseline=False, rows=[], version=0,
                checked_at=None, observed_at=None, next_poll=0, error=None)),))
        self.path.chmod(0o600)
        self.sources = DatabaseSources(self, settings)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def state(self, db=None):
        if db is None:
            with self.db() as connection:
                return self.state(connection)
        return json.loads(db.execute('SELECT payload FROM holdings_state WHERE id=1').fetchone()[0])

    def write(self, db, state):
        previous = self.state(db)
        if previous['epoch'] != state['epoch'] and (state.get('manual') or {}).get('status') == 'pending':
            state['manual'] = dict(state['manual'], status='cancelled', error='配置或监控状态已变更，请重新同步')
        db.execute('UPDATE holdings_state SET payload=? WHERE id=1', (json.dumps(state, ensure_ascii=False),))

    def _system_configuration(self):
        from app.core.database import database_identity
        value = SystemConfiguration(self.settings).load()
        schedule = value['schedules']['holdings']
        config = dict(value['database'], table=schedule['table'], name='个人持仓')
        ref = dict(id='system:' + database_identity(config) + ':' + config['table'],
                   revision=value['database_revision']) if config['table'] else None
        return config, schedule, ref

    def _apply_system_configuration(self):
        with portalocker.Lock(str(self.root / 'configuration.lock'), timeout=10):
            return self._apply_system_configuration_locked()

    def _apply_system_configuration_locked(self):
        config, schedule, ref = self._system_configuration()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = self.state(db)
            # A failed legacy import yields an empty table. Keep the recovery
            # pointer and snapshots, but never poll that legacy connection.
            if ref is None and state['source'] and not state['source']['id'].startswith('system:'):
                if state['enabled']:
                    state.update(enabled=False, epoch=uuid.uuid4().hex)
                    self.write(db, state)
                return config, schedule, None
            enabled = bool(schedule['enabled'] and ref)
            if state['source'] != ref or state['enabled'] != enabled:
                target_changed = (state['source'] or {}).get('id') != (ref or {}).get('id')
                state.update(source=ref, enabled=enabled, epoch=uuid.uuid4().hex,
                             next_poll=0, system_table=config['table'])
                if target_changed:
                    state.update(baseline=False, rows=[], version=state['version']+1,
                                 checked_at=None, observed_at=None, error=None)
                self.write(db, state)
        return config, schedule, ref

    def configuration(self):
        config, _, ref = self._apply_system_configuration()
        return ({key: value for key, value in config.items() if key != 'password'} |
                {'password_set': config['password'] is not None}) if ref else None

    def input_config(self, config):
        config = self.sources.canonical(dict(config, name='个人持仓', table=table_name(config['table'])))
        if config.get('password') is None:
            previous, _, _ = self._system_configuration()
            if any(config[key] != previous[key] for key in ('host', 'port', 'database', 'username')):
                raise HoldingsError('连接已改变，请重新输入密码')
            config['password'] = previous['password']
        return config

    def configure(self, config):
        # Internal compatibility only; the retired HTTP endpoint rejects edits.
        with portalocker.Lock(str(self.root / 'configuration.lock'), timeout=10):
            config = self.input_config(config)
            if self.settings.validation_allowed_targets and f"{config['host'].lower().rstrip('.')}:{config['port']}" not in self.settings.validation_allowed_targets:
                raise SourceError('数据库目标未获部署者允许，请检查 VALIDATION_ALLOWED_TARGETS')
            service = SystemConfiguration(self.settings)
            value = service.load()
            database = {key: config[key] for key in ('host', 'port', 'database', 'username', 'password')}
            if value['database'] != database or value['schedules']['holdings']['table'] != config['table']:
                value['database'] = database
                value['schedules']['holdings'].update(table=config['table'], enabled=False)
                service.save(value)
            result, _, ref = self._apply_system_configuration_locked()
        return {key: val for key, val in result.items() if key != 'password'} | {'password_set': True}

    def request_sync(self, request_id):
        _, _, ref = self._apply_system_configuration()
        if ref is None:
            raise HoldingsError('请先在系统配置中设置持仓数据表')
        # Durable intent: HTTP completion/navigation does not own the sampling work.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = self.state(db)
            if not state['source']:
                raise HoldingsError('请先保存数据库配置')
            if not db.execute('SELECT 1 FROM holdings_manual_requests WHERE id=?', (request_id,)).fetchone():
                db.execute('INSERT INTO holdings_manual_requests VALUES(?)', (request_id,))
                if (state.get('manual') or {}).get('status') != 'pending':
                    state['manual'] = dict(id=request_id, status='pending', error=None)
                    self.write(db, state)
        return self.status()

    def set_enabled(self, enabled):
        with portalocker.Lock(str(self.root / 'configuration.lock'), timeout=10):
            service = SystemConfiguration(self.settings)
            value = service.load()
            if enabled and not value['schedules']['holdings']['table']:
                raise HoldingsError('请先在系统配置中设置持仓数据表')
            value['schedules']['holdings']['enabled'] = enabled
            service.save(value)
            self._apply_system_configuration_locked()

    def status(self):
        _, _, ref = self._apply_system_configuration()
        state = self.state()
        state.pop('source')
        state.pop('epoch')
        state['window_open'] = in_window(clock(), self.settings)
        state['configured'] = ref is not None
        state['trading_day'] = None
        state['calendar_error'] = None
        return state

    def history(self, day, before=None, limit=100):
        with self.db() as db:
            rows = db.execute('''SELECT id,observed_at,payload FROM holdings_events
                WHERE day=? AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?''',
                              (day, before, before, limit+1)).fetchall()
            days = [r[0] for r in db.execute('SELECT DISTINCT day FROM holdings_events ORDER BY day DESC')]
        return dict(days=days, events=[dict(id=i, observed_at=at, **json.loads(payload))
                                     for i, at, payload in rows[:limit]],
                    next_before=rows[limit-1][0] if len(rows)>limit else None)

    @staticmethod
    def read(connection, table):
        statement = f'SELECT strategy2, name, amt, code FROM `{table_name(table)}` LIMIT {MAX_ROWS+1}'
        if connection.closed or connection.invalidated:
            raise HoldingsError('持仓连接已断开，等待下一轮重连')
        with connection.begin():
            # No reconnect/replay on a checked-out dead socket. The next scheduled
            # poll creates a new connection after this one has been discarded.
            # Buffer this single bounded, four-column snapshot in the driver. An
            # SSCursor tries to drain unread packets on close even after a read
            # failure has destroyed its socket, producing a secondary exception.
            # Keep the same connection across polls, but no unbuffered cursor.
            with connection.execution_options(stream_results=False).execute(text(statement)) as result:
                def rows():
                    while True:
                        batch = result.mappings().fetchmany(1000)
                        if not batch:
                            return
                        yield from batch
                return normalize(rows())

    def test(self, config):
        config = self.input_config(config)
        engine = self.sources.build_engine(config)
        try:
            with engine.connect() as connection:
                self.read(connection, config['table'])
        except HoldingsError:
            raise
        except Exception:
            raise HoldingsError('连接测试失败，请检查网络、账号只读权限、表名和四个字段') from None
        finally:
            engine.dispose()
        return {'ok': True}

    def close_source(self):
        connection, engine = self.connection, self.engine
        self.connection = self.engine = self.source_ref = None
        # Cleanup failures must not prevent recording the original read failure.
        for operation, resource in (('close', connection), ('dispose', engine)):
            if resource is not None:
                try:
                    getattr(resource, operation)()
                except Exception as error:
                    LOG.warning('Personal holdings cleanup failed operation=%s type=%s driver_type=%s mysql_errno=%s',
                                operation, *failure_details(error))

    def release(self):
        try:
            self.close_source()
        finally:
            if self.lock is not None:
                self.lock.release()
                self.lock = None

    def poll(self):
        moment = clock()
        config, schedule, expected_ref = self._apply_system_configuration()
        if expected_ref is None:
            self.release()
            return False
        state = self.state()
        manual = (state.get('manual') or {}).get('status') == 'pending'
        if self.stop_event.is_set() or (not manual and (not state['enabled'] or not in_window(moment, self.settings))):
            self.release()
            return False
        if self.lock is None:
            lock = portalocker.Lock(str(self.root / 'worker.lock'), timeout=0,
                                    flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
            try:
                lock.acquire()
            except portalocker.exceptions.LockException:
                return False
            self.lock = lock
        # Re-read after lock acquisition; no stale claimant may use changed config.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = self.state(db)
            manual = (state.get('manual') or {}).get('status') == 'pending'
            if not manual and (not state['enabled'] or not in_window(clock(), self.settings)):
                self.release()
                return False
            if not manual and state['next_poll'] > moment.timestamp():
                return False
            if not manual:
                state['next_poll'] = moment.timestamp()+schedule['interval_seconds']
                self.write(db, state)
        stage = 'source_connect'
        try:
            if self.source_ref != state['source']:
                self.close_source()
            if self.engine is None:
                self.engine = self.sources.build_engine(config)
                self.source_ref = state['source']
                self.connection = self.engine.connect()
            stage = 'source_read'
            snapshot = self.read(self.connection, config['table'])
            stage = 'snapshot_save'
            observed = clock()
            with portalocker.Lock(str(self.root / 'configuration.lock'), timeout=10):
                _, current_schedule, current_ref = self._system_configuration()
                if current_ref != expected_ref or (not manual and not current_schedule['enabled']):
                    self.close_source()
                    return False
                with self.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    current = self.state(db)
                    if (self.stop_event.is_set() or current['epoch'] != state['epoch']
                            or (not manual and (not current['enabled'] or not in_window(observed, self.settings)))):
                        self.close_source()
                        return False
                    events = changes(current['rows'], snapshot) if current['baseline'] else []
                    version = current['version'] + int(not current['baseline'] or current['rows'] != snapshot)
                    for event in events:
                        db.execute('INSERT INTO holdings_events(epoch,version,day,observed_at,payload) VALUES(?,?,?,?,?)',
                                   (current['epoch'], version, observed.astimezone(SHANGHAI).date().isoformat(),
                                    observed.isoformat(), json.dumps(event, ensure_ascii=False)))
                    current.update(rows=snapshot, baseline=True, version=version,
                                   observed_at=observed.isoformat(), checked_at=observed.isoformat(), error=None,
                                   next_poll=current['next_poll'] if manual else max(current['next_poll'], observed.timestamp()))
                    if manual:
                        current['manual'] = dict(current['manual'], status='completed', error=None)
                    self.write(db, current)
            return True
        except Exception as error:
            # Record the original failure before cleanup; never log SQL/messages.
            # SQLAlchemy's initial-connection rollback can raise InterfaceError(0)
            # after the driver already closed a failed socket. Keep the safe
            # cause/context chain so that the original timeout/reset is visible.
            LOG.warning('Personal holdings poll failed stage=%s type=%s driver_type=%s mysql_errno=%s %s',
                        stage, *failure_details(error), diagnostic_details(error))
            self.close_source()
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                current = self.state(db)
                if current['epoch'] == state['epoch'] and (manual or current['enabled']):
                    current.update(checked_at=clock().isoformat(), next_poll=current['next_poll'] if manual else clock().timestamp()+schedule['interval_seconds'],
                                   error=str(error) if isinstance(error, (HoldingsError, SourceError)) else
                                   '读取或保存失败，保留上次持仓；下一轮自动重试')
                    if manual:
                        current['manual'] = dict(current['manual'], status='failed', error='同步失败，原持仓和历史保留，请重试')
                        current['error'] = current['manual']['error']
                    self.write(db, current)
            return False
        finally:
            if manual and (not self.state()['enabled'] or not in_window(clock(), self.settings)):
                self.release()

    def loop(self):
        try:
            while not self.stop_event.is_set():
                try:
                    if time.monotonic() >= self.cooldown:
                        self.poll()
                except Exception as error:
                    LOG.warning('Personal holdings worker failed %s', diagnostic_details(error))
                    self.release()
                    self.cooldown = time.monotonic()+INTERVAL
                self.stop_event.wait(1)
        finally:
            self.release()

    def start(self):
        self.thread = threading.Thread(target=self.loop, daemon=True, name='personal-holdings')
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        else:
            self.release()
