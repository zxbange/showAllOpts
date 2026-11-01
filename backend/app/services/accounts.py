"""Local accounts: Argon2id passwords, revocable sessions and durable login limits."""
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

COOKIE = 'showallopts_session'
SESSION_SECONDS = 8 * 3600
IDLE_SECONDS = 2 * 3600
KDF_SLOTS = threading.BoundedSemaphore(2)


class AccountError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise AccountError('密码须为12～128个字符，建议使用较长的随机密码')
    if not KDF_SLOTS.acquire(timeout=2):
        raise AccountError('登录服务繁忙，请稍后重试', 429)
    try:
        return Argon2id(salt=os.urandom(16), length=32, iterations=3,
                        lanes=1, memory_cost=65536).derive_phc_encoded(password.encode())
    finally:
        KDF_SLOTS.release()


def password_matches(password, encoded):
    if not KDF_SLOTS.acquire(timeout=2):
        raise AccountError('登录服务繁忙，请稍后重试', 429)
    try:
        Argon2id.verify_phc_encoded(password.encode(), encoded)
        return True
    except InvalidKey:
        return False
    finally:
        KDF_SLOTS.release()


@lru_cache(maxsize=1)
def dummy_hash():
    return password_hash(secrets.token_urlsafe(24))


def username(value):
    value = value.strip().lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,31}', value):
        raise AccountError('用户名须为3～32位字母、数字、下划线、点或短横线')
    return value


def public(row):
    return {k: row[k] for k in ('id', 'username', 'role', 'enabled', 'created_at')}


