"""Durable, single-writer research jobs. Source DB is SELECT-only.

The OS lock spans a work unit; SQLite atomically commits results and cursor.
All workers may poll, but only one can perform a unit, including after restart.
New tasks read bounded static-source batches and commit each stock's results.
Legacy tasks retain their frozen inputs and never reload a committed input.
"""
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timezone, timedelta
import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid

import pandas as pd
import numpy as np
import portalocker
from sqlalchemy import text, bindparam

from app.services import short_statistics as stats
from app.services.validation_errors import mark_phase, report, automatic_retry_allowed

from app.services.validation_timing import ExecutionTimer, interrupt_timing, stop_timing
from app.services.validation_connection import TaskSource

logger = logging.getLogger(__name__)
VERSION = 'four-close-v2'
SUPPORTED_VERSIONS = {'four-close-v1', VERSION}
METHODS = {'bukou': '补票龙', 'redhover': '红悬停龙', 'xuli': '蓄力龙', 'oversold': '超跌后止跌反弹'}
MAX_ROWS = 4000
BATCH_SIZE = 20
AUTO_RETRY_LIMIT = 5
AUTO_RETRY_SECONDS = 60
STATIC_MODE = 'static-batch-v1'
BAR_COLUMNS = 'trade_date, code, open, high, low, close, adj_open, adj_high, adj_low, adj_close, volume, money, paused, high_limit, low_limit'
UNIVERSE_COLUMNS = 'trade_date, code, display_name, listed_date, eligible, is_st, market_cap'


class DeleteConflict(ValueError):
    """A safe, user-facing explanation for a deletion that cannot run yet."""


def now():
    return datetime.now(timezone.utc).isoformat()


def retry_time():
    return (datetime.fromisoformat(now()) + timedelta(seconds=AUTO_RETRY_SECONDS)).isoformat()


def reset_retry(job):
    # Call only inside a successful progress transaction or explicit manual retry.
    job.update(auto_retry_count=0, next_retry_at=None, retry_exhausted=False,
               auto_retry_allowed=False, error=None)
    job.pop('error_code', None)
    job.pop('error_phase', None)



def frame_json(frame):
    return json.dumps({'columns': list(frame.columns), 'data': frame.astype(object).where(frame.notna(), None).values.tolist()},
                      default=lambda v: v.isoformat() if hasattr(v, 'isoformat') else v.item(), allow_nan=False)


def frame_read(value):
    data = json.loads(value)
    frame = pd.DataFrame(data['data'], columns=data['columns'])
    for name in frame.columns:
        if name.endswith(('_date', '_anchor')):
            frame[name] = pd.to_datetime(frame[name])
    return frame


def records(frame):
    return json.loads(frame.to_json(orient='records', date_format='iso'))


