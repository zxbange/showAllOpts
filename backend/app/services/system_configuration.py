"""Versioned administrator configuration, with the selected dotenv file authoritative."""
from contextlib import contextmanager, closing
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import ipaddress
import json
import logging
import re
import sqlite3
from sqlalchemy import URL, text
from app.services.source_keys import persistent_cipher, KeyStorageError
from app.services.safe_env import env_transaction, EnvFileError

LOG = logging.getLogger(__name__)
_PIN = ContextVar('system_configuration_snapshot', default=None)
TIME = re.compile(r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')

class SystemConfigurationError(ValueError):
    pass

def default_schedules(settings, *, legacy=True, warnings=None):
    def daily(value, enabled=True):
        return {'enabled': enabled, 'times': [value]}
    result = {
        'cache': daily(getattr(settings, 'cache_refresh_time', '07:00'), getattr(settings, 'cache_auto_refresh', True)),
        'margin': daily('08:00', getattr(settings, 'margin_worker_enabled', True)),
        'etf': daily('09:00', getattr(settings, 'etf_collection_enabled', True)),
        'commodities': {'enabled': True, 'times': ['11:00', '18:00']},
        'growth_dividend': daily('18:00', getattr(settings, 'growth_dividend_worker_enabled', True)),
        'market': daily('00:01', getattr(settings, 'market_collection_enabled', True) and getattr(settings, 'market_collection_worker_enabled', True)),
        'valuation_ruler': daily('20:00', getattr(settings, 'valuation_ruler_worker_enabled', True)),
        'cci': daily('21:00'),
        'cash_cow': daily('21:00', getattr(settings, 'cash_cow_worker_enabled', True)) | {'frequency': 'month_end'},
        'holdings': dict(enabled=False, start='09:25', end='15:10', interval_seconds=15, table=''),
        'social': dict(enabled=False, start='07:00', end='24:00', min_interval_minutes=10, max_interval_minutes=16),
        'social_cubes': dict(enabled=True, start='09:00', end='15:00'),
    }
    for key in ('etf', 'margin', 'commodities', 'growth_dividend', 'market', 'valuation_ruler', 'cci', 'cash_cow'):
        result[key]['telegram_notify'] = False
    path = getattr(settings, 'cache_path', settings.log_path / 'cache') / 'schedule.json'
    if legacy and path.exists():
        try:
            value = json.loads(path.read_text())['daily_time']
            if not TIME.fullmatch(value):
                raise ValueError()
            result['cache']['times'] = [value]
        except Exception:
            result['cache']['enabled'] = False
            if warnings is not None:
                warnings.append('已有同步时间配置无法读取，已暂停自动同步；原配置保留，请恢复后重试。')
    if legacy:
        _legacy_preferences(settings, result, warnings)
    return result

def _legacy_preferences(settings, result, warnings=None):
    def warn(old, module):
        target = (old.get('host', '').lower().rstrip('.'), int(old.get('port', 3306)), old.get('database'))
        current = (getattr(settings, 'db_host', '').lower().rstrip('.'), getattr(settings, 'db_port', 3306), getattr(settings, 'db_name', ''))
        if warnings is not None and (target != current or old.get('username') != getattr(settings, 'db_user', '')):
            warnings.append(module + '原数据库目标或账号与应用配置不同；当前沿用应用数据库，请确认统一账号具备所需读写权限。原数据保留，不自动迁移。')
    def failed(key, label):
        result[key]['enabled'] = False
        if warnings is not None:
            warnings.append('已有' + label + '配置无法读取，已暂停自动任务；原配置及密钥保留，请恢复后重试。')
    def same_target(old):
        return (old['host'].lower().rstrip('.'), int(old['port']), old['database']) == (settings.db_host.lower().rstrip('.'), settings.db_port, settings.db_name)
    root = settings.log_path / 'personal-holdings' / 'data'
    path = root / 'monitor.sqlite3'
    if path.exists():
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                row = db.execute('SELECT payload FROM holdings_state WHERE id=1').fetchone()
                state = json.loads(row[0]) if row else {}
                ref = state.get('source')
                if ref and not ref['id'].startswith('system:'):
                    row = db.execute('SELECT ciphertext FROM validation_sources WHERE id=? AND revision=?', (ref['id'], ref['revision'])).fetchone()
                    if row is None:
                        raise ValueError()
                    def validate(cipher):
                        if cipher is None:
                            raise KeyStorageError('已有持仓配置密钥缺失')
                        cipher.decrypt(row[0])
                    cipher = persistent_cipher(root, settings.validation_encryption_key.get_secret_value(), validate)
                    payload = json.loads(cipher.decrypt(row[0]))
                    if payload['id'] != ref['id'] or payload['revision'] != ref['revision']:
                        raise ValueError()
                    old = payload['config']
                    warn(old, '个人持仓')
                    result['holdings']['table'] = old['table']
                    result['holdings']['enabled'] = bool(state.get('enabled')) and same_target(old)
                elif ref and ref['id'].startswith('system:'):
                    result['holdings']['table'] = state.get('system_table', '')
                    result['holdings']['enabled'] = bool(state.get('enabled')) and bool(result['holdings']['table'])
        except Exception:
            failed('holdings', '持仓')
    for key, directory, label in (('etf', 'etf-collection', 'ETF'), ('market', 'market-collection', '市场')):
        root = settings.log_path / 'market-data' / directory
        path = root / 'state.sqlite'
        if not path.exists():
            continue
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                row = db.execute('SELECT revision,ciphertext FROM configuration WHERE id=1').fetchone()
            if row:
                def validate(cipher):
                    if cipher is None or json.loads(cipher.decrypt(row[1]))['revision'] != row[0]:
                        raise KeyStorageError('已有配置密钥缺失或不匹配')
                cipher = persistent_cipher(root if key == 'etf' else root / 'credentials', '', validate)
                old = json.loads(cipher.decrypt(row[1]))
                warn(old, label + '采集')
                if key == 'etf':
                    times = old.get('schedule', [])
                    if len(times) == 1 and isinstance(times[0], str) and TIME.fullmatch(times[0]):
                        result[key]['times'] = times
                    result[key]['enabled'] = bool(old.get('enabled')) and same_target(old)
                else:
                    result[key]['enabled'] = bool(old.get('enabled')) and getattr(settings, 'market_collection_worker_enabled', True)
        except Exception:
            failed(key, label)
    path = settings.log_path / 'social' / 'monitor.sqlite3'
    if path.exists():
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                row = db.execute('SELECT payload FROM config WHERE id=1').fetchone()
            if row:
                old = json.loads(row[0])
                result['social'].update(enabled=bool(old.get('enabled')), min_interval_minutes=old.get('interval_min', 600) / 60, max_interval_minutes=old.get('interval_max', 960) / 60)
        except Exception:
            failed('social', '社交监控')

class SystemConfiguration:
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.log_path / 'system-configuration'
        self.path = self.root / 'configuration.sqlite3'

    def default(self):
        from app.services.system_integrations import integration_defaults
        from app.services.system_runtime import default_runtime
        s = self.settings
        warnings = []
        schedules = default_schedules(s, warnings=warnings)
        result = dict(revision=0, database_revision=0, timezone='Asia/Shanghai', database=dict(host=getattr(s, 'db_host', '127.0.0.1'), port=getattr(s, 'db_port', 3306), database=getattr(s, 'db_name', 'showallopts'), username=getattr(s, 'db_user', 'readonly'), password=s.db_password.get_secret_value() if hasattr(s, 'db_password') else ''), schedules=schedules, integrations=integration_defaults(s), runtime=default_runtime(s))
        if warnings:
            result['migration_warnings'] = warnings
        return result

    def record(self):
        if not self.path.exists():
            return None
        try:
            with sqlite3.connect(self.path) as db:
                return db.execute('SELECT revision,ciphertext FROM configuration WHERE id=1').fetchone()
        except Exception:
            raise SystemConfigurationError('系统配置无法读取；请保留配置文件与密钥并检查存储权限') from None

    def cipher(self):
        def validate(cipher):
            row = self.record()
            if row:
                try:
                    if cipher is None or json.loads(cipher.decrypt(row[1]))['revision'] != row[0]:
                        raise ValueError()
                except Exception:
                    raise KeyStorageError('系统配置无法解密，请恢复匹配密钥；不会覆盖已有配置') from None
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            return persistent_cipher(self.root / 'credentials', '', validate)
        except KeyStorageError as exc:
            raise SystemConfigurationError(str(exc)) from None

    def _local_load(self):
        row = self.record()
        if row is None:
            return self.default()
        try:
            result = json.loads(self.cipher().decrypt(row[1]))
            if result['revision'] != row[0]:
                raise ValueError()
            from app.services.system_integrations import integration_defaults
            result.setdefault('integrations', integration_defaults(self.settings))
            from app.services.system_runtime import default_runtime
            result.setdefault('runtime', default_runtime(self.settings))
            return result
        except SystemConfigurationError:
            raise
        except Exception:
            raise SystemConfigurationError('系统配置无法解密，请恢复匹配密钥；不会回退旧配置') from None

    @contextmanager
    def environment(self):
        try:
            with env_transaction(self.settings) as transaction:
                yield transaction
        except EnvFileError as exc:
            raise SystemConfigurationError(str(exc)) from None

    def _from_environment(self, value, environment):
        if environment.values is None:
            return value
        from app.services.system_integrations import integration_defaults
        values = environment.values
        defaults = getattr(type(self.settings), 'model_fields', {})
        mapping = {'host': 'db_host', 'port': 'db_port', 'database': 'db_name', 'username': 'db_user', 'password': 'db_password'}
        keys = {'host': 'DB_HOST', 'port': 'DB_PORT', 'database': 'DB_NAME', 'username': 'DB_USER', 'password': 'DB_PASSWORD'}
        database = {}
        for name, attr in mapping.items():
            fallback = defaults[attr].default if attr in defaults else getattr(self.settings, attr)
            if hasattr(fallback, 'get_secret_value'):
                fallback = fallback.get_secret_value()
            database[name] = values.get(keys[name], fallback)
        try:
            database['port'] = int(database['port'])
            if not 1 <= database['port'] <= 65535 or any(not isinstance(database[k], str) for k in ('host','database','username','password')):
                raise ValueError()
        except Exception:
            raise SystemConfigurationError('配置文件中的数据库字段无效，请修复后重试') from None
        return value | {'database': database, 'integrations': integration_defaults(self.settings, values), 'environment_overrides': environment.overridden}

    def _persist(self, value, cipher):
        with sqlite3.connect(self.path, timeout=10) as db:
            self.path.chmod(0o600)
            db.execute('CREATE TABLE IF NOT EXISTS configuration(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL,ciphertext BLOB NOT NULL)')
            db.execute('INSERT OR REPLACE INTO configuration VALUES(1,?,?)', (value['revision'], cipher.encrypt(json.dumps(value).encode())))

    def _reconcile(self, environment):
        current = self._local_load()
        value = self._from_environment(current, environment)
        changed = value['database'] != current['database'] or value.get('integrations') != current.get('integrations')
        if environment.values is not None and (self.record() is None or changed):
            if self.record() is not None and changed:
                value['revision'] += 1
                value['database_revision'] += int(value['database'] != current['database'])
                value['changed_at'] = datetime.now(timezone.utc).isoformat()
            self._persist(value, self.cipher())
        return value

    def load(self):
        pinned = _PIN.get()
        if pinned and pinned[0] == str(self.path):
            return deepcopy(pinned[1])
        with self.environment() as environment:
            return self._reconcile(environment)

    def public(self, value=None):
        value = deepcopy(value if value is not None else self.load())
        password = value['database'].pop('password', None)
        value['database']['password_set'] = password is not None and password != ''
        from app.services.system_integrations import sanitize_integrations
        from app.services.system_runtime import source_status
        value['data_sources'] = source_status(self.settings, value['runtime'], value.get('integrations', {}))
        value['integrations'] = sanitize_integrations(value.get('integrations', {}))
        return value

    def validate(self, body, previous=None):
        previous = previous or self.load()
        if type(body.get('revision')) is not int or body['revision'] != previous['revision']:
            raise SystemConfigurationError('配置已更新，请刷新后重试')
        candidate = body.get('database')
        if not isinstance(candidate, dict):
            raise SystemConfigurationError('请完整填写数据库配置')
        db = {k: candidate.get(k) for k in ('host', 'port', 'database', 'username', 'password')}
        for key in ('host', 'database', 'username'):
            if not isinstance(db[key], str) or not db[key].strip() or any(ord(c) < 32 for c in db[key]):
                raise SystemConfigurationError('数据库配置格式无效')
            db[key] = db[key].strip()
        db['host'] = db['host'].lower().rstrip('.')
        try:
            ipaddress.ip_address(db['host'])
        except ValueError:
            if not re.fullmatch(r'(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', db['host']):
                raise SystemConfigurationError('数据库主机应填写域名或IP地址') from None
        if type(db['port']) is not int or not 1 <= db['port'] <= 65535 or len(db['database']) > 64 or len(db['username']) > 128:
            raise SystemConfigurationError('数据库配置格式无效')
        if db['password'] is None:
            if any(db[k] != previous['database'][k] for k in ('host', 'port', 'database', 'username')):
                raise SystemConfigurationError('更换数据库或账号时请重新输入密码')
            db['password'] = previous['database']['password']
        if not isinstance(db['password'], str) or len(db['password']) > 4096:
            raise SystemConfigurationError('数据库密码格式无效')
        schedules = deepcopy(body.get('schedules'))
        defaults = default_schedules(self.settings, legacy=False)
        if not isinstance(schedules, dict) or set(schedules) != set(defaults):
            raise SystemConfigurationError('请提交完整任务配置')
        for name, value in schedules.items():
            if not isinstance(value, dict) or set(value) != set(defaults[name]) or type(value.get('enabled')) is not bool:
                raise SystemConfigurationError('任务配置格式无效')
            if 'telegram_notify' in value and type(value['telegram_notify']) is not bool:
                raise SystemConfigurationError('通知开关格式无效')
            if 'times' in value:
                times = value['times']
                count = 2 if name == 'commodities' else 1
                if not isinstance(times, list) or len(times) != count or any(not isinstance(t, str) or not TIME.fullmatch(t) for t in times) or times != sorted(set(times)):
                    raise SystemConfigurationError('任务时间须按顺序填写HH:MM')
                if name == 'cash_cow' and value['frequency'] != 'month_end':
                    raise SystemConfigurationError('奶牛股按每月最后一天运行')
            elif (not isinstance(value['start'], str) or not TIME.fullmatch(value['start']) or not isinstance(value['end'], str) or not (TIME.fullmatch(value['end']) or value['end'] == '24:00') or value['start'] >= value['end']):
                raise SystemConfigurationError('时间窗口须按当日开始、结束顺序填写')
            if name == 'holdings':
                if type(value['interval_seconds']) is not int or not 5 <= value['interval_seconds'] <= 86400:
                    raise SystemConfigurationError('持仓轮询间隔须为5至86400秒')
                if not isinstance(value['table'], str) or (value['table'] and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', value['table'])):
                    raise SystemConfigurationError('持仓表名格式无效')
                if value['enabled'] and not value['table']:
                    raise SystemConfigurationError('启用持仓监控前请填写持仓表名')
            if name == 'social':
                lo, hi = value['min_interval_minutes'], value['max_interval_minutes']
                if type(lo) not in (int, float) or type(hi) not in (int, float) or not 1 <= lo < hi <= 1440:
                    raise SystemConfigurationError('社交轮询周期须为1至1440分钟且上限大于下限')
        from app.services.system_integrations import validate_integrations
        integrations = validate_integrations(body.get('integrations'), previous.get('integrations', {}))
        from app.services.system_runtime import validate_runtime, default_runtime
        runtime = validate_runtime(body.get('runtime'), previous.get('runtime', default_runtime(self.settings)))
        return dict(revision=previous['revision'], database_revision=previous['database_revision'], timezone='Asia/Shanghai', database=db, schedules=schedules, integrations=integrations, runtime=runtime)

    def save(self, body):
        from app.services.system_integrations import integration_env_updates
        with self.environment() as environment:
            current = self._reconcile(environment)
            candidate = self.validate(body, current)
            cipher = self.cipher()
            candidate['revision'] += 1
            candidate['database_revision'] += int(candidate['database'] != current['database'])
            candidate['changed_at'] = datetime.now(timezone.utc).isoformat()
            keys = {'host': 'DB_HOST', 'port': 'DB_PORT', 'database': 'DB_NAME', 'username': 'DB_USER', 'password': 'DB_PASSWORD'}
            updates = {target: str(candidate['database'][key]) for key, target in keys.items() if candidate['database'][key] != current['database'][key]}
            updates.update(integration_env_updates(candidate.get('integrations', {}), current.get('integrations', {})))
            if updates and environment.path is None and getattr(self.settings, 'resolved_env_file', None) is not None:
                raise SystemConfigurationError('配置文件不存在，请先设置可读写配置文件；未保存凭据')
            with sqlite3.connect(self.path, timeout=10) as db:
                self.path.chmod(0o600)
                db.execute('CREATE TABLE IF NOT EXISTS configuration(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL,ciphertext BLOB NOT NULL)')
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT revision FROM configuration WHERE id=1').fetchone()
                if row is not None and row[0] != body['revision']:
                    raise SystemConfigurationError('配置已更新，请刷新后重试')
                if environment.path is not None:
                    environment.write(updates)
                db.execute('INSERT OR REPLACE INTO configuration VALUES(1,?,?)', (candidate['revision'], cipher.encrypt(json.dumps(candidate).encode())))
        return self.public(candidate)

    @staticmethod
    def url(config):
        return URL.create('mysql+pymysql', username=config['username'], password=config['password'], host=config['host'], port=config['port'], database=config['database'], query={'charset': 'utf8mb4'})

    def test(self, body):
        candidate = self.validate(body)
        engine = None
        try:
            from app.core.database import build_engine
            engine = build_engine(self.settings, configuration=candidate['database'])
            with engine.connect() as connection:
                connection.execute(text('SELECT 1'))
            return dict(ok=True, stage='connection', message='连接成功；此测试不代表全部业务表和写入权限已验证')
        except Exception as exc:
            from app.core.diagnostics import diagnostic_details
            LOG.warning('System database test failed %s', diagnostic_details(exc))
            raise SystemConfigurationError('数据库连接测试失败，请查看服务日志中的错误阶段和错误码') from None
        finally:
            if engine is not None:
                engine.dispose()

def database_config(settings):
    value = SystemConfiguration(settings).load()
    return value['database'] | {'revision': value['database_revision']}

def task_config(settings, key):
    return deepcopy(SystemConfiguration(settings).load()['schedules'][key])

@contextmanager
def pin_configuration(settings):
    service = SystemConfiguration(settings)
    token = _PIN.set((str(service.path), service.load()))
    try:
        yield
    finally:
        _PIN.reset(token)

def latest_task_slot(settings, key, moment):
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    local = moment.astimezone(ZoneInfo('Asia/Shanghai'))
    for value in reversed(task_config(settings, key)['times']):
        hour, minute = map(int, value.split(':'))
        slot = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if slot <= local:
            return slot
    hour, minute = map(int, task_config(settings, key)['times'][-1].split(':'))
    return (local - timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)

def window_open(settings, key, moment, *, weekdays=False):
    from zoneinfo import ZoneInfo
    local = moment.astimezone(ZoneInfo('Asia/Shanghai'))
    config = task_config(settings, key)
    return config['enabled'] and (not weekdays or local.weekday() < 5) and config['start'] <= local.strftime('%H:%M') < config['end']

def next_window_time(settings, key, moment, delay=0, *, weekdays=False):
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    candidate = moment.astimezone(ZoneInfo('Asia/Shanghai')) + timedelta(seconds=delay)
    config = task_config(settings, key)
    if (not weekdays or candidate.weekday() < 5) and config['start'] <= candidate.strftime('%H:%M') < config['end']:
        return candidate.timestamp()
    hour, minute = map(int, config['start'].split(':'))
    if candidate.strftime('%H:%M') >= config['end']:
        candidate += timedelta(days=1)
    candidate = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
    while weekdays and candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.timestamp()
