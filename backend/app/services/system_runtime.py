"""Validated runtime controls wired to logging, sync connections and source requests."""
from copy import deepcopy
import logging
from pathlib import Path
import threading
from urllib.parse import urlsplit

import httpx

LOG = logging.getLogger(__name__)
LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
SOURCES = {
    'eastmoney': ('东方财富', ('eastmoney.com',), 60),
    'asharehub': ('AShareHub', ('asharehub.com',), 45),
    'tdx': ('通达信', ('tdx.com.cn',), 600),
    'sse': ('上海证券交易所', ('sse.com.cn',), 180),
    'szse': ('深圳证券交易所', ('szse.cn',), 60),
    'tencent': ('腾讯行情', ('gtimg.cn', 'gtimg.com', 'qq.com'), 20),
    'sina': ('新浪行情', ('sina.com.cn', 'sina.cn', 'sinajs.cn'), 25),
    'ccmn': ('长江有色', ('ccmn.cn',), 15),
    'coalindex': ('CCI 网络报价', ('coalindex.com',), 25),
    'csi': ('中证指数', ('csindex.com.cn',), 20),
}
_WATCHERS = {}
_WATCHERS_LOCK = threading.Lock()


def default_runtime(settings):
    return {
        'logging': {'level': getattr(settings, 'log_level', 'INFO'),
                    'retention_days': getattr(settings, 'log_retention_days', 30)},
        'database_sync': {'read_timeout_seconds': getattr(settings, 'cache_db_read_timeout', 600),
                          'attempts': getattr(settings, 'cache_sync_attempts', 3)},
        'sources': {key: {'request_timeout_seconds': definition[2]} for key, definition in SOURCES.items()},
    }


def validate_runtime(body, previous):
    from app.services.system_configuration import SystemConfigurationError
    result = deepcopy(previous)
    if body is None:
        return result
    if not isinstance(body, dict) or set(body) - {'logging', 'database_sync', 'sources'}:
        raise SystemConfigurationError('运行参数格式无效')
    for group, fields in body.items():
        if not isinstance(fields, dict) or set(fields) - set(result[group]):
            raise SystemConfigurationError('运行参数格式无效')
        if group == 'sources':
            for source, source_fields in fields.items():
                if not isinstance(source_fields, dict) or set(source_fields) != {'request_timeout_seconds'}:
                    raise SystemConfigurationError('数据源超时格式无效')
                seconds = source_fields['request_timeout_seconds']
                if type(seconds) is not int or not 5 <= seconds <= 600:
                    raise SystemConfigurationError('数据源请求超时须为5至600秒')
                result[group][source] = dict(source_fields)
        else:
            result[group].update(fields)
    logging_config = result['logging']
    if logging_config['level'] not in LEVELS:
        raise SystemConfigurationError('日志级别无效')
    retention = logging_config['retention_days']
    if type(retention) is not int or not 1 <= retention <= 3650:
        raise SystemConfigurationError('日志保留天数须为1至3650天')
    sync = result['database_sync']
    if type(sync['read_timeout_seconds']) is not int or not 5 <= sync['read_timeout_seconds'] <= 3600:
        raise SystemConfigurationError('数据库同步读取超时须为5至3600秒')
    if type(sync['attempts']) is not int or not 1 <= sync['attempts'] <= 5:
        raise SystemConfigurationError('数据库同步尝试次数须为1至5次，包含首次尝试')
    return result


def runtime_config(settings):
    from app.services.system_configuration import SystemConfiguration
    value = SystemConfiguration(settings).load()
    return deepcopy(value.get('runtime', default_runtime(settings)))


def source_status(settings, runtime=None, integrations=None):
    """Describe configured read-only integrations, not unperformed health probes."""
    if runtime is None:
        runtime = runtime_config(settings)
    if integrations is None:
        from app.services.system_integrations import integrations_config
        integrations = integrations_config(settings)
    result = []
    for key, (label, hosts, _) in SOURCES.items():
        required = key == 'asharehub'
        credential_set = bool(integrations.get('asharehub', {}).get('token')) if required else False
        result.append(dict(id=key, label=label, hosts=list(hosts),
                           request_timeout_seconds=runtime['sources'][key]['request_timeout_seconds'],
                           credential_required=required, credential_set=credential_set,
                           status='credential_missing' if required and not credential_set else 'configured',
                           health='not_checked', read_only=True))
    return result


