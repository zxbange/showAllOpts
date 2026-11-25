"""Apply same-date public closing quotes to the privately observed positions."""
from datetime import time as wall_time
import json
import logging
from threading import Thread

import portalocker

from app.services.collection_calendar import automatic_collection_allowed
from app.services.holdings_close_source import HoldingsCloseUnavailable, fetch_daily_closes
from app.services.holdings_valuation import value_positions

LOG = logging.getLogger(__name__)
FIRST_CHECK = wall_time(15, 30)
LAST_CHECK = wall_time(16, 30)
RETRY_INTERVAL_SECONDS = 300


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS holdings_close_attempts (
        source_id TEXT PRIMARY KEY, trade_date TEXT NOT NULL, attempted_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS holdings_close_values (
        source_id TEXT PRIMARY KEY, trade_date TEXT NOT NULL, payload TEXT NOT NULL)''')


def _latest(db, ref, today):
    return db.execute(
        'SELECT trade_date,payload FROM holdings_close_values WHERE source_id=? AND trade_date<=?',
        (ref['id'], today.isoformat())).fetchone()


def close_valuation(db, ref, today, rows):
    current = _latest(db, ref, today) if ref else None
    if current is None:
        _, totals = value_positions(rows, {})
        return dict(trade_date=None, prices={}, strategy_totals=totals)
    day, payload = current
    prices, totals = value_positions(rows, json.loads(payload))
    return dict(trade_date=day, prices=prices, strategy_totals=totals)


class HoldingsClose:
    def __init__(self, monitor, clock, zone):
        self.monitor = monitor
        self.clock = clock
        self.zone = zone
        self.thread = None

    def start(self):
        self.thread = Thread(target=self.loop, daemon=True, name='holdings-close')
        self.thread.start()

    def stop(self):
        if self.thread is not None:
            self.thread.join(timeout=2)

    def loop(self):
        while not self.monitor.stop_event.is_set():
            try:
                self.collect_due(self.clock())
            except Exception as exc:
                # The upstream body, token, positions and raw errors are private.
                LOG.warning('Personal holdings close worker failed type=%s', type(exc).__name__)
            self.monitor.stop_event.wait(5)

    def collect_due(self, now):
        local = now.astimezone(self.zone)
        day = local.date()
        if (self.monitor.stop_event.is_set() or
                not FIRST_CHECK <= local.time().replace(second=0, microsecond=0) <= LAST_CHECK or
                not automatic_collection_allowed(day)):
            return False
        lock = portalocker.Lock(str(self.monitor.root / 'close.lock'), timeout=0,
                                flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
        try:
            lock.acquire()
        except portalocker.exceptions.LockException:
            return False
        try:
            return self._collect(now, day)
        finally:
            lock.release()

    def _collect(self, now, day):
        from app.services.system_configuration import SystemConfiguration

        monitor = self.monitor
        _, schedule, ref = monitor._system_configuration()
        if not schedule['enabled'] or ref is None:
            return False
        iso_day = day.isoformat()
        with monitor.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = monitor.state(db)
            if state['source'] != ref or not state['enabled'] or not state['baseline'] or not state['rows']:
                return False
            last = db.execute('SELECT trade_date,attempted_at FROM holdings_close_attempts WHERE source_id=?',
                              (ref['id'],)).fetchone()
            if last and (last[0] > iso_day or
                         (last[0] == iso_day and now.timestamp() - last[1] < RETRY_INTERVAL_SECONDS)):
                return False
            current = _latest(db, ref, day)
            if current and current[0] == iso_day:
                prices, _ = value_positions(state['rows'], json.loads(current[1]))
                if {row['code'] for row in state['rows']} <= prices.keys():
                    return False
            token = (SystemConfiguration(monitor.settings).load()['integrations']['tushare']['token'] or '').strip()
            if not token:
                return False
            db.execute('INSERT OR REPLACE INTO holdings_close_attempts VALUES(?,?,?)',
                       (ref['id'], iso_day, now.timestamp()))
        try:
            closes = fetch_daily_closes(day, token)
        except HoldingsCloseUnavailable:
            LOG.info('Personal holdings daily close unavailable trade_date=%s', iso_day)
            return False
        with portalocker.Lock(str(monitor.root / 'configuration.lock'), timeout=10):
            _, current_schedule, current_ref = monitor._system_configuration()
            if not current_schedule['enabled'] or current_ref != ref or monitor.stop_event.is_set():
                return False
            with monitor.db() as db:
                db.execute('BEGIN IMMEDIATE')
                current = monitor.state(db)
                if (current['source'] != ref or current['epoch'] != state['epoch']
                        or not current['enabled'] or monitor.stop_event.is_set()
                        or self.clock().astimezone(self.zone).date() != day):
                    return False
                old = db.execute('SELECT trade_date,payload FROM holdings_close_values WHERE source_id=?',
                                 (ref['id'],)).fetchone()
                if old and old[0] > iso_day:
                    return False
                if old and old[0] == iso_day:
                    before, _ = value_positions(current['rows'], json.loads(old[1]))
                    after, _ = value_positions(current['rows'], closes)
                    if not before.keys() <= after.keys():
                        # No blending with even an earlier response for the same date.
                        return False
                db.execute('INSERT OR REPLACE INTO holdings_close_values VALUES(?,?,?)',
                           (ref['id'], iso_day, json.dumps(closes, ensure_ascii=False)))
                prices, _ = value_positions(current['rows'], closes)
                complete = {row['code'] for row in current['rows']} <= prices.keys()
        LOG.info('Personal holdings daily close stored trade_date=%s complete=%s', iso_day, complete)
        return True
