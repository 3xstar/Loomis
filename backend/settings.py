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
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""


settings = Settings()