class Accounts:
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.log_path / 'accounts'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'accounts.sqlite3'
        if self.root.is_symlink() or self.path.is_symlink():
            raise AccountError('账号存储路径不能为符号链接', 503)
        if os.name == 'posix':
            self.root.chmod(0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        if os.name == 'posix':
            self.path.chmod(0o600)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','viewer')),
                    enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at REAL NOT NULL, touched_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
                CREATE TABLE IF NOT EXISTS attempts (bucket TEXT PRIMARY KEY, since REAL NOT NULL, count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT, action TEXT NOT NULL, target TEXT);
            ''')
        # Unknown usernames perform the same expensive password check.
        self.dummy = dummy_hash()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def configured(self):
        with self.db() as db:
            return bool(db.execute('SELECT 1 FROM users LIMIT 1').fetchone())

    def audit(self, db, actor, action, target):
        db.execute('INSERT INTO audit(at,actor,action,target) VALUES (?,?,?,?)',
                   (time.time(), actor, action, target))

    def create(self, name, password, role='viewer', actor=None, bootstrap=False):
        name = username(name)
        if role not in ('admin', 'viewer'):
            raise AccountError('用户角色无效')
        encoded, ident = password_hash(password), secrets.token_hex(16)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if bootstrap:
                if db.execute('SELECT 1 FROM users LIMIT 1').fetchone():
                    raise AccountError('已存在账号，请使用管理员登录或终端重置密码', 409)
                role = 'admin'
            else:
                self.assert_admin(db, actor)
            try:
                db.execute('INSERT INTO users VALUES (?,?,?,?,1,?)', (ident, name, encoded, role, time.time()))
            except sqlite3.IntegrityError:
                raise AccountError('用户名已存在', 409) from None
            self.audit(db, actor, 'create', ident)
            return public(db.execute('SELECT * FROM users WHERE id=?', (ident,)).fetchone())

    @staticmethod
    def assert_admin(db, actor):
        if not db.execute("SELECT 1 FROM users WHERE id=? AND role='admin' AND enabled=1", (actor,)).fetchone():
            raise AccountError('此操作需要管理员权限', 403)

    def login(self, name, password, ip):
        now = time.time()
        name = name.strip().lower()
        buckets = [(digest('user:' + name), 10), (digest('ip:' + ip), 40)]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM attempts WHERE since<?', (now - 900,))
            db.execute('DELETE FROM sessions WHERE expires_at<=? OR touched_at<=?', (now, now-IDLE_SECONDS))
            for bucket, limit in buckets:
                row = db.execute('SELECT count FROM attempts WHERE bucket=?', (bucket,)).fetchone()
                if row and row[0] >= limit:
                    raise AccountError('尝试过于频繁，请15分钟后重试', 429)
            for bucket, _ in buckets:
                db.execute('INSERT INTO attempts VALUES (?,?,1) ON CONFLICT(bucket) DO UPDATE SET count=count+1', (bucket, now))
            user = db.execute('SELECT * FROM users WHERE username=?', (name,)).fetchone()
        valid = password_matches(password, user['password_hash'] if user else self.dummy)
        if not valid or not user or not user['enabled']:
            raise AccountError('用户名或密码错误', 401)
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM users WHERE id=?', (user['id'],)).fetchone()
            if not current or not current['enabled'] or current['password_hash'] != user['password_hash']:
                raise AccountError('账号状态已变化，请重新登录', 401)
            db.execute('DELETE FROM attempts WHERE bucket=?', (buckets[0][0],))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (digest(token), user['id'], now+SESSION_SECONDS, now))
            self.audit(db, user['id'], 'login', user['id'])
        return token

    def session(self, token):
        if not token or len(token) > 100:
            raise AccountError('请先登录', 401)
        now = time.time()
        with self.db() as db:
            row = db.execute('''SELECT u.*,s.expires_at,s.touched_at FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.expires_at>? AND s.touched_at>? AND u.enabled=1''',
                (digest(token), now, now-IDLE_SECONDS)).fetchone()
            if row is None:
                raise AccountError('登录已过期，请重新登录', 401)
            if row['touched_at'] < now - 60:
                db.execute('UPDATE sessions SET touched_at=? WHERE token_hash=?', (now, digest(token)))
            return dict(user=public(row), expires_at=row['expires_at'],
                        csrf_token=hmac.new(token.encode(), b'showallopts-csrf-v1', hashlib.sha256).hexdigest())

    def logout(self, token):
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (digest(token),))

    def limit_probe(self, ident):
        now, bucket = time.time(), digest('probe:' + ident)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM attempts WHERE bucket=?', (bucket,)).fetchone()
            if row and row['since'] > now - 60 and row['count'] >= 4:
                raise AccountError('连接测试过于频繁，请稍后重试', 429)
            count = row['count'] + 1 if row and row['since'] > now - 60 else 1
            since = row['since'] if count > 1 else now
            db.execute('INSERT OR REPLACE INTO attempts VALUES (?,?,?)', (bucket, since, count))

    def list(self):
        with self.db() as db:
            return [public(row) for row in db.execute('SELECT * FROM users ORDER BY created_at,id')]

    def update(self, ident, actor, *, role=None, enabled=None, password=None):
        encoded = password_hash(password) if password is not None else None
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.assert_admin(db, actor)
            row = db.execute('SELECT * FROM users WHERE id=?', (ident,)).fetchone()
            if not row:
                raise AccountError('用户不存在', 404)
            new_role, new_enabled = role or row['role'], row['enabled'] if enabled is None else int(enabled)
            if new_role not in ('admin', 'viewer') or new_enabled not in (0, 1):
                raise AccountError('用户设置无效')
            if row['role'] == 'admin' and row['enabled'] and (new_role != 'admin' or not new_enabled):
                count = db.execute("SELECT count(*) FROM users WHERE role='admin' AND enabled=1").fetchone()[0]
                if count <= 1:
                    raise AccountError('至少保留一位启用的管理员', 409)
            db.execute('UPDATE users SET role=?,enabled=?,password_hash=? WHERE id=?',
                       (new_role, new_enabled, encoded or row['password_hash'], ident))
            db.execute('DELETE FROM sessions WHERE user_id=?', (ident,))
            self.audit(db, actor, 'update_and_revoke', ident)
            return public(db.execute('SELECT * FROM users WHERE id=?', (ident,)).fetchone())

    def change_password(self, ident, old, new):
        with self.db() as db:
            row = db.execute('SELECT * FROM users WHERE id=? AND enabled=1', (ident,)).fetchone()
        if not row or not password_matches(old, row['password_hash']):
            raise AccountError('原密码不正确', 400)
        encoded = password_hash(new)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute('UPDATE users SET password_hash=? WHERE id=? AND password_hash=? AND enabled=1',
                                 (encoded, ident, row['password_hash'])).rowcount
            if not changed:
                raise AccountError('账号状态已变化，请重新登录', 409)
            db.execute('DELETE FROM sessions WHERE user_id=?', (ident,))
            self.audit(db, ident, 'change_password', ident)

    def reset_from_console(self, name, password):
        encoded = password_hash(password)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM users WHERE username=?', (username(name),)).fetchone()
            if not row:
                raise AccountError('用户不存在', 404)
            db.execute('UPDATE users SET password_hash=? WHERE id=?', (encoded, row['id']))
            db.execute('DELETE FROM sessions WHERE user_id=?', (row['id'],))
            db.execute('DELETE FROM attempts WHERE bucket=?', (digest('user:'+row['username']),))
            self.audit(db, 'console', 'reset_password', row['id'])
