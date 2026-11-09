"""CCI has its own model, history and daily 21:00 scheduling ledger."""
from datetime import datetime, timedelta
from pathlib import Path
import json
import logging
import threading
from time import monotonic
import httpx
from app.services.commodities import Commodities, CHINA, now
from app.services.cci_source import START, GRADES, SOURCE_URL, validate_rows, fetch_rows, safe_error

LOG = logging.getLogger(__name__)
SEED = Path(__file__).resolve().parents[1] / 'data/cci_coal.json'


def latest_slot(moment):
    local = moment.astimezone(CHINA)
    slot = local.replace(hour=21, minute=0, second=0, microsecond=0)
    return slot if local >= slot else slot - timedelta(days=1)


class CCICoal(Commodities):
    tracks_failed_targets = False
    schedule_label = '每天 21:00 · 北京时间，未完整取得时当日最多重试3次'
    retry_delay = timedelta(minutes=30)
    max_scheduled_attempts = 4
    slot_for = staticmethod(latest_slot)

    def __init__(self, settings):
        self.root = settings.log_path / 'market-data' / 'cci-coal'
        self.root.mkdir(parents=True, exist_ok=True)
        self.thread = self.scheduler = None
        self.stopping = threading.Event()
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS cci_quotes(day TEXT NOT NULL, grade TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(day,grade))')
            db.execute('CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute("INSERT OR IGNORE INTO state VALUES(1,?)", (json.dumps(dict(stage='待采集'), ensure_ascii=False),))
            db.execute('CREATE TABLE IF NOT EXISTS schedule_meta(id INTEGER PRIMARY KEY, initialized_at TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO schedule_meta VALUES(1,?)', (now().isoformat(),))
            db.execute('CREATE TABLE IF NOT EXISTS schedule_runs(slot TEXT PRIMARY KEY, status TEXT NOT NULL, finished_at TEXT)')
            if SEED.is_file():
                rows = validate_rows(json.loads(SEED.read_text(encoding='utf-8'))['rows'], today=now().astimezone(CHINA).date())
                for row in rows:
                    db.execute('INSERT OR IGNORE INTO cci_quotes VALUES(?,?,?)', (row['quoteDate'], row['grade'], json.dumps(row, ensure_ascii=False)))

    def history(self):
        with self.db() as db:
            rows = [json.loads(r[0]) for r in db.execute('SELECT payload FROM cci_quotes ORDER BY day,grade')]
        return dict(instrument='CCI动力煤', grades=GRADES, unit='元/吨', currency='CNY', start=START,
                    sourceUrl=SOURCE_URL, official=False, rows=rows, status=self.status(),
                    missingChanges=sum(r['change'] is None for r in rows))

    def refresh(self, *, slot=None):
        if slot is None:
            return super().refresh()
        if self.stopping.is_set():
            return False
        lock = self.lock()
        if not lock:
            return False
        launched = False
        try:
            observed = now()
            current = observed.astimezone(CHINA)
            slot = slot.astimezone(CHINA)
            with self.db() as db:
                # Recovery, migration-aware retry planning and the durable claim
                # share one lock with collection. Never treat a live old worker
                # as interrupted, or claim 21:00 immediately after recovering it.
                interrupted = db.execute("UPDATE schedule_runs SET status='interrupted', finished_at=? "
                                         "WHERE status='running'", (observed.isoformat(),)).rowcount
                if interrupted:
                    state = dict(stage='上次定时采集中断', outcome='interrupted',
                                 checkedAt=observed.isoformat(), error='历史保留，当日仍须等待重试间隔')
                    db.execute('UPDATE state SET payload=? WHERE id=1',
                               (json.dumps(state, ensure_ascii=False),))
                    LOG.warning('CCI scheduled collection interrupted count=%s', interrupted)
                if slot > current:
                    return False
                # Normalize legacy UTC and Shanghai timestamps without rounding
                # a near-midnight attempt into the following day. Existing 18:00
                # attempts count towards the same daily budget.
                candidates = db.execute('SELECT slot,status,finished_at FROM schedule_runs '
                                        'WHERE substr(slot,1,10) BETWEEN ? AND ?',
                                        ((current.date() - timedelta(days=1)).isoformat(),
                                         (current.date() + timedelta(days=1)).isoformat())).fetchall()
                runs = [(result, finished) for stamp, result, finished in candidates
                        if datetime.fromisoformat(stamp).astimezone(CHINA).date() == current.date()]
                if slot.date() < current.date() and runs:
                    # Before 21:00 the new schedule points at yesterday's 21:00.
                    # Do not manufacture that obsolete slot and replace today's
                    # existing 18:00 result with a misleading skipped state.
                    return False
                if slot.date() == current.date():
                    primary = current.replace(hour=21, minute=0, second=0, microsecond=0)
                    if current < primary:
                        return False
                    if any(result == 'completed' for result, _ in runs):
                        return False
                    if len(runs) >= self.max_scheduled_attempts:
                        return False
                    slot = primary
                    if runs:
                        if any(result not in {'empty', 'partial', 'failed', 'interrupted'} or not finished
                               for result, finished in runs):
                            return False
                        latest_finish = max(datetime.fromisoformat(finished) for _, finished in runs)
                        slot = max(primary, latest_finish.astimezone(CHINA) + self.retry_delay)
                        if slot.date() != current.date() or current < slot:
                            return False
                floor = datetime.fromisoformat(db.execute(
                    'SELECT initialized_at FROM schedule_meta WHERE id=1').fetchone()[0])
                if slot < floor:
                    return False
                claimed = db.execute('INSERT OR IGNORE INTO schedule_runs(slot,status) VALUES(?,?)',
                                     (slot.isoformat(), 'running')).rowcount
                if not claimed:
                    return False
            self.state(stage='正在查询当日报价', outcome='running', error=None, trigger='scheduled',
                       targetDate=slot.date().isoformat(), slot=slot.isoformat())
            LOG.info('CCI collection start trigger=scheduled slot=%s', slot.isoformat())
            self.thread = threading.Thread(target=self.collect, args=(lock, slot), daemon=True, name='cci-coal')
            self.thread.start()
            launched = True
            return True
        finally:
            if not launched:
                lock.release()

    def collect(self, lock, slot=None):
        began = monotonic()
        today = now().astimezone(CHINA).date()
        target = slot.astimezone(CHINA).date() if slot is not None else today
        day = target.isoformat()
        details = []
        count = inserted = changed = 0
        outcome = 'failed'
        trigger = 'scheduled' if slot else 'manual'
        context = dict(targetDate=day, trigger=trigger, slot=slot.isoformat() if slot else None)
        try:
            # A restart or a delayed worker may resume yesterday's scheduled slot.
            # CCI has no historical recovery: never query today's quotes and
            # record them as the outcome of an older scheduling target.
            if slot is not None and target < today:
                outcome = 'skipped'
                self.state(stage='过期定时时点已跳过，不补采历史', outcome=outcome,
                           **context, skippedAt=now().isoformat(), error=None,
                           observed=0, inserted=0, changed=0, grades=[],
                           elapsedMs=round((monotonic()-began)*1000))
                return
            self.state(stage='正在查询当日报价', **context, outcome='running', error=None)
            with httpx.Client(timeout=httpx.Timeout(25, connect=10), trust_env=False, follow_redirects=False) as client:
                rows = validate_rows(fetch_rows(client, day, day, should_stop=self.stopping.is_set, diagnostics=details), today=today)
            if any(row['quoteDate'] != day for row in rows):
                raise ValueError('Unexpected target date')
            if self.stopping.is_set():
                outcome = 'interrupted'
                return
            with self.db() as db:
                for row in rows:
                    previous = db.execute('SELECT payload FROM cci_quotes WHERE day=? AND grade=?', (day, row['grade'])).fetchone()
                    added = previous is None
                    updated = previous is not None and json.loads(previous[0]) != row
                    inserted += int(added)
                    changed += int(updated)
                    for detail in details:
                        if detail['grade'] == row['grade']:
                            detail.update(inserted=int(added), changed=int(updated))
                    if added or updated:
                        db.execute('INSERT INTO cci_quotes VALUES(?,?,?) ON CONFLICT(day,grade) DO UPDATE SET payload=excluded.payload', (day, row['grade'], json.dumps(row, ensure_ascii=False)))
                count = len(rows)
                errors = sum(bool(d['error']) for d in details)
                outcome = 'completed' if count == len(GRADES) else 'partial' if count else 'failed' if errors else 'empty'
                stage = {'completed': '当日三档报价已核验', 'partial': '当日报价部分可用',
                         'failed': '当日报价读取失败，历史保留', 'empty': '来源无当日报价，历史保留'}[outcome]
                state = dict(stage=stage, outcome=outcome, **context,
                             error='部分档位网络请求或响应解析失败，详见各档状态' if errors else None,
                             checkedAt=now().isoformat(), observed=count, inserted=inserted, changed=changed,
                             grades=details, elapsedMs=round((monotonic()-began)*1000))
                db.execute('UPDATE state SET payload=? WHERE id=1', (json.dumps(state, ensure_ascii=False),))
        except Exception as exc:
            outcome = 'failed'
            inserted = changed = 0
            for detail in details:
                detail.update(inserted=0, changed=0)
            if self.stopping.is_set():
                outcome = 'interrupted'
            LOG.warning('CCI collection failed target=%s type=%s error=%s', day, type(exc).__name__, safe_error(exc))
            self.state(stage='当日报价读取失败，历史保留', outcome=outcome, **context,
                       error=safe_error(exc), grades=details, checkedAt=now().isoformat())
        finally:
            try:
                if outcome == 'interrupted':
                    self.state(stage='采集已停止', outcome=outcome, **context, error='历史保留')
                if slot is not None:
                    with self.db() as db:
                        db.execute('UPDATE schedule_runs SET status=?,finished_at=? WHERE slot=?', (outcome, now().isoformat(), slot.isoformat()))
                for detail in details:
                    LOG.info('CCI grade result %s', json.dumps(detail, ensure_ascii=False))
                LOG.info('CCI collection finish target=%s trigger=%s outcome=%s observed=%s inserted=%s changed=%s elapsedMs=%s slot=%s',
                         day, trigger, outcome, count, inserted, changed, round((monotonic()-began)*1000), slot.isoformat() if slot else '-')
            finally:
                lock.release()
