"""CCI has its own model, history and daily 18:00 scheduling ledger."""
from datetime import timedelta
from pathlib import Path
import json
import logging
import threading
import httpx
from app.services.commodities import Commodities, CHINA, now
from app.services.cci_source import START, GRADES, SOURCE_URL, validate_rows, fetch_rows

LOG = logging.getLogger(__name__)
SEED = Path(__file__).resolve().parents[1] / 'data/cci_coal.json'


def latest_slot(moment):
    local = moment.astimezone(CHINA)
    slot = local.replace(hour=18, minute=0, second=0, microsecond=0)
    return slot if local >= slot else slot - timedelta(days=1)


class CCICoal(Commodities):
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
        count = 0
        outcome = 'failed'
        try:
            with httpx.Client(timeout=httpx.Timeout(25, connect=10), trust_env=False, follow_redirects=False) as client:
                rows = validate_rows(fetch_rows(client, START, now().astimezone(CHINA).date().isoformat(), should_stop=self.stopping.is_set), today=now().astimezone(CHINA).date())
            if self.stopping.is_set():
                outcome = 'interrupted'
                return
            with self.db() as db:
                for row in rows:
                    db.execute('INSERT INTO cci_quotes VALUES(?,?,?) ON CONFLICT(day,grade) DO UPDATE SET payload=excluded.payload', (row['quoteDate'], row['grade'], json.dumps(row, ensure_ascii=False)))
                count = len(rows)
                state = dict(stage='采集完成' if rows else '来源暂无报价，历史保留', error=None,
                             checkedAt=now().isoformat(), observed=count)
                db.execute('UPDATE state SET payload=? WHERE id=1', (json.dumps(state, ensure_ascii=False),))
            outcome = 'completed'
        except Exception as exc:
            if self.stopping.is_set():
                outcome = 'interrupted'
            LOG.warning('CCI collection failed type=%s slot=%s', type(exc).__name__, slot.isoformat() if slot else '-')
            self.state(stage='采集失败', error='公开来源读取或校验失败，历史保留', checkedAt=now().isoformat())
        finally:
            try:
                if outcome == 'interrupted':
                    self.state(stage='采集已停止', error='历史保留')
                if slot is not None:
                    with self.db() as db:
                        db.execute('UPDATE schedule_runs SET status=?,finished_at=? WHERE slot=?', (outcome, now().isoformat(), slot.isoformat()))
                LOG.info('CCI collection finish outcome=%s observed=%s slot=%s', outcome, count, slot.isoformat() if slot else '-')
            finally:
                lock.release()
