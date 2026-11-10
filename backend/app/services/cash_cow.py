"""Strict ten-fiscal-year cash-cow screen, independent of the source MySQL DB.

Only complete, validated observations can qualify. Interrupted refreshes never
replace a published snapshot. All currency amounts are CNY; rates use percent.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import sqlite3
import threading
import uuid

import httpx
import portalocker

LOG = logging.getLogger(__name__)
CHINA = timezone(timedelta(hours=8))
RULE_VERSION = 'cash_cow_strict_window_average_close_v1'


def now():
    return datetime.now(CHINA)


class CollectionCancelled(Exception):
    pass


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def evaluate_year(year, dividend, financial):
    """A known failing predicate is a rejection, null data are never zero."""
    dividend = dividend or {}
    financial = financial or {}
    cash = dividend.get('cash_dividend')
    adjusted = financial.get('adjusted_profit')
    profit = financial.get('net_profit')
    operating = financial.get('operating_cashflow')
    if any(numeric(value) and value <= 0 for value in (cash, adjusted, profit)):
        return 'excluded', None
    if numeric(cash) and numeric(operating) and operating < cash:
        return 'excluded', None
    payout = 100 * cash / profit if numeric(cash) and numeric(profit) and profit > 0 else None
    if payout is not None and not 30 <= payout <= 70:
        return 'excluded', None
    if not all(numeric(value) for value in (cash, adjusted, profit, operating)):
        return 'insufficient', None
    return 'eligible', dict(year=year, cash_dividend=cash, adjusted_profit=adjusted,
                           net_profit=profit, operating_cashflow=operating,
                           payout_ratio=payout, cashflow_coverage=operating / cash)


def finish_stock(stock, years, dividends, prices, period=10):
    """Dividend per share and unadjusted close must share a valid share basis."""
    window = [row['year'] for row in years]
    if len(window) != period or sorted(window) != list(range(min(window), min(window) + period)):
        return 'insufficient', None
    result = []
    for annual in years:
        year = annual['year']
        dps = dividends.get(year)
        price = prices.get(year) or {}
        close = price.get('year_average_close')
        day = price.get('price_date')
        # A suspended stock's stale monthly close is not its year-end close.
        if (not numeric(dps) or dps <= 0 or not numeric(close) or close <= 0
                or not isinstance(day, str) or not day.startswith(f'{year}-12-')):
            return 'insufficient', None
        result.append(dict(annual, dividend_per_share=dps, year_average_close=close,
                           price_date=day, dividend_yield=100 * dps / close))
    average = sum(row['dividend_yield'] for row in result) / period
    if average <= 4:
        return 'excluded', None
    return 'matched', dict(code=stock['code'], name=stock['name'], exchange=stock['exchange'],
                           average_yield=average,
                           payout_min=min(row['payout_ratio'] for row in result),
                           payout_max=max(row['payout_ratio'] for row in result),
                           coverage_min=min(row['cashflow_coverage'] for row in result),
                           years=result)


class CashCowScreen:
    def __init__(self, settings, *, years=10, source_factory=None, client_factory=None):
        if years not in (3, 5, 10):
            raise ValueError('仅支持近3年、5年或10年')
        self.years = years
        self.schedule_enabled = settings.cash_cow_worker_enabled
        self.root = settings.log_path / 'market-data' / 'cash-cow' / str(years)
        self.root.mkdir(parents=True, exist_ok=True)
        self.source_factory = source_factory
        self.client_factory = client_factory or (lambda: httpx.Client(
            timeout=httpx.Timeout(25, connect=10), trust_env=True,
            follow_redirects=False, headers={'User-Agent': 'Mozilla/5.0 ShowAllOpts/1.0'}))
        self.stop_event = threading.Event()
        self.worker = None
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
                INSERT OR IGNORE INTO state VALUES (1, '{}');
                CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / 'screen.sqlite3', timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def read_state(self):
        with self.db() as db:
            return json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])

    def write_state(self, state):
        with self.db() as db:
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

    def cancel_path(self, state):
        identifier = state.get('run_id', '')
        if len(identifier) == 32 and all(c in '0123456789abcdef' for c in identifier):
            return self.root / ('cancel-' + identifier)
        return None

    def status(self):
        from app.services.cash_cow_schedule import next_run
        with self.publication_lock():
            state = self.read_state()
            running = self.running()
        marker = self.cancel_path(state)
        return dict(years=self.years, next_run_at=next_run().isoformat() if self.schedule_enabled else None,
                    schedule_time='21:00', running=running, cancelling=bool(running and marker and marker.exists()),
                    stage=('上次更新中断，请重新更新' if not running and state.get('active')
                           else state.get('stage', '尚未筛选')),
                    processed=state.get('processed', 0), total=state.get('total'),
                    last_attempt_at=state.get('last_attempt_at'), last_success_at=state.get('last_success_at'),
                    previous_success_at=state.get('previous_success_at'),
                    previous_previous_success_at=state.get('previous_previous_success_at'),
                    error=state.get('error'))

    def latest(self):
        status = self.status()
        with self.db() as db:
            row = db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
        result = json.loads(row[0]) if row else dict(snapshot=None, items=[])
        return dict(result, status=status, years=self.years)

    def publication_lock(self):
        return portalocker.Lock(str(self.root / 'publication.lock'), timeout=5)

    def cancel(self):
        with self.publication_lock():
            state = self.read_state()
            if not self.running() or not state.get('active'):
                return False
            marker = self.cancel_path(state)
            if marker is None:
                return False
            marker.touch()
            return True

    def refresh(self, background=True, *, moment=None, slot=None):
        if self.stop_event.is_set():
            return False
        lock = self.acquire()
        if lock is None:
            return False
        moment = moment or now()
        state = self.read_state()
        state.update(run_id=uuid.uuid4().hex, active=True, stage='读取全A股股票池', phase='读取全A股股票池',
                     processed=0, total=None, error=None, last_attempt_at=moment.isoformat(),
                     current_scheduled_slot=slot)
        if slot:
            state.update(scheduled_slot=slot, scheduled_attempt_at=moment.isoformat())
        try:
            self.write_state(state)
            if background:
                self.worker = threading.Thread(target=self._collect, args=(lock, state, moment),
                                               name='cash-cow-screen', daemon=True)
                self.worker.start()
            else:
                self._collect(lock, state, moment)
        except BaseException:
            state.update(active=False, stage='启动失败', error='未能启动筛选，请重试。')
            self.write_state(state)
            lock.release()
            raise
        return True

    def _collect(self, lock, state, moment):
        from app.services.cash_cow_source import EastmoneyCashCowSource, CashCowSourceError
        marker = self.cancel_path(state)

        def check_cancel():
            if self.stop_event.is_set() or marker.exists():
                raise CollectionCancelled()

        def progress(stage, processed=None, total=None):
            check_cancel()
            if processed is not None:
                state['phase'] = stage
                state['stage'] = stage
                state['processed'] = processed
            else:
                state['stage'] = state.get('phase', '读取公开数据') + ' · ' + stage
            if total is not None:
                state['total'] = total
            self.write_state(state)

        try:
            end_year = moment.astimezone(CHINA).year - 1
            start_year = end_year - self.years + 1
            with self.client_factory() as client:
                source = (self.source_factory or EastmoneyCashCowSource)(client, check_cancel=check_cancel,
                                                                           as_of=moment.astimezone(CHINA).date(),
                                                                           progress=progress,
                                                                           cache_dir=self.root.parent / 'source-cache')
                stocks = source.universe()
                if not stocks:
                    raise CashCowSourceError('股票池为空，未替换已有结果。')
                universe = {stock['code']: stock for stock in stocks}
                if len(universe) != len(stocks):
                    raise CashCowSourceError('股票池代码重复，未替换已有结果。')
                pending = dict(universe)
                annual = {code: [] for code in universe}
                excluded = insufficient = 0
                # Newest-first pruning reduces requests; every exclusion has a
                # demonstrated failed rule, not an absent source observation.
                for year in range(end_year, start_year - 1, -1):
                    if not pending:
                        break
                    progress(f'核对{year}年分红与财报', len(stocks) - len(pending), len(stocks))
                    dividends = source.dividends(year, codes=list(pending))
                    eligible = []
                    for code in list(pending):
                        cash = (dividends.get(code) or {}).get('cash_dividend')
                        if numeric(cash) and cash <= 0:
                            excluded += 1
                            pending.pop(code)
                        else:
                            eligible.append(code)
                    financials = source.financials(year, codes=eligible) if eligible else {}
                    for code in eligible:
                        outcome, row = evaluate_year(year, dividends.get(code), financials.get(code))
                        if outcome == 'eligible':
                            annual[code].append(row)
                        else:
                            pending.pop(code)
                            excluded += outcome == 'excluded'
                            insufficient += outcome == 'insufficient'
                items = []
                for code, stock in pending.items():
                    progress(f'核对{stock["name"]}的{self.years}年股息率', excluded + insufficient + len(items), len(stocks))
                    dps = source.per_share_dividends(code, start_year, end_year)
                    prices = source.prices(code, start_year, end_year)
                    outcome, item = finish_stock(stock, sorted(annual[code], key=lambda r: r['year']), dps, prices, self.years)
                    if outcome == 'matched':
                        item.update(source.current_dividend_yield(code))
                        items.append(item)
                    else:
                        excluded += outcome == 'excluded'
                        insufficient += outcome == 'insufficient'
                check_cancel()
                items.sort(key=lambda row: (-row['average_yield'], row['code']))
                updated_at = now().isoformat()
                snapshot = dict(as_of=moment.astimezone(CHINA).date().isoformat(), updated_at=updated_at,
                                previous_updated_at=state.get('last_success_at'),
                                previous_previous_updated_at=state.get('previous_success_at'),
                                years=self.years, start_year=start_year, end_year=end_year, universe_count=len(stocks),
                                evaluated_count=excluded + len(items), excluded_count=excluded,
                                insufficient_count=insufficient, matched_count=len(items),
                                source='东方财富公开数据接口', rule_version=RULE_VERSION)
                payload = json.dumps(dict(snapshot=snapshot, items=items), ensure_ascii=False, allow_nan=False)
                # Cancellation acceptance and publication share a cross-process
                # lock: an accepted cancellation cannot publish new results.
                with self.publication_lock():
                    check_cancel()
                    success_state = dict(state, active=False, last_success_at=updated_at,
                                         previous_success_at=state.get('last_success_at'),
                                         previous_previous_success_at=state.get('previous_success_at'),
                                         processed=len(stocks), total=len(stocks), stage='筛选完成')
                    if state.get('current_scheduled_slot'):
                        success_state['scheduled_success_slot'] = state['current_scheduled_slot']
                    with self.db() as db:
                        db.execute('INSERT OR REPLACE INTO snapshot VALUES(1,?)', (payload,))
                        db.execute('UPDATE state SET payload=? WHERE id=1',
                                   (json.dumps(success_state, ensure_ascii=False),))
                    # Only a committed snapshot advances successful-update history.
                    state.clear()
                    state.update(success_state)
        except CollectionCancelled:
            state.update(stage='已取消，保留上次结果')
        except Exception as exc:
            LOG.exception('Cash cow screen failed: %s', type(exc).__name__)
            state.update(stage='更新失败，保留上次结果',
                         error=str(exc) if isinstance(exc, CashCowSourceError) else '筛选暂未完成，请稍后重试。')
        finally:
            state['active'] = False
            with self.publication_lock():
                try:
                    self.write_state(state)
                finally:
                    marker.unlink(missing_ok=True)
                    lock.release()

    def stop(self):
        self.stop_event.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=30)
