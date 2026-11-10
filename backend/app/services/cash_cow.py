"""Shared 3/5/10-fiscal-year cash-cow screen, independent of the source MySQL DB.

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


PERIODS = (3, 5, 10)


class CashCowScreen:
    """A period-specific view of one cross-process collection and publication."""
    def __init__(self, settings, *, years=10, source_factory=None, client_factory=None):
        if years not in PERIODS:
            raise ValueError('仅支持近3年、5年或10年')
        self.years = years
        self.schedule_enabled = settings.cash_cow_worker_enabled
        self.root = settings.log_path / 'market-data' / 'cash-cow'
        self.root.mkdir(parents=True, exist_ok=True)
        self.source_factory = source_factory
        self.client_factory = client_factory or (lambda: httpx.Client(
            timeout=httpx.Timeout(25, connect=10), trust_env=True,
            follow_redirects=False, headers={'User-Agent': 'Mozilla/5.0 ShowAllOpts/1.0'}))
        self.stop_event = threading.Event()
        self.worker = None
        with self.publication_lock(), self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
                INSERT OR IGNORE INTO state VALUES (1, '{}');
                CREATE TABLE IF NOT EXISTS snapshots (years INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            ''')
            # Upgrade without dropping an existing period's successful snapshot.
            # Legacy files remain untouched; only missing unified rows are seeded.
            legacy_success = {}
            for period in PERIODS:
                path = self.root / str(period) / 'screen.sqlite3'
                if (path.is_file() and not db.execute(
                        'SELECT 1 FROM snapshots WHERE years=?', (period,)).fetchone()):
                    legacy = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)
                    try:
                        row = legacy.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
                        if row:
                            db.execute('INSERT INTO snapshots VALUES (?,?)', (period, row[0]))
                            if legacy.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='state'").fetchone():
                                old_state = legacy.execute('SELECT payload FROM state WHERE id=1').fetchone()
                                if old_state:
                                    legacy_success[period] = json.loads(old_state[0]).get('scheduled_success_slot')
                    finally:
                        legacy.close()
            # A completely successful legacy month should not be re-collected on
            # upgrade. Partial legacy completion still requires one unified run.
            slots = set(legacy_success.values())
            if len(legacy_success) == len(PERIODS) and len(slots) == 1 and None not in slots:
                saved = db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0]
                if json.loads(saved) == {}:
                    db.execute('UPDATE state SET payload=? WHERE id=1',
                               (json.dumps(dict(scheduled_success_slot=slots.pop())),))

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

    def _status(self, state, result):
        from app.services.cash_cow_schedule import next_run
        running = self.running()
        marker = self.cancel_path(state)
        snapshot = result.get('snapshot') or {}
        return dict(years=self.years, periods=list(PERIODS), collection_years=max(PERIODS),
                    run_id=state.get('run_id'),
                    next_run_at=next_run().isoformat() if self.schedule_enabled else None,
                    schedule_time='21:00', running=running, cancelling=bool(running and marker and marker.exists()),
                    stage=('上次更新中断，请重新更新' if not running and state.get('active')
                           else state.get('stage', '尚未筛选')),
                    processed=state.get('processed', 0), total=state.get('total'),
                    last_attempt_at=state.get('last_attempt_at'),
                    last_success_at=snapshot.get('updated_at'),
                    previous_success_at=snapshot.get('previous_updated_at'),
                    previous_previous_success_at=snapshot.get('previous_previous_updated_at'),
                    error=state.get('error'))

    def _read_result(self, db):
        row = db.execute('SELECT payload FROM snapshots WHERE years=?', (self.years,)).fetchone()
        return json.loads(row[0]) if row else dict(snapshot=None, items=[])

    def status(self):
        with self.publication_lock(), self.db() as db:
            state = json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
            return self._status(state, self._read_result(db))

    def latest(self):
        # Read snapshot and state under the publication lock: the response never
        # combines a new completion status with the previous run's snapshot.
        with self.publication_lock(), self.db() as db:
            state = json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
            result = self._read_result(db)
            return dict(result, status=self._status(state, result), years=self.years)

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
        state = None
        try:
            moment = moment or now()
            if moment.tzinfo is None:
                raise ValueError('Collection requires a timezone-aware datetime')
            state = self.read_state()
            state.update(run_id=uuid.uuid4().hex, active=True, stage='读取全A股股票池（统一更新3/5/10年）',
                         phase='读取全A股股票池', processed=0, total=None, error=None,
                         last_attempt_at=moment.isoformat(), current_scheduled_slot=slot)
            if slot:
                state.update(scheduled_slot=slot, scheduled_attempt_at=moment.isoformat())
            self.write_state(state)
            if background:
                self.worker = threading.Thread(target=self._collect, args=(lock, state, moment),
                                               name='cash-cow-screen', daemon=True)
                self.worker.start()
            else:
                self._collect(lock, state, moment)
        except BaseException:
            try:
                if state is not None:
                    state.update(active=False, stage='启动失败', error='未能启动筛选，请重试。')
                    self.write_state(state)
            finally:
                lock.release()
            raise
        return True

    def _collect(self, lock, state, moment):
        from app.services.cash_cow_source import EastmoneyCashCowSource, CashCowSourceError
        marker = self.cancel_path(state)
        published = False

        def check_cancel():
            if self.stop_event.is_set() or marker.exists():
                raise CollectionCancelled()

        def progress(stage, processed=None, total=None):
            check_cancel()
            if processed is not None:
                state.update(phase=stage, stage=stage, processed=processed)
            else:
                state['stage'] = state.get('phase', '读取公开数据') + ' · ' + stage
            if total is not None:
                state['total'] = total
            self.write_state(state)

        try:
            end_year = moment.astimezone(CHINA).year - 1
            with self.client_factory() as client:
                source = (self.source_factory or EastmoneyCashCowSource)(client, check_cancel=check_cancel,
                    as_of=moment.astimezone(CHINA).date(), progress=progress,
                    cache_dir=self.root / 'source-cache')
                stocks = source.universe()
                if not stocks:
                    raise CashCowSourceError('股票池为空，未替换已有结果。')
                universe = {stock['code']: stock for stock in stocks}
                if len(universe) != len(stocks):
                    raise CashCowSourceError('股票池代码重复，未替换已有结果。')
                pending = dict(universe)
                annual = {code: [] for code in universe}
                candidates = {period: set() for period in PERIODS}
                outcomes = {period: {} for period in PERIODS}

                def reject(code, outcome, depth):
                    pending.pop(code)
                    # An older failure belongs only to windows containing that
                    # year. Shorter windows' survivors are already frozen.
                    for period in PERIODS:
                        if period >= depth:
                            outcomes[period][code] = outcome

                # Cheap, newest-first annual predicates are evaluated once. At
                # each boundary freeze the eligible set, then prune only longer
                # windows. Never prune by a shorter window's average yield.
                for depth in range(1, max(PERIODS) + 1):
                    if not pending:
                        break
                    year = end_year - depth + 1
                    progress(f'初筛{year}年分红与财报（3/5/10年共用）',
                             len(stocks) - len(pending), len(stocks))
                    dividends = source.dividends(year, codes=list(pending))
                    for code in list(pending):
                        cash = (dividends.get(code) or {}).get('cash_dividend')
                        if numeric(cash) and cash <= 0:
                            reject(code, 'excluded', depth)
                    financials = source.financials(year, codes=list(pending)) if pending else {}
                    for code in list(pending):
                        outcome, row = evaluate_year(year, dividends.get(code), financials.get(code))
                        if outcome == 'eligible':
                            annual[code].append(row)
                        else:
                            reject(code, outcome, depth)
                    if depth in candidates:
                        candidates[depth] = set(pending)

                items = {period: [] for period in PERIODS}
                survivors = set().union(*candidates.values())
                for index, code in enumerate(sorted(survivors)):
                    check_cancel()
                    stock = universe[code]
                    periods = [period for period in PERIODS if code in candidates[period]]
                    progress(f'核对候选{stock["name"]}的股息率（共用分红与行情）',
                             len(stocks) - len(survivors) + index, len(stocks))
                    dps = source.per_share_dividends_by_period(code, end_year, periods)
                    usable = []
                    for period in periods:
                        window = range(end_year - period + 1, end_year + 1)
                        if all(numeric(dps[period].get(year)) and dps[period][year] > 0 for year in window):
                            usable.append(period)
                        else:
                            outcomes[period][code] = 'insufficient'
                    # Invalid share basis needs no expensive historical prices.
                    if not usable:
                        continue
                    prices = source.prices(code, end_year - max(usable) + 1, end_year)
                    matched = []
                    for period in usable:
                        rows = sorted((row for row in annual[code] if row['year'] > end_year - period),
                                      key=lambda row: row['year'])
                        outcome, item = finish_stock(stock, rows, dps[period], prices, period)
                        outcomes[period][code] = outcome
                        if outcome == 'matched':
                            matched.append(item)
                            items[period].append(item)
                    if matched:
                        current = source.current_dividend_yield(code)
                        for item in matched:
                            item.update(current)
                check_cancel()
                updated_at = now().isoformat()
                # One SQLite transaction publishes all periods and shared state.
                # Prepare/serialize outside the publication lock so cancellation
                # can still be accepted during potentially expensive encoding.
                with self.db() as db:
                    previous = {period: json.loads(payload).get('snapshot') or {} for period, payload in
                                db.execute('SELECT years, payload FROM snapshots')}
                payloads = {}
                for period in PERIODS:
                    check_cancel()
                    if len(outcomes[period]) != len(stocks):
                        raise RuntimeError('Incomplete period classification')
                    items[period].sort(key=lambda row: (-row['average_yield'], row['code']))
                    excluded = sum(value == 'excluded' for value in outcomes[period].values())
                    insufficient = sum(value == 'insufficient' for value in outcomes[period].values())
                    old = previous.get(period, {})
                    snapshot = dict(as_of=moment.astimezone(CHINA).date().isoformat(), updated_at=updated_at,
                        run_id=state['run_id'], previous_updated_at=old.get('updated_at'),
                        previous_previous_updated_at=old.get('previous_updated_at'),
                        years=period, start_year=end_year - period + 1, end_year=end_year,
                        universe_count=len(stocks), evaluated_count=excluded + len(items[period]),
                        excluded_count=excluded, insufficient_count=insufficient, matched_count=len(items[period]),
                        source='东方财富公开数据接口', rule_version=RULE_VERSION)
                    payloads[period] = json.dumps(dict(snapshot=snapshot, items=items[period]),
                                                 ensure_ascii=False, allow_nan=False)
                with self.publication_lock():
                    check_cancel()
                    success_state = dict(state, active=False, last_success_at=updated_at,
                        previous_success_at=state.get('last_success_at'),
                        previous_previous_success_at=state.get('previous_success_at'),
                        processed=len(stocks), total=len(stocks), stage='3/5/10年筛选完成')
                    if state.get('current_scheduled_slot'):
                        success_state['scheduled_success_slot'] = state['current_scheduled_slot']
                    with self.db() as db:
                        for period, payload in payloads.items():
                            db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?)', (period, payload))
                        db.execute('UPDATE state SET payload=? WHERE id=1',
                                   (json.dumps(success_state, ensure_ascii=False),))
                    state.clear()
                    state.update(success_state)
                    published = True
        except CollectionCancelled:
            state.update(stage='已取消，保留上次结果')
        except Exception as exc:
            LOG.exception('Cash cow screen failed: %s', type(exc).__name__)
            state.update(stage='更新失败，保留上次结果',
                         error=str(exc) if isinstance(exc, CashCowSourceError) else '筛选暂未完成，请稍后重试。')
        finally:
            state['active'] = False
            try:
                if not published:
                    with self.publication_lock():
                        self.write_state(state)
            finally:
                try:
                    marker.unlink(missing_ok=True)
                finally:
                    lock.release()

    def stop(self):
        self.stop_event.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=30)
