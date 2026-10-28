"""Independent, persistent Telegram and Feishu delivery for social notifications."""
import base64
import hashlib
import hmac
import json
import re
import time
from urllib.parse import urlsplit

import httpx
import portalocker


CHANNELS = ('telegram', 'feishu')
CHANNEL_LABELS = {'telegram': 'Telegram', 'feishu': '飞书'}


class NotificationSkipped(Exception):
    pass


class NotificationDeferred(Exception):
    pass


class NotificationError(RuntimeError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def enabled_channels(config):
    return [channel for channel in CHANNELS if config.get(channel + '_enabled', channel == 'telegram')]


def initial_delivery(config, legacy_next_part=None):
    channels = ('telegram',) if legacy_next_part is not None else enabled_channels(config)
    return {channel: {'next_part': legacy_next_part or 0, 'status': 'pending'} for channel in channels}


def aggregate_status(delivery):
    if any(state.get('status') == 'pending' for state in delivery.values()):
        return 'pending'
    return 'sent' if any(state.get('status') == 'sent' for state in delivery.values()) else 'muted'


def validate_webhook(value):
    parts = urlsplit(value)
    if (parts.scheme != 'https' or parts.netloc != 'open.feishu.cn' or parts.query or parts.fragment
            or not re.fullmatch(r'/open-apis/bot/v2/hook/[A-Za-z0-9_-]{16,256}', parts.path)):
        raise ValueError('飞书 Webhook 须为 open.feishu.cn 的 HTTPS 群机器人地址。')
    return value


def feishu_body(text, secret, timestamp=None):
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    signature = base64.b64encode(hmac.new((stamp + '\n' + secret).encode(), b'', hashlib.sha256).digest()).decode()
    return {'timestamp': stamp, 'sign': signature, 'msg_type': 'text', 'content': {'text': text}}


def feishu_parts(parts):
    """Keep each UTF-8 JSON body below 20 KB, including escaping and signature."""
    result = []
    for part in parts:
        chunk, size = [], 0
        for char in part:
            cost = len(json.dumps(char, ensure_ascii=False).encode('utf-8')) - 2
            if size + cost > 19000:
                result.append(''.join(chunk))
                chunk, size = [], 0
            chunk.append(char)
            size += cost
        if chunk:
            result.append(''.join(chunk))
    return result


def send_feishu(webhook, secret, text):
    validate_webhook(webhook)
    data = json.dumps(feishu_body(text, secret), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(data) > 20 * 1024:
        raise NotificationError('飞书消息超过 20 KB，请检查消息拆分。')
    try:
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            response = client.post(webhook, content=data, headers={'Content-Type': 'application/json; charset=utf-8'})
            if response.status_code == 429:
                retry = response.headers.get('Retry-After', '60')
                delay = min(3600, max(1, int(retry))) if retry.isdigit() else 60
                raise NotificationError('飞书发送触发限流，稍后自动重试。', delay)
            result = response.json()
            code = result.get('code') if isinstance(result, dict) else None
            if code == 11232:
                retry = response.headers.get('Retry-After', '60')
                delay = min(3600, max(1, int(retry))) if retry.isdigit() else 60
                raise NotificationError('飞书发送触发限流，稍后自动重试。', delay)
            if not response.is_success or type(code) is not int or code != 0:
                raise NotificationError('飞书发送失败，请检查 Webhook、签名密钥或群机器人设置。')
    except (httpx.HTTPError, ValueError):
        raise NotificationError('飞书网络或响应异常，稍后自动重试。') from None


class NotificationMixin:
    @staticmethod
    def init_notification_schema(db):
        for table in ('posts', 'cube_rebalances'):
            columns = {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')}
            if 'delivery' not in columns:
                db.execute('ALTER TABLE ' + table + ' ADD COLUMN delivery TEXT')

    def migrate_notifications(self, db, config):
        for table, status in (('posts', 'status'), ('cube_rebalances', 'notify_status')):
            for row in db.execute(f"SELECT id,next_part FROM {table} WHERE {status}='pending' AND delivery IS NULL").fetchall():
                routes = initial_delivery(config, legacy_next_part=row['next_part'])
                db.execute(f'UPDATE {table} SET delivery=? WHERE id=?', (json.dumps(routes), row['id']))
        for state in config.get('platforms', {}).values():
            if state.get('paused') and not state.get('alert_sent_at') and 'alert_delivery' not in state:
                state['alert_delivery'] = initial_delivery(config, legacy_next_part=0)

    def skip_disabled_deliveries(self, db, config):
        enabled = enabled_channels(config)
        for table, status in (('posts', 'status'), ('cube_rebalances', 'notify_status')):
            for row in db.execute(f"SELECT id,delivery FROM {table} WHERE {status}='pending'").fetchall():
                routes = json.loads(row['delivery'] or '{}')
                for channel, state in routes.items():
                    if channel not in enabled and state['status'] == 'pending':
                        state.update(status='skipped', last_error=None)
                error = next((state.get('last_error') for state in routes.values() if state.get('last_error')), None)
                db.execute(f'UPDATE {table} SET delivery=?,{status}=?,last_error=? WHERE id=?',
                           (json.dumps(routes), aggregate_status(routes), error, row['id']))
        for state in config.get('platforms', {}).values():
            for channel, delivery in state.get('alert_delivery', {}).items():
                if channel not in enabled and delivery['status'] == 'pending':
                    delivery.update(status='skipped', last_error=None)

    def send_channel(self, channel, text, require_enabled=True, guard=None):
        """Serialize sends across workers and re-read switches just before network I/O."""
        try:
            with portalocker.Lock(str(self.root / 'notification-send.lock'), timeout=35):
                if channel == 'feishu':
                    with self.db() as db:
                        config = self.config(db)
                        delay = max(0., config.get('notification_next_send', {}).get(channel, 0) - time.time())
                    if delay and self.stop_event.wait(min(delay, 1.0)):
                        raise NotificationDeferred()
                with self.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    current = self.config(db)
                    if channel not in enabled_channels(current):
                        raise NotificationSkipped()
                    if require_enabled and not current['enabled']:
                        raise NotificationDeferred()
                    if guard is not None and not guard(db, current):
                        raise NotificationDeferred()
                    retry_at = current.get('notification_retry_at', {}).get(channel, 0)
                    if retry_at > time.time():
                        raise NotificationError(CHANNEL_LABELS[channel] + '发送仍在限流等待期。')
                    if channel == 'feishu':
                        current.setdefault('notification_next_send', {})[channel] = time.time() + .65
                        self.write_config(db, current)
                try:
                    if channel == 'telegram':
                        self.send_part(self.decode(current['token']), self.decode(current['chat_id']), text)
                    else:
                        send_feishu(self.decode(current['feishu_webhook']), self.decode(current['feishu_secret']), text)
                except NotificationError as error:
                    if error.retry_after:
                        with self.db() as db:
                            db.execute('BEGIN IMMEDIATE')
                            active = self.config(db)
                            active.setdefault('notification_retry_at', {})[channel] = time.time() + error.retry_after
                            self.write_config(db, active)
                    raise
                except RuntimeError:
                    raise
                except Exception:
                    raise NotificationError(CHANNEL_LABELS[channel] + '通知配置或发送异常，请检查渠道设置。') from None
        except portalocker.exceptions.LockException:
            raise NotificationError('通知发送繁忙，请稍后重试。') from None

    @staticmethod
    def pending_channel_rows(db, sql, config, params=(), blocked=()):
        """Each channel gets its own bounded queue; one backlog cannot starve another."""
        result = {}
        for channel in enabled_channels(config):
            if channel in blocked:
                continue
            for row in db.execute(sql.format(channel=channel), params):
                item = dict(row)
                result[(item.get('kind', ''), item['id'])] = item
        return sorted(result.values(), key=lambda item: (item['event_time'], item.get('kind', ''), item['id']))

    def _save_delivery(self, table, status_column, identifier, channel, **changes):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute(f'SELECT delivery FROM {table} WHERE id=?', (identifier,)).fetchone()
            if row is None:
                return
            routes = json.loads(row['delivery'] or '{}')
            state = routes.get(channel)
            if state is None:
                return
            # Disabling during an in-flight part must not revive remaining parts.
            if state['status'] == 'skipped' and changes.get('status') == 'pending':
                changes.pop('status')
            state.update(changes)
            status = aggregate_status(routes)
            error = next((s.get('last_error') for s in routes.values() if s.get('last_error')), None)
            tg_next = routes.get('telegram', {}).get('next_part', 0)
            db.execute(f'UPDATE {table} SET delivery=?,{status_column}=?,next_part=?,last_error=?,sent_at=? WHERE id=?',
                       (json.dumps(routes, ensure_ascii=False), status, tg_next, error,
                        self.current_moment().isoformat(timespec='seconds') if status == 'sent' else None, identifier))

    def deliver_message(self, table, status_column, row, parts, active_check, blocked):
        errors = []
        routes = json.loads(row['delivery'] or '{}')
        if 'telegram' in routes and routes['telegram']['status'] == 'pending':
            routes['telegram']['next_part'] = max(routes['telegram']['next_part'], row['next_part'])
        for channel, state in routes.items():
            if state['status'] != 'pending' or channel in blocked:
                continue
            selected_parts = parts if channel == 'telegram' else feishu_parts(parts)
            for index in range(state['next_part'], len(selected_parts)):
                if self.stop_event.is_set():
                    return errors
                with self.db() as db:
                    config = self.config(db)
                    if not config['enabled'] or not active_check(db, config):
                        return errors
                    current = db.execute(f'SELECT delivery FROM {table} WHERE id=?', (row['id'],)).fetchone()
                    if current is None or json.loads(current['delivery'] or '{}').get(channel, {}).get('status') != 'pending':
                        break
                try:
                    self.send_channel(channel, selected_parts[index], guard=active_check)
                except NotificationSkipped:
                    self._save_delivery(table, status_column, row['id'], channel, status='skipped', last_error=None)
                    break
                except NotificationDeferred:
                    return errors
                except RuntimeError as error:
                    self._save_delivery(table, status_column, row['id'], channel, last_error=str(error))
                    blocked.add(channel)
                    errors.append(error)
                    break
                self._save_delivery(table, status_column, row['id'], channel, next_part=index+1,
                                    status='sent' if index == len(selected_parts)-1 else 'pending', last_error=None)
                self.stop_event.wait(1.1)
        return errors
