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


from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class BotScope:
    """Бот, через который идёт текущий запрос к Telegram: свой бот клиента или (None) бот агентства."""
    token: str
    workspace_id: int
    bot_row_id: int
    username: str


bot_scope: ContextVar[BotScope | None] = ContextVar("bot_scope", default=None)


def current_token() -> str:
    scope = bot_scope.get()
    return scope.token if scope else settings.telegram_bot_token
