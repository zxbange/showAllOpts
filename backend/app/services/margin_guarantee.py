"""中证两融担保比例：独立本地存储、增量补采、北京时间20点调度。"""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import logging
import math
import sqlite3
import ssl
import threading
import uuid

import httpx
import portalocker

from app.services.margin_source import (
    MarginReport, MarginSourceError, fetch_point, list_reports, safe_error_details, safe_report_url,
)

LOG = logging.getLogger(__name__)
CHINA = timezone(timedelta(hours=8))
DAILY_TIME = '20:00'


def now():
    return datetime.now(CHINA)


def six_years_ago(day):
    try:
        return day.replace(year=day.year - 6)
    except ValueError:
        return day.replace(year=day.year - 6, day=28)


def latest_slot(moment):
    moment = moment.astimezone(CHINA)
    slot = moment.replace(hour=20, minute=0, second=0, microsecond=0)
    return slot if moment >= slot else slot - timedelta(days=1)


class CollectionCancelled(Exception):
    pass


def failure_summary(failure):
    label = {'catalog': '披露目录', 'report': '日报', 'collection': '采集任务'}[failure['scope']]
    day = ' ' + failure['date'] if failure.get('date') else ''
    kind = failure.get('cause_type', failure['error_type'])
    return f"{label}{day}：{failure['reason']} [{failure['code']}/{kind}]"


