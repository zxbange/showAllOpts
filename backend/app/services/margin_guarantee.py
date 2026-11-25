"""两融担保比例：东方财富接口、独立本地存储、北京时间08点调度。"""

import json
import logging
import math
import sqlite3
import ssl
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from functools import partial
from time import monotonic

import httpx
import portalocker

from app.core.diagnostics import diagnostic_details
from app.services.collection_calendar import (
    automatic_collection_allowed,
    latest_due_slot,
    manual_failure_isolated,
    next_automatic_collection,
)
from app.services.collection_errors import safe_error
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
from app.services.holdings_calendar import trading_day
from app.services.margin_eastmoney import API_URL, RatioReport, fetch_point, list_reports
from app.services.margin_source import (
    TITLE_DATE_CORRECTION,
    MarginReport,
    MarginSourceError,
    safe_error_details,
    safe_report_url,
)
from app.services.system_configuration import latest_task_slot, task_config
from app.services.system_runtime import source_client

LOG = logging.getLogger(__name__)
CHINA = timezone(timedelta(hours=8))
DAILY_TIME = '08:00'


def now():
    return datetime.now(CHINA)


def six_years_ago(day):
    try:
        return day.replace(year=day.year - 6)
    except ValueError:
        return day.replace(year=day.year - 6, day=28)


def latest_slot(moment):
    moment = moment.astimezone(CHINA)
    slot = moment.replace(hour=8, minute=0, second=0, microsecond=0)
    return slot if moment >= slot else slot - timedelta(days=1)


def _manual_statistics_limit(settings, moment):
    times = task_config(settings, 'margin')['times']
    slot = latest_due_slot(moment, times, is_open=trading_day)
    if slot is None:
        return None
    # Eastmoney exposes a statistics date, not a verifiable publication
    # timestamp. Do not admit same-session observations at any clock time.
    before_day = slot.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(microseconds=1)
    previous = latest_due_slot(before_day, times, is_open=trading_day)
    return previous.date() if previous else None


class CollectionCancelled(Exception):
    pass


def failure_summary(failure):
    label = {'catalog': '数据接口', 'report': '日数据', 'collection': '采集任务'}[failure['scope']]
    day = ' ' + failure['date'] if failure.get('date') else ''
    kind = failure.get('cause_type', failure['error_type'])
    return f"{label}{day}：{failure['reason']} [{failure['code']}/{kind}]"


