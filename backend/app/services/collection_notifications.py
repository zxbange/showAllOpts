"""Opt-in collection-result Telegram outbox; never a dependency of collection success."""
from contextvars import ContextVar
from datetime import date, datetime
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

from app.services.collection_errors import safe_error, sanitize_error

LOG = logging.getLogger(__name__)
LABELS = {'etf': '国家队动态', 'margin': '两融担保比例', 'commodities': 'A00 铝',
          'growth_dividend': '创红比和科创红利比', 'market': '市场热度和抱团拥挤度',
          'valuation_ruler': '估值标尺', 'cci': 'CCI 动力煤', 'cash_cow': '奶牛股', 'dividend_selection': '高股息（银行 / 非银行）'}
_WORKERS = {}
_WORKERS_LOCK = threading.Lock()
_CAPTURE = ContextVar('collection_notification_outcome', default=None)
_RESULT_LABELS = {
    'success': '成功', 'complete': '成功', 'completed': '成功',
    'partial': '部分完成', 'publication_pending': '部分完成（数据发布待确认）',
    'failed': '失败', 'error': '失败', 'cancelled': '已取消', 'interrupted': '已中断',
    'skipped': '未采集', 'blocked': '未采集', 'paused': '已暂停',
    'no_quote': '部分完成（来源尚无目标日报价）',
}


def capture_task_outcome(task, outcome, error_details=None):
    """Share the exact terminal decision with the active decorator, not an emitter."""
    active = _CAPTURE.get()
    if active is not None and active[0] == task:
        active[1].update(outcome=outcome, error_details=error_details)


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


MAX_TELEGRAM_UNITS = 3900
_CHINA = ZoneInfo('Asia/Shanghai')
_ERROR_STAGES = {'fetch': '数据读取', 'validate': '数据核验', 'database_write': '数据库写入',
                 'cache_publish': '缓存发布', 'collection': '采集', 'database_sync': '数据库同步'}
_ERROR_OPERATIONS = {'schema': '检查表结构', 'lock': '锁定快照', 'read': '读取快照',
                     'write': '写入快照', 'commit': '提交事务', 'cache_refresh': '刷新缓存',
                     'cache_verify': '核验缓存', 'local_publish': '发布本地快照'}


def _data_day(value):
    if type(value) is date:
        return value.isoformat()
    if isinstance(value, str) and len(value) == 10:
        try:
            parsed = date.fromisoformat(value)
            if value == parsed.isoformat():
                return value
        except ValueError:
            pass
    return None


def _format_slice(lines, units=MAX_TELEGRAM_UNITS):
    # The outbox sends one plain text message; keep it within the Telegram
    # UTF-16 limit even for non-BMP characters, without silently cutting a row.
    rendered = []
    marker = '其余项目请在页面查看。'
    remaining = units - len(marker) - 1
    for line in lines:
        for raw in str(line).splitlines() or ['']:
            clean = ''.join(c for c in raw[:550] if not 0xD800 <= ord(c) <= 0xDFFF)
            length = sum(2 if ord(c) > 0xFFFF else 1 for c in clean) + 1
            if length > remaining:
                return '\n'.join([*rendered, marker])
            rendered.append(clean)
            remaining -= length
    return '\n'.join(rendered)


def format_result(task_id, outcome, changed_count, summary, collected_at=None, *, manual=False,
                  error_details=None, data_date=None, retrying=False):
    try:
        moment = datetime.fromisoformat(collected_at) if isinstance(collected_at, str) else collected_at
        if moment is None or moment.utcoffset() is None:
            raise ValueError
        stamp = moment.astimezone(_CHINA).isoformat(timespec='seconds')
    except (ValueError, TypeError, AttributeError):
        stamp = datetime.now(_CHINA).isoformat(timespec='seconds')
    status = _RESULT_LABELS.get(outcome, '结果未核实')
    if status == '成功' and changed_count == 0:
        status += '（无新增或变更）'
    error = sanitize_error(error_details) if outcome not in ('success', 'complete', 'completed') else None
    if error is None and outcome in ('cancelled', 'interrupted', 'failed', 'error', 'partial', 'publication_pending'):
        error = safe_error(code=outcome if outcome in ('cancelled', 'interrupted', 'publication_pending') else 'unknown')
    lines = [LABELS[task_id], '触发方式：' + ('手动采集' if manual else '自动采集'),
             '采集时间：' + stamp, '采集结果：' + status,
             '目标数据日期：' + (_data_day(data_date) or '未核实'),
             '更新记录：' + (str(changed_count) if type(changed_count) is int and changed_count >= 0 else '未核实')]
    if error:
        tokens = []
        if error.get('stage') in _ERROR_STAGES:
            tokens.append(_ERROR_STAGES[error['stage']])
        if error.get('operation') in _ERROR_OPERATIONS:
            tokens.append(_ERROR_OPERATIONS[error['operation']])
        if error.get('trade_date'):
            tokens.append(error['trade_date'])
        elif error.get('start_date') and error.get('end_date'):
            tokens.append(error['start_date'] + ' 至 ' + error['end_date'])
        for field, label in (('mysql_errno', 'MySQL'), ('sqlite_errno', 'SQLite'), ('http_status', 'HTTP')):
            if field in error:
                tokens.append(f'{label} {error[field]}')
        lines.append('原因：' + error['message'] + ('（' + '，'.join(tokens) + '）' if tokens else ''))
    if retrying:
        lines.append('此结果仅对应本次采集尝试。')
    lines.extend(summary or ['已发布数据：暂无可核实明细'])
    return _format_slice(lines)


