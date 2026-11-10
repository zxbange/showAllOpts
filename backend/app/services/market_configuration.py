"""One-time, encrypted writer settings for the two daily market aggregates."""
import json

from sqlalchemy import URL, create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.services.etf_configuration import ConfigurationFailure
from app.services.source_keys import KeyStorageError, persistent_cipher


SCHEDULE = ['18:00']


class MarketConfiguration:
    def __init__(self, owner):
        self.owner = owner
        self.settings = owner.settings

    def record(self):
        if not (self.owner.root / 'state.sqlite').exists():
            return None
        with self.owner.connect() as db:
            return db.execute('SELECT revision,ciphertext FROM configuration WHERE id=1').fetchone()

    def cipher(self):
        self.owner.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        def validate(cipher):
            record = self.record()
            if record:
                try:
                    if cipher is None:
                        raise ValueError()
                    value = json.loads(cipher.decrypt(record['ciphertext']))
                    if value['revision'] != record['revision']:
                        raise ValueError()
                except Exception:
                    raise KeyStorageError('市场采集配置无法解密，请恢复原密钥；已保存配置与数据保留') from None
        try:
            # This new module has its own key namespace. A missing sibling ETF
            # key must never be regenerated while configuring market collection.
            return persistent_cipher(self.owner.root / 'credentials', '', validate)
        except KeyStorageError as error:
            raise ConfigurationFailure(str(error)) from None

    @staticmethod
    def url(config):
        return URL.create('mysql+pymysql', username=config['username'], password=config['password'],
                          host=config['host'], port=config['port'], database=config['database'],
                          query={'charset': 'utf8mb4'})

    def validate_target(self, url):
        def target(value):
            return value.drivername, (value.host or '').lower().rstrip('.'), value.port or 3306, value.database
        if target(url) != target(self.settings.database_url):
            raise ConfigurationFailure('writer_target_mismatch')

    def base(self):
        return dict(revision=0, host=self.settings.db_host, port=self.settings.db_port,
                    database=self.settings.db_name, username='', password=None,
                    enabled=getattr(self.settings, 'market_collection_enabled', True),
                    schedule=list(SCHEDULE), source='none')

    def load(self):
        record = self.record()
        if record:
            try:
                result = json.loads(self.cipher().decrypt(record['ciphertext']))
                if result['revision'] != record['revision']:
                    raise ValueError()
                return result | {'schedule': list(SCHEDULE)}
            except ConfigurationFailure:
                raise
            except Exception:
                raise ConfigurationFailure('市场采集配置无法解密；已保存配置与数据保留') from None
        result = self.base()
        secret = getattr(self.settings, 'market_collection_writer_url', None)
        raw = secret.get_secret_value().strip() if secret is not None else ''
        if raw:
            try:
                url = make_url(raw)
                self.validate_target(url)
                return result | {'username': url.username or '', 'password': url.password or '',
                                 'source': 'environment'}
            except Exception:
                return result | {'configuration_error': 'writer_target_mismatch'}
        # A previously configured writer can be reused only for this exact reader
        # database. Never treat the application's read-only account as a writer.
        try:
            shared = getattr(self.owner, 'shared_configuration', None)
            if shared is None:
                from app.services.etf_collection import ETFCollection
                shared = ETFCollection(self.settings).configuration
            candidate = shared.load()
            if candidate.get('username') and candidate.get('password') is not None:
                self.validate_target(shared.url(candidate))
                return result | {'username': candidate['username'], 'password': candidate['password'],
                                 'source': 'etf'}
        except Exception:
            pass  # Other modules' configuration failures cannot expose credentials.
        return result

    def issue(self):
        try:
            config = self.load()
            if config.get('configuration_error'):
                return config['configuration_error']
            self.validate_target(self.url(config))
            if not config['username'] or config['password'] is None:
                return 'missing_writer_configuration'
        except ConfigurationFailure as error:
            return 'writer_target_mismatch' if str(error) == 'writer_target_mismatch' else 'configuration_unavailable'
        except Exception:
            return 'invalid_writer_configuration'
        return None

    def public(self):
        config = self.load()
        # Host/database reflect the immutable display-reader target. Passwords
        # are write-only and are never returned in an error or status response.
        result = {key: config[key] for key in ('revision', 'username', 'enabled', 'source')}
        changed = any(config[key] != self.base()[key] for key in ('host', 'port', 'database'))
        return result | {key: self.base()[key] for key in ('host', 'port', 'database')} | {
            'schedule': list(SCHEDULE), 'password_set': config['password'] is not None and not changed,
            'target_changed': changed,
        }

    def validate(self, body):
        previous = self.load()
        if body['revision'] != previous['revision']:
            raise ConfigurationFailure('配置已更新，请刷新后重试')
        username = body['username'].strip()
        if not username or len(username) > 128 or any(ord(c) < 32 for c in username):
            raise ConfigurationFailure('请填写有效的数据库写入账号')
        password = body.get('password')
        changed = any(previous[key] != self.base()[key] for key in ('host', 'port', 'database'))
        if password is None:
            if previous['password'] is None or username != previous['username'] or changed:
                raise ConfigurationFailure('首次配置或更换账号时请填写密码')
            password = previous['password']
        if not isinstance(password, str) or len(password) > 4096 or type(body['enabled']) is not bool:
            raise ConfigurationFailure('采集配置格式无效')
        return self.base() | {'revision': previous['revision'], 'username': username,
                              'password': password, 'enabled': body['enabled'], 'source': 'page'}

    def save(self, body):
        config = self.validate(body)
        config['revision'] += 1
        config['effective_after'] = self.owner.clock().isoformat()
        encrypted = self.cipher().encrypt(json.dumps(config).encode())
        with self.owner.connect() as db:
            db.execute('INSERT OR REPLACE INTO configuration VALUES(1,?,?)', (config['revision'], encrypted))
        return self.public()

    def engine(self, config):
        self.validate_target(self.url(config))
        factory = getattr(self.owner, 'engine_factory', create_engine)
        return factory(self.url(config), pool_pre_ping=True, hide_parameters=True,
                       connect_args={'connect_timeout': 10, 'read_timeout': 30, 'write_timeout': 30})

    def test(self, body):
        config = self.validate(body)
        engine = None
        try:
            engine = self.engine(config)
            with engine.connect() as connection:
                for table in ('market_sentiment', 'market_crowding'):
                    if not inspect(connection).has_table(table):
                        raise ValueError()
                    connection.execute(text(f'SELECT 1 FROM `{table}` LIMIT 0'))
        except Exception:
            raise ConfigurationFailure('连接或数据表检查失败，请检查账号与市场热度、抱团拥挤度两张表') from None
        finally:
            if engine is not None:
                engine.dispose()
        return dict(ok=True, permissions_verified=False,
                    message='连接与两张表读取通过；实际写入权限以采集结果为准')