class ValidationJobs:
    def __init__(self, settings, engine):
        self.root = settings.log_path / 'short-validation'
        self.root.mkdir(parents=True, exist_ok=True)
        self.engine = engine
        self.sources = None
        self.execution_timer = None
        self.source_lease = None
        self.stop_event = threading.Event()
        self.thread = None
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS job_requests (
                    fingerprint TEXT PRIMARY KEY, job TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS job_requests_job ON job_requests(job);
                CREATE TABLE IF NOT EXISTS stocks (
                    job TEXT, code TEXT, bars TEXT, universe TEXT, details TEXT, coverage TEXT,
                    PRIMARY KEY(job,code));
                CREATE TABLE IF NOT EXISTS outcomes (
                    job TEXT, code TEXT, day TEXT, strategy TEXT, horizon INTEGER, value REAL, status TEXT,
                    PRIMARY KEY(job,code,day,strategy,horizon));
                CREATE TABLE IF NOT EXISTS windows (
                    job TEXT, code TEXT, day TEXT, strategy TEXT, best REAL, worst REAL, win INTEGER,
                    PRIMARY KEY(job,code,day,strategy));
                CREATE INDEX IF NOT EXISTS outcomes_summary ON outcomes(job,strategy,horizon,value);
                CREATE INDEX IF NOT EXISTS windows_summary ON windows(job,strategy);
            ''')
            # The unreleased first implementation used 'cancelled' to mean pause.
            for ident, payload in db.execute('SELECT id,payload FROM jobs').fetchall():
                job = json.loads(payload)
                if job['status'] == 'cancelled' and not job.get('cancel_requested_at'):
                    job['status'] = 'paused'
                    db.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(job), ident))

    @contextmanager
    def db(self):
        db = None
        try:
            db = sqlite3.connect(self.root / 'jobs.sqlite3', timeout=15)
            with db:
                yield db
        except Exception as exc:
            mark_phase(exc, 'local_store')
            raise
        finally:
            if db is not None:
                db.close()

    def save(self, db, job):
        job['updated_at'] = now()
        db.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(job, ensure_ascii=False), job['id']))

    def list(self):
        with self.db() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM jobs ORDER BY rowid DESC LIMIT 30')]

    def get(self, ident, db=None):
        if db is None:
            with self.db() as connection:
                return self.get(ident, connection)
        row = db.execute('SELECT payload FROM jobs WHERE id=?', (ident,)).fetchone()
        if not row:
            raise KeyError(ident)
        return json.loads(row[0])

    def create(self, methods, start, end=None, observed=None, request_id=None, source=None, input_mode=STATIC_MODE):
        if self.sources is not None:
            if source is None:
                source = self.sources.freeze_service()
            else:
                self.sources.get(source['id'], source['revision'])
        if input_mode not in (STATIC_MODE, 'frozen'):
            raise ValueError('不支持的输入模式')
        # Older clients may still submit observed; it must never extend a new task.
        observed = end
        params = dict(input_mode=input_mode, source=source, methods=sorted(set(methods)), start=str(start), end=str(end) if end else None, observed=str(observed) if observed else None, version=VERSION)
        if not params['methods'] or any(m not in METHODS for m in params['methods']):
            raise ValueError('请选择至少一种支持的策略')
        if not date(2000, 1, 1) <= start <= (end or date.today()) <= date.today() or ((end or date.today())-start).days > 3660:
            raise ValueError('要求2000年起、开始≤截止≤今天，区间不超过10年')
        fingerprint = hashlib.sha256(json.dumps([params, request_id or str(date.today())], sort_keys=True).encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT payload FROM jobs WHERE fingerprint=?', (fingerprint,)).fetchone()
            if existing:
                return json.loads(existing[0])
            alias = db.execute('SELECT j.payload FROM job_requests r JOIN jobs j ON j.id=r.job WHERE r.fingerprint=?',
                               (fingerprint,)).fetchone()
            if alias:
                return json.loads(alias[0])
            for row in db.execute('SELECT payload FROM jobs'):
                active = json.loads(row[0])
                if active['status'] in ('queued', 'running', 'retry_wait') and active.get('requested') == params:
                    # Persist accepted retries too: a lost response must still map
                    # to this job after it completes or the server restarts.
                    db.execute('INSERT INTO job_requests VALUES (?,?)', (fingerprint, active['id']))
                    return active
            job = dict(id=uuid.uuid4().hex, **params, requested=params, formula_params=dict(stats.PARAMS), price_field='adj_close', warmup_days=160, generation=0, status='queued', stage='等待读取计划', done=0, total=0,
                       captured=0, execution_seconds=0.0, execution_partial=False, first_started_at=None, auto_retry_count=0, next_retry_at=None, retry_exhausted=False, created_at=now(), updated_at=now(), error=None, summary=[], windows=[], base_samples=0)
            db.execute('INSERT INTO jobs VALUES (?,?,?)', (job['id'], fingerprint, json.dumps(job)))
            return job

    def action(self, ident, action):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self.get(ident, db)
            if action == 'cancel':
                if job['status'] in ('completed', 'cancelled', 'cancelling'):
                    return job
                stop_timing(job)
                job['generation'] += 1
                job.update(next_retry_at=None, work_pending=False)
                job.update(status='cancelling', stage='正在取消，等待在途工作退出；不会提交新结果', cancel_requested_at=now())
            elif action == 'pause' and job['status'] in ('queued', 'running', 'retry_wait'):
                stop_timing(job)
                job['generation'] += 1
                job.update(next_retry_at=None, work_pending=False)
                job['status'], job['stage'] = 'paused', '已暂停；在途工作返回后丢弃未提交部分'
            elif action == 'retry':
                if job['status'] in ('cancelled', 'cancelling'):
                    raise ValueError('已取消或正在取消的任务不能继续，请新建任务')
                if job['status'] in ('failed', 'paused', 'retry_wait'):
                    if job['version'] not in SUPPORTED_VERSIONS:
                        raise ValueError('算法版本已变化，请创建新任务')
                    job['generation'] += 1
                    reset_retry(job)
                    job['work_pending'] = False
                    job.pop('error_code', None)
                    job.pop('error_phase', None)
                    job['status'], job['error'], job['stage'] = 'queued', None, '等待从已提交断点继续'
            self.save(db, job)
        if action == 'cancel':
            self.acknowledge_cancel()
        return self.get(ident)

    def delete(self, ident):
        # IDs are never reused. A retry of a completed deletion needs no worker lock.
        try:
            self.get(ident)
        except KeyError:
            return
        lock = portalocker.Lock(str(self.root / 'worker.lock'), timeout=0,
                                flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
        try:
            lock.acquire()
        except portalocker.exceptions.LockException:
            raise DeleteConflict('后台工作尚未退出，请等待当前工作单元结束后重试删除') from None
        try:
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                try:
                    job = self.get(ident, db)
                except KeyError:
                    return
                if job['status'] not in ('queued', 'paused', 'failed', 'retry_wait', 'completed', 'cancelled'):
                    raise DeleteConflict('运行中或正在取消的任务不能删除，请先取消并等待显示“已取消”')
                # All job-owned data is in these tables, including payload cursors,
                # frozen plans and aggregates. Never unlink the shared DB or lock:
                # unlinking a locked file would permit two different lock inodes.
                for table in ('outcomes', 'windows', 'stocks', 'job_requests'):
                    db.execute(f'DELETE FROM {table} WHERE job=?', (ident,))
                db.execute('DELETE FROM jobs WHERE id=?', (ident,))
        finally:
            lock.release()

    def finish_cancellations(self):
        # Caller holds the OS worker lock: there can be no in-flight work in any worker.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for (payload,) in db.execute('SELECT payload FROM jobs').fetchall():
                job = json.loads(payload)
                if job['status'] == 'cancelling':
                    job.update(status='cancelled', stage='已取消（不能继续；可新建任务）', cancelled_at=now())
                    self.save(db, job)

    def acknowledge_cancel(self):
        lock = portalocker.Lock(str(self.root / 'worker.lock'), timeout=0,
                                flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
        try:
            lock.acquire()
        except portalocker.exceptions.LockException:
            return
        try:
            self.finish_cancellations()
        finally:
            lock.release()

    def start(self):
        self.thread = threading.Thread(target=self.loop, daemon=True, name='short-validation')
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.execution_timer:
            self.execution_timer.request_stop()
        if self.thread:
            self.thread.join(timeout=2)

    def loop(self):
        while not self.stop_event.is_set():
            try:
                worked = self.drain()
            except Exception as exc:
                report(logger, exc, phase='worker')
                # Storage errors must not kill the polling worker or expose private SQL.
                worked = False
            if not worked:
                self.stop_event.wait(2)

    def drain(self):
        # Keep the execution lock across batches, so another worker cannot open
        # a second connection for this same task between its checkpoints.
        lock = portalocker.Lock(str(self.root / 'worker.lock'), timeout=0,
                                flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
        try:
            lock.acquire()
        except portalocker.exceptions.LockException:
            return False
        lease = TaskSource(self, lock)
        self.source_lease = lease
        worked = False
        try:
            while not self.stop_event.is_set():
                if not self.tick(_lease=lease):
                    break
                worked = True
                if lease.key is None:
                    break  # release the global lock between different tasks
            return worked
        finally:
            try:
                lease.close()
            finally:
                self.source_lease = None
                lock.release()

    def tick(self, _lease=None):
        lock = portalocker.Lock(str(self.root / 'worker.lock'), timeout=0,
                                flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
        if _lease is None:
            try:
                lock.acquire()
            except portalocker.exceptions.LockException:
                return False
        timer = None
        try:
            self.finish_cancellations()
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                jobs = [json.loads(r[0]) for r in db.execute('SELECT payload FROM jobs ORDER BY rowid')]
                job = next((j for j in jobs if j['status'] in ('queued', 'running') or
                            (j['status'] == 'retry_wait' and j.get('next_retry_at') and
                             datetime.fromisoformat(j['next_retry_at']) <= datetime.fromisoformat(now()))), None)
                if job is None:
                    return False
                if job.get('work_pending'):
                    interrupt_timing(job)
                    # Holding the OS lock proves the previous work unit no longer
                    # owns execution. A leftover guard can also follow an ordinary
                    # deployment whose shutdown exceeded stop()'s join timeout.
                    # Persist a delayed recovery before touching the source; never
                    # reset its budget merely because a process was replaced.
                    used = job.get('auto_retry_count', 0)
                    waiting = used < AUTO_RETRY_LIMIT
                    job.update(status='retry_wait' if waiting else 'failed',
                               work_pending=False, next_retry_at=retry_time() if waiting else None,
                               auto_retry_allowed=True, retry_exhausted=not waiting,
                               error_code='WORK_INTERRUPTED', error_phase='worker',
                               stage='等待自动重试' if waiting else '自动重试已用尽',
                               error=('上次工作中断，已提交断点保留；将自动重试。' if waiting else
                                      '上次工作中断，已提交断点保留；自动重试5次已用尽，请检查后手动重试。'))
                    self.save(db, job)
                    return True
                if job['status'] == 'retry_wait':
                    job['auto_retry_count'] = job.get('auto_retry_count', 0) + 1
                    job['generation'] += 1
                    job.update(status='running', next_retry_at=None, stage='正在自动重试')
                # Reserve the work unit before touching the source or computing.
                job['work_pending'] = True
                timer = ExecutionTimer(self, job, now())
                self.save(db, job)
            self.execution_timer = timer
            timer.start()
            job['_phase'] = 'source_config'
            try:
                if job['version'] not in SUPPORTED_VERSIONS:
                    raise ValueError('任务算法版本已变化，请创建新任务')
                if job.get('input_mode') not in (None, 'frozen', STATIC_MODE):
                    raise ValueError('任务输入模式不受当前版本支持，请创建新任务')
                needs_source = ('calendar' not in job or
                                (job['done'] < job['total'] if job.get('input_mode') == STATIC_MODE
                                 else job['captured'] < job['total']))
                if self.sources is not None and needs_source and not job.get('source'):
                    raise ValueError('旧任务未绑定不可变数据源，请新建任务；已完成输入冻结的旧任务仍可继续')
                context = (nullcontext(_lease.get(job, needs_source)) if _lease is not None else
                           self.sources.engine(job['source']) if self.sources is not None and needs_source else nullcontext(self.engine))
                with context as source_engine:
                    self.work(job, source_engine)
                with self.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    current = self.get(job['id'], db)
                    if current['generation'] == job['generation']:
                        current['work_pending'] = False
                        if job.get('timings'):
                            current['timings'] = job['timings']
                        self.save(db, current)
            except Exception as exc:
                if self.stop_event.is_set():
                    with self.db() as db:
                        db.execute('BEGIN IMMEDIATE')
                        current = self.get(job['id'], db)
                        if current['generation'] == job['generation']:
                            current['work_pending'] = False
                            self.save(db, current)
                    return False
                info = report(logger, exc, job, job.get('_phase', 'worker'))
                with self.failure_state(job), self.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    current = self.get(job['id'], db)
                    if current['status'] in ('queued', 'running') and current['generation'] == job['generation']:
                        current['timings'] = job.get('timings', {})
                        current['work_pending'] = False
                        permitted = automatic_retry_allowed(exc, info)
                        used = current.get('auto_retry_count', 0)
                        waiting = permitted and used < AUTO_RETRY_LIMIT
                        current.update(status='retry_wait' if waiting else 'failed',
                                       auto_retry_allowed=permitted,
                                       retry_exhausted=permitted and not waiting,
                                       next_retry_at=retry_time() if waiting else None,
                                       error_code=info['code'], error_phase=info['phase'])
                        message = info['message']
                        if waiting:
                            current['stage'] = '等待自动重试'
                            message = message.replace('修复后点击重试。', '将自动重试。')
                        elif permitted:
                            current['stage'] = '自动重试已用尽'
                            message += ' 自动重试5次已用尽，请检查后手动重试。'
                        else:
                            current['stage'] = '失败，等待手动重试'
                            message += ' 此错误不自动重试。'
                        current['error'] = message
                        self.save(db, current)
            return True
        finally:
            try:
                try:
                    if timer is not None:
                        timer.finish()
                finally:
                    self.finish_cancellations()
                    if _lease is not None and _lease.key:
                        current = self.get(_lease.key[0])
                        if (self.stop_event.is_set() or current['status'] not in ('queued', 'running')
                                or current['generation'] != _lease.key[1]):
                            _lease.close()
            finally:
                self.execution_timer = None
                if _lease is None:
                    lock.release()

    @contextmanager
    def failure_state(self, job):
        try:
            yield
        except Exception as exc:
            report(logger, exc, job, 'local_store')
            raise

    def stage(self, ident, label, generation):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self.get(ident, db)
            if job['status'] not in ('queued', 'running') or job['generation'] != generation or self.stop_event.is_set():
                return False
            job['status'], job['stage'] = 'running', label
            job['stage_started_at'] = now()
            self.save(db, job)
        return True

    def work(self, job, source_engine=None):
        source_engine = source_engine if source_engine is not None else self.engine
        ident = job['id']
        if 'calendar' not in job:
            if not self.stage(ident, '读取日历和股票清单（查询超时20秒）', job['generation']):
                return
            job['_phase'] = 'source_schema'
            stats.validate_schema(source_engine)
            if not self.stage(ident, '读取研究日期计划', job['generation']):
                return
            job['_phase'] = 'source_plan'
            plan_options = {'hard_cutoff': True} if job['version'] == VERSION else {}
            start, end, observed, calendar = stats.load_plan(
                source_engine, job['start'], job['end'], job['observed'], **plan_options)
            if not self.stage(ident, '读取合格股票清单', job['generation']):
                return
            job['_phase'] = 'source_universe'
            codes = stats.read_sql(source_engine, '''SELECT DISTINCT code FROM ultra_research_universe
                WHERE trade_date BETWEEN :first AND :last AND eligible=1 AND is_st=0
                AND market_cap>=:cap ORDER BY code LIMIT 20001''',
                dict(first=start.date(), last=end.date(), cap=stats.PARAMS['min_market_cap']))['code'].tolist()
            if len(codes) > 20000:
                raise ValueError('股票数量超过任务上限20000，请检查源数据')
            codes = [c for c in codes if stats.four_stock(c)]
            if not codes:
                raise ValueError('区间内没有合格股票快照，请检查已有采集数据')
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                current = self.get(ident, db)
                if current['status'] != 'running' or current['generation'] != job['generation']:
                    return
                current.update(start=str(start.date()), end=str(end.date()), calendar=[str(d.date()) for d in calendar], codes=codes, total=len(codes),
                               observed=str(observed.date()), stage='计划已保存')
                self.save(db, current)
            return
        calendar = pd.DatetimeIndex(pd.to_datetime(job['calendar']))
        if job.get('input_mode') == STATIC_MODE and job['done'] < job['total']:
            self.stream_batch(job, source_engine, calendar)
            return
        if job.get('input_mode') != STATIC_MODE and job['captured'] < job['total']:
            code = job['codes'][job['captured']]
            if not self.stage(ident, '冻结输入 '+code+'（单股有界读取，超时20秒）', job['generation']):
                return
            job['_phase'] = 'source_read'
            frames = []
            # A stock's two source tables are read in the same transaction.
            with source_engine.connect() as connection, connection.begin():
                for table in ('ultra_research_bar', 'ultra_research_universe'):
                    if not self.stage(ident, '冻结输入 '+code+' / '+table, job['generation']):
                        return
                    extra = "AND asset_type='stock'" if table.endswith('_bar') else ''
                    frame = pd.read_sql_query(text(f'''SELECT * FROM {table} WHERE code=:code
                        AND trade_date BETWEEN :first AND :last {extra} ORDER BY trade_date LIMIT {MAX_ROWS+1}'''),
                        connection, params=dict(code=code, first=calendar[0].date(), last=calendar[-1].date()))
                    if len(frame) > MAX_ROWS:
                        raise ValueError('单股行情超过4000行，请缩短日期区间或检查重复数据')
                    frames.append(frame_json(frame))
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                current = self.get(ident, db)
                if current['status'] != 'running' or current['generation'] != job['generation']:
                    return
                db.execute('INSERT INTO stocks(job,code,bars,universe) VALUES (?,?,?,?)', (ident, code, *frames))
                current['captured'] += 1
                reset_retry(current)
                current['stage'] = '输入已保存 '+code
                self.save(db, current)
            return
        if job['done'] < job['total']:
            code = job['codes'][job['done']]
            if not self.stage(ident, '计算 '+code+'（按原90日窗口；完成后保存断点）', job['generation']):
                return
            with self.db() as db:
                row = db.execute('SELECT bars,universe FROM stocks WHERE job=? AND code=?', (ident, code)).fetchone()
            job['_phase'] = 'compute'
            bars, universe = map(frame_read, row)
            self.compute_stock(job, code, bars, universe, calendar)
            return
        job['_phase'] = 'summarize'
        if not self.stage(ident, '汇总已提交的策略信号', job['generation']):
            return
        if not job['base_samples']:
            raise ValueError('没有可用基础样本；请检查预热、行情与历史快照，不能当作零胜率')
        summary, windows = self.summarize(ident, [METHODS[m] for m in job['methods']])
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.get(ident, db)
            if current['status'] != 'running' or current['generation'] != job['generation']:
                return
            current.update(status='completed', stage='完成', summary=summary, windows=windows,
                           error=None, next_retry_at=None, retry_exhausted=False, auto_retry_allowed=False)
            current.pop('error_code', None)
            current.pop('error_phase', None)
            self.save(db, current)

    @contextmanager
    def timed(self, job, phase):
        started = time.monotonic()
        job['_phase'] = phase
        try:
            yield
        except Exception as exc:
            mark_phase(exc, phase)
            raise
        finally:
            seconds = round(time.monotonic() - started, 3)
            job.setdefault('timings', {})[phase] = seconds
            logger.info('Validation phase job=%s phase=%s seconds=%.3f', job['id'], phase, seconds)

    def stream_batch(self, job, source_engine, calendar):
        codes = job['codes'][job['done']:job['done'] + BATCH_SIZE]
        label = '读取批次 {}–{} / {}（静态源，尚未提交的股票重试时重读）'.format(
            job['done'] + 1, job['done'] + len(codes), job['total'])
        if not self.stage(job['id'], label, job['generation']):
            return
        frames = []
        with self.timed(job, 'connect'):
            connection = self.source_lease.checkout() if self.source_lease else source_engine.connect()
        job['_phase'] = 'source_read'
        with (nullcontext(connection) if self.source_lease else connection), connection.begin():
            if self.source_lease:
                # Check an idle socket without implicit reconnect or replay. A
                # failed ping follows the ordinary persisted retry budget.
                with self.timed(job, 'connect'):
                    connection.exec_driver_sql('SELECT 1')
            for table, columns, extra in (
                ('ultra_research_bar', BAR_COLUMNS, "AND asset_type='stock'"),
                ('ultra_research_universe', UNIVERSE_COLUMNS, '')):
                if not self.stage(job['id'], label + ' / ' + table, job['generation']):
                    return
                # A hard aggregate limit bounds buffering even if the source schema is malformed.
                limit = len(codes) * MAX_ROWS + 1
                query = text(f"SELECT {columns} FROM {table} WHERE code IN :codes "
                             f"AND trade_date BETWEEN :first AND :last {extra} "
                             f"ORDER BY code,trade_date LIMIT {limit}").bindparams(bindparam('codes', expanding=True))
                with self.timed(job, table):
                    frame = pd.read_sql_query(query, connection, params=dict(
                        codes=codes, first=calendar[0].date(), last=calendar[-1].date()))
                if len(frame) >= limit or (not frame.empty and frame.groupby('code').size().max() > MAX_ROWS):
                    raise ValueError('单股行情超过4000行，请缩短日期区间或检查重复数据')
                frames.append(frame)
        # End the read transaction before CPU work; the task lease retains its
        # physical connection across batches. No raw input is persisted.
        job['_phase'] = 'compute'
        groups = [dict(tuple(frame.groupby('code', sort=False))) for frame in frames]
        for code in codes:
            if not self.stage(job['id'], '计算股票 ' + code + '（静态批次；逐股提交断点）', job['generation']):
                return
            bars, universe = [group.get(code, frame.iloc[:0]) for group, frame in zip(groups, frames)]
            if not self.compute_stock(job, code, bars, universe, calendar):
                return

    def compute_stock(self, job, code, bars, universe, calendar):
        ident = job['id']
        with self.timed(job, 'compute'):
            selected = [METHODS[m] for m in job['methods']]
            start, end = pd.Timestamp(job['start']), pd.Timestamp(job['end'])
            samples, quality = stats.stock_samples(code, bars, universe, calendar, start, end, stats.PARAMS, selected=selected)
            three = [m for m in job['methods'] if m != 'oversold']
            if three:
                def activity(day):
                    if not self.stage(ident, '计算 '+code+' / '+str(day.date()), job['generation']):
                        raise InterruptedError('任务已暂停')
                samples = stats.add_three_candidates(code, samples, bars, universe, calendar, start, end, stats.PARAMS,
                                                     stats.make_three_selector(three), activity=activity)
            pieces = []
            for method in selected:
                part = samples.loc[samples[method]].copy()
                part['strategy'] = method
                pieces.append(part)
            details = pd.concat(pieces, ignore_index=True)
        with self.timed(job, 'commit'), self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.get(ident, db)
            if current['status'] != 'running' or current['generation'] != job['generation'] or self.stop_event.is_set():
                return
            if job.get('input_mode') == STATIC_MODE:
                db.execute('INSERT INTO stocks(job,code) VALUES (?,?)', (ident, code))
            db.execute('UPDATE stocks SET details=?,coverage=? WHERE job=? AND code=?',
                       (frame_json(details), json.dumps(quality), ident, code))
            for sample in details.to_dict('records'):
                day, strategy = str(sample['signal_date']), sample['strategy']
                values = []
                for h in (1, 2, 3):
                    value = sample[f'd{h}_return_pct']
                    value = float(value) if pd.notna(value) and np.isfinite(value) else None
                    status = sample[f'd{h}_status']
                    db.execute('INSERT INTO outcomes VALUES (?,?,?,?,?,?,?)',
                               (ident, code, day, strategy, h, value, status))
                    values.append(value if status == '有效' else None)
                complete = all(v is not None for v in values)
                db.execute('INSERT INTO windows VALUES (?,?,?,?,?,?,?)',
                    (ident, code, day, strategy, max(values) if complete else None,
                     min(values) if complete else None, int(max(values)>0) if complete else None))
            if current['done'] != job['codes'].index(code):
                raise ValueError('任务断点冲突，请重试')
            current['timings'] = dict(job.get('timings', {}))
            current['done'] += 1
            reset_retry(current)
            current['base_samples'] += quality['base_samples']
            current['stage'] = '计算断点已保存 '+code
            self.save(db, current)
        return True

    def summarize(self, ident, methods):
        """SQLite performs aggregation and median sorting on disk; no all-stock concat."""
        summary, windows = [], []
        with self.db() as db:
            for method in methods:
                for h in (1, 2, 3):
                    where = "job=? AND strategy=? AND horizon=?"
                    args = (ident, method, h)
                    total, stocks, pending, paused, missing = db.execute(f"""SELECT count(*),count(DISTINCT code),
                        coalesce(sum(status='待观察'),0),coalesce(sum(status='停牌'),0),coalesce(sum(status='缺少行情'),0)
                        FROM outcomes WHERE {where}""", args).fetchone()
                    valid = where + " AND status='有效' AND value IS NOT NULL"
                    count, wins, flat, losses, mean, low, high = db.execute(f"""SELECT count(*),
                        coalesce(sum(value>0),0),coalesce(sum(value=0),0),coalesce(sum(value<0),0),avg(value),min(value),max(value)
                        FROM outcomes WHERE {valid}""", args).fetchone()
                    median = db.execute(f"""SELECT avg(value) FROM (SELECT value FROM outcomes WHERE {valid}
                        ORDER BY value LIMIT ? OFFSET ?)""", (*args, 2 if count%2==0 else 1, max(0,(count-1)//2))).fetchone()[0]
                    summary.append({'策略':method,'后续交易日':h,'触发样本':total,'股票数':stocks,'有效样本':count,
                        '上涨次数':wins,'持平次数':flat,'下跌次数':losses,'胜率(%)':wins/count*100 if count else None,
                        '平均收益(%)':mean,'中位收益(%)':median,'最差收益(%)':low,'最好收益(%)':high,
                        '待观察':pending,'其余不可统计':total-count-pending,'停牌样本':paused,'行情缺失':missing})
                total, count, win, best, worst = db.execute("""SELECT count(*),count(win),avg(win)*100,avg(best),avg(worst)
                    FROM windows WHERE job=? AND strategy=?""", (ident,method)).fetchone()
                windows.append({'策略':method,'触发样本':total,'完整三日样本':count,
                    '三日内任一收盘上涨率(%)':win,'三日内最好收盘收益均值(%)':best,'三日内最差收盘收益均值(%)':worst})
        return summary, windows