def emit_collection_result(settings, task_id, *, scheduled, run_key, outcome,
                           changed_count, summary, collected_at=None, manual_result=None,
                           error_details=None, data_date=None, retrying=False):
    """Enqueue an actual terminal event; same policy/schema for both triggers."""
    try:
        if outcome in ('running', 'queued', 'publishing') or (scheduled and outcome in ('skipped', 'blocked')):
            return False
        if not scheduled and manual_result is False and not changed_count:  # Legacy explicit import.
            return False
        if not enabled(settings, task_id):
            LOG.info('Collection notification skipped task=%s reason=disabled', task_id)
            return False
        message = format_result(task_id, outcome, changed_count, summary, collected_at,
                                manual=not scheduled, error_details=error_details,
                                data_date=data_date, retrying=retrying)
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
            scheduled = slot is not None or (task == 'growth_dividend' and state.get('trigger') == 'startup')
            # Explicit historical import is not a website collection. Preserve
            # its existing change-only eligibility.
            regular_collection = not (task == 'valuation_ruler' and state.get('phase') == 'joinquant')
            before = report_before = None
            opted_in = False
            try:
                opted_in = enabled(self.settings, task)
                if opted_in:
                    before = published_values(self, task)
                    if task == 'cash_cow':
                        with self.db() as db:
                            report_before = {r[0]: json.loads(r[1]) for r in db.execute(
                                'SELECT years,payload FROM snapshots')}
                else:
                    LOG.info('Collection notification skipped task=%s reason=disabled', task)
            except Exception as exc:
                LOG.warning('Collection notification snapshot failed task=%s error_type=%s', task, type(exc).__name__)
            run = state.get('run_id') or uuid.uuid4().hex
            captured = {}

            def capture():
                try:
                    captured['after'] = published_values(self, task)
                except Exception as exc:
                    LOG.warning('Collection notification final snapshot failed task=%s error_type=%s', task, type(exc).__name__)
                try:
                    with self.db() as db:
                        captured['state'] = json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
                        if task == 'cash_cow':
                            captured['report_values'] = {r[0]: json.loads(r[1]) for r in db.execute(
                                'SELECT years,payload FROM snapshots')}
                except Exception as exc:
                    LOG.warning('Collection notification final state unavailable task=%s error_type=%s', task, type(exc).__name__)

            if opted_in and bound.get('lock') is not None:
                original_lock = bound['lock']
                class CaptureLock:
                    def release(lock_self):
                        capture()
                        return original_lock.release()
                    def __getattr__(lock_self, name):
                        return getattr(original_lock, name)
                call.arguments['lock'] = CaptureLock()
            token = _CAPTURE.set((task, captured)) if opted_in else None
            raised = False
            try:
                return function(*call.args, **call.kwargs)
            except BaseException as exc:
                raised = safe_error(exc, stage='collection')
                raise
            finally:
                if token is not None:
                    _CAPTURE.reset(token)
                if opted_in:
                    try:
                        after = captured.get('after')
                        changes = ({key: value for key, value in after.items() if key not in before or before[key] != value}
                                   if before is not None and after is not None else None)
                        latest = captured.get('state') or {}
                        outcome = (('failed' if raised else None) or captured.get('outcome') or latest.get('outcome')
                                   or (latest.get('phase') if latest.get('phase') in _RESULT_LABELS else None)
                                   or ('failed' if latest.get('error') else 'completed' if 'state' in captured else 'unknown'))
                        if outcome == 'partial' and latest.get('outcome') == 'no_quote':
                            outcome = 'no_quote'
                        if regular_collection or changes:
                            data_date = next((_data_day(latest.get(key)) for key in ('targetDate', 'target_date', 'due_day')
                                              if _data_day(latest.get(key))), None)
                            try:
                                from app.services.collection_summary import summarize_published
                                values = captured.get('report_values', after) or {}
                                if data_date and task != 'cash_cow':
                                    # Do not present previously published newer dates
                                    # as part of a bounded/manual recovery.
                                    values = {key: value for key, value in values.items()
                                              if (day := _data_day(key[0] if isinstance(key, tuple) else key))
                                              and day <= data_date}
                                summary = summarize_published(task, values, previous=report_before)
                            except Exception as exc:
                                summary = []
                                LOG.warning('Collection notification summary unavailable task=%s error_type=%s', task, type(exc).__name__)
                            changed_count = len(changes) if changes is not None else None
                            if task == 'cash_cow' and latest.get('changed_count_unconfirmed'):
                                # Local snapshots cannot establish the remote write count
                                # while a commit/publication acknowledgement is unresolved.
                                changed_count = None
                            emit_collection_result(self.settings, task, scheduled=scheduled, run_key=run, outcome=outcome,
                                                   changed_count=changed_count,
                                                   summary=summary, manual_result=regular_collection,
                                                   error_details=raised or captured.get('error_details'), data_date=data_date)
                    except Exception as exc:
                        LOG.warning('Collection notification result failed task=%s error_type=%s', task, type(exc).__name__)
        return wrapped
    return decorate
