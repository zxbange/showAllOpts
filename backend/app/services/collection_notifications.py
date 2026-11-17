"""Opt-in collection-result Telegram outbox; never a dependency of collection success."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
import inspect
import json
import logging
import sqlite3
import threading
import time
import uuid
from zoneinfo import ZoneInfo
import portalocker

LOG = logging.getLogger(__name__)
LABELS = {'etf': '国家队动态', 'margin': '两融担保比例', 'commodities': 'A00 铝',
          'growth_dividend': '创红比和科创红利比', 'market': '市场热度和抱团拥挤度',
          'valuation_ruler': '估值标尺', 'cci': 'CCI 动力煤', 'cash_cow': '奶牛股', 'dividend_selection': '高股息（银行 / 非银行）'}
_WORKERS = {}
_WORKERS_LOCK = threading.Lock()


def enabled(settings, task_id):
    from app.services.system_configuration import task_config
    return task_id in LABELS and task_config(settings, task_id).get('telegram_notify') is True


def _database(settings):
    root = settings.log_path / 'collection-notifications'
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / 'outbox.sqlite3', timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS outbox (task TEXT, run TEXT, message TEXT, '
               'status TEXT, attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0, '
               'PRIMARY KEY(task,run))')
    (root / 'outbox.sqlite3').chmod(0o600)
    return db


def format_result(task_id, outcome, changed_count, summary, collected_at=None):
    stamp = collected_at or datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
    status = '成功' if outcome in {'complete', 'completed', 'success'} else '失败（部分数据已更新）'
    lines = [LABELS[task_id], '采集时间：' + str(stamp), '采集结果：' + status,
             '更新记录：' + str(changed_count), *[str(line)[:400] for line in summary[:12]]]
    return '\n'.join(lines)[:1800]


def emit_collection_result(settings, task_id, *, scheduled, run_key, outcome,
                           changed_count, summary, collected_at=None):
    """Persist a deduplicated event describing committed business changes."""
    try:
        reason = ('no_business_change' if changed_count <= 0
                  else 'disabled' if not enabled(settings, task_id) else None)
        if reason:
            LOG.info('Collection notification skipped task=%s reason=%s', task_id, reason)
            return False
        message = format_result(task_id, outcome, changed_count, summary, collected_at)
        db = _database(settings)
        try:
            with db:
                inserted = db.execute('INSERT OR IGNORE INTO outbox(task,run,message,status) VALUES(?,?,?,?)',
                                      (task_id, str(run_key), message, 'pending')).rowcount
        finally:
            db.close()
        LOG.info('Collection notification %s task=%s changed_count=%s trigger=%s',
                 'enqueued' if inserted else 'duplicate', task_id, changed_count,
                 'automatic' if scheduled else 'manual')
        start_notifications(settings)
        return bool(inserted)
    except Exception as exc:
        LOG.warning('Collection notification enqueue failed task=%s error_type=%s', task_id, type(exc).__name__)
        return False


def deliver_pending(settings):
    """One bounded delivery pass, serialized across API workers and social sends."""
    from app.services.social import SocialMonitor
    root = settings.log_path / 'collection-notifications'
    root.mkdir(parents=True, exist_ok=True)
    try:
        with portalocker.Lock(str(root / 'delivery.lock'), timeout=0):
            db = _database(settings)
            try:
                rows = db.execute("SELECT * FROM outbox WHERE status='pending' AND next_try<=? LIMIT 8",
                                  (time.time(),)).fetchall()
                for row in rows:
                    if not enabled(settings, row['task']):
                        LOG.info('Collection notification skipped task=%s reason=disabled_before_delivery', row['task'])
                        with db:
                            db.execute("UPDATE outbox SET status='skipped' WHERE task=? AND run=?", (row['task'], row['run']))
                        continue
                    monitor = SocialMonitor(settings)
                    with portalocker.Lock(str(monitor.root / 'notification-send.lock'), timeout=1):
                        if not enabled(settings, row['task']):
                            continue
                        with monitor.db() as social_db:
                            config = monitor.config(social_db)
                        if not config.get('token') or not config.get('chat_id'):
                            LOG.warning('Collection notification destination missing task=%s', row['task'])
                            with db:
                                db.execute('UPDATE outbox SET next_try=? WHERE task=? AND run=?',
                                           (time.time() + 300, row['task'], row['run']))
                            continue
                        try:
                            monitor.send_part(monitor.decode(config['token']), monitor.decode(config['chat_id']), row['message'])
                        except Exception as exc:
                            attempts = row['attempts'] + 1
                            with db:
                                db.execute('UPDATE outbox SET attempts=?,next_try=?,status=? WHERE task=? AND run=?',
                                           (attempts, time.time() + 60 * min(attempts, 5),
                                            'failed' if attempts >= 3 else 'pending', row['task'], row['run']))
                            LOG.warning('Collection notification failed task=%s attempt=%s error_type=%s',
                                        row['task'], attempts, type(exc).__name__)
                        else:
                            with db:
                                db.execute("UPDATE outbox SET status='sent' WHERE task=? AND run=?", (row['task'], row['run']))
                            LOG.info('Collection notification sent task=%s', row['task'])
            finally:
                db.close()
    except portalocker.exceptions.LockException:
        pass


def start_notifications(settings):
    key = str(settings.log_path)
    with _WORKERS_LOCK:
        current = _WORKERS.get(key)
        if current and current[0].is_alive():
            return
        stop = threading.Event()
        def loop():
            while not stop.is_set():
                try:
                    deliver_pending(settings)
                except Exception as exc:
                    LOG.warning('Collection notification worker failed error_type=%s', type(exc).__name__)
                stop.wait(30)
        worker = threading.Thread(target=loop, daemon=True, name='collection-notifications')
        _WORKERS[key] = worker, stop
        worker.start()


def stop_notifications(settings):
    with _WORKERS_LOCK:
        current = _WORKERS.get(str(settings.log_path))
    if current:
        current[1].set()
        current[0].join(timeout=2)


def published_values(monitor, task):
    """Only published business values; timestamps/progress never trigger messages."""
    with monitor.db() as db:
        if task == 'margin':
            return {r[0]: r[1] for r in db.execute('SELECT trade_date,ratio FROM points')}
        if task == 'growth_dividend':
            return {(r[0], r[1]): r[2] for r in db.execute('SELECT trade_date,code,close FROM points')}
        if task == 'commodities':
            return {r[0]: {k: json.loads(r[1]).get(k) for k in ('low','high','mean','change','rangeEvidence')}
                    for r in db.execute('SELECT day,payload FROM quotes')}
        if task == 'cci':
            return {(r[0],r[1]): {k: json.loads(r[2]).get(k) for k in ('price','change')}
                    for r in db.execute('SELECT day,grade,payload FROM cci_quotes')}
        if task == 'valuation_ruler':
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_points_v2'").fetchone():
                from app.services.index_valuation import METRICS
                keys = ('index_close', 'member_count', 'statistics', *METRICS)
                current = {(r[0], r[1]): {k: json.loads(r[2]).get(k) for k in keys}
                           for r in db.execute('SELECT trade_date,index_code,payload FROM index_points_v2')}
                if current:
                    return current
            keys = ('index_close','mean_pe_ttm','pe_sum','positive_count','loss_count')
            return {r[0]: {k: json.loads(r[1]).get(k) for k in keys}
                    for r in db.execute('SELECT trade_date,payload FROM points')}
        if task == 'cash_cow':
            return {r[0]: json.loads(r[1]).get('items', []) for r in db.execute('SELECT years,payload FROM snapshots')}
    raise ValueError('unsupported task')


def _quote_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _quote_with_change(after, day, field, grade=None):
    """Compare with the latest earlier valid same-field quote, never same-day/future."""
    key = (day, grade) if grade is not None else day
    current = _quote_number(after.get(key, {}).get(field))
    if current is None:
        return '—'
    if grade is not None:
        earlier = sorted((key for key in after if isinstance(key, tuple)
                          and key[1] == grade and key[0] < day), reverse=True)
    else:
        earlier = sorted((key for key in after if isinstance(key, str) and key < day), reverse=True)
    previous = next((value for key in earlier
                     if (value := _quote_number(after[key].get(field))) is not None), None)
    def display(value):
        text = format(value, 'f')
        return text.rstrip('0').rstrip('.') if '.' in text else text
    delta = current - previous if previous is not None else None
    change = '—' if delta is None else ('+' if delta > 0 else '') + display(delta if delta else Decimal(0))
    return f'{display(current)}({change})'


def summarize_changes(task, changes, after):
    if task == 'cash_cow':
        lines = []
        for period in (3,5,10):
            items = after.get(period, [])
            names = '、'.join(str(item.get('name') or item.get('code', '')) for item in items[:5])
            suffix = ('；' + names + (' 等' if len(items) > 5 else '')) if names else ''
            lines.append(f'近{period}年：入选 {len(items)} 只' + suffix)
        return lines
    days = sorted({str(key[0] if isinstance(key, tuple) else key) for key in changes})
    lines = [f'更新日期：{days[0]} 至 {days[-1]}']
    day = days[-1]
    if task == 'margin':
        lines.append(f'最新担保比例：{after[day]}%')
    elif task == 'commodities':
        lines.append(f"报价日期：{day}")
        values = [_quote_with_change(after, day, field) for field in ('low', 'high', 'mean')]
        lines.append(f"最低 {values[0]} / 最高 {values[1]} / 均价 {values[2]} 元/吨")
    elif task == 'cci':
        # Each grade can have a different newest changed date during partial backfill.
        for grade in ('CCI5500', 'CCI5000', 'CCI4500'):
            grade_days = [key[0] for key in changes if isinstance(key, tuple) and key[1] == grade]
            if grade_days:
                quote_day = max(grade_days)
                value = _quote_with_change(after, quote_day, 'price', grade)
                lines.append(f'{grade}：{value} 元/吨（{quote_day}）')
    elif task == 'valuation_ruler':
        if any(isinstance(key, tuple) for key in after):
            from app.services.index_valuation import INDICES
            for code, (label, _, _) in INDICES.items():
                q = after.get((day, code))
                if q is not None:
                    value = q.get('mean_pe_ttm')
                    lines.append(f"{label}：{q['index_close']}；等权平均 PE(TTM)：{value if value is not None else '—'}")
            return lines
        q = after[day]
        lines.extend([f"沪深300：{q['index_close']}", f"等权平均 PE(TTM)：{q['mean_pe_ttm']}", f"盈利股：{q['positive_count']} 只"])
    elif task == 'growth_dividend':
        base = after.get((day,'000922'))
        if base:
            for code,label in [('399006','创红比'),('000688','科创红利比')]:
                if (day,code) in after:
                    lines.append(f'{label}：{after[(day,code)] / base:.6f}')
    return lines


def notify_collection(task):
    """Capture committed changes while holding the collector's business lock."""
    def decorate(function):
        signature = inspect.signature(function)
        @wraps(function)
        def wrapped(self, *args, **kwargs):
            call = signature.bind(self, *args, **kwargs)
            bound = call.arguments
            state = bound.get('state') or {}
            slot = bound.get('slot') or state.get('current_scheduled_slot')
            # Preserve trigger classification for logs; both triggers notify on changes.
            scheduled = slot is not None or (task == 'growth_dividend' and state.get('trigger') == 'startup')
            before = None
            try:
                reason = 'disabled' if not enabled(self.settings, task) else None
                if reason:
                    LOG.info('Collection notification skipped task=%s reason=%s', task, reason)
                else:
                    before = published_values(self, task)
            except Exception as exc:
                LOG.warning('Collection notification snapshot failed task=%s error_type=%s', task, type(exc).__name__)
            run = state.get('run_id') or uuid.uuid4().hex
            captured = {}
            def capture():
                try:
                    captured['after'] = published_values(self, task)
                    with self.db() as db:
                        captured['state'] = json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
                except Exception as exc:
                    LOG.warning('Collection notification final snapshot failed task=%s error_type=%s', task, type(exc).__name__)
            if before is not None and bound.get('lock') is not None:
                original_lock = bound['lock']
                class CaptureLock:
                    def release(lock_self):
                        capture()
                        return original_lock.release()
                    def __getattr__(lock_self, name):
                        return getattr(original_lock, name)
                call.arguments['lock'] = CaptureLock()
            raised = False
            try:
                return function(*call.args, **call.kwargs)
            except BaseException:
                raised = True
                raise
            finally:
                if before is not None and 'after' in captured and 'state' in captured:
                    try:
                        after = captured['after']
                        changes = {key: value for key,value in after.items() if key not in before or before[key] != value}
                        latest = captured['state']
                        failed = raised or latest.get('error') or latest.get('phase') in {'failed','cancelled','interrupted'}
                        failed = failed or any(word in latest.get('stage', '') for word in ('已停止','采集中断'))
                        outcome = latest.get('outcome') or ('failed' if failed else 'completed')
                        if changes:
                            emit_collection_result(self.settings,task,scheduled=scheduled,run_key=run,outcome=outcome,
                                                   changed_count=len(changes),summary=summarize_changes(task,changes,after))
                        else:
                            LOG.info('Collection notification skipped task=%s reason=no_business_change', task)
                    except Exception as exc:
                        LOG.warning('Collection notification result failed task=%s error_type=%s', task, type(exc).__name__)
        return wrapped
    return decorate
