"""One-time, encrypted writer settings for the two daily market aggregates."""
import json

from sqlalchemy import URL, create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.services.etf_configuration import ConfigurationFailure
from app.services.source_keys import KeyStorageError, persistent_cipher


SCHEDULE = ['00:01']


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
        from app.services.system_configuration import database_config
        expected=self.url(database_config(self.settings))
        target=lambda u:(u.drivername,(u.host or '').lower().rstrip('.'),u.port or 3306,u.database)
        if target(url)!=target(expected): raise ConfigurationFailure('writer_target_mismatch')

    def base(self):
        return self.load()

    def load(self):
        from app.services.system_configuration import database_config,task_config
        config=database_config(self.settings);task=task_config(self.settings,'market')
        return dict(config,enabled=task['enabled'],schedule=list(task['times']),source='system',target_mode='custom')

    def issue(self):
        try:
            config=self.load()
            if not config['username'] or config['password'] is None:return 'missing_writer_configuration'
        except Exception:return 'configuration_unavailable'
        return None

    def public(self):
        config=self.load()
        return {k:config[k] for k in ('revision','host','port','database','username','enabled','schedule','source')} | {
            'password_set':config['password'] is not None,'target_changed':False}

    def validate(self, body):
        raise ConfigurationFailure('数据库与自动任务配置已统一，请在系统配置页面修改')

    def save(self, body):
        raise ConfigurationFailure('数据库与自动任务配置已统一，请在系统配置页面修改')

    def engine(self, config):
        from app.services.etf_configuration import ETFConfiguration
        return ETFConfiguration.engine(self,dict(config,target_mode='custom'))

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
