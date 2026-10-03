from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Telegram Outreach API"
    database_url: str = "postgresql+asyncpg://outreach:outreach@postgres:5432/outreach"
    redis_url: str = "redis://redis:6379/0"
    frontend_origin: str = "http://localhost:3000"
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_bot_token: str = ""  # Bot from @BotFather for lead/sale notifications.
    encryption_secret: str = "change-this-before-production"
    account_status_interval: int = 20
    session_cookie_secure: bool = False  # В production HTTPS обязательно True.
    recordings_dir: str = "data/recordings"
    # AI assistant in chats. Provider: yandex (YandexGPT, OpenAI-compatible API) | openai | anthropic | openai_compatible.
    llm_provider: str = ""
    llm_api_key: str = ""
    llm_model: str = ""          # yandex: defaults to gpt://<folder>/yandexgpt/latest; anthropic: claude-sonnet-5-5
    llm_base_url: str = ""       # openai_compatible only (any OpenAI-style endpoint)
    yandex_folder_id: str = ""   # Yandex Cloud folder for YandexGPT
    # Web push for the installable portal (PWA). Generated on first start when empty.
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:admin@steptolead.ru"
    # Speech-to-text for AI call analysis: yandex (SpeechKit, key and folder default to the LLM ones) | openai (Whisper-style API).
    stt_provider: str = ""
    stt_api_key: str = ""
    stt_model: str = ""          # openai: whisper-1 by default
    stt_base_url: str = ""       # openai-style endpoint; defaults to LLM_BASE_URL or api.openai.com

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