class MarginGuarantee:
    def __init__(self, settings, *, client_factory=None, delay=.35):
        self.settings = settings
        self.root = settings.log_path / 'market-data' / 'margin-guarantee'
        self.root.mkdir(parents=True, exist_ok=True)
        self.client_factory = client_factory or (lambda: httpx.Client(
            timeout=httpx.Timeout(25, connect=10), verify=ssl.create_default_context(),
            trust_env=False, follow_redirects=False,
            headers={'User-Agent': 'ShowAllOpts/1.0 (market research)'}))
        self.delay = delay
        self.stop_event = threading.Event()
        self.scheduler = self.worker = None
        self.schedule_floor = None
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS points (
                    trade_date TEXT PRIMARY KEY, ratio REAL NOT NULL CHECK(ratio>0),
                    source_url TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reports (
                    trade_date TEXT PRIMARY KEY, source_url TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS state (
                    id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
                INSERT OR IGNORE INTO state VALUES (1, '{}');
            ''')

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.root / 'history.sqlite3', timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def read_state(self, db=None):
        if db is None:
            with self.db() as connection:
                return self.read_state(connection)
        return json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])

    def write_state(self, state, db=None):
        if db is None:
            with self.db() as connection:
                self.write_state(state, connection)
            return
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

    def cancel(self):
        if not self.running():
            return False
        identifier = self.read_state().get('run_id', '')
        if len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
            return False
        (self.root / ('cancel-' + identifier)).touch()
        return True

    def status(self, moment=None):
        moment = moment or now()
        state = self.read_state()
        running = self.running()
        stage = state.get('stage', '尚未采集')
        if not running and state.get('active'):
            stage = '上次采集中断，再次更新将继续补采'
        next_run = latest_slot(moment) + timedelta(days=1)
        with self.db() as db:
            pending = db.execute('SELECT count(*) FROM reports r LEFT JOIN points p '
                                 'ON p.trade_date=r.trade_date WHERE p.trade_date IS NULL').fetchone()[0]
        return dict(running=running, stage=stage, processed=state.get('processed', 0),
                    total=state.get('total'), percent=state.get('percent'),
                    cancelling=bool(running and state.get('run_id') and
                                    (self.root / ('cancel-' + state['run_id'])).exists()),
                    last_attempt_at=state.get('last_attempt_at'), last_success_at=state.get('last_success_at'),
                    error=state.get('error'), pending_reports=pending,
                    recent_failures=state.get('recent_failures', []), last_failure=state.get('last_failure'),
                    daily_time=DAILY_TIME, timezone='Asia/Shanghai',
                    next_run_at=next_run.isoformat() if self.settings.margin_worker_enabled else None)

    def history(self, moment=None):
        moment = moment or now()
        day = moment.astimezone(CHINA).date()
        with self.db() as db:
            rows = db.execute('SELECT trade_date AS date,ratio,source_url,updated_at FROM points '
                              'WHERE trade_date>=? AND trade_date<=? ORDER BY trade_date',
                              (six_years_ago(day).isoformat(), day.isoformat())).fetchall()
        points = [dict(row) for row in rows]
        latest = points[-1] if points else None
        low = min(points, key=lambda row: row['ratio']) if points else None
        peak = max(points, key=lambda row: row['ratio']) if points else None
        below = sum(row['ratio'] < latest['ratio'] for row in points) if points else 0
        return dict(start_date=points[0]['date'] if points else None,
                    end_date=latest['date'] if latest else None, latest=latest,
                    summary=dict(observed_days=len(points), below_count=below,
                                 previous_date=points[-2]['date'] if len(points)>1 else None,
                                 change_pp=latest['ratio']-points[-2]['ratio'] if len(points)>1 else None,
                                 percentile=100*below/len(points) if points else None,
                                 distance_pp=latest['ratio']-240 if points else None,
                                 low_ratio=low['ratio'] if low else None, low_date=low['date'] if low else None,
                                 peak_ratio=peak['ratio'] if peak else None, peak_date=peak['date'] if peak else None),
                    points=points, status=self.status(moment))

    def refresh(self, background=True, *, moment=None, slot=None):
        moment = moment or now()
        if self.stop_event.is_set():
            return False
        lock = self.acquire()
        if lock is None:
            return False
        try:
            state = self.read_state()
            if slot and state.get('scheduled_slot') == slot:
                lock.release()
                return False
            state.update(run_id=uuid.uuid4().hex, active=True, error=None, stage='检查中证官方披露目录',
                         processed=0, total=None, percent=None, last_attempt_at=moment.isoformat(),
                         recent_failures=[], last_failure=None)
            if slot:
                state['scheduled_slot'] = slot
            self.write_state(state)
        except BaseException:
            lock.release()
            raise
        if background:
            self.worker = threading.Thread(target=self._collect, args=(lock, state, moment),
                                           name='margin-guarantee-collector', daemon=True)
            try:
                self.worker.start()
            except BaseException:
                state.update(active=False, error='未能启动采集任务，请重试。', stage='启动失败')
                self.write_state(state)
                lock.release()
                raise
        else:
            self._collect(lock, state, moment)
        return True

    def _collect(self, lock, state, moment):
        marker = self.root / ('cancel-' + state['run_id'])
        def check_cancel():
            if self.stop_event.is_set() or marker.exists():
                raise CollectionCancelled()
        today = moment.astimezone(CHINA).date()
        cutoff = six_years_ago(today)
        failures = consecutive_failures = 0
        scope = 'catalog'
        stopped_after_failures = False
        LOG.info('Margin guarantee collection started date=%s', today)
        try:
            with self.db() as db:
                latest_report = db.execute('SELECT max(trade_date) FROM reports').fetchone()[0]
            start = (max(cutoff, date.fromisoformat(latest_report)-timedelta(days=7))
                     if state.get('catalog_ready') and latest_report else cutoff)
            with self.client_factory() as client:
                check_cancel()
                reports = list_reports(client, start, today, check_cancel=check_cancel)
                check_cancel()
                with self.db() as db:
                    for report in reports:
                        if not start <= report.trade_date <= today:
                            raise MarginSourceError('中证日报日期超出请求范围。')
                        db.execute('INSERT INTO reports(trade_date,source_url) VALUES (?,?) '
                                   'ON CONFLICT(trade_date) DO UPDATE SET source_url=excluded.source_url',
                                   (report.trade_date.isoformat(), report.url))
                    db.execute('DELETE FROM points WHERE trade_date<?', (cutoff.isoformat(),))
                    db.execute('DELETE FROM reports WHERE trade_date<?', (cutoff.isoformat(),))
                    pending = db.execute('SELECT r.trade_date,r.source_url FROM reports r LEFT JOIN points p '
                                         'ON p.trade_date=r.trade_date WHERE p.trade_date IS NULL AND r.trade_date<=? '
                                         'ORDER BY r.trade_date DESC', (today.isoformat(),)).fetchall()
                scope = 'collection'
                state.update(catalog_ready=True, total=len(pending), percent=0 if pending else 100)
                self.write_state(state)
                for index, row in enumerate(pending):
                    check_cancel()
                    state['stage'] = '读取中证日报 ' + row['trade_date']
                    self.write_state(state)
                    try:
                        report = MarginReport(date.fromisoformat(row['trade_date']), row['source_url'])
                        point = fetch_point(client, report)
                        check_cancel()
                        if str(point['date']) != row['trade_date']:
                            raise MarginSourceError('中证日报日期与待采日期不一致。')
                        try:
                            ratio = float(point['ratio'])
                        except (TypeError, ValueError):
                            raise MarginSourceError('中证日报比例不是有效数字或超出合理范围。') from None
                        if not math.isfinite(ratio) or not 0 < ratio < 10000:
                            raise MarginSourceError('中证日报比例不是有效数字或超出合理范围。')
                        # Commit each verified day immediately; a failed later request cannot lose it.
                        with self.db() as db:
                            db.execute('INSERT OR IGNORE INTO points VALUES (?,?,?,?)',
                                       (row['trade_date'], ratio, report.url, now().isoformat()))
                        consecutive_failures = 0
                    except CollectionCancelled:
                        raise
                    except Exception as exc:
                        failures += 1
                        consecutive_failures += 1
                        failure = dict(scope='report', date=row['trade_date'],
                                       source_url=safe_report_url(row['source_url']), **safe_error_details(exc))
                        state['recent_failures'] = (state['recent_failures'] + [failure])[-3:]
                        state['last_failure'] = failure
                        LOG.warning('Margin report failed details=%s', failure)
                    state.update(processed=index+1, percent=round(100*(index+1)/len(pending), 1))
                    self.write_state(state)
                    if consecutive_failures >= 3:
                        stopped_after_failures = True
                        raise RuntimeError('Three consecutive official reports failed')
                    if self.delay:
                        self.stop_event.wait(self.delay)
                check_cancel()
            if failures:
                state.update(stage='部分完成，失败日期下次补采', error=f'{failures} 份日报未成功读取，已保存其余数据。最后失败：'
                                   + failure_summary(state['last_failure']))
            else:
                state.update(stage='更新完成' if pending else '没有新增披露', error=None,
                             last_success_at=now().isoformat(), percent=100)
            LOG.info('Margin guarantee collection complete processed=%s failed=%s', state['processed'], failures)
        except CollectionCancelled:
            state.update(stage='已停止，已保存数据可继续补采', error=None)
            LOG.info('Margin guarantee collection cancelled processed=%s', state['processed'])
        except Exception as exc:
            if not stopped_after_failures:
                state['last_failure'] = dict(scope=scope, **safe_error_details(exc))
            prefix = ('连续三份日报读取失败，已停止本轮。' if stopped_after_failures
                      else '中证数据读取或校验失败。')
            state.update(stage='更新失败，已保存的数据保留',
                         error=prefix + failure_summary(state['last_failure'])
                               + ' 请稍后重试；再次更新会继续补采。')
            # Never log traceback/exception text: upstream errors can contain secrets or bodies.
            LOG.error('Margin guarantee collection failed details=%s', state['last_failure'])
        finally:
            state['active'] = False
            try:
                self.write_state(state)
                marker.unlink(missing_ok=True)
            finally:
                lock.release()

    def tick(self, moment=None):
        moment = moment or now()
        slot = latest_slot(moment).isoformat()
        if self.schedule_floor is None:
            self.schedule_floor = slot
        if slot > self.schedule_floor:
            self.refresh(background=False, moment=moment, slot=slot)

    def start(self):
        if not self.settings.margin_worker_enabled:
            return
        # Startup only installs the scheduler. It never starts a historical download.
        self.schedule_floor = latest_slot(now()).isoformat()
        def loop():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    LOG.error('Margin scheduler failed error_type=%s', type(exc).__name__)
                self.stop_event.wait(30)
        self.scheduler = threading.Thread(target=loop, name='margin-guarantee-scheduler', daemon=True)
        self.scheduler.start()

    def stop(self):
        self.stop_event.set()
        if self.scheduler:
            self.scheduler.join()
        if self.worker:
            self.worker.join()
