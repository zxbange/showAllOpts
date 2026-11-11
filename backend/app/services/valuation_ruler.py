"""Append-only valuation history, durable recovery and an independent 20:00 schedule."""
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import logging
import sqlite3
import threading
from time import monotonic
import uuid
from zoneinfo import ZoneInfo

import httpx
import portalocker

from app.core.diagnostics import diagnostic_details
from app.services.valuation_ruler_model import (START, INDEX, METHOD, PUBLIC_SOURCE, JQ_SOURCE,
    ValuationError, ValuationConflict, validate_day, valid_code)
from app.services.valuation_ruler_source import PublicSource, SourceError, CALENDAR_SOURCE

LOG=logging.getLogger(__name__)
CHINA=ZoneInfo('Asia/Shanghai')
MAX_RUN_SECONDS=1800
MAX_PENDING_DAYS=6500
RETRY_PASSES=3


def now():
    return datetime.now(timezone.utc)


def close_cutoff(moment):
    """Manual collection/history may include a session once its 15:00 close passed."""
    local=moment.astimezone(CHINA)
    return local.date() if local.hour>=15 else local.date()-timedelta(days=1)


def schedule_cutoff(moment):
    """Startup catches up only the last due 20:00 slot, never today's early close."""
    local=moment.astimezone(CHINA)
    return local.date() if local.hour>=20 else local.date()-timedelta(days=1)


def slot_for(day):
    return datetime.combine(day,time(20),CHINA).isoformat()


class CollectionCancelled(Exception):
    pass


def exact_delta(left,right):
    with localcontext() as context:
        context.prec=60
        return Decimal(str(left))-Decimal(str(right))


def quantile(values,q):
    if not values:return None
    ordered=sorted(Decimal(value) for value in values)
    with localcontext() as context:
        context.prec=60
        position=Decimal(len(ordered)-1)*Decimal(str(q))
        lower=int(position);upper=min(lower+1,len(ordered)-1)
        return float(ordered[lower]+(ordered[upper]-ordered[lower])*(position-lower))


