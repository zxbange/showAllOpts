"""Shared console/file logging with daily, process-safe rotation."""
import logging
import logging.config
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, quote_plus

from concurrent_log_handler import ConcurrentTimedRotatingFileHandler

from app.core.config import Settings

ANSI_ESCAPE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
CREDENTIAL_URL = re.compile(r'(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@')
SECRET_FIELD = re.compile(
    r'''(?i)(\b(?:password|passwd|pwd|token|secret|authorization)\b["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s&,;\)\]\}]+)'''
)


class StandardFormatter(logging.Formatter):
    """Prefix every line, including traceback lines; redact before any output."""

    def __init__(self, secrets=()):
        super().__init__()
        self.secrets = sorted({variant for value in secrets if value
                               for variant in (value, quote(value, safe=''), quote_plus(value))},
                              key=len, reverse=True)

    def format(self, record):
        message = record.getMessage()
        if record.exc_info:
            message += '\n' + self.formatException(record.exc_info)
        if record.stack_info:
            message += '\n' + record.stack_info
        message = ANSI_ESCAPE.sub('', message)
        message = CREDENTIAL_URL.sub(r'\1[REDACTED]@', message)
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
    secrets.extend(value for value in (settings.db_host, settings.db_user, settings.db_name)
                   if len(value) >= 4)
    return {
        'version': 1,
        'disable_existing_loggers': False,
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
            name: {'handlers': [], 'level': settings.log_level, 'propagate': True}
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
