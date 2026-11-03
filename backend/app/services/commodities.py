"""Independent persisted quotes. Daily Shanghai scheduling and manual collection share a cross-process lock."""
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import logging
import json
import sqlite3
import threading
import httpx
import portalocker
from app.services.commodity_source import CATALOG, discover, fetch, parse

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
            db.execute('CREATE TABLE IF NOT EXISTS schedule_runs(slot TEXT PRIMARY KEY, status TEXT NOT NULL, finished_at TEXT)')
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
        with self.db() as db:state=json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        lock=self.lock();running=lock is None
        if lock:lock.release()
        return dict(state,running=running,automatic=True,schedule='每天 11:00、18:00 · 北京时间')
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
                self.refresh(slot=latest_slot(now()))
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
                        self_state = json.dumps(dict(stage='上次定时采集中断', error='历史保留，可手动补采或等待下个采集时点'), ensure_ascii=False)
                        db.execute('UPDATE state SET payload=? WHERE id=1', (self_state,))
                        LOG.warning('Commodity scheduled collection interrupted count=%s', interrupted)
                    claimed = db.execute('INSERT OR IGNORE INTO schedule_runs(slot,status) VALUES(?,?)', (slot.isoformat(), 'running')).rowcount
                    if not claimed:
                        lock.release()
                        return False
            self.state(stage='读取公开历史报价',error=None,trigger='scheduled' if slot else 'manual',slot=slot.isoformat() if slot else None)
            LOG.info('Commodity collection start trigger=%s slot=%s', 'scheduled' if slot else 'manual', slot.isoformat() if slot else '-')
            self.thread=threading.Thread(target=self.collect,args=(lock,slot),daemon=True,name='commodities')
            self.thread.start()
        except BaseException:lock.release();raise
        return True
    def collect(self,lock,slot=None):
        failures=0;updated=0;completed=False
        try:
            known={r['sourceUrl']:r for r in json.loads(SEED.read_text())['rows'] if 'html' in r['sourceUrl']}
            with httpx.Client(timeout=httpx.Timeout(15,connect=8),trust_env=False,follow_redirects=False) as client:
                urls=list(known)
                try:urls=list(dict.fromkeys(urls+discover(fetch(client,CATALOG,True))))
                except Exception as exc:
                    failures+=1
                    LOG.warning('Commodity catalogue failed type=%s', type(exc).__name__)
                for url in urls:
                    if self.stopping.is_set():break
                    try:
                        row=parse(fetch(client,url),url,known.get(url))
                        with self.db() as db:
                            if self.stopping.is_set():break
                            db.execute('INSERT INTO quotes VALUES(?,?) ON CONFLICT(day) DO UPDATE SET payload=excluded.payload',(row['quoteDate'],json.dumps(row,ensure_ascii=False)))
                        updated+=1
                    except Exception as exc:
                        failures+=1
                        LOG.warning('Commodity article failed type=%s', type(exc).__name__)
                    self.state(stage='补采公开历史',updated=updated,failures=failures,error=None)
                    if self.stopping.wait(.35):break
            completed=True
            self.state(stage='已停止' if self.stopping.is_set() else '补采结束',updated=updated,failures=failures,
                       error='部分正文无法读取或核验，已有历史保留' if failures else None,checkedAt=datetime.now(timezone.utc).isoformat())
        except Exception as exc:
            LOG.error('Commodity collection failed type=%s', type(exc).__name__)
            self.state(stage='补采失败',error='公开来源暂时不可用，已有历史保留')
        finally:
            try:
                outcome = 'interrupted' if self.stopping.is_set() else ('failed' if not completed or failures else 'completed')
                if slot is not None:
                    with self.db() as db:
                        db.execute('UPDATE schedule_runs SET status=?,finished_at=? WHERE slot=?', (outcome, now().isoformat(), slot.isoformat()))
                LOG.info('Commodity collection finish outcome=%s checked=%s failures=%s slot=%s', outcome, updated, failures, slot.isoformat() if slot else '-')
            finally:
                lock.release()
    def stop(self):
        self.stopping.set()
        if self.scheduler:self.scheduler.join(timeout=2)
        if self.thread:self.thread.join(timeout=2)