class ValuationRuler:
    def __init__(self,settings,*,client_factory=None,source_factory=None):
        self.settings=settings
        self.root=settings.log_path/'market-data'/'valuation-ruler'
        self.root.mkdir(parents=True,exist_ok=True)
        self.client_factory=client_factory or (lambda:httpx.Client(
            timeout=httpx.Timeout(20,connect=8),trust_env=False,follow_redirects=False,
            headers={'User-Agent':'ShowAllOpts/1.0'}))
        self.source_factory=source_factory or PublicSource
        self.stop_event=threading.Event()
        self.scheduler=self.worker=None
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS calendar (trade_date TEXT PRIMARY KEY, source TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS points (trade_date TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS members (trade_date TEXT NOT NULL, code TEXT NOT NULL,
                    pe_ttm TEXT NOT NULL, PRIMARY KEY(trade_date,code));
                CREATE TABLE IF NOT EXISTS proofs (trade_date TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots (trade_date TEXT PRIMARY KEY, members TEXT NOT NULL,
                    proof TEXT NOT NULL, workbook BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS failures (trade_date TEXT PRIMARY KEY, code TEXT NOT NULL,
                    reason TEXT NOT NULL, attempts INTEGER NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS comparisons (id INTEGER PRIMARY KEY, trade_date TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS comparisons_date ON comparisons(trade_date);
                CREATE TABLE IF NOT EXISTS slots (slot TEXT PRIMARY KEY, outcome TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
                INSERT OR IGNORE INTO state VALUES(1,'{}');
            ''')

    @contextmanager
    def db(self):
        connection=sqlite3.connect(self.root/'history.sqlite3',timeout=15)
        connection.row_factory=sqlite3.Row
        try:
            with connection:yield connection
        finally:connection.close()

    def read_state(self,db=None):
        if db is None:
            with self.db() as connection:return self.read_state(connection)
        return json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])

    def write_state(self,state,db=None):
        if db is None:
            with self.db() as connection:self.write_state(state,connection)
            return
        if self.read_state(db).get('run_id')!=state.get('run_id'):
            raise ValuationError('任务标识已变化，拒绝过期状态写入。')
        state['updated_at']=now().isoformat()
        changed=db.execute("UPDATE state SET payload=? WHERE id=1 AND json_extract(payload,'$.run_id') IS ?",
            (json.dumps(state,ensure_ascii=False),state.get('run_id')))
        if changed.rowcount!=1:raise ValuationError('任务标识已变化，拒绝过期状态写入。')

    def acquire(self):
        lock=portalocker.Lock(str(self.root/'collection.lock'),timeout=0,
            flags=portalocker.LOCK_EX|portalocker.LOCK_NB)
        try:lock.acquire();return lock
        except portalocker.exceptions.LockException:return None

    def running(self):
        lock=self.acquire()
        if lock is None:return True
        lock.release();return False

    def cancel(self):
        if not self.running():return False
        identifier=self.read_state().get('run_id','')
        if len(identifier)!=32 or any(c not in '0123456789abcdef' for c in identifier):return False
        (self.root/('cancel-'+identifier)).touch()
        return True

    def save_calendar(self,db,days,source):
        days=list(days)
        if len(days)>20000 or days!=sorted(set(days)) or any(type(day) is not date or day<START or day.weekday()>=5 for day in days):
            raise ValuationError('交易日历日期或数量无效。')
        db.executemany('INSERT OR IGNORE INTO calendar VALUES (?,?)',[(str(day),source) for day in days])

    def save_snapshot(self,day,members,proof,*,raw):
        if (type(day) is not date or day>now().astimezone(CHINA).date() or len(members)!=300 or
                members!=sorted(set(members)) or any(not valid_code(code) for code in members)):
            raise ValuationError('官方快照日期或成分数量无效。')
        digest=hashlib.sha256('\n'.join(members).encode()).hexdigest()
        if (proof.get('internal_date')!=str(day) or proof.get('members_sha256')!=digest or
                proof.get('source')!='CSI.OFFICIAL.CONS.V1' or
                proof.get('workbook_sha256')!=hashlib.sha256(raw).hexdigest()):
            raise ValuationError('官方快照证据与日期或成分列表不一致。')
        with self.db() as db:
            old=db.execute('SELECT members FROM snapshots WHERE trade_date=?',(str(day),)).fetchone()
            # Excel metadata can change while the exact dated roster stays identical.
            if old and json.loads(old[0])!=members:
                raise ValuationConflict('同日官方成分快照冲突，已有快照保留。')
            db.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?)',
                (str(day),json.dumps(members),json.dumps(proof),raw))

    def load_snapshot(self,day):
        with self.db() as db:
            row=db.execute('SELECT members,proof FROM snapshots WHERE trade_date=?',(str(day),)).fetchone()
        return (json.loads(row['members']),json.loads(row['proof'])) if row else None

    def publish_day(self,db,daily,members,proof):
        checked=validate_day(date.fromisoformat(daily['trade_date']),daily['index_close'],members,daily['source'],expected=daily)
        day=checked['trade_date']
        old=db.execute('SELECT payload FROM points WHERE trade_date=?',(day,)).fetchone()
        if old:
            previous=json.loads(old[0])
            old_members={r['code']:r['pe_ttm'] for r in db.execute('SELECT code,pe_ttm FROM members WHERE trade_date=?',(day,))}
            changed=[dict(code=code,published=old_members.get(code),candidate=str(members[code]) if code in members else None)
                for code in sorted(set(old_members)|set(members))
                if code not in old_members or code not in members or Decimal(old_members[code])!=Decimal(str(members[code]))]
            fields=('index_close','mean_pe_ttm','pe_sum','member_count','positive_count','loss_count','members_sha256')
            differences={key:dict(published=previous[key],candidate=checked[key]) for key in fields if previous[key]!=checked[key]}
            if previous['source']!=checked['source'] or changed or differences:
                report=dict(date=day,published_source=previous['source'],candidate_source=checked['source'],
                    compared_at=now().isoformat(),match=not changed and not differences,
                    added_members=sorted(set(members)-set(old_members)),removed_members=sorted(set(old_members)-set(members)),
                    changed_member_count=len(changed),changed_members=changed,
                    max_abs_pe_delta=str(max((exact_delta(old_members[c],members[c]).copy_abs()
                        for c in set(old_members)&set(members)),default=Decimal(0))),
                    mean_pe_delta=str(exact_delta(checked['mean_pe_ttm'],previous['mean_pe_ttm'])),
                    index_close_delta=str(exact_delta(checked['index_close'],previous['index_close'])),
                    differences=differences,candidate_proof=proof,
                    candidate_members={code:str(value) for code,value in members.items()})
                db.execute('INSERT INTO comparisons(trade_date,payload) VALUES (?,?)',(day,json.dumps(report)))
            if changed or differences:
                raise ValuationConflict('同日数据与已发布记录不同，已保留原值并记录对照结果。')
            db.execute('DELETE FROM failures WHERE trade_date=?',(day,))
            return False
        db.execute('INSERT INTO points VALUES (?,?)',(day,json.dumps(checked)))
        db.executemany('INSERT INTO members VALUES (?,?,?)',[(day,code,format(Decimal(str(value)),'.12f')) for code,value in members.items()])
        db.execute('INSERT INTO proofs VALUES (?,?)',(day,json.dumps(proof)))
        db.execute('INSERT OR IGNORE INTO calendar VALUES (?,?)',(day,checked['source']))
        db.execute('DELETE FROM failures WHERE trade_date=?',(day,))
        return True

    def pending(self,db,cutoff,*,repeat_comparison=True):
        target=db.execute('SELECT max(trade_date) FROM calendar WHERE trade_date<=?',(str(cutoff),)).fetchone()[0]
        last=db.execute('SELECT max(trade_date) FROM points WHERE trade_date<=?',(str(cutoff),)).fetchone()[0]
        if target is None:return [],None
        rows=db.execute('SELECT c.trade_date FROM calendar c LEFT JOIN points p ON p.trade_date=c.trade_date '
            'WHERE c.trade_date<=? AND p.trade_date IS NULL AND c.trade_date>? ORDER BY c.trade_date',
            (str(cutoff),last or target)).fetchall()
        days={r[0] for r in rows}
        if last is None:days.add(target)
        days.update(r[0] for r in db.execute('SELECT f.trade_date FROM failures f LEFT JOIN points p '
            'ON f.trade_date=p.trade_date WHERE p.trade_date IS NULL AND f.trade_date<=?',(str(cutoff),)))
        current=db.execute('SELECT payload FROM points WHERE trade_date=?',(target,)).fetchone()
        if current and json.loads(current[0])['source']==JQ_SOURCE:
            compared=db.execute("SELECT 1 FROM comparisons WHERE trade_date=? AND "
                "json_extract(payload,'$.published_source')=? AND json_extract(payload,'$.candidate_source')=? LIMIT 1",
                (target,JQ_SOURCE,PUBLIC_SOURCE)).fetchone()
            # Scheduled holidays/weekends never refetch a completed comparison.
            # Explicit manual refresh can check vendor corrections again.
            if repeat_comparison or not compared:days.add(target)
        if len(days)>MAX_PENDING_DAYS:raise ValuationError('待补日期超过本轮安全上限。')
        return [date.fromisoformat(day) for day in sorted(days)],target

    def status(self,moment=None):
        with self.db() as db:
            state=self.read_state(db)
            failed=db.execute('SELECT count(*) FROM failures').fetchone()[0]
            failures=[dict(r) for r in db.execute('SELECT * FROM failures ORDER BY trade_date DESC LIMIT 10')]
            audit=db.execute('SELECT payload FROM comparisons ORDER BY id DESC LIMIT 1').fetchone()
        running=self.running();interrupted=bool(state.get('active') and not running)
        comparison=json.loads(audit[0]) if audit else None
        if comparison:comparison={key:value for key,value in comparison.items() if key not in ('changed_members','candidate_members','candidate_proof')}
        return dict(status='running' if running else 'failed' if state.get('error') or interrupted else 'idle',
            phase='interrupted' if interrupted else state.get('phase'),target_date=state.get('target_date'),
            updated_at=state.get('updated_at'),
            error='上次任务中断，已保存的数据保留，可继续补采。' if interrupted else state.get('error'),
            completed_days=state.get('completed_days',0),failed_days=failed,schedule='20:00 Asia/Shanghai',
            source_configured=True,automatic=self.settings.valuation_ruler_worker_enabled,recent_failures=failures,
            cancelled=state.get('phase')=='cancelled',comparison=comparison,
            comparison_status='compared' if comparison else 'not_compared',comparison_error=state.get('comparison_error'),
            comparison_attempted_date=state.get('comparison_attempted_date'),joinquant_imported=state.get('imported_days'),
            joinquant_conflicts=state.get('conflict_days',0),last_success_at=state.get('last_success_at'))

    def history(self,moment=None):
        cutoff=close_cutoff(moment or now())
        with self.db() as db:
            state=self.read_state(db)
            rows=[json.loads(row[0]) for row in db.execute('SELECT payload FROM points WHERE trade_date>=? AND trade_date<=? ORDER BY trade_date',(str(START),str(cutoff)))]
            days={row[0] for row in db.execute('SELECT trade_date FROM calendar WHERE trade_date>=? AND trade_date<=?',(str(START),str(cutoff)))}
        def point(row):
            return dict(date=row['trade_date'],index_close=float(row['index_close']),mean_pe_ttm=float(row['mean_pe_ttm']),
                member_count=row['member_count'],positive_count=row['positive_count'],loss_count=row['loss_count'],source=row['source'])
        actual={row['trade_date']:point(row) for row in rows}
        days|=set(actual);gaps=sorted(days-set(actual))
        points=[actual.get(day,dict(date=day,index_close=None,mean_pe_ttm=None,member_count=None,
            positive_count=None,loss_count=None,source=None,gap=True)) for day in sorted(days)] if actual else []
        first,last=(rows[0]['trade_date'],rows[-1]['trade_date']) if rows else (None,None)
        complete=state.get('calendar_through','')>=str(cutoff)
        return dict(points=points,latest=actual.get(last),method=METHOD,
            benchmarks=dict(p20=quantile([r['mean_pe_ttm'] for r in rows],.2),p80=quantile([r['mean_pe_ttm'] for r in rows],.8),
                sample_days=len(rows),start_date=first,end_date=last),
            coverage=dict(requested_start=str(START),first_date=first,last_date=last,missing_count=len(gaps) if complete else None,
                known_missing_count=len(gaps),calendar_complete=complete),gap_dates=gaps,sources=sorted({r['source'] for r in rows}))

    def start_job(self,kind,runner,*,background=True,moment=None,slot=None):
        moment=moment or now()
        if self.stop_event.is_set():return False
        lock=self.acquire()
        if lock is None:return False
        state=None
        try:
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                prior=db.execute('SELECT outcome FROM slots WHERE slot=?',(slot,)).fetchone() if slot else None
                if prior and prior[0]!='running':lock.release();return False
                state=self.read_state(db)
                state.update(run_id=uuid.uuid4().hex,active=True,phase=kind,error=None,completed_days=0,conflict_days=0,
                    comparison_error=None,comparison_attempted_date=None,target_date=None,updated_at=moment.isoformat())
                db.execute('UPDATE state SET payload=? WHERE id=1',(json.dumps(state),))
                if slot:db.execute('INSERT INTO slots VALUES (?,?) ON CONFLICT(slot) DO UPDATE SET outcome=excluded.outcome',(slot,'running'))
            if background:
                self.worker=threading.Thread(target=self._execute,args=(runner,lock,state,moment,slot),daemon=True,name='valuation-ruler-'+kind)
                self.worker.start()
            else:self._execute(runner,lock,state,moment,slot)
        except BaseException as exc:
            LOG.error('Valuation launch failed %s',diagnostic_details(exc))
            try:
                if state is not None:
                    state.update(active=False,phase='failed',error='无法启动估值标尺任务，请重试。')
                    with self.db() as db:
                        self.write_state(state,db)
                        if slot:db.execute('UPDATE slots SET outcome=? WHERE slot=?',('failed',slot))
            finally:lock.release()
            raise
        return True

    def _execute(self,runner,lock,state,moment,slot):
        marker=self.root/('cancel-'+state['run_id']);started=monotonic()
        def check_cancel():
            if self.stop_event.is_set() or marker.exists():raise CollectionCancelled()
            if monotonic()-started>MAX_RUN_SECONDS:raise SourceError('budget_exceeded',common=True)
        try:
            check_cancel();runner(self,state,check_cancel,moment);check_cancel()
            state['phase']='failed' if state.get('error') else 'complete'
            if not state.get('error'):state['last_success_at']=now().isoformat()
        except CollectionCancelled:state.update(phase='cancelled',error=None)
        except Exception as exc:
            LOG.error('Valuation run failed source=valuation_ruler target=%s %s',state.get('target_date'),diagnostic_details(exc))
            state.update(phase='failed',error=str(exc) if isinstance(exc,ValuationError) else '估值标尺更新失败，已保存数据保留。')
        finally:
            try:
                state['active']=False
                with self.db() as db:
                    self.write_state(state,db)
                    if slot:db.execute('UPDATE slots SET outcome=? WHERE slot=?',(state['phase'],slot))
                marker.unlink(missing_ok=True)
            finally:lock.release()

    def refresh(self,background=True,*,moment=None,slot=None,cutoff=None):
        def runner(monitor,state,check_cancel,moment):
            monitor._collect(state,check_cancel,moment,cutoff=cutoff,automatic=slot is not None)
        return self.start_job('public',runner,background=background,moment=moment,slot=slot)

    def _collect(self,state,check_cancel,moment,*,cutoff=None,automatic=False):
        cutoff=cutoff or close_cutoff(moment)
        with self.client_factory() as client:
            source=self.source_factory(client,self,check_cancel)
            days=source.calendar(cutoff);check_cancel()
            with self.db() as db:
                previous_days={date.fromisoformat(row[0]) for row in db.execute('SELECT trade_date FROM calendar')}
                if not days or days[-1]<cutoff or not previous_days.issubset(set(days)):
                    raise SourceError('incomplete',common=True)
                self.save_calendar(db,days,CALENDAR_SOURCE)
                state['calendar_through']=str(days[-1])
                work,target=self.pending(db,cutoff,repeat_comparison=not automatic)
                state.update(target_date=target,phase='collecting');self.write_state(state,db)
            # Persist the entire plan first so later successes never conceal unvisited gaps.
            with self.db() as db:
                for day in work:
                    if not db.execute('SELECT 1 FROM points WHERE trade_date=?',(str(day),)).fetchone():
                        db.execute('INSERT OR IGNORE INTO failures VALUES (?,?,?,?,?)',(str(day),'pending','等待补采。',0,now().isoformat()))
            for attempt in range(RETRY_PASSES+1):
                retry=[]
                for day in work:
                    check_cancel()
                    try:
                        daily,members,proof=source.collect(day)
                        if daily.get('trade_date')!=str(day):raise SourceError('invalid_response')
                        check_cancel()
                        with self.db() as db:
                            try:state['completed_days']+=int(self.publish_day(db,daily,members,proof))
                            except ValuationConflict:state['conflict_days']+=1
                            self.write_state(state,db)
                    except SourceError as exc:
                        with self.db() as db:
                            if db.execute('SELECT 1 FROM points WHERE trade_date=?',(str(day),)).fetchone():
                                state.update(comparison_error=str(exc),comparison_attempted_date=str(day));self.write_state(state,db)
                            else:
                                db.execute('INSERT INTO failures VALUES (?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET '
                                    'code=excluded.code,reason=excluded.reason,attempts=failures.attempts+1,updated_at=excluded.updated_at',
                                    (str(day),exc.code,str(exc),1,now().isoformat()))
                        LOG.warning('Valuation day failed source=CSI_EM trade_date=%s attempt=%s code=%s %s',day,attempt+1,exc.code,diagnostic_details(exc))
                        if exc.common:raise
                        if exc.retryable:retry.append(day)
                if not retry or attempt==RETRY_PASSES:break
                state['phase']='retrying';self.write_state(state)
                if self.stop_event.wait(1):raise CollectionCancelled()
                work=retry
            with self.db() as db:remaining=db.execute('SELECT count(*) FROM failures WHERE trade_date<=?',(str(cutoff),)).fetchone()[0]
            state['error']=('部分交易日仍缺数据，已保存成功日期；可重试或用聚宽补齐。' if remaining else
                '对照发现来源差异，已有数据未覆盖，请查看对照结果。' if state['conflict_days'] else None)
            self.write_state(state)

    def tick(self,moment=None,*,background=True):
        moment=moment or now()
        return self.refresh(background=background,moment=moment,slot=slot_for(schedule_cutoff(moment)),cutoff=schedule_cutoff(moment))

    def start(self):
        if not self.settings.valuation_ruler_worker_enabled or (self.scheduler and self.scheduler.is_alive()):return
        self.stop_event.clear()
        def schedule():
            while not self.stop_event.is_set():
                try:self.tick()
                except Exception as exc:LOG.error('Valuation scheduler failed %s',diagnostic_details(exc))
                self.stop_event.wait(30)
        self.scheduler=threading.Thread(target=schedule,daemon=True,name='valuation-ruler-scheduler')
        self.scheduler.start()

    def stop(self):
        self.stop_event.set()
        for thread in (self.scheduler,self.worker):
            if thread and thread.is_alive() and thread is not threading.current_thread():thread.join(timeout=25)
