"""Shared console/file logging with daily, process-safe rotation."""
import logging
import logging.config
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, quote_plus

from concurrent_log_handler import ConcurrentTimedRotatingFileHandler
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.core.diagnostics import diagnostic_details

ANSI_ESCAPE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
# Kept for standalone diagnostic scripts importing this legacy helper.
CREDENTIAL_URL = re.compile(r'(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@')
# Provider webhook paths and query strings may contain credentials too.
FULL_URL = re.compile(r'''(?i)\b[a-z][a-z0-9+.-]*://[^\s'"<>]+''')
AUTH_HEADER = re.compile(
    r'''(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\b["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\r\n]+)'''
)
SECRET_FIELD = re.compile(
    r'''(?i)(\b(?:password|passwd|pwd|authorization|(?:access_|refresh_|session_)?token|secret|api[_-]?key|csrf[_-]?token)\b["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s&,;\)\]\}]+)'''
)


class PollingAccessFilter(logging.Filter):
    """Omit only successful GET polling; keep failures and unexpected formats."""

    paths = frozenset({'/api/v1/etf-shares/collection-status', '/api/v1/cache/status',
                       '/api/v1/market-collection/status'})

    def filter(self, record):
        if record.name != 'uvicorn.access' or record.levelno >= logging.WARNING:
            return True
        # Uvicorn supplies (client, method, full_path, http_version, status_code).
        # Fail open if a future Uvicorn release changes its structured arguments.
        if not isinstance(record.args, tuple) or len(record.args) != 5:
            return True
        _, method, path, _, status = record.args
        if method != 'GET' or not isinstance(path, str) or type(status) is not int:
            return True
        return not (path.split('?', 1)[0] in self.paths and (200 <= status < 300 or status == 304))


class StandardFormatter(logging.Formatter):
    """Prefix every line, including traceback lines; redact before any output."""

    def __init__(self, secrets=()):
        super().__init__()
        self.secrets = sorted({variant for value in secrets if value
                               for variant in (value, quote(value, safe=''), quote_plus(value))},
                              key=len, reverse=True)

    def format(self, record):
        if record.name == 'uvicorn.access' and isinstance(record.args, tuple) and len(record.args) == 5 and isinstance(record.args[2], str):
            args = list(record.args)
            args[2] = args[2].split('?', 1)[0].split('#', 1)[0]
            message = str(record.msg) % tuple(args)
        else:
            message = record.getMessage()
        if record.exc_info:
            # Raw tracebacks include exception messages and source lines, which
            # may contain credentials unknown to the configured redactor.
            message += '\nTraceback diagnostics: message=[REDACTED] ' + diagnostic_details(record.exc_info[1])
        if record.stack_info:
            message += '\n[stack_info omitted; use exception diagnostics]'
        message = ANSI_ESCAPE.sub('', message)
        message = FULL_URL.sub('[REDACTED_URL]', message)
        message = AUTH_HEADER.sub(r'\1[REDACTED]', message)
        message = SECRET_FIELD.sub(r'\1[REDACTED]', message)
        for value in self.secrets:
            message = message.replace(value, '[REDACTED]')
        timestamp = datetime.fromtimestamp(record.created).astimezone().isoformat(timespec='milliseconds')
        pid = getattr(record, 'source_pid', record.process)
        prefix = f'{timestamp} | {record.levelname:<8} | pid={pid} | {record.name} | '
        return '\n'.join(prefix + line for line in message.splitlines()) or prefix


def daily_handler(filename, retention_days=30):
    target = Path(filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Create even error.log on startup without changing existing files or their mtimes.
    descriptor = os.open(target, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o640)
    os.close(descriptor)
    handler = ConcurrentTimedRotatingFileHandler(
        str(target), when='midnight', interval=1, backupCount=retention_days,
        maxBytes=0, encoding='utf-8', utc=False, chmod=0o640,
        keep_file_open=False,
    )
    handler.showallopts_log_dir = str(target.parent)
    return handler


def logging_config(settings: Settings):
    directory = settings.log_path
    # Do not pass a database URL into logs. Also redact known values in driver errors.
    secrets = [settings.db_password.get_secret_value()]
    secrets.append(settings.social_browser_login_key.get_secret_value())
    secrets.extend([settings.validation_admin_key.get_secret_value(), settings.validation_encryption_key.get_secret_value()])
    for field in ('market_collection_writer_url', 'etf_collection_writer_url'):
        raw = getattr(settings, field).get_secret_value()
        if raw:
            secrets.append(raw)
            try:
                password = make_url(raw).password
                if password:
                    secrets.append(password)
            except Exception:
                pass
    secrets.extend(value for value in (settings.db_host, settings.db_user, settings.db_name)
                   if len(value) >= 4)
    return {
        'version': 1,
        'disable_existing_loggers': False,
        'filters': {'polling_access': {'()': PollingAccessFilter}},
        'formatters': {'standard': {'()': StandardFormatter, 'secrets': secrets}},
        'handlers': {
            'console': {'class': 'logging.StreamHandler', 'formatter': 'standard',
                        'level': settings.log_level, 'stream': 'ext://sys.stderr'},
            'application': {'()': daily_handler, 'filename': str(directory / 'app.log'),
                            'retention_days': settings.log_retention_days,
                            'formatter': 'standard', 'level': settings.log_level},
            'errors': {'()': daily_handler, 'filename': str(directory / 'error.log'),
                       'retention_days': settings.log_retention_days,
                       'formatter': 'standard', 'level': 'ERROR'},
        },
        'root': {'handlers': ['console', 'application', 'errors'], 'level': settings.log_level},
        'loggers': {
            name: {'handlers': [], 'level': settings.log_level, 'propagate': True,
                   'filters': ['polling_access'] if name == 'uvicorn.access' else []}
            for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access', 'app', 'launcher', 'frontend')
        },
    }


def configure_logging(settings: Settings):
    logging.config.dictConfig(logging_config(settings))
    logging.captureWarnings(True)


def ensure_logging(settings: Settings):
    # Uvicorn's runner already configures every spawned worker. Also support direct app usage.
    if not any(getattr(handler, 'showallopts_log_dir', None) == str(settings.log_path)
               for handler in logging.getLogger().handlers):
        configure_logging(settings)
