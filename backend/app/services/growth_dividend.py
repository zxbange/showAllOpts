from app.core.history_sync import mark_sync_owner, clear_sync_owner, database_syncing, SYNC_MESSAGE
"""Restartable public index history, with Shanghai 18:00 trading-day updates."""
from app.services.system_runtime import source_client
from app.services.system_configuration import task_config
from app.services.collection_notifications import notify_collection
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import json
import logging
from time import monotonic

import math
import sqlite3
import threading
import uuid
from zoneinfo import ZoneInfo

import httpx
import portalocker

from app.core.diagnostics import diagnostic_details

from app.services.growth_dividend_source import (
    CALENDAR_URL, INDEXES, START, IndexSourceError, fetch_calendar, fetch_prices,
)

LOG = logging.getLogger(__name__)
CHINA = ZoneInfo('Asia/Shanghai')
DAILY_TIME = '18:00'


def now():
    return datetime.now(timezone.utc)


def close_cutoff(moment):
    local = moment.astimezone(CHINA)
    return local.date() if local.hour >= 18 else local.date() - timedelta(days=1)


def slot_for(day, settings=None):
    from app.services.system_configuration import task_config
    hhmm = task_config(settings, 'growth_dividend')['times'][0] if settings is not None else DAILY_TIME
    hour, minute = map(int, hhmm.split(':'))
    return datetime.combine(day, time(hour, minute), CHINA).isoformat()


class CollectionCancelled(Exception):
    pass


def windows(days):
    """Group nearby gaps without rereading completed years or distant intervals."""
    groups = []
    for day in sorted(set(days)):
        if not groups or day.year != groups[-1][-1].year or (day - groups[-1][-1]).days > 10:
            groups.append([day])
        else:
            groups[-1].append(day)
    return groups


