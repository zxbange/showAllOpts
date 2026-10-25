"""Persistent social monitoring, independent of the market snapshot database."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import json
import logging
import os
import random
import sqlite3
import threading
import time
import uuid

from cryptography.fernet import Fernet
import portalocker
import httpx

from app.services.social_sources import (fetch_posts, message_parts, PlatformIntervention,
                                         RateLimited, MonitoringDeferred)

LOG = logging.getLogger(__name__)
# HTTPX logs request URLs at INFO, which would expose Telegram bot credentials.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
CHINA = timezone(timedelta(hours=8))


def now():
    return datetime.now(CHINA).isoformat(timespec='seconds')


def night_time(moment):
    return moment.astimezone(CHINA).hour < 7


def next_check_time(moment, delay=0):
    moment = moment.astimezone(CHINA)
    if night_time(moment):
        return moment.replace(hour=7, minute=0, second=0, microsecond=0).timestamp()
    candidate = moment + timedelta(seconds=delay)
    if candidate.date() != moment.date() or night_time(candidate):
        candidate = candidate.replace(hour=7, minute=0, second=0, microsecond=0)
    return candidate.timestamp()


def retention_cutoff(moment):
    """Keep last year's same date onward, with Feb 29 falling back to Feb 28."""
    day = moment.astimezone(CHINA).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)


def published_time(value):
    try:
        moment = datetime.fromisoformat(str(value))
        return moment.replace(tzinfo=CHINA) if moment.tzinfo is None else moment.astimezone(CHINA)
    except (ValueError, TypeError):
        return None


