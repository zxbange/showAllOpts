"""Encrypted, immutable validation connection versions; never write to source DBs."""
from contextlib import contextmanager
import ipaddress
import json
import socket
import queue
import threading
import ssl
import uuid

from cryptography.fernet import Fernet
import pymysql
from sqlalchemy import create_engine


DNS_TIMEOUT = 5
_RESOLVER_SLOTS = threading.BoundedSemaphore(4)


def resolve_addresses(host, port):
    if not _RESOLVER_SLOTS.acquire(blocking=False):
        raise SourceError('DNS解析繁忙，请稍后重试')
    result = queue.Queue(maxsize=1)
    def resolve():
        try:
            result.put((True, socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        except Exception:
            result.put((False, None))
        finally:
            _RESOLVER_SLOTS.release()
    threading.Thread(target=resolve, daemon=True, name='validation-dns').start()
    try:
        ok, addresses = result.get(timeout=DNS_TIMEOUT)
    except queue.Empty:
        raise SourceError('DNS解析超时（5秒），请检查允许目标与DNS配置') from None
    if not ok:
        raise SourceError('DNS解析失败，请检查允许目标与DNS配置')
    return addresses


class SourceError(ValueError):
    """Safe public message, never a driver/validation exception or connection value."""


class ValidationSources:
    def __init__(self, jobs, settings):
        self.jobs, self.settings = jobs, settings
        with jobs.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS validation_sources (
                id TEXT, revision INTEGER, ciphertext BLOB NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(id,revision))''')
            db.execute('CREATE TABLE IF NOT EXISTS validation_source_key (id INTEGER PRIMARY KEY CHECK(id=1), verifier BLOB NOT NULL)')

    def cipher(self):
        key = self.settings.validation_encryption_key.get_secret_value()
        if not key:
            raise SourceError('请部署者设置 VALIDATION_ENCRYPTION_KEY；连接信息不会以明文保存')
        try:
            cipher = Fernet(key.encode('ascii'))
        except Exception:
            raise SourceError('VALIDATION_ENCRYPTION_KEY 格式无效，请检查服务器安全配置') from None
        with self.jobs.db() as db:
            row = db.execute('SELECT verifier FROM validation_source_key WHERE id=1').fetchone()
        if row:
            try:
                if cipher.decrypt(row[0]) != b'validation-source-key-v1':
                    raise ValueError()
            except Exception:
                raise SourceError('数据库配置无法解密：加密密钥与此管理数据库不匹配，已禁止读取和写入') from None
        return cipher

    def allowed(self, config):
        host, port = config['host'].lower().rstrip('.'), int(config['port'])
        targets = self.settings.validation_allowed_targets
        if f'{host}:{port}' not in targets:
            raise SourceError('数据库目标未获部署者允许，请检查 VALIDATION_ALLOWED_TARGETS')
        # Resolve once and pin the resulting socket, so DNS cannot rebind between check/use.
        try:
            addresses = sorted({item[4][0] for item in resolve_addresses(host, port)})
            if not addresses:
                raise ValueError()
            for raw in addresses:
                ip = ipaddress.ip_address(raw)
                ip = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
                # Never allow metadata endpoints/link-local, unspecified, multicast or reserved ranges.
                if (ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved
                        or str(ip) in ('100.100.100.200', '168.63.129.16', 'fd00:ec2::254')):
                    raise ValueError()
            return addresses[0]
        except SourceError:
            raise
        except Exception:
            raise SourceError('数据库目标解析失败或属于禁止连接的地址范围') from None

    def _decode(self, row, ident, revision):
        try:
            payload = json.loads(self.cipher().decrypt(row))
            if payload['id'] != ident or payload['revision'] != revision:
                raise ValueError()
            return payload['config']
        except SourceError:
            raise
        except Exception:
            raise SourceError('数据库配置无法解密或完整性校验失败，请检查原加密密钥；不会回退明文') from None

    def get(self, ident, revision):
        with self.jobs.db() as db:
            row = db.execute('SELECT ciphertext FROM validation_sources WHERE id=? AND revision=?', (ident, revision)).fetchone()
        if not row:
            raise SourceError('绑定的数据库配置版本不存在，请新建任务')
        return self._decode(row[0], ident, revision)

    @staticmethod
    def public(ident, revision, config):
        return dict(id=ident, revision=revision, **{k: v for k, v in config.items() if k != 'password'}, password_set=bool(config.get('password')))

    def list(self):
        self.cipher()
        with self.jobs.db() as db:
            rows = db.execute('''SELECT s.id,s.revision,s.ciphertext FROM validation_sources s
                WHERE s.id!='service' AND s.revision=(SELECT max(t.revision) FROM validation_sources t WHERE t.id=s.id)
                ORDER BY s.rowid DESC''').fetchall()
        return [self.public(i, r, self._decode(c, i, r)) for i, r, c in rows]

    def save(self, config, ident=None, expected_revision=None):
        cipher = self.cipher()
        # This check is offline: saving an allowed hostname does not initiate DNS/network access.
        if f"{config['host'].lower().rstrip('.')}:{config['port']}" not in self.settings.validation_allowed_targets:
            raise SourceError('数据库目标未获部署者允许，请检查 VALIDATION_ALLOWED_TARGETS')
        ident = ident or uuid.uuid4().hex
        from app.services.short_validation import now
        with self.jobs.db() as db:
            db.execute('BEGIN IMMEDIATE')
            verifier = db.execute('SELECT verifier FROM validation_source_key WHERE id=1').fetchone()
            if verifier:
                try:
                    if cipher.decrypt(verifier[0]) != b'validation-source-key-v1':
                        raise ValueError()
                except Exception:
                    raise SourceError('加密密钥与管理数据库不匹配，禁止写入') from None
            else:
                # Upgrade any pre-verifier records only after proving one key can decrypt all.
                for old_id, old_revision, old_ciphertext in db.execute('SELECT id,revision,ciphertext FROM validation_sources').fetchall():
                    self._decode(old_ciphertext, old_id, old_revision)
                db.execute('INSERT INTO validation_source_key VALUES (1,?)', (cipher.encrypt(b'validation-source-key-v1'),))
            row = db.execute('SELECT revision,ciphertext FROM validation_sources WHERE id=? ORDER BY revision DESC LIMIT 1', (ident,)).fetchone()
            previous = self._decode(row[1], ident, row[0]) if row else None
            if ident != 'service' and ((row is not None and expected_revision != row[0]) or (row is None and expected_revision is not None)):
                raise SourceError('配置版本已变化或不存在，请刷新后再编辑')
            config = dict(config)
            if config.get('password') is None:
                if previous is None:
                    raise SourceError('新连接需要填写密码（无密码账户可明确填写空字符串）')
                config['password'] = previous['password']
            if ident == 'service' and previous == config:
                return dict(id=ident, revision=row[0])
            revision = row[0]+1 if row else 1
            payload = json.dumps(dict(id=ident, revision=revision, config=config), ensure_ascii=False).encode()
            db.execute('INSERT INTO validation_sources VALUES (?,?,?,?)', (ident, revision, cipher.encrypt(payload), now()))
        return dict(id=ident, revision=revision)

    def freeze_service(self):
        s = self.settings
        # Match the service's PyMySQL default: prefer TLS, without requiring it.
        return self.save(dict(name='服务默认数据库', host=s.db_host, port=s.db_port, database=s.db_name,
                              username=s.db_user, password=s.db_password.get_secret_value(), tls='preferred', ca_pem=''), 'service')

    @contextmanager
    def engine(self, ref):
        config = self.get(ref['id'], ref['revision'])
        engine = self.build_engine(config)
        try:
            yield engine
        finally:
            engine.dispose()

    def build_engine(self, config):
        def connect():
            sock, connection = None, None
            try:
                address = self.allowed(config)
                context = None
                if config['tls'] not in ('disabled', 'preferred'):
                    context = ssl.create_default_context(cadata=config['ca_pem'] or None)
                    if config['tls'] == 'required':
                        context.check_hostname = False
                        context.verify_mode = ssl.CERT_NONE
                connection = pymysql.connect(host=config['host'], port=config['port'],
                    user=config['username'], password=config['password'], database=config['database'],
                    charset='utf8mb4', connect_timeout=8, read_timeout=20, write_timeout=20,
                    ssl=context, ssl_disabled=config['tls'] == 'disabled', defer_connect=True, autocommit=False)
                sock = socket.create_connection((address, config['port']), timeout=8)
                connection.connect(sock=sock)
                if context is not None and not isinstance(connection._sock, ssl.SSLSocket):
                    raise ValueError('TLS negotiation failed')
                return connection
            except Exception:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                if sock is not None:
                    sock.close()
                raise SourceError('连接失败：请检查允许目标、网络、账户、库名和TLS证书；详细凭证不会返回') from None
        return create_engine('mysql+pymysql://', creator=connect, hide_parameters=True, pool_pre_ping=True,
                             pool_size=1, max_overflow=0)

    def test(self, config):
        from app.services.short_statistics import validate_schema
        self.cipher()
        engine = self.build_engine(config)
        try:
            validate_schema(engine)
        except Exception:
            raise SourceError('连接测试失败：请检查允许目标、网络、账户权限、TLS和三张 ultra_research 表') from None
        finally:
            engine.dispose()
        return {'ok': True, 'message': '连接及三张研究表字段读取测试通过（只读）'}
