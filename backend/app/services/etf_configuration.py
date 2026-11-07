"""Encrypted page-owned ETF settings shared by ETF collection and display reads."""
import hashlib
import ipaddress
import json
import re

from sqlalchemy import URL, text
from sqlalchemy.engine import make_url

from app.services.etf_diagnostics import database_diagnostic, log_database_failure
from app.services.source_keys import KeyStorageError, persistent_cipher


class ConfigurationFailure(ValueError):
    """Only public, credential-free errors may cross this boundary."""


DEFAULT_COLLECTION_TIME = '09:00'


def target_key(config):
    """Account-independent identity for the collection ledger, never a credential."""
    values = [config['host'].lower().rstrip('.'), config['port'], config['database']]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def single_schedule(config):
    schedule = config.get('schedule', [])
    if len(schedule) != 1:
        config = config | {'schedule': [DEFAULT_COLLECTION_TIME], 'schedule_migrated': True}
    return config


def local_database(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == 'localhost'


class ETFConfiguration:
    def __init__(self, owner):
        self.owner = owner
        self.settings = owner.settings

    def record(self):
        if not (self.owner.root / 'state.sqlite').exists():
            return None
        with self.owner.connect() as db:
            return db.execute('SELECT revision,ciphertext FROM configuration WHERE id=1').fetchone()

    def cipher(self):
        def validate(cipher):
            record = self.record()
            if record:
                if cipher is None:
                    raise KeyStorageError('已有ETF加密配置但原密钥缺失，请恢复原密钥；不会覆盖配置或历史数据')
                try:
                    payload = json.loads(cipher.decrypt(record['ciphertext']))
                    if payload['revision'] != record['revision']:
                        raise ValueError()
                except Exception:
                    raise KeyStorageError('ETF配置无法解密，请恢复匹配的原密钥；不会回退或覆盖配置') from None
        try:
            return persistent_cipher(self.owner.root, '', validate)
        except KeyStorageError as error:
            raise ConfigurationFailure(str(error)) from None

    def load(self):
        record = self.record()
        if record:
            try:
                config = json.loads(self.cipher().decrypt(record['ciphertext']))
                if config['revision'] != record['revision']:
                    raise ValueError()
                # Older encrypted records may contain a TLS preference/acknowledgment.
                # Read them without rewriting credentials or revisions; the ETF driver
                # now uses one fixed non-TLS transport, and the next save drops the keys.
                config.pop('db_tls', None)
                config.pop('db_transport_acknowledged', None)
                return single_schedule(config)
            except ConfigurationFailure:
                raise
            except Exception:
                raise ConfigurationFailure('ETF配置无法解密，请恢复匹配的原密钥；现有数据保留') from None
        raw = self.settings.etf_collection_writer_url.get_secret_value().strip()
        config = dict(revision=0, host=self.settings.db_host, port=self.settings.db_port,
                      database=self.settings.db_name, username='', password=None,
                      enabled=self.settings.etf_collection_enabled,
                      lookback_days=self.settings.etf_collection_lookback_days,
                      batch_days=self.settings.etf_collection_batch_days,
                      schedule=[DEFAULT_COLLECTION_TIME], source='none')
        if raw:
            try:
                url = make_url(raw)
                self.validate_target(url)
                config.update(username=url.username or '', password=url.password or '', source='environment')
            except ConfigurationFailure:
                config['legacy_error'] = 'writer_target_mismatch'
            except Exception:
                config['legacy_error'] = 'invalid_writer_configuration'
        return config

    def reader_configuration(self):
        config = self.load()
        return config if config.get('target_mode') == 'custom' else None

    def validate_target(self, url):
        reader = self.settings.database_url
        target = lambda u: (u.drivername, u.host, u.port or 3306, u.database)
        if target(url) != target(reader):
            raise ConfigurationFailure('writer_target_mismatch')

    def validate(self, body):
        previous = self.load()
        if body['revision'] != previous['revision']:
            raise ConfigurationFailure('配置已被其他页面修改，请刷新配置后重试')
        username = body['username'].strip()
        if not username:
            raise ConfigurationFailure('请填写ETF采集账号')
        custom = any(body.get(k) is not None for k in ('host', 'port', 'database'))
        if custom and not all(body.get(k) is not None for k in ('host', 'port', 'database')):
            raise ConfigurationFailure('请完整填写数据库主机、端口和库名')
        target = ({k: body[k] for k in ('host', 'port', 'database')} if custom else
                  {k: previous[k] for k in ('host', 'port', 'database')} if previous.get('target_mode') == 'custom' else
                  dict(host=self.settings.db_host, port=self.settings.db_port, database=self.settings.db_name))
        target['host'] = target['host'].strip().lower().rstrip('.')
        target['database'] = target['database'].strip()
        try:
            ipaddress.ip_address(target['host'])
        except ValueError:
            if not re.fullmatch(r'(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', target['host']):
                raise ConfigurationFailure('数据库主机应填写域名或IP地址，不要填写URL或连接字符串') from None
        if type(target['port']) is not int or not 1 <= target['port'] <= 65535:
            raise ConfigurationFailure('数据库端口应为1至65535的整数')
        if not target['database'] or len(target['database']) > 64 or any(ord(c) < 32 for c in target['database']):
            raise ConfigurationFailure('请填写有效的数据库名称')
        password = body.get('password')
        if password is None:
            target_changed = target_key(previous) != target_key(target)
            if previous['password'] is None or username != previous['username'] or target_changed:
                raise ConfigurationFailure('首次配置、更换账号或数据库目标时请重新输入密码')
            password = previous['password']
        schedule = sorted(body['schedule'])
        if len(schedule) != 1 or any(
                not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', slot) for slot in schedule):
            raise ConfigurationFailure('请设置一个每日采集时间（北京时间），格式为HH:MM')
        return dict(revision=previous['revision'], **target, username=username, password=password,
                    enabled=body['enabled'], lookback_days=body['lookback_days'],
                    batch_days=body['batch_days'], schedule=schedule, source='page',
                    target_mode='custom' if custom or previous.get('target_mode') == 'custom' else 'service')

    def public(self):
        config = self.load()
        target_changed = config.get('target_mode') != 'custom' and any(config[k] != value for k, value in (
            ('host', self.settings.db_host), ('port', self.settings.db_port), ('database', self.settings.db_name)))
        return {k: v for k, v in config.items() if k not in {'password', 'legacy_error', 'effective_after'}} | {
            'host': self.settings.db_host if target_changed else config['host'],
            'port': self.settings.db_port if target_changed else config['port'],
            'database': self.settings.db_name if target_changed else config['database'],
            'password_set': config['password'] is not None and not target_changed,
            'db_local': local_database(self.settings.db_host if target_changed else config['host']), 'target_changed': target_changed,
        }

    def save(self, body):
        previous = self.load()
        config = self.validate(body)
        config['revision'] += 1
        config['effective_after'] = self.owner.clock().isoformat()
        cipher = self.cipher()
        encrypted = cipher.encrypt(json.dumps(config).encode())
        with self.owner.connect() as db:
            if target_key(previous) != target_key(config):
                # Keep the old target's bookkeeping for audit; never apply it to a new DB.
                archived = {name: [dict(row) for row in db.execute('SELECT * FROM ' + name)]
                            for name in ('observations', 'slots', 'metadata')}
                db.execute('CREATE TABLE IF NOT EXISTS target_archive (id INTEGER PRIMARY KEY, target TEXT, archived_at TEXT, state TEXT)')
                db.execute('INSERT INTO target_archive(target,archived_at,state) VALUES(?,?,?)',
                           (target_key(previous), config['effective_after'], json.dumps(archived)))
                for name in ('observations', 'slots', 'metadata'):
                    db.execute('DELETE FROM ' + name)
                last_manual = db.execute('SELECT COALESCE(MAX(rowid),0) FROM manual_runs').fetchone()[0]
                db.execute("INSERT INTO metadata VALUES('manual_after_rowid',?)", (str(last_manual),))
            db.execute('INSERT OR REPLACE INTO configuration VALUES(1,?,?)', (config['revision'], encrypted))
            db.execute("DELETE FROM metadata WHERE key='writer_error'")
        return self.public()

    @staticmethod
    def url(config):
        return URL.create('mysql+pymysql', username=config['username'], password=config['password'],
                          host=config['host'], port=config['port'], database=config['database'],
                          query={'charset': 'utf8mb4'})

    def engine(self, config):
        if config.get('target_mode') != 'custom':
            self.validate_target(self.url(config))
        # ETF database transport is intentionally non-TLS, including legacy records
        # and environment URLs. This does not change browser HTTPS or other modules.
        options = dict(connect_timeout=10, read_timeout=60, write_timeout=60, ssl_disabled=True)
        extra = {}
        if config.get('target_mode') == 'custom':
            # Resolve and pin one permitted socket before transmitting credentials.
            def connect():
                import socket
                import pymysql
                from app.services.database_sources import resolve_addresses
                connection, sock = None, None
                stage = 'resolve'
                try:
                    addresses = sorted({item[4][0] for item in resolve_addresses(config['host'], config['port'])})
                    if not addresses:
                        raise ValueError()
                    stage = 'validate_address'
                    for raw in addresses:
                        ip = ipaddress.ip_address(raw)
                        ip = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
                        if (ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved or
                                str(ip) in ('100.100.100.200', '168.63.129.16', 'fd00:ec2::254')):
                            raise ValueError()
                    stage = 'driver_init'
                    connection = pymysql.connect(host=config['host'], port=config['port'],
                        user=config['username'], password=config['password'], database=config['database'],
                        charset='utf8mb4', defer_connect=True, **options)
                    stage = 'socket_connect'
                    sock = socket.create_connection((addresses[0], config['port']), timeout=10)
                    stage = 'mysql_connect'
                    connection.connect(sock=sock)
                    return connection
                except Exception as error:
                    if connection is not None:
                        try:
                            connection.close()
                        except Exception:
                            pass
                    if sock is not None:
                        try:
                            sock.close()
                        except Exception:
                            pass  # Preserve the original connection failure.
                    failure = ConfigurationFailure('数据库连接失败，请检查目标地址、网络及账号权限')
                    failure.database_diagnostic = database_diagnostic(error, stage)
                    failure.database_error_number = failure.database_diagnostic.mysql_code
                    raise failure from None
            extra['creator'] = connect
        return self.owner.engine_factory(self.url(config), pool_pre_ping=True, hide_parameters=True,
                                         connect_args=options, **extra)

    def test(self, body):
        config = self.validate(body)
        engine = None
        stage, table = 'engine_create', None
        try:
            engine = self.engine(config)
            stage = 'connect'
            with engine.connect() as connection:
                stage = 'table_check'
                for table in ('etf_share_daily', 'etf_price_daily', 'etf_trading_day'):
                    connection.execute(text(f'SELECT 1 FROM `{table}` LIMIT 0'))
                stage, table = 'connection_close', None
        except Exception as error:
            log_database_failure(error, operation='collection-test', stage=stage,
                                 target=target_key(config)[:12], table=table)
            raise ConfigurationFailure('连接或表检查失败，请检查网络、账号权限及ETF三张表；具体原因已记录到服务端错误日志') from None
        finally:
            if engine is not None:
                engine.dispose()
        return dict(ok=True, permissions_verified=False,
                    message='连接及三张表读取通过；未写入数据，写权限须以实际采集结果为准')
