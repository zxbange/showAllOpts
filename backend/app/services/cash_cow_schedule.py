"""Persistent, unified month-end scheduling in Asia/Shanghai (UTC+8)."""
from calendar import monthrange
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import logging

import sqlite3
import threading

import portalocker

from app.core.diagnostics import diagnostic_details

CHINA = timezone(timedelta(hours=8))
LOG = logging.getLogger(__name__)


def china_time(moment=None):
    value = moment or datetime.now(CHINA)
    if value.tzinfo is None:
        raise ValueError('Scheduler requires a timezone-aware datetime')
    return value.astimezone(CHINA)


def month_end(year, month):
    return datetime(year, month, monthrange(year, month)[1], 21, tzinfo=CHINA)


def next_run(moment=None):
    """First month's last calendar day at 21:00 strictly after moment."""
    moment = china_time(moment)
    for year in (moment.year, moment.year + 1):
        for month in range(1, 13):
            slot = month_end(year, month)
            if slot > moment:
                return slot


def latest_due(moment):
    for year in (moment.year, moment.year - 1):
        for month in range(12, 0, -1):
            slot = month_end(year, month)
            if slot <= moment:
                return slot


class MonthlyCashCowScheduler:
    def __init__(self, settings, screens):
        self.screen = screens[10]
        self.root = settings.log_path / 'market-data' / 'cash-cow-schedule'
        self.root.mkdir(parents=True, exist_ok=True)
        self.stopped = threading.Event()
        self.worker = None
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    period INTEGER PRIMARY KEY, slot TEXT NOT NULL, attempted_at TEXT NOT NULL);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / 'schedule.sqlite3', timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def next_run(moment=None):
        return next_run(moment)

    def tick(self, moment=None):
        """Start one shared run. Return its periods, or None when not due."""
        moment = china_time(moment)
        lock = portalocker.Lock(str(self.root / 'tick.lock'), timeout=0,
                                flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
        try:
            lock.acquire()
        except portalocker.exceptions.LockException:
            return None
        try:
            with self.db() as db:
                db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)',
                           ('enabled_at', moment.isoformat()))
                enabled_at = china_time(datetime.fromisoformat(db.execute(
                    'SELECT value FROM settings WHERE key=?', ('enabled_at',)).fetchone()[0]))
            due = latest_due(moment)
            screen = self.screen
            if due < enabled_at or screen.running():
                return None
            slot = due.date().isoformat()
            state = screen.read_state()
            if state.get('scheduled_success_slot') == slot:
                return None
            interrupted = (state.get('active') and not screen.running()
                           and state.get('current_scheduled_slot', state.get('scheduled_slot')) == slot)
            with self.db() as db:
                # Period 0 identifies the unified job. Old per-window attempt
                # records are retained but cannot suppress an incomplete upgrade.
                saved = db.execute('SELECT slot, attempted_at FROM attempts WHERE period=0').fetchone()
            attempted = []
            if saved and saved[0] == slot:
                attempted.append(saved[1])
            scheduled_attempt = state.get('scheduled_attempt_at', state.get('last_attempt_at'))
            if state.get('scheduled_slot') == slot and scheduled_attempt:
                attempted.append(scheduled_attempt)
            already_today = any(china_time(datetime.fromisoformat(stamp)).date() >= moment.date()
                                for stamp in attempted)
            if already_today and not interrupted:
                return None
            # Manual and scheduled entry points share the collection lock.
            if screen.refresh(background=True, moment=moment, slot=slot):
                with self.db() as db:
                    db.execute('INSERT OR REPLACE INTO attempts VALUES (?,?,?)',
                               (0, slot, moment.isoformat()))
                return (3, 5, 10)
            # Another process won the lock; do not consume today's retry.
            return None
        finally:
            lock.release()

    def _loop(self):
        while not self.stopped.is_set():
            try:
                self.tick()
            except Exception as exc:
                LOG.error('Monthly cash-cow scheduling failed details=%s', diagnostic_details(exc))
            self.stopped.wait(30)

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.stopped.clear()
        self.worker = threading.Thread(target=self._loop, name='cash-cow-monthly', daemon=True)
        self.worker.start()

    def stop(self):
        self.stopped.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=30)