def platform_for(url):
    host = (urlsplit(str(url)).hostname or '').lower().rstrip('.')
    for key, (_, suffixes, _) in SOURCES.items():
        if any(host == suffix or host.endswith('.' + suffix) for suffix in suffixes):
            return key
    return None


class SourceClient:
    """A narrow HTTPX adapter; keeps redirects, TLS, headers and rate limits intact.

    HTTPX timeout is an I/O inactivity limit, not a total download budget. Existing
    provider/collector total budgets still apply. A request already running keeps
    its timeout; new requests use current config (or the job's pinned config).
    """
    def __init__(self, settings, client):
        self.settings, self.client, self.manager = settings, client, client

    def __enter__(self):
        self.client = self.manager.__enter__()
        return self

    def __exit__(self, *args):
        return self.manager.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.client, name)

    def options(self, url, kwargs):
        source = platform_for(url)
        if source:
            seconds = runtime_config(self.settings)['sources'][source]['request_timeout_seconds']
            kwargs['timeout'] = httpx.Timeout(seconds, connect=min(30, seconds), pool=min(30, seconds))
        return kwargs

    def get(self, url, *args, **kwargs):
        return self.client.get(url, *args, **self.options(url, kwargs))

    def post(self, url, *args, **kwargs):
        return self.client.post(url, *args, **self.options(url, kwargs))

    def request(self, method, url, *args, **kwargs):
        return self.client.request(method, url, *args, **self.options(url, kwargs))

    def stream(self, method, url, *args, **kwargs):
        return self.client.stream(method, url, *args, **self.options(url, kwargs))


def source_client(settings, *, factory=None, **kwargs):
    return SourceClient(settings, (factory or httpx.Client)(**kwargs))


def apply_logging(settings):
    """Update this process's existing app handlers without disabling redaction."""
    from app.core.logging import StandardFormatter
    from app.services.system_configuration import SystemConfiguration
    value = SystemConfiguration(settings).load()
    config = value.get('runtime', default_runtime(settings))['logging']
    root = logging.getLogger()
    owned = [handler for handler in root.handlers
             if getattr(handler, 'showallopts_log_dir', None) == str(settings.log_path)]
    # Do not change unrelated applications/test harnesses without owned handlers.
    if not owned:
        return False
    level = config['level']
    root.setLevel(level)
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access', 'app', 'launcher', 'frontend'):
        logging.getLogger(name).setLevel(level)
    secrets = [value['database'].get('password', '')]
    integrations = value.get('integrations', {})
    secrets.extend(integrations.get(group, {}).get('token', '') for group in ('telegram', 'asharehub'))
    for handler in root.handlers:
        if not isinstance(handler.formatter, StandardFormatter):
            continue
        is_error = Path(getattr(handler, 'baseFilename', '')).name == 'error.log'
        handler.setLevel('ERROR' if is_error else level)
        if handler in owned:
            handler.backupCount = config['retention_days']
        if not hasattr(handler, '_runtime_redaction_base'):
            handler._runtime_redaction_base = tuple(handler.formatter.secrets)
        handler.setFormatter(StandardFormatter([*handler._runtime_redaction_base, *secrets]))
    return True


def start_runtime(settings):
    """Every worker applies persisted logging changes within five seconds."""
    key = str(settings.log_path)
    with _WATCHERS_LOCK:
        existing = _WATCHERS.get(key)
        if existing and existing[0].is_alive():
            return
        stop = threading.Event()
        def watch():
            while not stop.is_set():
                try:
                    apply_logging(settings)
                except Exception as exc:
                    LOG.warning('Runtime logging configuration unavailable error_type=%s', type(exc).__name__)
                stop.wait(5)
        worker = threading.Thread(target=watch, daemon=True, name='runtime-configuration')
        _WATCHERS[key] = worker, stop
        worker.start()


def stop_runtime(settings):
    with _WATCHERS_LOCK:
        existing = _WATCHERS.get(str(settings.log_path))
    if existing:
        existing[1].set()
        existing[0].join(timeout=2)
