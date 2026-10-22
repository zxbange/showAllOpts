"""Shared persistent SQLite snapshots. Only refresh jobs read the source database."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
import threading
import time
import uuid

import portalocker
from sqlalchemy import create_engine, text, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.cache_errors import failure_details, retryable_source_error, SnapshotDataError
from app.core.cache_sync import sync_tables
from app.core.sync_progress import reporter, report
from app.core.ownership_sync import copy_ownership
from app.models.ownership import OwnershipStock, OwnershipReport, OwnershipHolders
from app.models.etf_heat import Heat, Pool
from app.models.growth import GrowthSnapshot, GrowthStock
from app.models.momentum import IndustryMomentum
from app.models.crowding import MarketCrowding
from app.models.microcap import MicrocapTemperature
from app.models.etf import ETFShare
from app.models.earnings import EarningsRun, EarningsStock
from app.models.breadth import MicrocapBreadth
from app.models.dividend import DividendSnapshot, DividendStock

logger = logging.getLogger(__name__)
CHINA = timezone(timedelta(hours=8))
MODULES = {
    'industry-momentum': ('行业动量', (IndustryMomentum,)),
    'market-crowding': ('抱团拥挤度', (MarketCrowding,)),
    'microcap-temperature': ('微盘温度计', (MicrocapTemperature,)),
    'etf-shares': ('ETF份额仪表盘', (ETFShare,)),
    'earnings-gap': ('净利润断层', (EarningsRun, EarningsStock)),
    'microcap-breadth': ('微盘新高新低', (MicrocapBreadth,)),
    'dividend-selection': ('高股息选股', (DividendSnapshot, DividendStock)),
    'high-growth': ('高增长', (GrowthSnapshot, GrowthStock)),
    'etf-momentum': ('ETF动量热度', (Pool, Heat)),
    'institution-ownership': ('个股机构持仓', (OwnershipStock, OwnershipReport, OwnershipHolders)),
}


def now():
    return datetime.now(CHINA)


def latest_slot(moment, daily_time):
    hour, minute = map(int, daily_time.split(':'))
    slot = moment.astimezone(CHINA).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment < slot:
        slot -= timedelta(days=1)
    return slot


class CacheUnavailable(RuntimeError):
    pass


class SnapshotCache:
    def __init__(self, settings, source):
        self.settings = settings
        self.source = source
        schema = [(key, [(m.__tablename__, [(c.name, str(c.type)) for c in m.__table__.columns])
                         for m in models]) for key, (_, models) in MODULES.items()]
        identity = [source.url.drivername, source.url.host, source.url.port, source.url.database,
                    source.url.username, schema, 'provider-results-v2']
        key = hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()[:20]
        self.root = settings.cache_path / key
        self.state_file = self.root / 'state.json'
        self.stop_event = threading.Event()
        self.scheduler = None
        self.worker = None
        self.schedule_floor = None

    def read_state(self):
        try:
            state = json.loads(self.state_file.read_text(encoding='utf-8'))
            return state if isinstance(state, dict) and isinstance(state.get('modules'), dict) else {'modules': {}}
        except (OSError, ValueError):
            return {'modules': {}}

    def write_state(self, state):
        temporary = self.root / ('state-' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('w', encoding='utf-8') as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(20):
                try:
                    os.replace(temporary, self.state_file)
                    break
                except PermissionError:
                    # Windows readers can briefly prevent replacement of an open state file.
                    if attempt == 19:
                        raise
                    time.sleep(0.05)
        finally:
            temporary.unlink(missing_ok=True)

    def acquire(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lock = portalocker.Lock(str(self.root / 'refresh.lock'), timeout=0,
                                 flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
        try:
            lock.acquire()
            return lock
        except portalocker.exceptions.LockException:
            return None

    def refreshing(self):
        lock = self.acquire()
        if lock is None:
            return True
        lock.release()
        return False

    def snapshot_path(self, module, entry):
        name = entry.get('file', '')
        if not name or Path(name).name != name or not name.startswith(module + '-') or not name.endswith('.sqlite'):
            return None
        path = self.root / name
        return path if path.is_file() else None

    def status(self):
        if not self.settings.cache_enabled:
            return {'enabled': False, 'refreshing': False, 'manual_enabled': False,
                    'daily_time': self.settings.cache_refresh_time, 'timezone': 'Asia/Shanghai',
                    'auto_refresh': False, 'modules': []}
        state = self.read_state()
        running = self.refreshing()
        items = []
        for module, (name, _) in MODULES.items():
            entry = state['modules'].get(module, {})
            items.append({'id': module, 'name': name, 'ready': self.snapshot_path(module, entry) is not None,
                          'updated_at': entry.get('updated_at'), 'last_attempt_at': entry.get('last_attempt_at'),
                          'rows': entry.get('rows'), 'error': entry.get('error'),
                          'progress': dict(entry.get('progress') or {},
                              status=('interrupted' if not running and (entry.get('progress') or {}).get('status') in ('running', 'waiting')
                                      else (entry.get('progress') or {}).get('status', 'idle')))})
        for item in items:
            progress = item['progress']
            if running and progress.get('status') == 'running' and progress.get('started_at'):
                progress['elapsed_seconds'] = max(0, round((now() - datetime.fromisoformat(progress['started_at'])).total_seconds()))
        return {'enabled': True, 'refreshing': running,
                'manual_enabled': True,
                'daily_time': self.settings.cache_refresh_time, 'timezone': 'Asia/Shanghai',
                'auto_refresh': self.settings.cache_auto_refresh, 'modules': items}

    @contextmanager
    def session(self, module):
        entry = self.read_state()['modules'].get(module, {})
        path = self.snapshot_path(module, entry)
        if path is None:
            raise CacheUnavailable('本地数据尚未准备好，请查看数据同步状态，等待首次同步或手动同步。')
        engine = create_engine('sqlite:///file:' + path.as_posix() + '?mode=ro&uri=true', poolclass=NullPool)
        try:
            with Session(engine) as session:
                session.info['cache_updated_at'] = entry.get('updated_at', '')
                yield session
        finally:
            engine.dispose()

    def copy_module(self, module, full=False):
        if module == 'institution-ownership':
            result = copy_ownership(self, full)
            if result is not None:
                return result
        models = MODULES[module][1]
        filename = module + '-' + uuid.uuid4().hex + '.sqlite'
        path = self.root / filename
        destination = create_engine('sqlite:///' + path.as_posix(), poolclass=NullPool)
        previous = self.snapshot_path(module, self.read_state()['modules'].get(module, {}))
        incremental = previous is not None and not full
        try:
            report('准备本地快照')
            if incremental:
                # Published files are immutable: copy locally before applying changes.
                shutil.copyfile(previous, path)
            tables = [model.__table__ for model in models]
            tables[0].metadata.create_all(destination, tables=tables)
            with self.source.connect() as source:
                if self.source.dialect.name == 'mysql':
                    source = source.execution_options(isolation_level='REPEATABLE READ')
                with source.begin(), destination.begin() as target:
                    if module == 'institution-ownership':
                        # Legacy fallback cannot certify an existing journal cursor.
                        target.exec_driver_sql('DROP TABLE IF EXISTS ownership_cache_cursor')
                    count = sync_tables(source, target, tables, module, incremental)
                    if module == 'etf-momentum':
                        head = target.execute(select(Pool.__table__)).mappings().first()
                        if head:
                            if head['rule_version'] not in ('etf_close_momentum_v1', 'etf_close_momentum_v2', 'etf_close_momentum_v3'):
                                raise SnapshotDataError('请先在聚宽运行计算入库版 ETF/LOF 采集脚本，再同步热度结果。')
                            codes = set(target.scalars(select(Heat.code).where(Heat.trade_date == head['as_of_date'])))
                            if {m['code'] for m in head['members']} - codes:
                                raise SnapshotDataError('ETF/LOF 最新热度结果尚未完整入库，保留上一快照。')
                    if module == 'industry-momentum':
                        target.execute(text('CREATE INDEX IF NOT EXISTS ix_local_momentum_type_date ON industry_momentum (industry_type, trade_date)'))
                        target.execute(text('CREATE INDEX IF NOT EXISTS ix_local_momentum_history ON industry_momentum (industry_type, industry_code, trade_date)'))
            report('校验本地快照')
            with destination.connect() as connection:
                if connection.exec_driver_sql('PRAGMA quick_check').scalar() != 'ok':
                    raise CacheUnavailable('Local snapshot validation failed')
            return filename, count
        except BaseException:
            destination.dispose()
            path.unlink(missing_ok=True)
            raise
        finally:
            destination.dispose()

    def copy_with_retry(self, module, full=False):
        for attempt in range(1, self.settings.cache_sync_attempts + 1):
            if self.stop_event.is_set():
                return None
            try:
                return self.copy_module(module, full=True) if full else self.copy_module(module)
            except Exception as exc:
                if not retryable_source_error(exc) or attempt == self.settings.cache_sync_attempts:
                    raise
                report('网络重试 {}/{}'.format(attempt + 1, self.settings.cache_sync_attempts))
                delay = min(2 ** attempt, 16)
                code, _, _ = failure_details(exc)
                logger.warning('Cache refresh retry module=%s attempt=%s/%s code=%s delay_seconds=%s',
                               module, attempt, self.settings.cache_sync_attempts, code, delay)
                # A failed copy was removed; retry opens a new source transaction and fresh local copy.
                if self.stop_event.wait(delay):
                    return None

    def _refresh_locked(self, lock, modules, reason, slot=None, full=False):
        try:
            state = self.read_state()
            state['last_attempt_at'] = now().isoformat()
            state['pending'] = list(modules)
            state['pending_full'] = full
            for module in modules:
                state['modules'].setdefault(module, {})['progress'] = {'status': 'waiting', 'stage': '等待同步', 'processed': 0}
            if slot:
                state['scheduled_slot'] = slot
            self.write_state(state)
            for module in modules:
                if self.stop_event.is_set():
                    break
                started = time.monotonic()
                entry = state['modules'].setdefault(module, {})
                entry['last_attempt_at'] = now().isoformat()
                entry['progress'] = {'status': 'running', 'stage': '连接数据库', 'processed': 0, 'started_at': now().isoformat()}
                self.write_state(state)
                last_progress_write = [0.0]
                last_progress_log = [0.0]
                def progress(stage, rows=0):
                    data = entry['progress']
                    data.update(stage=stage, processed=data['processed'] + rows,
                                elapsed_seconds=round(time.monotonic() - started, 1))
                    if time.monotonic() - last_progress_write[0] >= 1 or stage.startswith('网络重试'):
                        self.write_state(state)
                        last_progress_write[0] = time.monotonic()
                    if time.monotonic() - last_progress_log[0] >= 30:
                        logger.info('Cache progress module=%s stage=%s processed=%s seconds=%.1f',
                                    module, stage, data['processed'], data['elapsed_seconds'])
                        last_progress_log[0] = time.monotonic()
                token = reporter.set(progress)
                logger.info('Cache refresh started module=%s reason=%s', module, reason)
                try:
                    result = self.copy_with_retry(module, full)
                    if result is None:
                        break  # Leave this module pending so a restart can resume it.
                    filename, rows = result
                    if filename != entry.get('file') or not entry.get('updated_at'):
                        entry['updated_at'] = now().isoformat()
                    entry.update(file=filename, rows=rows, error=None)
                    entry['progress'].update(status='complete', stage='同步完成', elapsed_seconds=round(time.monotonic()-started, 1))
                    logger.info('Cache refresh complete module=%s rows=%s seconds=%.2f', module, rows, time.monotonic()-started)
                except Exception as exc:
                    entry['progress'].update(status='failed', stage='同步失败，保留旧快照')
                    code, hint, frames = failure_details(exc)
                    entry['error'] = '同步失败，保留上次成功数据。[{}] {}'.format(code, hint)
                    logger.error('Cache refresh failed module=%s error_type=%s code=%s hint=%s frames=%s',
                                 module, type(exc).__name__, code, hint, frames)
                finally:
                    reporter.reset(token)
                state['pending'].remove(module)
                self.write_state(state)
            self.cleanup(state)
        finally:
            lock.release()

    def refresh(self, module=None, background=True, reason='manual', full=False):
        if not self.settings.cache_enabled:
            raise CacheUnavailable('本地缓存未启用。')
        if module is not None and module not in MODULES:
            raise ValueError('Unknown cache module')
        lock = self.acquire()
        if lock is None:
            return False
        selected = [module] if module else list(MODULES)
        # Preserve unfinished work left by an interrupted process.
        state = self.read_state()
        pending = [m for m in state.get('pending', []) if m in MODULES]
        selected += [m for m in pending if m not in selected]
        full = full or bool(pending and state.get('pending_full'))
        slot = latest_slot(now(), self.settings.cache_refresh_time).isoformat() if module is None else None
        if background:
            self.worker = threading.Thread(target=self._run_background, args=(lock, selected, reason, slot, full),
                                           name='cache-refresh', daemon=True)
            try:
                self.worker.start()
            except BaseException:
                lock.release()
                raise
        else:
            self._refresh_locked(lock, selected, reason, slot, full)
        return True

    def _run_background(self, lock, selected, reason, slot, full):
        try:
            self._refresh_locked(lock, selected, reason, slot, full)
        except Exception as exc:
            logger.error('Cache refresh interrupted error_type=%s', type(exc).__name__)

    def cleanup(self, state):
        keep = {entry.get('file') for entry in state['modules'].values()}
        for path in self.root.glob('*.sqlite'):
            try:
                if path.name not in keep and time.time() - path.stat().st_mtime > 86400:
                    path.unlink()
            except OSError:
                pass

    def tick(self, moment=None):
        moment = moment or now()
        slot = latest_slot(moment, self.settings.cache_refresh_time).isoformat()
        if self.schedule_floor is None:
            self.schedule_floor = slot
        # Read schedule state after acquiring the shared lock: multiple Uvicorn workers run this loop.
        lock = self.acquire()
        if lock is None:
            return
        try:
            state = self.read_state()
            if slot <= self.schedule_floor or state.get('scheduled_slot') == slot:
                lock.release()
                return
            selected, reason = list(MODULES), 'scheduled'
            full = bool(state.get('pending') and state.get('pending_full'))
        except BaseException:
            lock.release()
            raise
        self._refresh_locked(lock, selected, reason, slot, full)

    def start(self):
        if not self.settings.cache_enabled or not self.settings.cache_auto_refresh:
            return
        def loop():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    logger.error('Cache scheduler failed error_type=%s', type(exc).__name__)
                self.stop_event.wait(30)
        self.scheduler = threading.Thread(target=loop, name='cache-scheduler', daemon=True)
        self.scheduler.start()

    def stop(self):
        self.stop_event.set()
        if self.scheduler:
            self.scheduler.join()
        if self.worker:
            self.worker.join()
