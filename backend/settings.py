import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    bot_token: str = ""
    loomis_worker_token: str = ""
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    database_url: str = "sqlite+aiosqlite:///./loomis.db"
    whisper_model: str = "base"
    secret_key: str = ""


settings = Settings()
if not settings.secret_key:
    settings.secret_key = secrets.token_urlsafe(32)