class SocialMonitor:
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.log_path / 'social'
        self.root.mkdir(parents=True, exist_ok=True)
        # Serialize key creation across API workers. Never replace an existing key.
        with portalocker.Lock(str(self.root / 'key.lock'), timeout=10):
            key_path = self.root / 'secrets.key'
            if not key_path.exists():
                descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'wb') as output:
                    output.write(Fernet.generate_key())
            self.cipher = Fernet(key_path.read_bytes())
        self.stop_event = threading.Event()
        self.thread = None
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
                INSERT OR IGNORE INTO config VALUES(1, '{"enabled":false,"token":"","chat_id":"","weibo_cookie":"","xueqiu_cookie":"","next_check":0,"last_check":null,"last_error":null}');
                CREATE TABLE IF NOT EXISTS authors (
                    id INTEGER PRIMARY KEY, platform TEXT NOT NULL, uid TEXT NOT NULL,
                    name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    initialized INTEGER NOT NULL DEFAULT 0, last_check TEXT, last_error TEXT,
                    UNIQUE(platform,uid));
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY, author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
                    platform TEXT NOT NULL, uid TEXT NOT NULL, post_id TEXT NOT NULL,
                    payload TEXT NOT NULL, published_at TEXT NOT NULL, discovered_at TEXT NOT NULL,
                    status TEXT NOT NULL, next_part INTEGER NOT NULL DEFAULT 0,
                    sent_at TEXT, last_error TEXT, UNIQUE(platform,uid,post_id));
                CREATE INDEX IF NOT EXISTS posts_author ON posts(author_id,id);
            ''')
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(authors)')}
            for name, definition in {'last_success_at': 'TEXT', 'scan_since': 'TEXT',
                                     'scan_page': 'INTEGER NOT NULL DEFAULT 1', 'added_at': 'TEXT', 'screen_name': 'TEXT'}.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE authors ADD COLUMN {name} {definition}')
            db.execute("UPDATE authors SET screen_name=(SELECT json_extract(p.payload,'$.name') FROM posts p "
                       "WHERE p.author_id=authors.id AND json_extract(p.payload,'$.name')<>authors.uid "
                       "AND json_extract(p.payload,'$.name')<>'' ORDER BY p.id DESC LIMIT 1) "
                       "WHERE screen_name IS NULL OR screen_name=''")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / 'monitor.sqlite3', timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def encode(self, value):
        return self.cipher.encrypt(value.encode()).decode() if value else ''

    def decode(self, value):
        return self.cipher.decrypt(value.encode()).decode() if value else ''

    def config(self, db):
        return json.loads(db.execute('SELECT payload FROM config WHERE id=1').fetchone()[0])

    def write_config(self, db, value):
        db.execute('UPDATE config SET payload=? WHERE id=1', (json.dumps(value),))

    def update_config(self, changes):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            config = self.config(db)
            for key in ('token', 'chat_id', 'weibo_cookie', 'xueqiu_cookie'):
                if changes.get(key) is not None:
                    config[key] = self.encode(changes[key].strip())
            if 'enabled' in changes and changes['enabled'] is not None:
                was_enabled = config['enabled']
                config['enabled'] = changes['enabled']
                if config['enabled']:
                    if not config['token'] or not config['chat_id']:
                        raise ValueError('请先配置 Telegram Bot Token 和 Chat ID')
                    if not db.execute('SELECT 1 FROM authors WHERE enabled=1 LIMIT 1').fetchone():
                        raise ValueError('请先添加至少一个已启用的监控用户；输入主页后需要点击“添加用户”')
                    config['next_check'] = next_check_time(datetime.now(CHINA))
                    if not was_enabled:
                        config['enabled_since'] = now()
            self.write_config(db, config)

    def overview(self):
        with self.db() as db:
            config = self.config(db)
            authors = [dict(row) for row in db.execute('SELECT * FROM authors ORDER BY id')]
            counts = dict(db.execute('SELECT status,count(*) FROM posts GROUP BY status').fetchall())
        return {
            'enabled': config['enabled'], 'interval_min': 180, 'interval_max': 360,
            'night_rest': night_time(datetime.now(CHINA)),
            'platforms': config.get('platforms', {}),
            'last_check': config['last_check'], 'last_error': config['last_error'],
            'last_cleanup_date': config.get('last_cleanup_date'),
            'last_cleanup_count': config.get('last_cleanup_count', 0),
            'next_check': datetime.fromtimestamp(config['next_check'], CHINA).isoformat() if config['next_check'] else None,
            'token_configured': bool(config['token']), 'chat_id_configured': bool(config['chat_id']),
            'weibo_cookie_configured': bool(config['weibo_cookie']),
            'xueqiu_cookie_configured': bool(config['xueqiu_cookie']),
            'authors': authors, 'counts': counts,
        }

    def add_author(self, platform, uid, name):
        try:
            with self.db() as db:
                db.execute('INSERT INTO authors(platform,uid,name,added_at) VALUES(?,?,?,?)', (platform, uid, name or uid, now()))
        except sqlite3.IntegrityError:
            raise ValueError('这个平台用户已经在监控列表中') from None

    def change_author(self, identifier, enabled=None, delete=False):
        with self.db() as db:
            if delete:
                result = db.execute('DELETE FROM authors WHERE id=?', (identifier,))
            else:
                result = db.execute('UPDATE authors SET enabled=? WHERE id=?', (int(enabled), identifier))
            return result.rowcount > 0

    def posts(self, author_id, platform, page, size, keyword=''):
        conditions, values = ['1=1'], []
        if author_id is not None:
            conditions.append('p.author_id=?')
            values.append(author_id)
        if platform:
            conditions.append('p.platform=?')
            values.append(platform)
        if keyword.strip():
            conditions.append("instr(lower(COALESCE(json_extract(p.payload,'$.text'),'')),lower(?))>0")
            values.append(keyword.strip())
        where = ' AND '.join(conditions)
        with self.db() as db:
            total = db.execute(f'SELECT count(*) FROM posts p WHERE {where}', values).fetchone()[0]
            rows = db.execute(f"SELECT p.*,COALESCE(NULLIF(a.screen_name,''),NULLIF(a.name,a.uid),'待获取博主名称') AS author_name FROM posts p JOIN authors a ON a.id=p.author_id "
                              f'WHERE {where} ORDER BY p.published_at DESC,p.id DESC LIMIT ? OFFSET ?',
                              (*values, size, (page - 1) * size)).fetchall()
        return {'total': total, 'page': page, 'items': [dict(row, payload=json.loads(row['payload'])) for row in rows]}

    def update_author_name(self, identifier, name):
        with self.db() as db:
            db.execute("UPDATE authors SET screen_name=? WHERE id=? AND uid<>? AND screen_name IS NOT ?", (name, identifier, name, name))

    def collect(self, author, posts, baseline=None, finalize=True, notify_since=None):
        discovered = now()
        cutoff = retention_cutoff(datetime.fromisoformat(discovered))
        with self.db() as db:
            # Re-read after network I/O: user may have paused/deleted the author.
            active = db.execute('SELECT * FROM authors WHERE id=? AND enabled=1', (author['id'],)).fetchone()
            if not active:
                return False
            for post in posts:
                if post.get('name') and post['name'] != active['uid'] and (post['name'] != active['name'] or not active['screen_name']):
                    db.execute('UPDATE authors SET screen_name=? WHERE id=?', (post['name'], author['id']))
                published = published_time(post['time'])
                if published is not None and published < cutoff:
                    # Old pinned posts must not be reinserted/notified after cleanup.
                    continue
                historical = not active['initialized'] if baseline is None else baseline
                if notify_since is not None and published is not None and published >= notify_since:
                    historical = False
                db.execute('INSERT OR IGNORE INTO posts(author_id,platform,uid,post_id,payload,published_at,discovered_at,status) '
                           'VALUES(?,?,?,?,?,?,?,?)',
                           (author['id'], author['platform'], author['uid'], post['id'], json.dumps(post, ensure_ascii=False),
                            post['time'], discovered, 'baseline' if historical else 'pending'))
            if finalize:
                db.execute('UPDATE authors SET initialized=1,last_check=?,last_error=NULL WHERE id=?', (now(), author['id']))
        return True

    def platform_state(self, platform, **changes):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            config = self.config(db)
            state = config.setdefault('platforms', {}).setdefault(platform, {})
            state.update(changes)
            self.write_config(db, config)
            return dict(state)

    def pause_platform(self, platform, reason, reason_code='platform_restricted', http_status=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            config = self.config(db)
            state = config.setdefault('platforms', {}).setdefault(platform, {})
            if not state.get('paused'):
                state.update(paused=True, reason=reason, paused_at=now(), incident_id=uuid.uuid4().hex,
                             alert_sent_at=None, alert_next_try=0, reason_code=reason_code, http_status=http_status)
            self.write_config(db, config)

    def resume_platform(self, platform):
        self.platform_state(platform, paused=False, reason=None, retry_at=0, failures=0,
                            incident_id=None, alert_sent_at=None, alert_next_try=0, reason_code=None, http_status=None)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            config = self.config(db)
            config['next_check'] = next_check_time(datetime.now(CHINA))
            self.write_config(db, config)

    def send_alerts(self):
        with self.db() as db:
            config = self.config(db)
        if not config['token'] or not config['chat_id']:
            return
        for platform, state in config.get('platforms', {}).items():
            if not state.get('paused') or state.get('alert_sent_at') or time.time() < state.get('alert_next_try', 0):
                continue
            succeeded = False
            try:
                label = '微博' if platform == 'weibo' else '雪球'
                self.send_part(self.decode(config['token']), self.decode(config['chat_id']),
                               f'ShowAllOpts：{label}监控已自动暂停。\n{state["reason"]}\n'
                               '请在浏览器人工处理登录/验证码，更新 Cookie 后，在监控页面点击“人工处理完成，恢复”。')
                succeeded = True
            except Exception as error:
                LOG.warning('Social intervention alert failed platform=%s type=%s', platform, type(error).__name__)
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                current = self.config(db)
                active = current.get('platforms', {}).get(platform, {})
                if active.get('incident_id') == state.get('incident_id'):
                    active['alert_next_try'] = time.time() + random.uniform(180, 360)
                    if succeeded:
                        active['alert_sent_at'] = now()
                    self.write_config(db, current)

    def cleanup_posts(self, moment=None):
        moment = (moment or datetime.now(CHINA)).astimezone(CHINA)
        today = moment.date().isoformat()
        cutoff = retention_cutoff(moment).isoformat()
        with self.db() as db:
            if self.config(db).get('last_cleanup_date') == today:
                return 0
            db.execute('BEGIN IMMEDIATE')
            config = self.config(db)
            if config.get('last_cleanup_date') == today:
                return 0
            # Unknown publication dates expire by discovery time instead.
            result = db.execute('DELETE FROM posts WHERE '
                                'COALESCE(julianday(published_at),julianday(discovered_at)) < julianday(?)', (cutoff,))
            count = result.rowcount
            config.update(last_cleanup_date=today, last_cleanup_count=count)
            self.write_config(db, config)
        LOG.info('Social post cleanup date=%s removed=%s cutoff=%s', today, count, cutoff)
        return count

    def send_part(self, token, chat_id, text):
        try:
            with httpx.Client(timeout=30) as client:
                response = client.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                       json={'chat_id': chat_id, 'text': text})
                result = response.json()
                if not response.is_success or not isinstance(result, dict) or result.get('ok') is not True:
                    raise RuntimeError(f'Telegram 发送失败（HTTP {response.status_code}），请检查 Token、Chat ID 或限流')
        except (httpx.HTTPError, ValueError):
            raise RuntimeError('Telegram 网络或响应异常，下轮自动重试') from None

    def test_telegram(self):
        with self.db() as db:
            config = self.config(db)
        if not config['token'] or not config['chat_id']:
            raise ValueError('请先保存 Telegram Bot Token 和 Chat ID')
        self.send_part(self.decode(config['token']), self.decode(config['chat_id']), 'ShowAllOpts 博主监控：Telegram 连接测试成功。')

    def deliver(self, config):
        with self.db() as db:
            rows = db.execute("SELECT p.* FROM posts p JOIN authors a ON a.id=p.author_id "
                              "WHERE p.status='pending' AND a.enabled=1 ORDER BY p.id LIMIT 100").fetchall()
        token, chat_id = self.decode(config['token']), self.decode(config['chat_id'])
        for row in rows:
            parts = message_parts(row['platform'], json.loads(row['payload']))
            for index in range(row['next_part'], len(parts)):
                if self.stop_event.is_set() or night_time(datetime.now(CHINA)):
                    return
                with self.db() as db:
                    current = self.config(db)
                    author = db.execute('SELECT enabled FROM authors WHERE id=?', (row['author_id'],)).fetchone()
                if not current['enabled']:
                    return
                if current.get('platforms', {}).get(row['platform'], {}).get('paused'):
                    break
                if not author or not author['enabled']:
                    break
                try:
                    self.send_part(token, chat_id, parts[index])
                except RuntimeError as error:
                    with self.db() as db:
                        db.execute('UPDATE posts SET last_error=? WHERE id=?', (str(error), row['id']))
                    raise
                with self.db() as db:
                    db.execute('UPDATE posts SET next_part=?,status=?,sent_at=?,last_error=NULL WHERE id=?',
                               (index + 1, 'sent' if index == len(parts) - 1 else 'pending',
                                now() if index == len(parts) - 1 else None, row['id']))
                self.stop_event.wait(1.1)

    def cycle(self):
        moment = datetime.now(CHINA)
        if night_time(moment):
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                config = self.config(db)
                config['next_check'] = next_check_time(moment)
                self.write_config(db, config)
            return
        with self.db() as db:
            config = self.config(db)
            authors = [dict(row) for row in db.execute('SELECT * FROM authors WHERE enabled=1')]
        error_message = None
        for author in authors:
            if self.stop_event.is_set():
                return
            with self.db() as db:
                current = self.config(db)
                if not current['enabled']:
                    return
                state = current.get('platforms', {}).get(author['platform'], {})
                if state.get('paused') or time.time() < state.get('retry_at', 0):
                    continue
                known = {row[0] for row in db.execute('SELECT post_id FROM posts WHERE author_id=?', (author['id'],))}
            try:
                scan_since = author['scan_since'] or author['last_success_at'] or author['last_check']
                scan_started = now()
                added = published_time(author['added_at'])
                enabled_since = published_time(current.get('enabled_since'))
                tracking_start = max(filter(None, (added, enabled_since)), default=None)
                notify_since = None
                if not author['initialized'] and tracking_start:
                    midnight = tracking_start.replace(hour=0, minute=0, second=0, microsecond=0)
                    notify_since = midnight if night_time(tracking_start) else midnight + timedelta(days=1)
                    if notify_since > moment:
                        notify_since = None
                    elif not author['scan_since']:
                        scan_since = notify_since.isoformat()
                if not scan_since:
                    scan_since = scan_started
                with self.db() as db:
                    db.execute('UPDATE authors SET scan_since=? WHERE id=?', (scan_since, author['id']))

                def should_stop():
                    if self.stop_event.is_set() or night_time(datetime.now(CHINA)):
                        return True
                    with self.db() as db:
                        row = db.execute('SELECT enabled FROM authors WHERE id=?', (author['id'],)).fetchone()
                        cfg = self.config(db)
                        return not row or not row['enabled'] or not cfg['enabled'] or cfg.get('platforms', {}).get(author['platform'], {}).get('paused', False)

                def save_page(posts, next_page, complete):
                    if not self.collect(author, posts, baseline=not author['initialized'], finalize=False, notify_since=notify_since):
                        raise MonitoringDeferred('用户已暂停或删除，本页补抓进度保留')
                    with self.db() as db:
                        db.execute('UPDATE authors SET scan_page=? WHERE id=?', (next_page, author['id']))

                source = dict(author, cookie=self.decode(current[author['platform'] + '_cookie']),
                              _known_ids=known, _scan_since=scan_since, _scan_page=author['scan_page'],
                              _initial=not author['initialized'] and notify_since is None,
                              _stop_before=notify_since.isoformat() if notify_since else None,
                              _should_stop=should_stop, _on_page=save_page,
                              _on_author_name=lambda name: self.update_author_name(author['id'], name),
                              _wait=self.stop_event.wait, _retention_cutoff=retention_cutoff(moment).isoformat())
                result = fetch_posts(source, pages=3 if source['_initial'] else 20)
                if not self.collect(author, result, baseline=not author['initialized'],
                                    finalize=getattr(result, 'complete', True), notify_since=notify_since):
                    raise MonitoringDeferred('用户已暂停或删除')
                if getattr(result, 'complete', True):
                    with self.db() as db:
                        db.execute('UPDATE authors SET last_success_at=?,scan_since=NULL,scan_page=1 WHERE id=?',
                                   (scan_started, author['id']))
                self.platform_state(author['platform'], failures=0, retry_at=0, reason=None, reason_code=None, http_status=None)
            except PlatformIntervention as error:
                self.pause_platform(author['platform'], str(error), error.reason_code, error.http_status)
                with self.db() as db:
                    db.execute('UPDATE authors SET last_check=?,last_error=? WHERE id=?', (now(), str(error), author['id']))
                error_message = '平台监控已暂停，需要人工处理；详情见平台状态'
            except RateLimited as error:
                self.platform_state(author['platform'], retry_at=time.time() + error.retry_after, reason=str(error), reason_code='rate_limited', http_status=None)
                with self.db() as db:
                    db.execute('UPDATE authors SET last_check=?,last_error=? WHERE id=?', (now(), str(error), author['id']))
                error_message = '平台已限流，已遵守等待时间暂缓请求'
            except MonitoringDeferred:
                break
            except Exception as error:
                # Only controlled RuntimeError strings are safe for UI/logs.
                message = str(error) if type(error) is RuntimeError else '抓取失败，请检查网络、UID、Cookie 或平台访问限制'
                if isinstance(error, httpx.TimeoutException):
                    message = '平台请求超时，已暂缓抓取；请检查服务器网络，系统会自动退避重试'
                elif isinstance(error, httpx.ConnectError):
                    message = '无法连接平台，已暂缓抓取；请检查服务器网络、域名解析或代理设置'
                elif isinstance(error, httpx.HTTPError):
                    message = '平台网络传输异常，已暂缓抓取，系统会自动退避重试'
                with self.db() as db:
                    db.execute('UPDATE authors SET last_check=?,last_error=? WHERE id=?', (now(), message, author['id']))
                error_message = '部分用户抓取失败，请查看用户列表'
                failures = state.get('failures', 0) + 1
                self.platform_state(author['platform'], failures=failures,
                                    reason=message, reason_code='fetch_failed', http_status=None,
                                    retry_at=time.time() + min(21600, random.uniform(180, 360) * 2 ** min(failures - 1, 6)))
                LOG.warning('Social fetch failed platform=%s uid=%s type=%s', author['platform'], author['uid'], type(error).__name__)
        try:
            self.send_alerts()
            self.deliver(config)
        except Exception as error:
            error_message = str(error) if type(error) is RuntimeError else 'Telegram 发送失败，已保留待发送帖子'
            LOG.warning('Social delivery failed type=%s', type(error).__name__)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.config(db)
            current.update(last_check=now(), last_error=error_message,
                           next_check=next_check_time(datetime.now(CHINA), random.uniform(180, 360)))
            self.write_config(db, current)

    def start(self):
        if self.settings.social_worker_enabled:
            self.thread = threading.Thread(target=self.run, name='social-monitor', daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            # Requests have finite timeouts; the thread checks stop between authors/parts.
            self.thread.join(timeout=45)

    def run(self):
        while not self.stop_event.is_set():
            try:
                # Exactly one background poller across Uvicorn workers sharing this directory.
                with portalocker.Lock(str(self.root / 'worker.lock'), timeout=0):
                    while not self.stop_event.is_set():
                        # Retention runs daily even when polling is paused.
                        self.cleanup_posts()
                        self.send_alerts()
                        with self.db() as db:
                            config = self.config(db)
                        if night_time(datetime.now(CHINA)):
                            wake = next_check_time(datetime.now(CHINA))
                            if config['next_check'] != wake:
                                with self.db() as db:
                                    db.execute('BEGIN IMMEDIATE')
                                    config = self.config(db)
                                    config['next_check'] = wake
                                    self.write_config(db, config)
                            self.stop_event.wait(1)
                            continue
                        if config['enabled'] and time.time() >= config['next_check']:
                            self.cycle()
                        self.stop_event.wait(1)
            except portalocker.exceptions.LockException:
                self.stop_event.wait(5)
            except Exception as error:
                LOG.error('Social worker failure type=%s', type(error).__name__)
                self.stop_event.wait(30)
