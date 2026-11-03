"""CCI has its own model, history and daily 18:00 scheduling ledger."""
from datetime import timedelta
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
    slot = local.replace(hour=18, minute=0, second=0, microsecond=0)
    return slot if local >= slot else slot - timedelta(days=1)


class CCICoal(Commodities):
    tracks_failed_targets = False
    schedule_label = '每天 18:00 · 北京时间'
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

    def collect(self, lock, slot=None):
        began = monotonic()
        target = now().astimezone(CHINA).date()
        day = target.isoformat()
        details = []
        count = inserted = changed = 0
        outcome = 'failed'
        trigger = 'scheduled' if slot else 'manual'
        try:
            self.state(stage='正在查询当日报价', targetDate=day, trigger=trigger, outcome='running', error=None)
            with httpx.Client(timeout=httpx.Timeout(25, connect=10), trust_env=False, follow_redirects=False) as client:
                rows = validate_rows(fetch_rows(client, day, day, should_stop=self.stopping.is_set, diagnostics=details), today=target)
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
                state = dict(stage=stage, outcome=outcome, targetDate=day, trigger=trigger,
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
            self.state(stage='当日报价读取失败，历史保留', outcome=outcome, targetDate=day,
                       error=safe_error(exc), grades=details, checkedAt=now().isoformat())
        finally:
            try:
                if outcome == 'interrupted':
                    self.state(stage='采集已停止', outcome=outcome, targetDate=day, error='历史保留')
                if slot is not None:
                    with self.db() as db:
                        db.execute('UPDATE schedule_runs SET status=?,finished_at=? WHERE slot=?', (outcome, now().isoformat(), slot.isoformat()))
                for detail in details:
                    LOG.info('CCI grade result %s', json.dumps(detail, ensure_ascii=False))
                LOG.info('CCI collection finish target=%s trigger=%s outcome=%s observed=%s inserted=%s changed=%s elapsedMs=%s slot=%s',
                         day, trigger, outcome, count, inserted, changed, round((monotonic()-began)*1000), slot.isoformat() if slot else '-')
            finally:
                lock.release()
