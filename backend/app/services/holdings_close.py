"""Apply same-date public closing quotes to the privately observed positions."""
from datetime import time as wall_time
import json
import logging
from threading import Thread

import portalocker

from app.services.collection_calendar import automatic_collection_allowed, latest_due_slot, trading_day
from app.services.holdings_close_source import HoldingsCloseUnavailable, fetch_daily_closes
from app.services.holdings_valuation import value_positions

LOG = logging.getLogger(__name__)
FIRST_CHECK = wall_time(15, 30)
LAST_CHECK = wall_time(16, 30)
RETRY_INTERVAL_SECONDS = 300


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS holdings_close_attempts (
        source_id TEXT PRIMARY KEY, trade_date TEXT NOT NULL, attempted_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS holdings_close_checks (
        source_id TEXT PRIMARY KEY, epoch TEXT NOT NULL, payload TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS holdings_close_values (
        source_id TEXT PRIMARY KEY, trade_date TEXT NOT NULL, payload TEXT NOT NULL)''')


def _latest(db, ref, today):
    return db.execute(
        'SELECT trade_date,payload FROM holdings_close_values WHERE source_id=? AND trade_date<=?',
        (ref['id'], today.isoformat())).fetchone()


def latest_check(db, ref, epoch):
    if not ref:
        return None
    row = db.execute('SELECT payload FROM holdings_close_checks WHERE source_id=? AND epoch=?',
                     (ref['id'], epoch)).fetchone()
    return json.loads(row[0]) if row else None


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
        self.last_reason = None

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
            before = self.monitor.state()
            stored, result = self._collect(now, day)
            reason = (day, before['epoch'], result["status"], result.get("reason"))
            if reason != self.last_reason and result["status"] != "cooldown":
                LOG.info("Personal holdings close check trade_date=%s status=%s reason=%s", day, result["status"], result.get("reason") or "-")
                self.last_reason = reason
                _, _, ref = self.monitor._system_configuration()
                with self.monitor.db() as db:
                    state = self.monitor.state(db)
                    if ref and state['source'] == ref == before['source'] and state['epoch'] == before['epoch']:
                        db.execute('INSERT OR REPLACE INTO holdings_close_checks VALUES(?,?,?)',
                            (ref['id'], state['epoch'], json.dumps(dict(result, checked_at=now.isoformat()), ensure_ascii=False)))
            return stored
        finally:
            lock.release()

    def collect_manual(self, now, *, expected_ref, expected_epoch, request_id):
        due = latest_due_slot(now, [FIRST_CHECK.strftime('%H:%M')], is_open=trading_day)
        if due is None:
            return dict(status='unavailable', reason='没有已核验的到期交易日', trade_date=None)
        day = due.date()
        lock = portalocker.Lock(str(self.monitor.root / 'close.lock'), timeout=30)
        try:
            lock.acquire()
        except portalocker.exceptions.LockException:
            return dict(status='unavailable', reason='已有收盘价采集正在执行，请稍后重试', trade_date=day.isoformat())
        try:
            _, result = self._collect(now, day, manual=True, expected_ref=expected_ref,
                                      expected_epoch=expected_epoch, request_id=request_id)
            LOG.info('Personal holdings manual close trade_date=%s status=%s reason=%s', day, result['status'], result.get('reason') or '-')
            return result
        finally:
            lock.release()

    def _collect(self, now, day, *, manual=False, expected_ref=None, expected_epoch=None, request_id=None):
        from app.services.system_configuration import SystemConfiguration

        monitor = self.monitor
        _, schedule, ref = monitor._system_configuration()
        iso_day = day.isoformat()
        def result(status, reason=None, stored=False):
            return stored, dict(status=status, reason=reason, trade_date=iso_day)
        if ref is None:
            return result('unavailable', '持仓数据源未配置')
        if not manual and not schedule['enabled']:
            return result('skipped', '持仓监控未开启')
        if manual and ref != expected_ref:
            return result('cancelled', '持仓配置已变更')
        with monitor.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = monitor.state(db)
            if state['source'] != ref or not state['baseline']:
                return result('unavailable', '尚未成功读取当前持仓')
            if manual and (state['epoch'] != expected_epoch or (state.get('manual') or {}).get('id') != request_id):
                return result('cancelled', '持仓配置或任务已变更')
            if not manual and not state['enabled']:
                return result('skipped', '持仓监控未开启')
            if not state['rows']:
                return result('complete', '无持仓，无需获取行情')
            last = db.execute('SELECT trade_date,attempted_at FROM holdings_close_attempts WHERE source_id=?',
                              (ref['id'],)).fetchone()
            if not manual and last and (last[0] > iso_day or
                         (last[0] == iso_day and now.timestamp() - last[1] < RETRY_INTERVAL_SECONDS)):
                return result('cooldown')
            current = _latest(db, ref, day)
            if not manual and current and current[0] == iso_day:
                prices, _ = value_positions(state['rows'], json.loads(current[1]))
                if {row['code'] for row in state['rows']} <= prices.keys():
                    return result('complete', '当天所需收盘价均已获取')
            token = (SystemConfiguration(monitor.settings).load()['integrations']['tushare']['token'] or '').strip()
            if not token:
                return result('unavailable', '未配置 Tushare Token')
            if not manual:
                db.execute('INSERT OR REPLACE INTO holdings_close_attempts VALUES(?,?,?)',
                       (ref['id'], iso_day, now.timestamp()))
        LOG.info('Personal holdings daily close start trade_date=%s trigger=%s', iso_day, 'manual' if manual else 'scheduled')
        try:
            closes = fetch_daily_closes(day, token)
        except HoldingsCloseUnavailable:
            LOG.info('Personal holdings daily close unavailable trade_date=%s', iso_day)
            return result('unavailable', '目标日行情未完整返回或接口不可用')
        with portalocker.Lock(str(monitor.root / 'configuration.lock'), timeout=10):
            _, current_schedule, current_ref = monitor._system_configuration()
            if (not manual and not current_schedule['enabled']) or current_ref != ref or monitor.stop_event.is_set():
                return result('cancelled', '持仓配置或监控状态已变更')
            with monitor.db() as db:
                db.execute('BEGIN IMMEDIATE')
                current = monitor.state(db)
                if (current['source'] != ref or current['epoch'] != state['epoch']
                        or (not manual and not current['enabled']) or monitor.stop_event.is_set()
                        or self.clock().astimezone(self.zone).date() != now.astimezone(self.zone).date()):
                    return result('cancelled', '持仓配置、监控状态或日期已变更')
                old = db.execute('SELECT trade_date,payload FROM holdings_close_values WHERE source_id=?',
                                 (ref['id'],)).fetchone()
                if old and old[0] > iso_day:
                    return result('unavailable', '已有较新收盘价，保留原估值')
                if old and old[0] == iso_day:
                    before, _ = value_positions(current['rows'], json.loads(old[1]))
                    after, _ = value_positions(current['rows'], closes)
                    if not before.keys() <= after.keys():
                        # No blending with even an earlier response for the same date.
                        return result('unavailable', '行情覆盖较上次减少，保留原估值')
                db.execute('INSERT OR REPLACE INTO holdings_close_values VALUES(?,?,?)',
                           (ref['id'], iso_day, json.dumps(closes, ensure_ascii=False)))
                prices, _ = value_positions(current['rows'], closes)
                complete = {row['code'] for row in current['rows']} <= prices.keys()
        LOG.info('Personal holdings daily close stored trade_date=%s complete=%s', iso_day, complete)
        return result('complete' if complete else 'partial', None if complete else '部分持仓收盘价缺失，总金额待定', stored=True)
