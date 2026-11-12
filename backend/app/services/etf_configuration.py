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
        from app.services.system_configuration import database_config,task_config,SystemConfiguration
        config=database_config(self.settings); task=task_config(self.settings,'etf')
        return dict(config,enabled=task['enabled'],schedule=list(task['times']),
            lookback_days=self.settings.etf_collection_lookback_days,batch_days=self.settings.etf_collection_batch_days,
            source='system',target_mode='custom',effective_after=SystemConfiguration(self.settings).load().get('changed_at'))

    def reader_configuration(self):
        return self.load()

    def validate_target(self, url):
        from app.services.system_configuration import database_config
        expected=self.url(database_config(self.settings))
        target=lambda u:(u.drivername,(u.host or '').lower().rstrip('.'),u.port or 3306,u.database)
        if target(url)!=target(expected): raise ConfigurationFailure('writer_target_mismatch')

    def validate(self, body):
        raise ConfigurationFailure('数据库与自动任务配置已统一，请在系统配置页面修改')

    def public(self):
        config=self.load()
        return {k:config[k] for k in ('revision','host','port','database','username','enabled','schedule','lookback_days','batch_days','source')} | {
            'password_set':config['password'] is not None,'db_local':local_database(config['host']),'target_changed':False}

    def save(self, body):
        raise ConfigurationFailure('数据库与自动任务配置已统一，请在系统配置页面修改')

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


def reconcile_system_ledger(configuration, db, tables, stamp):
    """Retarget only under collector lock; preserve old bookkeeping and secrets."""
    import logging
    from app.services.system_configuration import SystemConfiguration
    config=configuration.load(); target=target_key(config)
    row=db.execute("SELECT value FROM metadata WHERE key='system_database_target'").fetchone()
    previous=row[0] if row else None
    if previous is None:
        record=configuration.record()
        if record:
            try:
                previous=target_key(json.loads(configuration.cipher().decrypt(record['ciphertext'])))
            except Exception:
                previous='unverified-legacy-target'
                logging.getLogger(__name__).warning('Legacy collection target unavailable; original credentials preserved')
                if SystemConfiguration(configuration.settings).load()['revision']==0:
                    raise ConfigurationFailure('旧采集目标无法核验，请在系统配置中明确保存统一数据库；原记录保留') from None
        else:
            previous=target_key(dict(host=configuration.settings.db_host,port=configuration.settings.db_port,database=configuration.settings.db_name))
    if previous!=target:
        db.execute('''CREATE TABLE IF NOT EXISTS system_target_archive (
            id INTEGER PRIMARY KEY,target TEXT NOT NULL,archived_at TEXT NOT NULL,
            table_name TEXT NOT NULL,payload TEXT NOT NULL)''')
        for name in tables:
            for item in db.execute('SELECT * FROM '+name).fetchall():
                db.execute('INSERT INTO system_target_archive(target,archived_at,table_name,payload) VALUES(?,?,?,?)',
                    (previous,stamp,name,json.dumps(dict(item))))
            db.execute('DELETE FROM '+name)
        db.execute("UPDATE manual_runs SET status='interrupted',phase='interrupted',error='configuration_changed' WHERE status='running'")
    db.execute("INSERT OR REPLACE INTO metadata VALUES('system_database_target',?)",(target,))
    return config
