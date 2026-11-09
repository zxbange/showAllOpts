"""CCI has its own model, history and daily 21:00 scheduling ledger."""
from datetime import date, datetime, timedelta
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
    recovery_window_days = 31
    max_requests = 36
    max_collection_seconds = 120
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
            db.execute('CREATE TABLE IF NOT EXISTS cci_recovery(grade TEXT PRIMARY KEY, start_day TEXT NOT NULL, origin TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS cci_gap_checks(day TEXT NOT NULL, grade TEXT NOT NULL, checked_at TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(day,grade))')
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
            self.state(stage='正在更新数据并补齐缺失报价', outcome='running', error=None, trigger='scheduled',
                       targetDate=slot.date().isoformat(), slot=slot.isoformat())
            LOG.info('CCI collection start trigger=scheduled slot=%s', slot.isoformat())
            self.thread = threading.Thread(target=self.collect, args=(lock, slot), daemon=True, name='cci-coal')
            self.thread.start()
            launched = True
            return True
        finally:
            if not launched:
                lock.release()

    def recovery_plan(self, day):
        """Freeze the first recovery floor; newer rows must not hide older gaps."""
        with self.db() as db:
            installation = datetime.fromisoformat(db.execute(
                'SELECT initialized_at FROM schedule_meta WHERE id=1').fetchone()[0]).astimezone(CHINA).date().isoformat()
            for grade in GRADES:
                latest = db.execute('SELECT max(day) FROM cci_quotes WHERE grade=? AND day<=?', (grade, day)).fetchone()[0]
                floor = max(START, min(latest or installation, day))
                db.execute('INSERT OR IGNORE INTO cci_recovery VALUES(?,?,?)',
                           (grade, floor, 'latest_quote' if latest else 'installation_date'))
            floors = db.execute('SELECT grade,start_day,origin FROM cci_recovery ORDER BY grade').fetchall()
            existing = set(db.execute('SELECT day,grade FROM cci_quotes WHERE day<=?', (day,)))
            checks = {(d, g): (stamp, result) for d, g, stamp, result in db.execute(
                'SELECT day,grade,checked_at,result FROM cci_gap_checks')}
        missing = set()
        for grade, floor, _ in floors:
            cursor = date.fromisoformat(floor)
            while cursor.isoformat() <= day:
                key = (cursor.isoformat(), grade)
                if key not in existing:
                    missing.add(key)
                cursor += timedelta(days=1)
        # Retry old holes too, including source-empty dates. Prioritize unchecked
        # and least-recently checked slots so long outages cannot starve later data.
        windows = []
        for grade, floor, _ in floors:
            cursor = date.fromisoformat(floor)
            while cursor.isoformat() < day:
                end = min(cursor + timedelta(days=self.recovery_window_days - 1),
                          date.fromisoformat(day) - timedelta(days=1)).isoformat()
                keys = {key for key in missing if key[1] == grade and cursor.isoformat() <= key[0] <= end}
                if keys:
                    windows.append((cursor.isoformat(), end, grade, keys))
                cursor = date.fromisoformat(end) + timedelta(days=1)
        windows.sort(key=lambda window: (min(checks.get(key, ('', ''))[0] for key in window[3]),
                                         window[0], window[2]))
        recovery = dict(dateFrom=min(row[1] for row in floors), dateTo=day,
                        initializedFrom=[dict(grade=g, dateFrom=d, origin=o) for g, d, o in floors],
                        pendingCount=len(missing), checkedCount=0, unavailableCount=0,
                        deferredCount=len(missing), requestCount=0)
        return windows, missing, recovery

    def collect(self, lock, slot=None):
        began = monotonic()
        today = now().astimezone(CHINA).date()
        target = slot.astimezone(CHINA).date() if slot is not None else today
        day = target.isoformat()
        details = []
        count = inserted = changed = target_hits = 0
        outcome = 'failed'
        trigger = 'scheduled' if slot else 'manual'
        context = dict(targetDate=day, trigger=trigger, slot=slot.isoformat() if slot else None)
        recovery = None
        missing = checked = unavailable = set()

        def should_stop():
            return self.stopping.is_set() or monotonic() - began >= self.max_collection_seconds

        def summarize():
            if recovery is not None:
                recovery.update(pendingCount=len(missing), checkedCount=len(checked),
                                unavailableCount=len(unavailable), deferredCount=len(missing - checked))

        def read_window(client, start, end, grades, keys):
            nonlocal count, inserted, changed, target_hits, missing, checked, unavailable
            offset = len(details)
            try:
                rows = validate_rows(fetch_rows(client, start, end, grades=grades,
                                               should_stop=should_stop, diagnostics=details), today=today)
            finally:
                attempted = details[offset:]
                recovery['requestCount'] += (sum(d.get('requested', True) for d in attempted)
                                             if attempted else len(grades))
            if any(not start <= row['quoteDate'] <= end or row['grade'] not in grades for row in rows):
                raise ValueError('Unexpected quote range or grade')
            if self.stopping.is_set():
                return
            # A valid, complete window commits independently. Later failures may
            # not discard already recovered data, nor overwrite existing history.
            window_details = details[offset:]
            attempted_grades = {d['grade'] for d in window_details if d.get('requested', True)} if window_details else set(grades)
            attempted_keys = {key for key in keys if key[1] in attempted_grades}
            for detail in window_details:
                # Source targetHits refers to its window end, not necessarily today.
                detail['targetHits'] = sum(r['quoteDate'] == day and r['grade'] == detail['grade'] for r in rows)
            window_inserted = window_changed = 0
            changes = {}
            with self.db() as db:
                for row in rows:
                    key = (row['quoteDate'], row['grade'])
                    previous = db.execute('SELECT payload FROM cci_quotes WHERE day=? AND grade=?', key).fetchone()
                    added = previous is None
                    updated = previous is not None and key[0] == day and json.loads(previous[0]) != row
                    window_inserted += int(added)
                    window_changed += int(updated)
                    g_added, g_changed = changes.get(row['grade'], (0, 0))
                    changes[row['grade']] = (g_added + int(added), g_changed + int(updated))
                    if added or updated:
                        db.execute('INSERT INTO cci_quotes VALUES(?,?,?) ON CONFLICT(day,grade) DO UPDATE SET payload=excluded.payload',
                                   (*key, json.dumps(row, ensure_ascii=False)))
                    db.execute('DELETE FROM cci_gap_checks WHERE day=? AND grade=?', key)
                errors = {d['grade'] for d in window_details if d['error']}
                returned = {(r['quoteDate'], r['grade']) for r in rows}
                window_unavailable = set()
                for key in attempted_keys:
                    result = 'failed' if key[1] in errors else 'unavailable'
                    if key not in returned:
                        db.execute('INSERT INTO cci_gap_checks VALUES(?,?,?,?) '
                                   'ON CONFLICT(day,grade) DO UPDATE SET checked_at=excluded.checked_at,result=excluded.result',
                                   (*key, now().isoformat(), result))
                        if result == 'unavailable':
                            window_unavailable.add(key)
            missing -= returned
            unavailable |= window_unavailable
            checked |= attempted_keys
            inserted += window_inserted
            changed += window_changed
            for detail in window_details:
                detail['inserted'], detail['changed'] = changes.get(detail['grade'], (0, 0))
            count += len(rows)
            target_hits += sum(r['quoteDate'] == day for r in rows)
            summarize()
            self.state(stage='正在更新数据并补齐缺失报价', **context, outcome='running', error=None,
                       recovery=recovery, observed=count, targetHits=target_hits,
                       inserted=inserted, changed=changed, grades=details)

        try:
            # Do not replay yesterday's schedule before today's 21:00 or label
            # today's data as yesterday. The next real update recovers that gap.
            if slot is not None and target < today:
                outcome = 'skipped'
                self.state(stage='过期定时时点已跳过，缺失数据在下次更新补采', outcome=outcome,
                           **context, skippedAt=now().isoformat(), error=None,
                           observed=0, inserted=0, changed=0, grades=[],
                           elapsedMs=round((monotonic()-began)*1000))
                return
            windows, missing, recovery = self.recovery_plan(day)
            checked, unavailable = set(), set()
            self.state(stage='正在更新数据并补齐缺失报价', **context, outcome='running', error=None,
                       recovery=recovery)
            with httpx.Client(timeout=httpx.Timeout(25, connect=10), trust_env=False, follow_redirects=False) as client:
                # Always prioritize today's three grades and preserve the daily
                # retry semantics. Historical windows have a separate durable ledger.
                if not should_stop():
                    read_window(client, day, day, GRADES, {key for key in missing if key[0] == day})
                for start, end, grade, keys in windows:
                    if should_stop() or recovery['requestCount'] >= self.max_requests:
                        break
                    read_window(client, start, end, (grade,), keys)
            if self.stopping.is_set():
                outcome = 'interrupted'
                return
            summarize()
            errors = sum(bool(d['error']) for d in details)
            incomplete = bool(errors or recovery['deferredCount'] or should_stop())
            outcome = ('completed' if target_hits == len(GRADES) and not incomplete else
                       'partial' if count else 'failed' if incomplete else 'empty')
            stage = {'completed': '更新完成，缺失日期已核查', 'partial': '部分数据已更新，仍有待获取报价',
                     'failed': '报价读取失败，历史保留', 'empty': '来源无可用报价，历史保留'}[outcome]
            limited = bool(recovery['deferredCount'] or should_stop())
            if limited:
                stage = '本次更新达到上限，未完成缺口下次继续'
            error = ('本次达到采集上限，剩余缺口在下次更新继续' if limited else
                     '部分日期或档位请求失败，缺口保留以便下次补采' if errors else None)
            self.state(stage=stage, outcome=outcome, **context, error=error,
                       checkedAt=now().isoformat(), observed=count, targetHits=target_hits,
                       inserted=inserted, changed=changed, recovery=recovery,
                       grades=details, elapsedMs=round((monotonic()-began)*1000))
        except Exception as exc:
            outcome = 'interrupted' if self.stopping.is_set() else 'failed'
            summarize()
            LOG.warning('CCI collection failed target=%s type=%s error=%s', day, type(exc).__name__, safe_error(exc))
            self.state(stage='报价读取失败，已保存数据保留', outcome=outcome, **context,
                       error=safe_error(exc), grades=details, recovery=recovery,
                       observed=count, targetHits=target_hits, inserted=inserted, changed=changed,
                       checkedAt=now().isoformat())
        finally:
            try:
                if outcome == 'interrupted':
                    summarize()
                    self.state(stage='采集已停止', outcome=outcome, **context, error='已保存数据保留，未完成缺口下次继续',
                               recovery=recovery, observed=count, targetHits=target_hits,
                               inserted=inserted, changed=changed, grades=details)
                if slot is not None:
                    with self.db() as db:
                        db.execute('UPDATE schedule_runs SET status=?,finished_at=? WHERE slot=?', (outcome, now().isoformat(), slot.isoformat()))
                for detail in details:
                    LOG.info('CCI grade result %s', json.dumps(detail, ensure_ascii=False))
                LOG.info('CCI collection finish target=%s trigger=%s outcome=%s observed=%s inserted=%s changed=%s recovery=%s elapsedMs=%s slot=%s',
                         day, trigger, outcome, count, inserted, changed, json.dumps(recovery, ensure_ascii=False),
                         round((monotonic()-began)*1000), slot.isoformat() if slot else '-')
            finally:
                lock.release()
