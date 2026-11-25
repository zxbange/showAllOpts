"""Durable daily earnings queue: one shared bank/non-bank run, then cash cows.

Manual and scheduled collections use the same execution lease. The coordinator
never holds that lease and never kills a live worker during a possible commit.
"""

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import portalocker

from app.core.diagnostics import diagnostic_details
from app.services.collection_calendar import automatic_collection_allowed, next_automatic_collection
from app.services.collection_lifecycle import RunLease, try_lease
from app.services.system_configuration import task_config

CHINA = timezone(timedelta(hours=8))
LOG = logging.getLogger(__name__)
TASKS = ('dividend_selection', 'cash_cow')


def daily_next_run(moment=None, at='23:00'):
    moment = moment or datetime.now(CHINA)
    if moment.tzinfo is None:
        raise ValueError('Timezone-aware scheduler time required')
    moment = moment.astimezone(CHINA)
    hour, minute = map(int, at.split(':'))
    result = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return next_automatic_collection(result if result > moment else result + timedelta(days=1))


class EarningsNotDue(ValueError):
    """The current-only source cannot safely supply the requested older day."""


def earnings_due_day(settings, moment):
    from app.services.collection_calendar import latest_due_slot, trading_day
    # The current-day sources are only eligible at/after 16:00; an
    # earlier custom clock still cannot advance the effective target day.
    clocks = [max(value, '16:00') for value in task_config(settings, 'earnings_selection')['times']]
    due = latest_due_slot(moment, clocks, is_open=trading_day)
    return due.date() if due else None


def earnings_not_due_message(day):
    target = day.isoformat() if day else '暂无已核验交易日'
    return (f'计划采集截止：{target}。当前接口不能重建历史快照，'
            '本次未请求当天数据，保留已有结果。请在下一次计划到期后重试。')


class ExecutionLease:
    def __init__(self, group, task):
        self.group, self.task = group, task
    def release(self):
        try:
            self.task.release()
        finally:
            self.group.release()


def acquire_execution(settings, acquire_task):
    root = settings.log_path / 'market-data'
    root.mkdir(parents=True, exist_ok=True)
    group = portalocker.Lock(str(root / 'earnings-execution.lock'), timeout=0,
                             flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
    group = try_lease(group, portalocker.exceptions.LockException)
    if group is None:
        return None
    try:
        task = acquire_task()
        if task is None:
            group.release()
            return None
        return RunLease(ExecutionLease(group, task))
    except BaseException:
        group.release()
        raise


class EarningsScheduler:
    def __init__(self, settings, dividend, cash_cow):
        self.settings = settings
        self.services = dict(dividend_selection=dividend, cash_cow=cash_cow)
        self.root = settings.log_path / 'market-data' / 'earnings-schedule'
        self.root.mkdir(parents=True, exist_ok=True)
        self.stopped = threading.Event()
        self.worker = None
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs(slot TEXT, task TEXT, position INTEGER, '
                       'status TEXT NOT NULL, run_id TEXT, detail TEXT, PRIMARY KEY(slot,task))')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / 'schedule.sqlite3', timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def jobs(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM jobs ORDER BY slot,position')]

    def update(self, job, status, run_id=None, detail=None):
        with self.db() as db:
            db.execute('UPDATE jobs SET status=?,run_id=?,detail=? WHERE slot=? AND task=?',
                       (status, run_id, detail, job['slot'], job['task']))
        LOG.info('Earnings job slot=%s task=%s status=%s run=%s', job['slot'], job['task'], status, run_id or '-')

    @staticmethod
    def matches(state, slot):
        return state.get('current_scheduled_slot', state.get('current_slot')) == slot

    def tick(self, moment=None):
        moment = moment or datetime.now(CHINA)
        if moment.tzinfo is None:
            raise ValueError('Timezone-aware scheduler time required')
        moment = moment.astimezone(CHINA)
        config = task_config(self.settings, 'earnings_selection')
        if not automatic_collection_allowed(moment) or not config['enabled']:
            return None
        lock = portalocker.Lock(str(self.root / 'tick.lock'), timeout=0, flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
        try:
            lock.acquire()
        except portalocker.exceptions.LockException:
            return None
        try:
            # Do not manufacture yesterday's job on first installation. Persisted
            # unfinished jobs, however, continue across midnight and restarts.
            if moment.strftime('%H:%M') >= config['times'][0]:
                with self.db() as db:
                    for pos, task in enumerate(TASKS):
                        db.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?)',
                                   (moment.date().isoformat(), task, pos, 'pending', None, None))
            with self.db() as db:
                jobs = [dict(row) for row in db.execute("SELECT * FROM jobs WHERE status IN ('pending','launching','running') ORDER BY slot,position")]
            for job in jobs:
                if not automatic_collection_allowed(datetime.fromisoformat(job['slot']).date()):
                    self.update(job, 'skipped', detail='Closed or unverified calendar date; automatic collection skipped')
                    continue
                service = self.services[job['task']]
                state = service.read_state()
                busy = service.running()
                if job['status'] in ('launching', 'running'):
                    matched = self.matches(state, job['slot'])
                    if matched and (not job['run_id'] or job['run_id'] == state.get('run_id')):
                        if busy:
                            if job['status'] != 'running' or job['run_id'] != state.get('run_id'):
                                self.update(job, 'running', state.get('run_id'))
                            return job['task']
                        success = (state.get('scheduled_success_slot') == job['slot'] if job['task'] == 'cash_cow'
                                   else state.get('stage') == 'complete')
                        if success and state.get('result_date') and state['result_date'] < job['slot']:
                            self.update(job, 'pending', detail='Previous publication recovered; current date still pending')
                            return None
                        if state.get('active'):
                            self.update(job, 'interrupted', state.get('run_id'), 'Worker exited before completion; publication status unconfirmed')
                        else:
                            self.update(job, 'complete' if success else 'failed', state.get('run_id'), state.get('error'))
                        continue
                    if busy:
                        return None
                    # No matching run was launched, or a later manual run changed
                    # shared state. Do not replay uncertain work after a crash.
                    self.update(job, 'interrupted', job['run_id'], 'No matching collection state; not replayed')
                    continue
                if busy:
                    return None
                if job['slot'] != moment.date().isoformat():
                    # Current-only sources cannot replay unstarted older slots.
                    # A prior validated payload can still be resumed by a new run.
                    self.update(job, 'skipped', detail='Current-only source; past slot cannot be fetched')
                    continue
                if moment.strftime('%H:%M') < config['times'][0] or moment.hour < 16:
                    return None
                self.update(job, 'launching')
                try:
                    started = service.refresh(background=True, moment=moment, slot=job['slot'])
                except Exception as exc:
                    self.update(job, 'failed', detail='Collection could not start; prior result retained')
                    LOG.error('Earnings job launch failed task=%s details=%s', job['task'], diagnostic_details(exc))
                    continue
                if not started:
                    # A manual operation or another API worker won the execution
                    # lease. Leave this job pending, not failed and not duplicated.
                    self.update(job, 'pending')
                    return None
                state = service.read_state()
                self.update(job, 'running', state.get('run_id'))
                return job['task']
            return None
        finally:
            lock.release()

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.stopped.clear()
        def loop():
            while not self.stopped.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    LOG.error('Earnings scheduler failed details=%s', diagnostic_details(exc))
                self.stopped.wait(10)
        self.worker = threading.Thread(target=loop, daemon=True, name='earnings-schedule')
        self.worker.start()

    def stop(self):
        self.stopped.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=15)
