import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV_FILE = PROJECT_ROOT / 'backend' / '.env'


class ConfigurationError(ValueError):
    """Invalid external configuration location; never includes file contents."""


def resolve_env_file() -> Path:
    """Resolve per instance so workers and command-line tools share the same rules."""
    raw_path = os.environ.get('APP_ENV_FILE')
    if raw_path is None:
        return DEFAULT_ENV_FILE
    if not raw_path.strip():
        raise ConfigurationError('APP_ENV_FILE must not be empty; unset it to use backend/.env.')
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path = path.resolve()
    try:
        if not path.is_file():
            raise OSError('Not a regular configuration file')
        with path.open('rb'):
            pass
    except OSError:
        raise ConfigurationError(
            f'APP_ENV_FILE is missing or unreadable: {path}. Check the path and service account permissions.'
        ) from None
    return path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=DEFAULT_ENV_FILE,
        env_file_encoding='utf-8',
        extra='ignore',
        hide_input_in_errors=True,
    )

    def __init__(self, **values):
        # An explicit _env_file (including None in tests) overrides automatic selection.
        if '_env_file' not in values:
            values['_env_file'] = resolve_env_file()
        try:
            super().__init__(**values)
        except (OSError, UnicodeError):
            raise ConfigurationError('Cannot read the selected configuration file. Check permissions and UTF-8 encoding.') from None

    app_name: str = 'ShowAllOpts API'
    db_host: str = '127.0.0.1'
    db_port: int = Field(default=3306, ge=1, le=65535)
    db_name: str = 'showallopts'
    db_user: str = 'readonly'
    db_password: SecretStr = SecretStr('')
    cors_origins: list[str] = ['http://127.0.0.1:5173', 'http://localhost:5173']

    log_dir: Path = Path('log')
    log_level: Literal['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] = 'INFO'
    log_retention_days: int = Field(default=30, ge=1, le=3650)

    cache_enabled: bool = True
    cache_auto_refresh: bool = True
    cache_refresh_time: str = Field(default='18:00', pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')

    @property
    def cache_path(self) -> Path:
        return self.log_path / 'cache'

    @property
    def log_path(self) -> Path:
        return (PROJECT_ROOT / self.log_dir).resolve()

    @property
    def database_url(self) -> URL:
        return URL.create(
            'mysql+pymysql', username=self.db_user,
            password=self.db_password.get_secret_value(), host=self.db_host,
            port=self.db_port, database=self.db_name, query={'charset': 'utf8mb4'},
        )