class MarginGuarantee:
    def __init__(self, settings, *, client_factory=None, delay=.35):
        self.settings = settings
        self.root = settings.log_path / 'market-data' / 'margin-guarantee'
        self.root.mkdir(parents=True, exist_ok=True)
        self.client_factory = client_factory or (lambda: source_client(self.settings, factory=httpx.Client,
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
                CREATE TABLE IF NOT EXISTS report_resolutions (
                    report_date TEXT PRIMARY KEY, source_url TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL, observation_date TEXT NOT NULL,
                    ratio REAL NOT NULL, reason TEXT NOT NULL, resolved_at TEXT NOT NULL);
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
        return try_lease(lock, portalocker.exceptions.LockException)

    def running(self):
        return is_running(self.acquire)

    def cancel(self):
        if not self.running():
            return False
        return request_cancel(self.root, self.read_state().get('run_id'))

    def status(self, moment=None):
        moment = moment or now()
        state = self.read_state()
        running = self.running()
        stage = state.get('stage', '尚未采集')
        if not running and state.get('active'):
            stage = '上次采集中断，再次更新将继续补采'
        next_run = next_automatic_collection(latest_task_slot(self.settings, 'margin', moment) + timedelta(days=1))
        with self.db() as db:
            pending = db.execute('SELECT count(*) FROM reports r LEFT JOIN points p '
                                 'ON p.trade_date=r.trade_date WHERE p.trade_date IS NULL '
                                 + self.unresolved_sql()).fetchone()[0]
            corrections = [dict(row) for row in db.execute(
                'SELECT s.* FROM report_resolutions s JOIN reports r '
                'ON r.trade_date=s.report_date AND r.source_url=s.source_url '
                'JOIN points p ON p.trade_date=s.observation_date AND p.ratio=s.ratio '
                'ORDER BY s.report_date')]
        return dict(running=running, stage=stage, processed=state.get('processed', 0),
                    total=state.get('total'), percent=state.get('percent'),
                    cancelling=bool(running and state.get('run_id') and
                                    (self.root / ('cancel-' + state['run_id'])).exists()),
                    last_attempt_at=state.get('last_attempt_at'), last_success_at=state.get('last_success_at'),
                    error=state.get('error'), pending_reports=pending,
                    target_date=state.get('target_date'), source_corrections=corrections,
                    recent_failures=state.get('recent_failures', []), last_failure=state.get('last_failure'),
                    daily_time=task_config(self.settings, 'margin')['times'][0], timezone='Asia/Shanghai',
                    next_run_at=next_run.isoformat() if next_run and task_config(self.settings, 'margin')['enabled'] else None)

    @staticmethod
    def unresolved_sql():
        # A receipt only resolves its original URL while the verified observation
        # still exists. Changed catalog URLs and missing observations are retried.
        return ('AND NOT EXISTS (SELECT 1 FROM report_resolutions s JOIN points actual '
                'ON actual.trade_date=s.observation_date AND actual.ratio=s.ratio '
                'WHERE s.report_date=r.trade_date AND s.source_url=r.source_url) ')

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
        if slot is not None and not automatic_collection_allowed(moment):
            return False
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
            data_end = (moment.astimezone(CHINA).date() if slot is not None
                        else _manual_statistics_limit(self.settings, moment))
            state.update(run_id=uuid.uuid4().hex, active=True, error=None, stage='读取东方财富两融数据',
                         target_date=data_end.isoformat() if data_end is not None else None,
                         processed=0, total=None, percent=None, last_attempt_at=moment.isoformat(),
                         recent_failures=[], last_failure=None, current_scheduled_slot=slot)
            if slot:
                state['scheduled_slot'] = slot
            self.write_state(state)
        except BaseException as exc:
            LOG.error('Margin collection initialization failed details=%s', diagnostic_details(exc))
            lock.release()
            raise
        if background:
            try:
                start_worker(partial(self._collect, lock, state, moment), name='margin-guarantee-collector',
                             assign=lambda worker: setattr(self, 'worker', worker), factory=threading.Thread)
            except BaseException as exc:
                LOG.error('Margin collection launch failed details=%s', diagnostic_details(exc))
                state.update(active=False, error='未能启动采集任务，请重试。', stage='启动失败')
                try:
                    self.write_state(state)
                finally:
                    lock.release()
                raise
        else:
            self._collect(lock, state, moment)
        return True

    @notify_collection('margin')
    def _collect(self, lock, state, moment):
        outcome = 'failed'
        run_error = None
        marker = cancellation_marker(self.root, state['run_id'])
        assert marker is not None
        slot = state.get('current_scheduled_slot')
        context = RunContext(
            RunRequest(moment, slot, 'scheduled' if slot is not None else 'manual'), marker,
            self.stop_event, automatic_collection_allowed, manual_failure_isolated,
            CollectionCancelled, monotonic,
        )
        check_cancel = context.check_cancel
        today = moment.astimezone(CHINA).date()
        cutoff = six_years_ago(today)
        target = date.fromisoformat(state['target_date']) if state.get('target_date') else None
        failures = consecutive_failures = 0
        isolate = state.get('current_scheduled_slot') is None and manual_failure_isolated(moment)
        existing_reports = None
        scope = 'catalog'
        stopped_after_failures = False
        LOG.info('Margin guarantee collection started date=%s target=%s', today, target or '-')
        try:
            if target is None or target < cutoff:
                state.update(stage='更新暂停，已保存的数据保留', error='无法核验有效采集时点及其前一交易日，本次未请求数据。')
                run_error = safe_error(stage='validate', code='invalid_data')
                return
            with self.db() as db:
                existing_reports = {row[0] for row in db.execute('SELECT trade_date FROM reports')}
            with self.db() as db:
                latest_report = db.execute('SELECT max(trade_date) FROM reports WHERE trade_date<=?',
                                           (target.isoformat(),)).fetchone()[0]
                pending_api = db.execute(
                    'SELECT min(r.trade_date) FROM reports r LEFT JOIN points p '
                    'ON p.trade_date=r.trade_date WHERE p.trade_date IS NULL AND r.source_url LIKE ? AND r.trade_date<=?',
                    (API_URL + '?%', target.isoformat())).fetchone()[0]
            start = (max(cutoff, date.fromisoformat(latest_report)-timedelta(days=7))
                     if state.get('eastmoney_ready') and latest_report else cutoff)
            if pending_api:
                start = max(cutoff, min(start, date.fromisoformat(pending_api)))
            with self.client_factory() as client:
                check_cancel()
                reports = list_reports(client, start, target, check_cancel=check_cancel)
                fetched = {(str(report.trade_date), report.url): report for report in reports}
                check_cancel()
                with self.db() as db:
                    for report in reports:
                        if not start <= report.trade_date <= target:
                            raise MarginSourceError('中证日报日期超出请求范围。')
                        db.execute('INSERT INTO reports(trade_date,source_url) VALUES (?,?) '
                                   'ON CONFLICT(trade_date) DO UPDATE SET source_url=excluded.source_url',
                                   (report.trade_date.isoformat(), report.url))
                    db.execute('DELETE FROM points WHERE trade_date<?', (cutoff.isoformat(),))
                    db.execute('DELETE FROM reports WHERE trade_date<?', (cutoff.isoformat(),))
                    db.execute('DELETE FROM report_resolutions WHERE observation_date<?', (cutoff.isoformat(),))
                    pending = db.execute('SELECT r.trade_date,r.source_url FROM reports r LEFT JOIN points p '
                                         'ON p.trade_date=r.trade_date WHERE p.trade_date IS NULL AND r.trade_date<=? '
                                         + self.unresolved_sql() +
                                         'ORDER BY r.trade_date DESC', (target.isoformat(),)).fetchall()
                LOG.info('Margin catalog complete reports=%s pending=%s', len(reports), len(pending))
                scope = 'collection'
                # If a correctly titled report also exists, verify it first so
                # an inconsistent correction remains pending and visibly fails.
                if any(row['trade_date'] == TITLE_DATE_CORRECTION['observation_date'] for row in pending):
                    pending.sort(key=lambda row: (row['trade_date'] == TITLE_DATE_CORRECTION['report_date']
                                                  and row['source_url'] == TITLE_DATE_CORRECTION['source_url']))
                state.update(catalog_ready=True, eastmoney_ready=True,
                             total=len(pending), percent=0 if pending else 100)
                self.write_state(state)
                for index, row in enumerate(pending):
                    check_cancel()
                    state['stage'] = '保存两融担保比例 ' + row['trade_date']
                    self.write_state(state)
                    report = None
                    report_stage = 'fetch'
                    try:
                        report = fetched.get((row['trade_date'], row['source_url'])) or MarginReport(
                            date.fromisoformat(row['trade_date']), row['source_url'])
                        point = fetch_point(client, report)
                        check_cancel()
                        report_stage = 'validate'
                        correction = point.get('source_correction')
                        corrected = (correction == TITLE_DATE_CORRECTION
                                     and row['trade_date'] == TITLE_DATE_CORRECTION['report_date']
                                     and row['source_url'] == TITLE_DATE_CORRECTION['source_url']
                                     and point['source_url'] == row['source_url']
                                     and str(point['date']) == TITLE_DATE_CORRECTION['observation_date'])
                        if ((correction is not None and not corrected)
                                or (str(point['date']) != row['trade_date'] and not corrected)):
                            raise MarginSourceError('中证日报日期与待采日期不一致。')
                        observation_date = date.fromisoformat(str(point['date']))
                        if not cutoff <= observation_date <= target:
                            raise MarginSourceError('中证日报日期超出请求范围。')
                        try:
                            ratio = float(point['ratio'])
                        except (TypeError, ValueError):
                            raise MarginSourceError('中证日报比例不是有效数字或超出合理范围。') from None
                        if not math.isfinite(ratio) or not 0 < ratio < 10000:
                            raise MarginSourceError('中证日报比例不是有效数字或超出合理范围。')
                        report_stage = 'database_write'
                        # Commit each verified day immediately; a failed later request cannot lose it.
                        with self.db() as db:
                            existing = db.execute('SELECT ratio FROM points WHERE trade_date=?',
                                                  (point['date'],)).fetchone()
                            if corrected and existing is None and db.execute(
                                    'SELECT 1 FROM reports WHERE trade_date=?', (point['date'],)).fetchone():
                                raise MarginSourceError('中证更正日报对应统计日的正式日报尚未校验。')
                            if corrected and existing is not None and existing['ratio'] != ratio:
                                raise MarginSourceError('中证更正日报与已保存统计日的比例冲突，未覆盖现有数据。')
                            db.execute('INSERT OR IGNORE INTO points VALUES (?,?,?,?)',
                                       (point['date'], ratio, report.url, now().isoformat()))
                            if corrected:
                                db.execute('INSERT OR REPLACE INTO report_resolutions VALUES (?,?,?,?,?,?,?)',
                                           (row['trade_date'], report.url, correction['content_sha256'],
                                            point['date'], ratio, correction['reason'], now().isoformat()))
                        consecutive_failures = 0
                    except CollectionCancelled:
                        raise
                    except Exception as exc:
                        run_error = safe_error(exc, stage=report_stage, trade_date=row['trade_date'])
                        failures += 1
                        consecutive_failures += 1
                        failure = dict(scope='report', date=row['trade_date'],
                                       source_url=safe_report_url(row['source_url']), **safe_error_details(exc))
                        state['recent_failures'] = (state['recent_failures'] + [failure])[-3:]
                        state['last_failure'] = failure
                        LOG.warning('Margin report failed date=%s source=margin_report reason=%s details=%s',
                                    row['trade_date'], safe_error_details(exc), diagnostic_details(exc))
                    state.update(processed=index+1, percent=round(100*(index+1)/len(pending), 1))
                    self.write_state(state)
                    if consecutive_failures >= 3:
                        stopped_after_failures = True
                        raise RuntimeError('Three consecutive official reports failed')
                    # Batched JSON values are already in memory; only throttle actual fetches.
                    if self.delay and not isinstance(report, RatioReport):
                        self.stop_event.wait(self.delay)
                check_cancel()
            if failures:
                state.update(stage='部分完成，失败日期下次补采', error=f'{failures} 份日报未成功读取，已保存其余数据。最后失败：'
                                   + failure_summary(state['last_failure']))
            else:
                state.update(stage='更新完成' if pending else '没有新增披露', error=None,
                             last_success_at=now().isoformat(), percent=100)
            outcome = 'partial' if failures else 'success'
        except CollectionCancelled:
            state.update(stage='已停止，已保存数据可继续补采', error=None)
            outcome = 'cancelled'
        except Exception as exc:
            if not stopped_after_failures:
                run_error = safe_error(exc, stage='collection')
                state['last_failure'] = dict(scope=scope, **safe_error_details(exc))
            prefix = ('连续三份日报读取失败，已停止本轮。' if stopped_after_failures
                      else '两融数据读取或校验失败。')
            state.update(stage='更新失败，已保存的数据保留',
                         error=prefix + failure_summary(state['last_failure'])
                               + ' 请稍后重试；再次更新会继续补采。')
            # Never log traceback/exception text: upstream errors can contain secrets or bodies.
            LOG.error('Margin guarantee collection failed scope=%s stopped_after_failures=%s reason=%s details=%s',
                      scope, stopped_after_failures, safe_error_details(exc), diagnostic_details(exc))
        finally:
            isolate = isolate or (context.isolate_manual_failure(include_requested=False))
            state['active'] = False
            try:
                if isolate and existing_reports is not None:
                    with self.db() as db:
                        pending_reports = [row[0] for row in db.execute('SELECT r.trade_date FROM reports r LEFT JOIN points p ON p.trade_date=r.trade_date WHERE p.trade_date IS NULL')]
                        for day in set(pending_reports) - existing_reports:
                            db.execute('DELETE FROM reports WHERE trade_date=?', (day,))
                    state['manual_failure_isolated'] = outcome != 'success'
                self.write_state(state)
                marker.unlink(missing_ok=True)
            except BaseException as exc:
                outcome = 'failed'
                run_error = safe_error(exc, stage='cache_publish')
                LOG.error('Margin finalization failed details=%s', diagnostic_details(exc))
                raise
            finally:
                try:
                    try:
                        record_task(self.settings, 'margin', outcome=outcome, scheduled=state.get('current_scheduled_slot') is not None, run_key=state['run_id'], error_details=run_error if outcome in ('failed', 'partial') else None)
                    finally:
                        lock.release()
                except BaseException as exc:
                    outcome = 'failed'
                    LOG.error('Collection lock release failed details=%s', diagnostic_details(exc))
                    raise
                finally:
                    LOG.info('Margin guarantee collection finished outcome=%s processed=%s total=%s failed=%s duration_seconds=%.3f',
                             outcome, state.get('processed', 0), state.get('total'), failures, context.elapsed)

    def tick(self, moment=None):
        moment = moment or now()
        if not automatic_collection_allowed(moment) or not task_config(self.settings, 'margin')['enabled']:
            return
        planned = latest_task_slot(self.settings, 'margin', moment)
        if not automatic_collection_allowed(planned):
            return
        slot = planned.isoformat()
        if self.schedule_floor is None:
            self.schedule_floor = slot
        if slot > self.schedule_floor:
            self.refresh(background=False, moment=moment, slot=slot)

    def start(self):
        if self.scheduler and self.scheduler.is_alive():
            return
        # Startup only installs the scheduler. It never starts a historical download.
        self.schedule_floor = latest_task_slot(self.settings, 'margin', now()).isoformat()
        def loop():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    LOG.error('Margin scheduler failed details=%s', diagnostic_details(exc))
                self.stop_event.wait(30)
        self.scheduler = threading.Thread(target=loop, name='margin-guarantee-scheduler', daemon=True)
        self.scheduler.start()

    def stop(self):
        self.stop_event.set()
        if self.scheduler:
            self.scheduler.join()
        if self.worker:
            self.worker.join()
