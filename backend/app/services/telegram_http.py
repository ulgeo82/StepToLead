"""Where the portal's Telegram bots send Bot API requests.

The portal and its database stay in Russia; only Telegram traffic may go through a relay abroad
(TELEGRAM_API_BASE and/or TELEGRAM_PROXY). Other channels (WhatsApp, Avito) never use it.
"""
from app.core.config import settings


def bot_url(token: str, method: str = "") -> str:
    base = (settings.telegram_api_base or "https://api.telegram.org").rstrip("/")
    return f"{base}/bot{token}" + (f"/{method}" if method else "")


def client_options() -> dict:
    proxy = (settings.telegram_proxy or "").strip()
    return {"proxy": proxy} if proxy else {}
