from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Telegram Outreach API"
    database_url: str = "postgresql+asyncpg://outreach:outreach@postgres:5432/outreach"
    redis_url: str = "redis://redis:6379/0"
    frontend_origin: str = "http://localhost:3000"
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    encryption_secret: str = "change-this-before-production"
    account_status_interval: int = 20
    session_cookie_secure: bool = False  # В production HTTPS обязательно True.

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