class GrowthDividend:
    def __init__(self, settings, *, client_factory=None):
        self.settings = settings
        self.root = settings.log_path / 'market-data' / 'growth-dividend'
        self.root.mkdir(parents=True, exist_ok=True)
        self.client_factory = client_factory or (lambda: source_client(self.settings, factory=httpx.Client,
            timeout=httpx.Timeout(20, connect=8), trust_env=False, follow_redirects=False,
            headers={'User-Agent': 'Mozilla/5.0 ShowAllOpts/1.0',
                     'Referer': 'https://gu.qq.com/'}))
        self.stop_event = threading.Event()
        self.scheduler = self.worker = None
        self.startup_checked = False
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS calendar (trade_date TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS instruments (
                    code TEXT PRIMARY KEY, first_date TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS points (
                    code TEXT NOT NULL, trade_date TEXT NOT NULL,
                    close REAL NOT NULL CHECK(close>0), source TEXT NOT NULL,
                    source_url TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(code, trade_date));
                CREATE TABLE IF NOT EXISTS comparisons (
                    code TEXT NOT NULL, trade_date TEXT NOT NULL,
                    published_close REAL NOT NULL, candidate_close REAL NOT NULL,
                    checked_at TEXT NOT NULL,
                    PRIMARY KEY(code, trade_date, candidate_close));
                CREATE TABLE IF NOT EXISTS state (
                    id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
                INSERT OR IGNORE INTO state VALUES (1, '{}');
            ''')
            db.executemany('INSERT INTO instruments VALUES (?,?) '
                           'ON CONFLICT(code) DO UPDATE SET first_date=excluded.first_date',
                           [(index.code, str(index.first_date)) for index in INDEXES])

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

    def acquire(self, *, for_history=False):
        lock = portalocker.Lock(str(self.root / 'collection.lock'), timeout=0,
                                flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
        try:
            lock.acquire()
        except portalocker.exceptions.LockException:
            return None
        try:
            (mark_sync_owner if for_history else clear_sync_owner)(self)
        except BaseException:
            lock.release()
            raise
        return lock

    def running(self):
        lock = self.acquire()
        if lock is None:
            return True
        lock.release()
        return False

    def cancel(self):
        busy = self.running()
        if not busy or database_syncing(self, busy):
            return False
        state = self.read_state()
        if not state.get('active'):
            return False
        identifier = state.get('run_id', '')
        if len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
            return False
        (self.root / ('cancel-' + identifier)).touch()
        return True

    @staticmethod
    def pending(db, cutoff):
        return db.execute(
            'SELECT i.code,c.trade_date FROM calendar c CROSS JOIN instruments i '
            'LEFT JOIN points p ON p.code=i.code AND p.trade_date=c.trade_date '
            'WHERE c.trade_date>=? AND c.trade_date>=i.first_date AND c.trade_date<=? '
            'AND p.trade_date IS NULL ORDER BY c.trade_date DESC,i.code',
            (str(START), str(cutoff))).fetchall()

    def status(self, moment=None):
        moment = moment or now()
        local, cutoff = moment.astimezone(CHINA), close_cutoff(moment)
        with self.db() as db:
            state = self.read_state(db)
            pending = self.pending(db, cutoff)
            future = db.execute('SELECT trade_date FROM calendar WHERE trade_date>=? '
                                'ORDER BY trade_date LIMIT 20', (str(local.date()),)).fetchall()
            comparison_conflicts = db.execute('SELECT count(*) FROM comparisons').fetchone()[0]
            recent_conflicts = [dict(row) for row in db.execute(
                'SELECT code,trade_date,published_close,candidate_close,checked_at '
                'FROM comparisons ORDER BY checked_at DESC,trade_date DESC,code LIMIT 10')]
        plan = task_config(self.settings, 'growth_dividend')
        next_slot = datetime.fromisoformat(slot_for(local.date(), self.settings))
        if next_slot <= local:
            next_slot += timedelta(days=1)
        next_run = next_slot.isoformat()
        busy = self.running()
        syncing = database_syncing(self, busy)
        running = bool(busy and not syncing and state.get('active'))
        stage = state.get('stage', '等待首次补齐历史数据')
        if not running and state.get('active'):
            stage = '上次更新中断，将从已保存的进度继续补齐'
        ready = state.get('calendar_through', '') >= str(cutoff)
        return dict(
            running=running, database_syncing=syncing, sync_message=SYNC_MESSAGE if syncing else None, stage=stage, sync_summary=state.get('sync_summary'), processed=state.get('processed', 0),
            total=state.get('total'), percent=state.get('percent'),
            cancelling=bool(running and state.get('run_id') and
                            (self.root / ('cancel-' + state['run_id'])).exists()),
            last_attempt_at=state.get('last_attempt_at'),
            last_success_at=state.get('last_success_at'), error=state.get('error'),
            pending_dates=len({row['trade_date'] for row in pending}), pending_points=len(pending),
            calendar_ready=ready, calendar_through=state.get('calendar_through'),
            calendar_error=state.get('calendar_error'), target_date=str(cutoff),
            recent_failures=state.get('recent_failures', []),
            comparison_conflicts=comparison_conflicts, recent_conflicts=recent_conflicts,
            daily_time=plan['times'][0], timezone='Asia/Shanghai',
            automatic=plan['enabled'],
            next_run_at=next_run if plan['enabled'] else None,
        )

    def history(self, moment=None):
        moment = moment or now()
        cutoff = close_cutoff(moment)
        with self.db() as db:
            rows = db.execute('SELECT * FROM points WHERE trade_date>=? AND trade_date<=? '
                              'ORDER BY trade_date,code', (str(START), str(cutoff))).fetchall()
            dates = [row[0] for row in db.execute('SELECT trade_date FROM calendar '
                     'WHERE trade_date>=? AND trade_date<=? ORDER BY trade_date',
                     (str(START), str(cutoff)))]
        by_date = {}
        provenance = {}
        code_keys = {index.code: index.key for index in INDEXES}
        updated = None
        for row in rows:
            if row['code'] not in code_keys or (row['code'] == '000688' and row['trade_date'] < '2020-01-02'):
                continue
            by_date.setdefault(row['trade_date'], {})[code_keys[row['code']]] = row['close']
            provenance.setdefault(row['trade_date'], {})[code_keys[row['code']]] = row['source']
            updated = max(updated or row['updated_at'], row['updated_at'])
        points = []
        if by_date:
            for day in sorted(set(dates) | set(by_date)):
                values = by_date.get(day, {})
                point = dict(date=day, sources_by_index=provenance.get(day, {}),
                             **{index.key: values.get(index.key) for index in INDEXES})
                dividend = point['dividend']
                point['chinext_dividend_ratio'] = (round(point['chinext'] / dividend, 8)
                    if dividend and point['chinext'] is not None else None)
                point['star50_dividend_ratio'] = (round(point['star50'] / dividend, 8)
                    if dividend and point['star50'] is not None else None)
                points.append(point)
        latest = next((point for point in reversed(points)
                       if any(point[index.key] is not None for index in INDEXES)), None)
        return dict(start_date=points[0]['date'] if points else None,
                    end_date=latest['date'] if latest else None, latest=latest, points=points,
                    updated_at=updated, status=self.status(moment),
                    indices=[dict(key=index.key, code=index.code, name=index.name,
                                  first_date=str(index.first_date)) for index in INDEXES],
                    sources=['腾讯财经', '东方财富（备用）', '聚宽（仅已同步记录）'], calendar_source=CALENDAR_URL,
                    star50_base_date='2019-12-31', history_start=str(START))

    def refresh(self, background=True, *, moment=None, slot=None, trigger='manual'):
        moment = moment or now()
        if self.stop_event.is_set():
            return False
        lock = self.acquire()
        if lock is None:
            return False
        state = None
        try:
            state = self.read_state()
            if slot and state.get('scheduled_slot') == slot:
                lock.release()
                return False
            state.update(run_id=uuid.uuid4().hex, active=True, stage='核对交易日历',
                         error=None, calendar_error=None, processed=0, total=None, percent=None,
                         last_attempt_at=moment.isoformat(), trigger=trigger, recent_failures=[],
                         conflict_points=0)
            if slot:
                state['scheduled_slot'] = slot
            self.write_state(state)
            if background:
                self.worker = threading.Thread(target=self.collect, args=(lock, state, moment, slot),
                                               name='growth-dividend-collector', daemon=True)
                self.worker.start()
            else:
                self.collect(lock, state, moment, slot)
        except BaseException as exc:
            LOG.error('Growth/dividend launch or cleanup failed details=%s', diagnostic_details(exc))
            try:
                if background and state is not None:
                    state.update(active=False, stage='未能启动更新任务', error='任务启动失败，请重试。')
                    self.write_state(state)
            finally:
                lock.release()
            raise
        return True

    @notify_collection('growth_dividend')
    def collect(self, lock, state, moment, slot=None):
        marker = self.root / ('cancel-' + state['run_id'])
        cutoff = close_cutoff(moment)
        local = moment.astimezone(CHINA)
        started = monotonic()
        outcome = 'failed'
        failed_windows = 0
        LOG.info('Growth/dividend collection started source=public_index trigger=%s cutoff=%s', state.get('trigger', '-'), cutoff)

        def check_cancel():
            if self.stop_event.is_set() or marker.exists():
                raise CollectionCancelled()

        try:
            with self.client_factory() as client:
                check_cancel()
                calendar_ok = False
                try:
                    days = fetch_calendar(client)
                    check_cancel()
                    if not days or days != sorted(set(days)) or days[0] > START:
                        raise IndexSourceError('公开交易日历内容不完整。')
                    through = str(days[-1])
                    # Validate before replacing the persistent session catalogue.
                    # A stale upstream/cache response must not erase known future
                    # sessions or hide existing gaps from the recovery query.
                    if through < state.get('calendar_through', ''):
                        raise IndexSourceError('公开交易日历范围回退，保留已有日历并等待重试。')
                    with self.db() as db:
                        known_days = {row[0] for row in db.execute(
                            'SELECT trade_date FROM calendar WHERE trade_date>=?', (str(START),))}
                        if not known_days.issubset({str(day) for day in days}):
                            raise IndexSourceError('公开交易日历缺少已确认交易日，保留原日历并等待重试。')
                        db.executemany('INSERT OR IGNORE INTO calendar VALUES (?)',
                                       [(str(day),) for day in days if day >= START])
                        state.update(calendar_through=through, calendar_checked_at=moment.isoformat())
                        self.write_state(state, db)
                    # A first/extended calendar can still provide useful known
                    # sessions at the turn of a year. Save that forward progress,
                    # but never claim coverage or success for unknown dates.
                    if through < str(cutoff):
                        raise IndexSourceError('公开交易日历尚未覆盖本次截止日，先补齐已知历史并等待重试。')
                    calendar_ok = True
                except (httpx.HTTPError, ValueError, TypeError) as exc:
                    state['calendar_error'] = ('交易日历暂不可用，保留已有数据并等待重试。'
                                              if not isinstance(exc, IndexSourceError) else str(exc))
                    LOG.warning('Growth/dividend calendar failed details=%s', diagnostic_details(exc))
                with self.db() as db:
                    expected_days = [date.fromisoformat(row[0]) for row in db.execute(
                        'SELECT trade_date FROM calendar WHERE trade_date>=? AND trade_date<=? '
                        'ORDER BY trade_date', (str(START), str(cutoff)))]
                    today_trades = db.execute('SELECT 1 FROM calendar WHERE trade_date=?',
                                              (str(local.date()),)).fetchone() is not None
                    missing = self.pending(db, cutoff)
                if state['trigger'] == 'startup' and local.hour >= 18 and today_trades:
                    state['scheduled_slot'] = slot_for(local.date(), self.settings)
                needed = {index.code: set() for index in INDEXES}
                for row in missing:
                    needed[row['code']].add(date.fromisoformat(row['trade_date']))
                # Recheck today's close on explicit refresh; completed historical
                # dates are never downloaded merely because a newer date exists.
                if today_trades and local.hour >= 18:
                    for index in INDEXES:
                        if local.date() >= index.first_date:
                            needed[index.code].add(local.date())
                work = [(index, group) for index in INDEXES for group in windows(needed[index.code])]
                work.sort(key=lambda item: (item[1][-1], item[0].code), reverse=True)
                state.update(total=sum(len(group) for _, group in work), percent=0 if work else 100,
                             stage='补齐指数历史数据')
                self.write_state(state)
                LOG.info('Growth/dividend plan ready calendar_ok=%s windows=%s points=%s',
                         calendar_ok, len(work), state['total'])
                consecutive_failures = 0
                for index, target_days in work:
                    check_cancel()
                    state['stage'] = f'更新{index.name} {target_days[0]} — {target_days[-1]}'
                    self.write_state(state)
                    try:
                        quotes = fetch_prices(client, index, target_days[0], target_days[-1],
                                              expected_dates=set(target_days), check_cancel=check_cancel)
                        check_cancel()
                        accepted = []
                        for day, quote in quotes.items():
                            if day not in target_days or quote.day != day or not index.first_date <= day <= cutoff:
                                raise IndexSourceError('行情返回日期与请求交易日不一致。')
                            if not math.isfinite(quote.close) or not 0 < quote.close < 10000000:
                                raise IndexSourceError('指数收盘点位无效。')
                            accepted.append((index.code, str(day), quote.close, quote.source,
                                             quote.source_url, moment.isoformat()))
                        with self.db() as db:
                            db.execute('BEGIN IMMEDIATE')
                            for row in accepted:
                                old = db.execute('SELECT close FROM points WHERE code=? AND trade_date=?',
                                                 row[:2]).fetchone()
                                if old is None:
                                    db.execute('INSERT INTO points VALUES (?,?,?,?,?,?)', row)
                                elif old['close'] != row[2]:
                                    # Today's recheck must not overwrite a confirmed public or JQ row.
                                    # Persist only validated market values, never upstream responses/errors.
                                    db.execute('INSERT OR IGNORE INTO comparisons VALUES (?,?,?,?,?)',
                                               (*row[:2], old['close'], row[2], moment.isoformat()))
                                    state['conflict_points'] += 1
                            state['processed'] += len(accepted)
                            state['percent'] = round(100 * state['processed'] / state['total'], 1) if state['total'] else 100
                            self.write_state(state, db)
                        absent = set(target_days) - set(quotes)
                        if absent:
                            raise IndexSourceError(f'{index.name}仍有 {len(absent)} 个交易日未返回收盘数据。')
                        consecutive_failures = 0
                    except (httpx.HTTPError, ValueError, TypeError) as exc:
                        consecutive_failures += 1
                        failed_windows += 1
                        state['recent_failures'] = (state['recent_failures'] + [dict(
                            index=index.name, start=str(target_days[0]), end=str(target_days[-1]),
                            reason=str(exc) if isinstance(exc, IndexSourceError) else '公开行情请求失败。',
                        )])[-10:]
                        self.write_state(state)
                        LOG.warning('Growth/dividend window failed index=%s start=%s end=%s details=%s',
                                    index.code, target_days[0], target_days[-1], diagnostic_details(exc))
                        if consecutive_failures >= 4:
                            break
                with self.db() as db:
                    pending = self.pending(db, cutoff)
                if pending or not calendar_ok or not expected_days or state['recent_failures'] or state['conflict_points']:
                    outcome = 'partial'
                    count = len({row['trade_date'] for row in pending})
                    state['error'] = '；'.join(part for part in [state.get('calendar_error'),
                        f'{count} 个交易日、{len(pending)} 条指数数据待补齐。' if pending else None,
                        '部分行情请求未成功，已保存数据保留，稍后会重试。' if state['recent_failures'] and not pending else None,
                        f"{state['conflict_points']} 条公开行情与已保存记录不同；已保留原值，未覆盖。" if state['conflict_points'] else None,
                        '尚未取得有效交易日历。' if not expected_days else None] if part)
                    state['stage'] = ('本次更新结束，来源差异已记录，原值未覆盖' if state['conflict_points']
                                      else '本次更新结束，未完成数据将继续补齐')
                else:
                    outcome = 'success'
                    state.update(stage='已更新至 ' + str(expected_days[-1]), error=None,
                                 last_success_at=now().isoformat(), percent=100)
                state['initialized'] = True
        except CollectionCancelled:
            outcome = 'cancelled'
            state.update(stage='已停止，已保存的数据将用于下次继续补齐', error=None)
        except Exception as exc:
            outcome = 'failed'
            LOG.error('Growth/dividend collection failed details=%s', diagnostic_details(exc))
            state.update(stage='更新失败，已保存的数据保留', error='更新失败，请重试或等待下个交易日自动补齐。')
        finally:
            state['active'] = False
            try:
                self.write_state(state)
                marker.unlink(missing_ok=True)
            except BaseException as exc:
                outcome = 'failed'
                LOG.error('Growth/dividend finalization failed details=%s', diagnostic_details(exc))
                raise
            finally:
                try:
                    lock.release()
                except BaseException as exc:
                    outcome = 'failed'
                    LOG.error('Collection lock release failed details=%s', diagnostic_details(exc))
                    raise
                finally:
                    LOG.info('Growth/dividend collection finished outcome=%s processed=%s total=%s failed_windows=%s duration_seconds=%.3f',
                             outcome, state.get('processed', 0), state.get('total'), failed_windows, monotonic() - started)

    def tick(self, moment=None):
        from app.services.system_configuration import task_config
        plan = task_config(self.settings, 'growth_dividend')
        if not plan['enabled']:
            return
        moment = moment or now()
        local = moment.astimezone(CHINA)
        if not self.startup_checked:
            self.startup_checked = True
            state = self.read_state()
            status = self.status(moment)
            if (not state.get('initialized') or state.get('active') or
                    status['pending_points'] or not status['calendar_ready']):
                self.refresh(background=False, moment=moment, trigger='startup')
                return
        if local < datetime.fromisoformat(slot_for(local.date(), self.settings)):
            return
        self.refresh(background=False, moment=moment, slot=slot_for(local.date(), self.settings), trigger='scheduled')

    def start(self):
        if self.scheduler and self.scheduler.is_alive():
            return

        def loop():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    LOG.error('Growth/dividend scheduler failed details=%s', diagnostic_details(exc))
                self.stop_event.wait(30)
        self.scheduler = threading.Thread(target=loop, name='growth-dividend-scheduler', daemon=True)
        self.scheduler.start()

    def stop(self):
        self.stop_event.set()
        if self.scheduler:
            self.scheduler.join()
        if self.worker:
            self.worker.join()
