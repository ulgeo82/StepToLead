"""Здоровье Telegram-аккаунтов аутрича: балл 0–100, уровень и причины по-русски (docs/TELEGRAM_INFRA.md, раздел 4).
Плюс проверка спам-блока через @SpamBot."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

DEAD_STATUSES = {"error", "disconnected"}
SPAMBOT = "SpamBot"


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


async def message_stats(db, account_ids: list[int], now: datetime) -> dict[int, dict]:
    """Отправлено сегодня (первые сообщения), за сутки, ответов за 7 дней, ошибок за сутки — одним проходом."""
    from app.models.automation import CampaignMessage
    if not account_ids:
        return {}
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    out = {aid: {"sent_today": 0, "sent_24h": 0, "replies_7d": 0, "errors_24h": 0} for aid in account_ids}
    rows = (await db.execute(select(CampaignMessage.account_id, CampaignMessage.kind, CampaignMessage.status,
                                    CampaignMessage.created_at)
                             .where(CampaignMessage.account_id.in_(account_ids),
                                    CampaignMessage.created_at >= now - timedelta(days=7)))).all()
    for aid, kind, status, created in rows:
        created = _aware(created)
        s = out[aid]
        if status == "received":
            s["replies_7d"] += 1
            continue
        if created >= now - timedelta(hours=24):
            if status == "sent":
                s["sent_24h"] += 1
            elif status in ("failed", "unknown"):
                s["errors_24h"] += 1
        if kind == "first" and status == "sent" and created >= day:
            s["sent_today"] += 1
    return out


def compute(account, proxy_level: str | None, stats: dict | None, now: datetime) -> dict:
    stats = stats or {}
    health = account.health or {}
    spam = health.get("spamblock") or {"state": "unknown", "until": None, "checked_at": None}
    profile = {"photo": bool(health.get("has_photo") if "has_photo" in health else (account.avatar_version or 0) > 0),
               "username": bool(account.username), "bio": bool(account.bio)}
    created = _aware(account.created_at)
    age = (now - created).days if created else None
    reasons: list[str] = []
    score = 100
    dead = False
    if not account.session_encrypted or account.status in DEAD_STATUSES:
        reasons.append("Сессия не работает — нужен повторный вход" if not account.session_encrypted
                       else (account.last_error or "Аккаунт отключён"))
        dead = True
    if account.proxy_id is None:
        reasons.append("Нет прокси — аккаунт не подключается")
        dead = True
    elif proxy_level == "dead":
        reasons.append("Прокси не работает")
        dead = True
    elif proxy_level == "warn":
        reasons.append("С прокси есть проблемы")
        score -= 10
    until = spam.get("until")
    if spam.get("state") == "limited":
        reasons.append("Спам-блок" + (f" до {until[:10]}" if until else ""))
        score -= 50
    flood = _aware(account.flood_wait_until)
    if flood and flood > now:
        reasons.append("Пауза Telegram за частые действия")
        score -= 25
    if stats.get("errors_24h", 0) >= 3:
        reasons.append(f"Ошибок отправки за сутки: {stats['errors_24h']}")
        score -= 20
    missing = [name for name, ok in (("фото", profile["photo"]), ("username", profile["username"])) if not ok]
    if missing:
        reasons.append("В профиле нет: " + ", ".join(missing))
        score -= 10
    if age is not None and age < 7:
        reasons.append("Новый аккаунт — меньше 7 дней в работе")
        score -= 5
    if dead:
        score = 0
    level = "dead" if dead else "warn" if reasons else "ok"
    return {"score": max(0, min(100, score)), "level": level, "reasons": reasons, "spamblock": spam,
            "sent_today": stats.get("sent_today", 0), "sent_24h": stats.get("sent_24h", 0),
            "replies_7d": stats.get("replies_7d", 0), "errors_24h": stats.get("errors_24h", 0),
            "age_days": age, "profile": profile}


# ---------------------------------------------------------------- @SpamBot

_UNTIL = re.compile(r"(?:until|до)\s+(\d{1,2}\s+\w+\s+\d{4}(?:,?\s+\d{1,2}:\d{2})?)", re.I)
_FREE = ("no limits", "good news", "свободен от каких-либо ограничений", "нет ограничений", "ограничений нет")
_LIMITED = ("limited", "ограничен", "restricted", "can't send messages", "не можете отправлять", "к сожалению")


def parse_spambot(text: str) -> dict:
    """Ответ @SpamBot → {"state": "none"|"limited"|"unknown", "until": str|None}."""
    low = (text or "").lower()
    if any(x in low for x in _FREE):
        return {"state": "none", "until": None}
    if any(x in low for x in _LIMITED):
        m = _UNTIL.search(text or "")
        return {"state": "limited", "until": m.group(1) if m else None}
    return {"state": "unknown", "until": None}


async def ask_spambot(client, wait: float = 15.0, sleep=asyncio.sleep) -> dict:
    """Написать @SpamBot /start и дождаться ответа (клиент уже подключён через прокси аккаунта)."""
    sent = await client.send_message(SPAMBOT, "/start")
    waited = 0.0
    while waited < wait:
        await sleep(1.5)
        waited += 1.5
        for message in await client.get_messages(SPAMBOT, limit=3):
            if message.id > sent.id and not message.out:
                return parse_spambot(message.message or "")
    return {"state": "unknown", "until": None}


def apply_spamblock(account, result: dict, now: datetime) -> None:
    health = dict(account.health or {})
    health["spamblock"] = {**result, "checked_at": now.isoformat()}
    account.health = health
    if result["state"] == "limited":
        account.status = "spamblock"
    elif account.status == "spamblock":
        account.status = "online"
