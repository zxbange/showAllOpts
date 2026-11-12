"""Write-only integration settings, with read-only migration of legacy Telegram secrets."""
from contextlib import closing
from copy import deepcopy
import json
import logging
import os
import sqlite3
from cryptography.fernet import Fernet

LOG = logging.getLogger(__name__)
FIELDS = {'asharehub': {'token': 'ASHAREHUB_API_KEY'},
          'telegram': {'token': 'TELEGRAM_BOT_TOKEN', 'chat_id': 'TELEGRAM_CHAT_ID'}}
ATTRIBUTES = {'ASHAREHUB_API_KEY': 'asharehub_api_key',
              'TELEGRAM_BOT_TOKEN': 'telegram_bot_token', 'TELEGRAM_CHAT_ID': 'telegram_chat_id'}


def _legacy_telegram(settings):
    """Never create a social DB/key or mutate old credentials during a GET."""
    root = settings.log_path / 'social'
    path = root / 'monitor.sqlite3'
    empty = dict(token='', chat_id='')
    if not path.is_file():
        return empty
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT payload FROM config WHERE id=1').fetchone()
        data = json.loads(row[0]) if row else {}
        if not data.get('token') and not data.get('chat_id'):
            return empty
        cipher = Fernet((root / 'secrets.key').read_bytes())
        return {field: cipher.decrypt(data[field].encode()).decode() if data.get(field) else ''
                for field in empty}
    except Exception as exc:
        LOG.warning('Legacy Telegram settings unavailable error_type=%s', type(exc).__name__)
        return empty


def integration_defaults(settings, env_values=None):
    # Explicitly empty file/process values are clears, not legacy fallbacks.
    values = dict(env_values or {})
    values.update({key: os.environ[key] for group in FIELDS.values() for key in group.values() if key in os.environ})
    result = {group: {} for group in FIELDS}
    legacy = None
    for group, fields in FIELDS.items():
        for field, env_key in fields.items():
            if env_key in values:
                value = values[env_key] or ''
            else:
                if env_values is not None:
                    model_field = getattr(type(settings), 'model_fields', {}).get(ATTRIBUTES[env_key])
                    value = model_field.default if model_field is not None else ''
                else:
                    value = getattr(settings, ATTRIBUTES[env_key], '')
                if hasattr(value, 'get_secret_value'):
                    value = value.get_secret_value()
                value = value or ''
                if not value and group == 'telegram':
                    if legacy is None:
                        legacy = _legacy_telegram(settings)
                    value = legacy[field]
            result[group][field] = value
    return result


def sanitize_integrations(value):
    return {group: {field + '_set': bool(value.get(group, {}).get(field)) for field in fields}
            for group, fields in FIELDS.items()}


def validate_integrations(body, previous):
    from app.services.system_configuration import SystemConfigurationError
    result = {group: {field: previous.get(group, {}).get(field, '') for field in fields}
              for group, fields in FIELDS.items()}
    if body is None:
        return result
    if not isinstance(body, dict) or set(body) - set(FIELDS):
        raise SystemConfigurationError('接口配置格式无效')
    for group, changes in body.items():
        if not isinstance(changes, dict) or set(changes) - set(FIELDS[group]) - {key + '_set' for key in FIELDS[group]}:
            raise SystemConfigurationError('接口配置格式无效')
        for field in FIELDS[group]:
            value = changes.get(field)
            if value is None:
                continue
            if not isinstance(value, str) or len(value) > (256 if field == 'chat_id' else 4096) or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise SystemConfigurationError('接口凭据格式无效')
            result[group][field] = value.strip()
    return result


def integration_env_updates(candidate, previous):
    return {env_key: candidate[group][field] for group, fields in FIELDS.items()
            for field, env_key in fields.items()
            if candidate[group][field] != previous.get(group, {}).get(field)}


def integrations_config(settings):
    from app.services.system_configuration import SystemConfiguration
    return deepcopy(SystemConfiguration(settings).load()['integrations'])
