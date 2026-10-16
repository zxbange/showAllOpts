from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / '.env',
        env_file_encoding='utf-8',
        extra='ignore',
    )

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

    @property
    def log_path(self) -> Path:
        project_root = Path(__file__).resolve().parents[3]
        return (project_root / self.log_dir).resolve()

    @property
    def database_url(self) -> URL:
        return URL.create(
            'mysql+pymysql', username=self.db_user,
            password=self.db_password.get_secret_value(), host=self.db_host,
            port=self.db_port, database=self.db_name, query={'charset': 'utf8mb4'},
        )
