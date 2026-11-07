"""Площадки радара в базе: вкл/выкл, глобальные настройки (секреты шифруются), статус, контекст прохода."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret, encrypt_secret
from app.domains.jobradar.models import JrSource
from app.domains.jobradar.sources import REGISTRY, Adapter, Context
from app.models.system import AppSetting

logger = logging.getLogger("uvicorn.error.jobradar")
LEGACY_KEY = "jobradar"            # там лежал токен hh до площадок
transport = None                   # подмена сети в тестах (httpx.MockTransport)
sleep = None                       # подмена пауз в тестах


def _secrets(adapter: Adapter) -> set[str]:
    return {s["key"] for s in adapter.settings_schema if s.get("type") == "secret"}


async def ensure(db: AsyncSession) -> dict[str, JrSource]:
    """Строки площадок для всех адаптеров реестра. Новая площадка появляется сама, с настройками по умолчанию."""
    rows = {r.code: r for r in (await db.scalars(select(JrSource))).all()}
    legacy = await db.get(AppSetting, LEGACY_KEY)
    for code, adapter in REGISTRY.items():
        if code in rows:
            continue
        settings = adapter.defaults()
        if code == "hh" and legacy and isinstance(legacy.value, dict) and legacy.value.get("hh_token_encrypted"):
            settings["token_encrypted"] = legacy.value["hh_token_encrypted"]   # токен из старых настроек радара
        row = JrSource(code=code, enabled=adapter.default_enabled, settings=settings)
        try:
            async with db.begin_nested():   # гонка с параллельным запросом или воркером — не падаем
                db.add(row)
            rows[code] = row
        except IntegrityError:
            rows[code] = await db.scalar(select(JrSource).where(JrSource.code == code))
    return rows


def plain(adapter: Adapter, stored: dict | None) -> dict:
    """Настройки для адаптера: секреты расшифрованы, пропущенные — по умолчанию."""
    stored = dict(stored or {})
    out = {**adapter.defaults(), **{k: v for k, v in stored.items() if not k.endswith("_encrypted")}}
    for key in _secrets(adapter):
        value = stored.get(f"{key}_encrypted")
        out[key] = decrypt_secret(value) if value else None
    return out


def public(adapter: Adapter, stored: dict | None) -> dict:
    """Настройки для экрана: секреты не отдаём, только «задан / не задан»."""
    stored = dict(stored or {})
    out = {**adapter.defaults(), **{k: v for k, v in stored.items() if not k.endswith("_encrypted")}}
    for key in _secrets(adapter):
        out[key] = {"set": bool(stored.get(f"{key}_encrypted"))}
    return out


def _coerce(spec: dict, value):
    kind = spec.get("type")
    if kind == "bool":
        return bool(value)
    if kind in ("number", "proxy"):
        if value in (None, ""):
            return None if kind == "proxy" else 0
        number = int(value)
        if number < 0:
            raise ValueError(f"{spec['label']}: не меньше 0")
        return number
    if kind == "list":
        items = value if isinstance(value, list) else str(value or "").replace(";", ",").replace("\n", ",").split(",")
        return [str(i).strip()[:120] for i in items if str(i).strip()][:200]
    return str(value or "").strip()[:500]


def apply(adapter: Adapter, row: JrSource, data: dict) -> None:
    """Записывает настройки с экрана. Секрет: строка — задать, "" — убрать. Неизвестные ключи — ошибка."""
    specs = {s["key"]: s for s in adapter.settings_schema}
    stored = dict(row.settings or {})
    for key, value in data.items():
        spec = specs.get(key)
        if spec is None:
            raise ValueError(f"У площадки «{adapter.title}» нет настройки «{key}»")
        if spec.get("type") == "secret":
            text = str(value or "").strip()
            if text:
                stored[f"{key}_encrypted"] = encrypt_secret(text)
            else:
                stored.pop(f"{key}_encrypted", None)
            continue
        stored[key] = _coerce(spec, value)
    row.settings = stored


def state(adapter: Adapter, row: JrSource) -> tuple[str, str | None]:
    """ok / needs_key / error / off / unknown + короткое сообщение."""
    if not row.enabled:
        return "off", "выключена"
    reason = adapter.ready(plain(adapter, row.settings))
    if reason:
        return "needs_key", reason
    last_ok = row.last_ok_at.replace(tzinfo=timezone.utc) if row.last_ok_at and row.last_ok_at.tzinfo is None \
        else row.last_ok_at
    last_err = row.last_error_at.replace(tzinfo=timezone.utc) if row.last_error_at and row.last_error_at.tzinfo is None \
        else row.last_error_at
    if row.last_error and (not last_ok or (last_err and last_err >= last_ok)):
        return "error", row.last_error[:80]
    return ("ok", None) if last_ok else ("unknown", "ещё не проверялась")


def mark(row: JrSource, moment: datetime, error: str | None) -> None:
    if error:
        row.last_error, row.last_error_at = error[:200], moment
    else:
        row.last_ok_at, row.last_error = moment, None


async def proxy_url(db: AsyncSession, proxy_id) -> str | None:
    """Прокси из пула портала -> socks5://… для чтения t.me."""
    if not proxy_id or not str(proxy_id).isdigit():
        return None
    from app.models.proxy import Proxy
    proxy = await db.get(Proxy, int(proxy_id))
    if proxy is None:
        return None
    from urllib.parse import quote
    auth = ""
    if proxy.username:
        password = decrypt_secret(proxy.password_encrypted) if proxy.password_encrypted else ""
        auth = f"{quote(proxy.username, safe='')}:{quote(password or '', safe='')}@"
    scheme = "http" if proxy.scheme == "http" else "socks5"
    return f"{scheme}://{auth}{proxy.host}:{proxy.port}"


async def context(db: AsyncSession, *, first_run_days: int = 3, workspace_id: int | None = None,
                  ai_extract=None) -> Context:
    rows = await ensure(db)
    proxy = None
    tg = rows.get("telegram")
    if tg is not None and transport is None:
        stored = tg.settings or {}
        proxy = await proxy_url(db, stored.get("proxy_id") or stored.get("proxy"))   # ключ настройки — proxy_id / proxy
    ctx = Context(transport=transport, proxy=proxy, first_run_days=first_run_days, ai_extract=ai_extract)
    if sleep is not None:
        ctx.sleep = sleep
    return ctx
