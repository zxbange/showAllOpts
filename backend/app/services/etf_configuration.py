"""Encrypted page-owned ETF settings, isolated from the application's reader account."""
import ipaddress
import json
import re

from sqlalchemy import URL, text
from sqlalchemy.engine import make_url

from app.services.source_keys import KeyStorageError, persistent_cipher


class ConfigurationFailure(ValueError):
    """Only public, credential-free errors may cross this boundary."""


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
                return config
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
                      schedule=['09:30', '11:30', '18:30'],
                      db_tls=not local_database(self.settings.db_host), source='none')
        if raw:
            try:
                url = make_url(raw)
                self.validate_target(url)
                config.update(username=url.username or '', password=url.password or '', source='environment',
                              db_tls=bool(url.query.get('ssl_ca') or url.query.get('ssl_check_hostname')))
            except ConfigurationFailure:
                config['legacy_error'] = 'writer_target_mismatch'
            except Exception:
                config['legacy_error'] = 'invalid_writer_configuration'
        return config

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
        if not username or username.lower() in {'root', 'mysql.sys', 'mysql.session', 'mysql.infoschema'}:
            raise ConfigurationFailure('请填写独立的ETF采集账号，不要使用数据库管理员账号')
        if username == self.settings.db_user:
            raise ConfigurationFailure('采集账号必须与应用只读账号分开，请填写独立写入账号')
        password = body.get('password')
        if password is None:
            target_changed = any(previous[k] != value for k, value in (
                ('host', self.settings.db_host), ('port', self.settings.db_port), ('database', self.settings.db_name)))
            if previous['password'] is None or username != previous['username'] or target_changed:
                raise ConfigurationFailure('首次配置或更换账号时请重新输入密码')
            password = previous['password']
        schedule = sorted(body['schedule'])
        if not schedule or len(schedule) > 6 or len(set(schedule)) != len(schedule) or any(
                not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', slot) for slot in schedule):
            raise ConfigurationFailure('采集时间需为1至6个不重复的北京时间，格式为HH:MM')
        if not body['db_tls'] and not local_database(self.settings.db_host) and not body.get('db_transport_acknowledged'):
            raise ConfigurationFailure('远程数据库未启用TLS，凭据和数据可能被截获；请明确确认风险或启用TLS')
        return dict(revision=previous['revision'], host=self.settings.db_host, port=self.settings.db_port,
                    database=self.settings.db_name, username=username, password=password,
                    enabled=body['enabled'], lookback_days=body['lookback_days'],
                    batch_days=body['batch_days'], schedule=schedule, db_tls=body['db_tls'], source='page')

    def public(self):
        config = self.load()
        target_changed = any(config[k] != value for k, value in (
            ('host', self.settings.db_host), ('port', self.settings.db_port), ('database', self.settings.db_name)))
        return {k: v for k, v in config.items() if k not in {'password', 'legacy_error', 'effective_after'}} | {
            'host': self.settings.db_host, 'port': self.settings.db_port, 'database': self.settings.db_name,
            'password_set': config['password'] is not None and not target_changed,
            'db_local': local_database(self.settings.db_host), 'target_changed': target_changed,
        }

    def save(self, body):
        config = self.validate(body)
        config['revision'] += 1
        config['effective_after'] = self.owner.clock().isoformat()
        cipher = self.cipher()
        encrypted = cipher.encrypt(json.dumps(config).encode())
        with self.owner.connect() as db:
            db.execute('INSERT OR REPLACE INTO configuration VALUES(1,?,?)', (config['revision'], encrypted))
            db.execute("DELETE FROM metadata WHERE key='writer_error'")
        return self.public()

    @staticmethod
    def url(config):
        return URL.create('mysql+pymysql', username=config['username'], password=config['password'],
                          host=config['host'], port=config['port'], database=config['database'],
                          query={'charset': 'utf8mb4'})

    def engine(self, config):
        self.validate_target(self.url(config))
        options = dict(connect_timeout=10, read_timeout=60, write_timeout=60)
        if config['db_tls']:
            # System trust store + hostname verification. No silent plaintext fallback.
            # Pass the context itself: driver ssl_verify_* kwargs replace it and
            # can disable hostname checks when no explicit CA file is supplied.
            import ssl
            options['ssl'] = ssl.create_default_context()
        else:
            options['ssl_disabled'] = True
        return self.owner.engine_factory(self.url(config), pool_pre_ping=True, hide_parameters=True,
                                         connect_args=options)

    def test(self, body):
        config = self.validate(body)
        engine = None
        try:
            engine = self.engine(config)
            with engine.connect() as connection:
                for table in ('etf_share_daily', 'etf_price_daily', 'etf_trading_day'):
                    connection.execute(text(f'SELECT 1 FROM `{table}` LIMIT 0'))
        except Exception:
            raise ConfigurationFailure('连接或表检查失败，请检查网络、TLS证书、账号权限及ETF三张表；不会返回原始数据库错误') from None
        finally:
            if engine is not None:
                engine.dispose()
        return dict(ok=True, permissions_verified=False,
                    message='连接及三张表读取通过；未写入数据，写权限须以实际采集结果为准')
