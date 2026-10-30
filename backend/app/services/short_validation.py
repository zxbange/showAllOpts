"""Durable, single-writer research jobs. Source DB is SELECT-only.

The OS lock spans a work unit; SQLite atomically commits results and cursor.
All workers may poll, but only one can perform a unit, including after restart.
Inputs are frozen before calculation. A retry never reloads a committed input.
"""
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timezone
import hashlib
import json
import logging
import sqlite3
import threading
import uuid

import pandas as pd
import numpy as np
import portalocker
from sqlalchemy import text

from app.services import short_statistics as stats

logger = logging.getLogger(__name__)
VERSION = 'four-close-v1'
METHODS = {'bukou': '补票龙', 'redhover': '红悬停龙', 'xuli': '蓄力龙', 'oversold': '超跌后止跌反弹'}
MAX_ROWS = 4000


class DeleteConflict(ValueError):
    """A safe, user-facing explanation for a deletion that cannot run yet."""


def now():
    return datetime.now(timezone.utc).isoformat()


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
        self.stop_event = threading.Event()
        self.thread = None
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, payload TEXT NOT NULL);
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
        db = sqlite3.connect(self.root / 'jobs.sqlite3', timeout=15)
        try:
            with db:
                yield db
        finally:
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

    def create(self, methods, start, end, observed, request_id=None, source=None):
        if self.sources is not None:
            if source is None:
                source = self.sources.freeze_service()
            else:
                self.sources.get(source['id'], source['revision'])
        params = dict(source=source, methods=sorted(set(methods)), start=str(start), end=str(end) if end else None, observed=str(observed) if observed else None, version=VERSION)
        if not params['methods'] or any(m not in METHODS for m in params['methods']):
            raise ValueError('请选择至少一种支持的策略')
        if not date(2000, 1, 1) <= start <= (end or observed or date.today()) <= (observed or date.today()) <= date.today() or ((observed or date.today())-start).days > 3660:
            raise ValueError('要求2000年起、开始≤信号截止≤观察截止≤今天，区间不超过10年')
        fingerprint = hashlib.sha256(json.dumps([params, request_id or str(date.today())], sort_keys=True).encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT payload FROM jobs WHERE fingerprint=?', (fingerprint,)).fetchone()
            if existing:
                return json.loads(existing[0])
            for row in db.execute('SELECT payload FROM jobs'):
                active = json.loads(row[0])
                if active['status'] in ('queued', 'running') and active.get('requested') == params:
                    return active
            job = dict(id=uuid.uuid4().hex, **params, requested=params, formula_params=dict(stats.PARAMS), price_field='adj_close', warmup_days=160, generation=0, status='queued', stage='等待读取计划', done=0, total=0,
                       captured=0, created_at=now(), updated_at=now(), error=None, summary=[], windows=[], base_samples=0)
            db.execute('INSERT INTO jobs VALUES (?,?,?)', (job['id'], fingerprint, json.dumps(job)))
            return job

    def action(self, ident, action):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self.get(ident, db)
            if action == 'cancel':
                if job['status'] in ('completed', 'cancelled', 'cancelling'):
                    return job
                job['generation'] += 1
                job.update(status='cancelling', stage='正在取消，等待在途工作退出；不会提交新结果', cancel_requested_at=now())
            elif action == 'pause' and job['status'] in ('queued', 'running'):
                job['generation'] += 1
                job['status'], job['stage'] = 'paused', '已暂停；在途工作返回后丢弃未提交部分'
            elif action == 'retry':
                if job['status'] in ('cancelled', 'cancelling'):
                    raise ValueError('已取消或正在取消的任务不能继续，请新建任务')
                if job['status'] in ('failed', 'paused'):
                    if job['version'] != VERSION:
                        raise ValueError('算法版本已变化，请创建新任务')
                    job['generation'] += 1
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
                if job['status'] not in ('queued', 'paused', 'failed', 'completed', 'cancelled'):
                    raise DeleteConflict('运行中或正在取消的任务不能删除，请先取消并等待显示“已取消”')
                # All job-owned data is in these tables, including payload cursors,
                # frozen plans and aggregates. Never unlink the shared DB or lock:
                # unlinking a locked file would permit two different lock inodes.
                for table in ('outcomes', 'windows', 'stocks'):
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
        if self.thread:
            self.thread.join(timeout=2)

    def loop(self):
        while not self.stop_event.is_set():
            try:
                worked = self.tick()
            except Exception as exc:
                logger.error('Validation worker storage failure: %s', type(exc).__name__)
                # Storage errors must not kill the polling worker or expose private SQL.
                worked = False
            if not worked:
                self.stop_event.wait(2)

    def tick(self):
        lock = portalocker.Lock(str(self.root / 'worker.lock'), timeout=0,
                                flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
        try:
            lock.acquire()
        except portalocker.exceptions.LockException:
            return False
        try:
            self.finish_cancellations()
            with self.db() as db:
                jobs = [json.loads(r[0]) for r in db.execute('SELECT payload FROM jobs ORDER BY rowid')]
            job = next((j for j in jobs if j['status'] in ('queued', 'running')), None)
            if job is None:
                return False
            try:
                if job['version'] != VERSION:
                    raise ValueError('任务算法版本已变化，请创建新任务')
                needs_source = 'calendar' not in job or job['captured'] < job['total']
                if self.sources is not None and needs_source and not job.get('source'):
                    raise ValueError('旧任务未绑定不可变数据源，请新建任务；已完成输入冻结的旧任务仍可继续')
                context = self.sources.engine(job['source']) if self.sources is not None and needs_source else nullcontext(self.engine)
                with context as source_engine:
                    self.work(job, source_engine)
            except Exception as exc:
                if self.stop_event.is_set():
                    return False
                logger.warning('Validation job failed id=%s type=%s', job['id'], type(exc).__name__)
                with self.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    current = self.get(job['id'], db)
                    if current['status'] in ('queued', 'running') and current['generation'] == job['generation']:
                        current['status'] = 'failed'
                        # Only our data validation messages are exposed; never driver messages.
                        current['error'] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__ + '：读取或计算失败，请检查数据/连接后重试'
                        self.save(db, current)
            return True
        finally:
            try:
                self.finish_cancellations()
            finally:
                lock.release()

    def stage(self, ident, label, generation):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self.get(ident, db)
            if job['status'] not in ('queued', 'running') or job['generation'] != generation or self.stop_event.is_set():
                return False
            job['status'], job['stage'] = 'running', label
            self.save(db, job)
        return True

    def work(self, job, source_engine=None):
        source_engine = source_engine if source_engine is not None else self.engine
        ident = job['id']
        if 'calendar' not in job:
            if not self.stage(ident, '读取日历和股票清单（查询超时20秒）', job['generation']):
                return
            stats.validate_schema(source_engine)
            if not self.stage(ident, '读取研究日期计划', job['generation']):
                return
            start, end, observed, calendar = stats.load_plan(source_engine, job['start'], job['end'], job['observed'])
            if not self.stage(ident, '读取合格股票清单', job['generation']):
                return
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
        if job['captured'] < job['total']:
            code = job['codes'][job['captured']]
            if not self.stage(ident, '冻结输入 '+code+'（单股有界读取，超时20秒）', job['generation']):
                return
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
                current['stage'] = '输入已保存 '+code
                self.save(db, current)
            return
        if job['done'] < job['total']:
            code = job['codes'][job['done']]
            if not self.stage(ident, '计算 '+code+'（按原90日窗口；完成后保存断点）', job['generation']):
                return
            with self.db() as db:
                row = db.execute('SELECT bars,universe FROM stocks WHERE job=? AND code=?', (ident, code)).fetchone()
            bars, universe = map(frame_read, row)
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
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                current = self.get(ident, db)
                if current['status'] != 'running' or current['generation'] != job['generation'] or self.stop_event.is_set():
                    return
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
                current['done'] += 1
                current['base_samples'] += quality['base_samples']
                current['stage'] = '计算断点已保存 '+code
                self.save(db, current)
            return
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
            current.update(status='completed', stage='完成', summary=summary, windows=windows)
            self.save(db, current)

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
