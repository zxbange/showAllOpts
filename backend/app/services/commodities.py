"""Independent persisted quotes. Daily Shanghai scheduling and manual collection share a cross-process lock."""
from contextlib import contextmanager
from pathlib import Path
from datetime import date, datetime, timezone, timedelta
from time import monotonic
from zoneinfo import ZoneInfo
import logging
import json
import sqlite3
import threading
import httpx
import portalocker
from app.services.commodity_source import (HOME, CATALOG, START, fetch, home_date_reference,
    public_report_date, parse_home, QuoteError, QuoteIncomplete, explicit_home_date,
    recovery_links, parse_recovery, diagnostic_reason)

LOG = logging.getLogger(__name__)
CHINA = ZoneInfo('Asia/Shanghai')

def now():
    return datetime.now(timezone.utc)

def latest_slot(moment):
    local = moment.astimezone(CHINA)
    for hour in (18, 11):
        slot = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if local >= slot:
            return slot
    return (local - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)

SEED=Path(__file__).resolve().parents[1]/'data/a00_changjiang.json'

class Commodities:
    tracks_failed_targets = True
    max_requests = 24
    max_collection_seconds = 120
    schedule_label = '每天 11:00、18:00 · 北京时间'
    slot_for = staticmethod(latest_slot)

    def __init__(self,settings):
        self.root=settings.log_path/'market-data'/'commodities'
        self.root.mkdir(parents=True,exist_ok=True)
        self.thread=None;self.scheduler=None;self.stopping=threading.Event()
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS quotes(day TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute("INSERT OR IGNORE INTO state VALUES(1,'{}')")
            db.execute('CREATE TABLE IF NOT EXISTS schedule_meta(id INTEGER PRIMARY KEY, initialized_at TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO schedule_meta VALUES(1,?)', (now().isoformat(),))
            db.execute('CREATE TABLE IF NOT EXISTS recovery_attempts(day TEXT PRIMARY KEY, attempted_at TEXT NOT NULL, result TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS failed_targets(day TEXT PRIMARY KEY, reason TEXT NOT NULL, attempted_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS schedule_runs(slot TEXT PRIMARY KEY, status TEXT NOT NULL, finished_at TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS a00_recovery(id INTEGER PRIMARY KEY, start_day TEXT NOT NULL, origin TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS a00_source_checks(url TEXT PRIMARY KEY, checked_at TEXT NOT NULL, result TEXT NOT NULL)')
            for row in json.loads(SEED.read_text())['rows']:
                # Keep research evidence but do not expose unverified range values.
                if row['rangeEvidence']!='page':row=dict(row,low=None,high=None)
                db.execute('INSERT OR IGNORE INTO quotes VALUES(?,?)',(row['quoteDate'],json.dumps(row,ensure_ascii=False)))
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.root/'history.sqlite3',timeout=10)
        try:
            with c:yield c
        finally:c.close()
    def lock(self):
        lock=portalocker.Lock(str(self.root/'collection.lock'),timeout=0,flags=portalocker.LOCK_EX|portalocker.LOCK_NB)
        try:lock.acquire();return lock
        except portalocker.exceptions.LockException:return None
    def status(self):
        with self.db() as db:
            state=json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
            if self.tracks_failed_targets:
                pending=[dict(day=row[0],reason=row[1]) for row in db.execute('SELECT day,reason FROM failed_targets ORDER BY day LIMIT 20')]
                count=db.execute('SELECT count(*) FROM failed_targets').fetchone()[0]
                state.update(pendingTargets=pending, pendingCount=count)
        lock=self.lock();running=lock is None
        if lock:lock.release()
        return dict(state,running=running,automatic=True,schedule=self.schedule_label)
    def state(self,**values):
        with self.db() as db:db.execute('UPDATE state SET payload=? WHERE id=1',(json.dumps(values,ensure_ascii=False),))
    def history(self):
        with self.db() as db:rows=[json.loads(r[0]) for r in db.execute('SELECT payload FROM quotes ORDER BY day')]
        return dict(instrument='A00铝',market='长江现货',publisher='长江有色金属网（CCMN）',unit='元/吨',start='2026-10-08',rows=rows,status=self.status())
    def start(self):
        if self.scheduler and self.scheduler.is_alive():
            return
        self.scheduler = threading.Thread(target=self.schedule_loop, daemon=True, name='commodity-scheduler')
        self.scheduler.start()

    def schedule_loop(self):
        while not self.stopping.is_set():
            try:
                self.refresh(slot=self.slot_for(now()))
            except Exception as exc:
                LOG.error('Commodity scheduler failed type=%s', type(exc).__name__)
            self.stopping.wait(5)

    def refresh(self, *, slot=None):
        if self.stopping.is_set():return False
        lock=self.lock()
        if not lock:return False
        try:
            if slot is not None:
                # Claim only after acquiring the same lock used by manual collection.
                # An interrupted claimed slot is not silently replayed after restart.
                with self.db() as db:
                    floor = datetime.fromisoformat(db.execute('SELECT initialized_at FROM schedule_meta WHERE id=1').fetchone()[0])
                    if slot < floor:
                        lock.release()
                        return False
                    interrupted = db.execute("UPDATE schedule_runs SET status='interrupted', finished_at=? WHERE status='running'", (now().isoformat(),)).rowcount
                    if interrupted:
                        self_state = json.dumps(dict(stage='上次定时采集中断', outcome='interrupted', checkedAt=now().isoformat(), error='历史保留，可手动补采或等待下个采集时点'), ensure_ascii=False)
                        db.execute('UPDATE state SET payload=? WHERE id=1', (self_state,))
                        LOG.warning('Commodity scheduled collection interrupted count=%s', interrupted)
                    claimed = db.execute('INSERT OR IGNORE INTO schedule_runs(slot,status) VALUES(?,?)', (slot.isoformat(), 'running')).rowcount
                    if not claimed:
                        lock.release()
                        return False
            self.state(stage='读取最新现货报价',error=None,trigger='scheduled' if slot else 'manual',slot=slot.isoformat() if slot else None)
            LOG.info('Commodity collection start trigger=%s slot=%s', 'scheduled' if slot else 'manual', slot.isoformat() if slot else '-')
            self.thread=threading.Thread(target=self.collect,args=(lock,slot),daemon=True,name='commodities')
            self.thread.start()
        except BaseException:lock.release();raise
        return True
    def recovery_plan(self, today):
        """Persist the initial floor before saving a new quote, so gaps cannot hide.

        Existing failed observation dates remain in scope. Otherwise begin at the
        latest stored quote, or the persisted installation date for an empty DB.
        """
        end = today.isoformat()
        with self.db() as db:
            latest = db.execute('SELECT max(day) FROM quotes WHERE day<=?', (end,)).fetchone()[0]
            installed = datetime.fromisoformat(db.execute(
                'SELECT initialized_at FROM schedule_meta WHERE id=1').fetchone()[0]).astimezone(CHINA).date().isoformat()
            failed = db.execute('SELECT min(day) FROM failed_targets WHERE day>=? AND day<=?', (START, end)).fetchone()[0]
            floor = max(START, min(latest or installed, end))
            if failed:
                floor = min(floor, failed)
            db.execute('INSERT OR IGNORE INTO a00_recovery VALUES(1,?,?)',
                       (floor, 'latest_quote' if latest else 'installation_date'))
            floor, origin = db.execute('SELECT start_day,origin FROM a00_recovery WHERE id=1').fetchone()
            existing = {r[0] for r in db.execute('SELECT day FROM quotes WHERE day<=?', (end,))}
            cursor = date.fromisoformat(floor)
            while cursor <= today:
                day = cursor.isoformat()
                if day not in existing:
                    db.execute('INSERT OR IGNORE INTO failed_targets VALUES(?,?,?)',
                               (day, 'not_yet_observed', now().isoformat()))
                cursor += timedelta(days=1)
            missing = {r[0] for r in db.execute('SELECT day FROM failed_targets WHERE day>=? AND day<=?', (floor, end)) if r[0] not in existing}
        return missing, dict(dateFrom=floor, dateTo=end, origin=origin,
                             pendingCount=len(missing), recoveredCount=0,
                             articlesChecked=0, requestCount=0, deferredArticles=0,
                             sourceLimited=True)

    def collect(self,lock,slot=None):
        failures = fetched = parsed = inserted = changed = requests = 0
        began = monotonic()
        missing = set()
        limited = False
        current_verified = False
        target_result = 'pending'
        home_quote = None
        recovery = None
        finished = False
        started = now().isoformat()
        today = now().astimezone(CHINA).date()
        target_day = slot.astimezone(CHINA).date() if slot else today
        target_mode = 'complete_spot' if slot is not None and slot.astimezone(CHINA).hour == 18 else 'current_spot'

        def should_stop():
            return self.stopping.is_set() or requests >= self.max_requests or monotonic() - began >= self.max_collection_seconds

        def status(stage, error=None, outcome='running'):
            with self.db() as db:
                latest = db.execute('SELECT max(day) FROM quotes').fetchone()[0]
            if recovery is not None:
                recovery.update(pendingCount=len(missing), requestCount=requests)
            self.state(stage=stage, outcome=outcome, trigger='scheduled' if slot else 'manual',
                       slot=slot.isoformat() if slot else None, startedAt=started,
                       checkedAt=now().isoformat() if outcome != 'running' else None,
                       recovery=recovery, updated=parsed, pagesFetched=fetched, pagesParsed=parsed, mode='current',
                       inserted=inserted, changed=changed, failures=failures,
                       latestQuoteDate=latest, currentVerified=current_verified,
                       targetDate=target_day.isoformat(), targetMode=target_mode, targetResult=target_result,
                       sourceQuoteDate=home_quote['quoteDate'] if home_quote else None,
                       stale=latest is None or latest < today.isoformat(),
                       noNew=inserted == 0 and changed == 0, error=error)

        def read(client, url, catalog=False):
            nonlocal fetched, requests, limited
            if should_stop():
                limited = True
                raise QuoteError('本次采集达到上限')
            requests += 1
            content = fetch(client, url, catalog)
            fetched += 1
            return content

        def save(row):
            nonlocal parsed, inserted, changed
            with self.db() as db:
                if self.stopping.is_set():
                    return
                previous = db.execute('SELECT payload FROM quotes WHERE day=?', (row['quoteDate'],)).fetchone()
                # Re-fetching unchanged prices is not a new or modified quote.
                comparable = ('low', 'high', 'mean', 'change', 'rangeEvidence')
                old = json.loads(previous[0]) if previous else None
                # Historical recovery never rewrites an existing quote. Only the
                # current actual date may be revised from the live homepage.
                differs = old is None or (row['quoteDate'] == today.isoformat() and any(old.get(k) != row.get(k) for k in comparable))
                if differs:
                    db.execute('INSERT INTO quotes VALUES(?,?) ON CONFLICT(day) DO UPDATE SET payload=excluded.payload',
                               (row['quoteDate'], json.dumps(row, ensure_ascii=False)))
                db.execute('DELETE FROM failed_targets WHERE day=?', (row['quoteDate'],))
                db.execute('DELETE FROM recovery_attempts WHERE day=?', (row['quoteDate'],))
            missing.discard(row['quoteDate'])
            parsed += 1
            inserted += int(old is None)
            changed += int(old is not None and differs)
            return True

        try:
            missing, recovery = self.recovery_plan(today)
            with self.db() as db:
                db.execute('INSERT INTO failed_targets VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET reason=excluded.reason, attempted_at=excluded.attempted_at',
                           (target_day.isoformat(), 'interrupted_or_pending', started))
            with httpx.Client(timeout=httpx.Timeout(15,connect=8),trust_env=False,follow_redirects=False) as client:
                status('读取最新现货报价')
                home = None
                try:
                    home = read(client, HOME, True)
                    quote_date = explicit_home_date(home)
                    date_url = HOME
                    if quote_date is None:
                        date_url = home_date_reference(home)
                        quote_date = public_report_date(read(client, date_url))
                    home_quote = parse_home(home, today=today, report_date=quote_date, report_url=date_url)
                    save(home_quote)
                    current_verified = not self.stopping.is_set() and quote_date == target_day
                    target_result = ('current' if quote_date == target_day else
                                     'sourceNotPublished' if quote_date < target_day else 'targetDateUnavailable')
                except Exception as exc:
                    target_result = ('partial' if isinstance(exc, QuoteIncomplete) else
                                     'unverified' if isinstance(exc, (QuoteError, ValueError)) else 'sourceReadFailed')
                    failures += 1
                    LOG.warning('Commodity current quote failed type=%s', type(exc).__name__)
                # Query only when a real interval gap remains. Calendar dates
                # are candidates, not assertions of trading days or publication.
                historical = {day for day in missing if day < today.isoformat()}
                if historical and not should_stop():
                    candidates = recovery_links(home or '', start=recovery['dateFrom'],
                                                end=today.isoformat(), base=HOME)
                    try:
                        catalog = read(client, CATALOG, True)
                        candidates += recovery_links(catalog, start=recovery['dateFrom'], end=today.isoformat())
                    except Exception as exc:
                        failures += 1
                        LOG.warning('Commodity recovery catalog failed type=%s', type(exc).__name__)
                    with self.db() as db:
                        for url in dict.fromkeys(candidates):
                            db.execute("INSERT OR IGNORE INTO a00_source_checks VALUES(?,'','pending')", (url,))
                        # Retain discovered links across catalog rotation/restarts;
                        # never let one unavailable article starve all later ones.
                        candidates = [r[0] for r in db.execute(
                            'SELECT url FROM a00_source_checks ORDER BY checked_at,url')]
                    attempted = 0
                    for url in candidates:
                        if not historical or should_stop():
                            limited = bool(historical and should_stop())
                            break
                        result = 'no_matching_quote'
                        try:
                            row = parse_recovery(read(client, url), url, today=today)
                            if self.stopping.is_set():
                                result = 'interrupted'
                            elif row['quoteDate'] in historical:
                                if save(row):
                                    recovery['recoveredCount'] += 1
                                    historical.discard(row['quoteDate'])
                                    result = 'recovered'
                            # Out-of-window, existing and future rows cannot change
                            # the historical data or the recovery floor.
                        except Exception as exc:
                            result = diagnostic_reason(exc)
                            if not isinstance(exc, QuoteError):
                                failures += 1
                            LOG.warning('Commodity recovery article type=%s reason=%s', type(exc).__name__, result)
                        attempted += 1
                        recovery['articlesChecked'] += 1
                        with self.db() as db:
                            db.execute('UPDATE a00_source_checks SET checked_at=?,result=? WHERE url=?',
                                       (now().isoformat(), result, url))
                    recovery['deferredArticles'] = len(candidates) - attempted if historical else 0
                    recovery['limited'] = limited
                    with self.db() as db:
                        for day in historical:
                            db.execute('INSERT INTO recovery_attempts VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET attempted_at=excluded.attempted_at,result=excluded.result',
                                       (day, now().isoformat(), 'deferred' if limited else 'no_verified_public_quote'))
                    LOG.info('Commodity recovery range=%s..%s checked=%s recovered=%s pending=%s limited=%s',
                             recovery['dateFrom'], recovery['dateTo'], recovery['articlesChecked'],
                             recovery['recoveredCount'], len(missing), limited)
            finished = True
        except Exception as exc:
            failures += 1
            LOG.error('Commodity collection failed type=%s', type(exc).__name__)
        finally:
            try:
                if self.stopping.is_set():
                    target_result = 'interrupted'
                    outcome, stage, error = 'interrupted', '采集中断', '历史保留，尚未完成采集'
                elif not finished or not parsed:
                    outcome, stage, error = 'failed', '采集失败', '公开来源读取或核验失败，历史保留'
                elif target_result in ('sourceNotPublished', 'targetDateUnavailable'):
                    outcome, stage, error = 'no_quote', '来源尚无目标日报价', None
                elif failures or not current_verified or any(day < today.isoformat() for day in missing):
                    outcome, stage = 'partial', '部分报价已更新'
                    error = ('最新报价未核验，已有历史保留' if not current_verified else
                             '本次达到采集上限，未完成日期下次继续' if limited else
                             '公开来源尚未提供全部缺失日期的可核验报价')
                else:
                    outcome, error = 'completed', None
                    stage = '采集成功' if inserted or changed else '暂无新报价'
                if not self.stopping.is_set() and not current_verified and target_result in ('sourceReadFailed', 'partial', 'unverified'):
                    stage = {'sourceReadFailed':'目标日报价读取失败', 'partial':'目标日报价字段不完整', 'unverified':'目标日报价无法核验'}[target_result]
                with self.db() as db:
                    if current_verified:
                        db.execute('DELETE FROM failed_targets WHERE day=?', (target_day.isoformat(),))
                    else:
                        # This is a failed observation target, not proof that the
                        # source published a quote (nor proof of a holiday).
                        db.execute('UPDATE failed_targets SET reason=? WHERE day=?',
                                   (target_result, target_day.isoformat()))
                status(stage, error, outcome)
                if slot is not None:
                    with self.db() as db:
                        db.execute('UPDATE schedule_runs SET status=?,finished_at=? WHERE slot=?',
                                   (outcome, now().isoformat(), slot.isoformat()))
                LOG.info('Commodity collection finish outcome=%s fetched=%s parsed=%s inserted=%s changed=%s failures=%s latest=%s slot=%s',
                         outcome, fetched, parsed, inserted, changed, failures,
                         self.status().get('latestQuoteDate'), slot.isoformat() if slot else '-')
            finally:
                lock.release()
    def stop(self):
        self.stopping.set()
        if self.scheduler:self.scheduler.join(timeout=2)
        if self.thread:self.thread.join(timeout=2)
