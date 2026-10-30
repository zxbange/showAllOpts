"""Console-issued, origin-bound one-time pairing and revocable browser sessions."""
from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
from urllib.parse import urlsplit

PAIR_TTL = 600
SESSION_TTL = 8 * 3600
COOKIE = 'validation_session'
COOKIE_PATH = '/api/v1/short-term/validation'


class AccessError(ValueError):
    def __init__(self, message, status=403):
        super().__init__(message)
        self.status = status


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def canonical_origin(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError()
        host = parsed.hostname.lower()
        if ':' in host:
            host = '[' + host + ']'
        port = parsed.port
        return parsed.scheme + '://' + host + (':' + str(port) if port and port != (443 if parsed.scheme == 'https' else 80) else '')
    except Exception:
        raise AccessError('站点地址必须是完整HTTP或HTTPS来源，不含路径、凭证或查询参数') from None


class ValidationAccess:
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.log_path / 'short-validation-auth'

    @property
    def legacy(self):
        # Even a malformed legacy key must not enable pairing around an explicit policy.
        return bool(self.settings.validation_admin_key.get_secret_value())

    @contextmanager
    def db(self):
        if os.name != 'posix':
            raise AccessError('网页配对的安全持久存储目前支持Linux；旧管理密钥模式保持兼容', 503)
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.root.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError()
            path = self.root / 'access.sqlite3'
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise ValueError()
            db = sqlite3.connect(path, timeout=10)
            db.row_factory = sqlite3.Row
        except Exception:
            raise AccessError('配对状态存储不可用，请检查服务账户的持久目录权限', 503) from None
        try:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS pairing (id INTEGER PRIMARY KEY CHECK(id=1), hash TEXT, origin TEXT, expires REAL, attempts INTEGER)')
                db.execute('CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, origin TEXT, expires REAL, http_confirmed INTEGER)')
                db.execute('CREATE TABLE IF NOT EXISTS probes (session TEXT PRIMARY KEY, window REAL, attempts INTEGER)')
            yield db
        finally:
            db.close()

    def issue(self, origin, revoke=False):
        if self.legacy:
            raise AccessError('旧管理密钥模式仍生效，请使用原管理密钥；配对命令不能绕过既有访问控制')
        origin = canonical_origin(origin)
        code = secrets.token_urlsafe(32)
        with self.db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR REPLACE INTO pairing VALUES (1,?,?,?,0)', (digest(code), origin, time.time()+PAIR_TTL))
            if revoke:
                db.execute('DELETE FROM sessions')
                db.execute('DELETE FROM probes')
            db.execute('DELETE FROM sessions WHERE expires<=?', (time.time(),))
        return code

    def pair(self, code, origin, http_confirmed):
        if self.legacy:
            raise AccessError('旧管理密钥模式仍生效，不能使用配对码绕过', 409)
        origin = canonical_origin(origin)
        if origin.startswith('http:') and not http_confirmed:
            raise AccessError('请先在页面确认HTTP明文传输风险')
        valid = False
        token = secrets.token_urlsafe(32)
        with self.db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM pairing WHERE id=1').fetchone()
            if row and row['expires'] > time.time() and row['attempts'] < 10:
                db.execute('UPDATE pairing SET attempts=attempts+1 WHERE id=1')
                valid = hmac.compare_digest(row['hash'], digest(code)) and row['origin'] == origin
                if valid:
                    db.execute('DELETE FROM pairing WHERE id=1')
                    db.execute('DELETE FROM sessions WHERE expires<=?', (time.time(),))
                    db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (digest(token), origin, time.time()+SESSION_TTL, int(http_confirmed)))
        if not valid:
            raise AccessError('配对码无效、已过期或尝试次数已用完，请在终端重新领取', 401)
        return token

    def session(self, token, origin):
        if self.legacy or not token or len(token) > 100:
            raise AccessError('会话已失效，请重新配对', 401)
        with self.db() as db:
            row = db.execute('SELECT * FROM sessions WHERE hash=?', (digest(token),)).fetchone()
        if not row or row['expires'] <= time.time() or row['origin'] != origin:
            raise AccessError('会话已失效，请重新配对', 401)
        if origin.startswith('http:') and not row['http_confirmed']:
            raise AccessError('请重新配对并确认HTTP风险', 401)
        return {'expires_at': row['expires'], 'csrf_token': hmac.new(token.encode(), b'validation-csrf-v1', hashlib.sha256).hexdigest()}

    def logout(self, token):
        with self.db() as db, db:
            db.execute('DELETE FROM sessions WHERE hash=?', (digest(token),))
            db.execute('DELETE FROM probes WHERE session=?', (digest(token),))

    def limit_probe(self, token):
        # Bounded per-session deliberate test calls; never background-scan targets.
        with self.db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            key, current = digest(token), time.time()
            row = db.execute('SELECT * FROM probes WHERE session=?', (key,)).fetchone()
            if row and current-row['window'] < 60:
                if row['attempts'] >= 5:
                    raise AccessError('连接测试过于频繁，请一分钟后再试', 429)
                db.execute('UPDATE probes SET attempts=attempts+1 WHERE session=?', (key,))
            else:
                db.execute('INSERT OR REPLACE INTO probes VALUES (?,?,1)', (key, current))
