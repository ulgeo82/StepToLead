"""Failure alerts for the agency: what broke in the portal, sent to the admins' Telegram.

Every 5 minutes: ad cabinets in error or not refreshed for half a day, chats (WhatsApp / Telegram / Avito) in error
or not polled, telephony in error, background workers that died, a burst of server errors, the recordings disk
filling up. A problem is reported once when it appears, again every 24 hours while it lasts, and «✅ восстановлено»
when it is gone. A dead server cannot report itself: keep an external uptime check on https://<domain>/api/health.
"""
import asyncio
import html
import logging
import shutil
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.access import AdminUser
from app.models.marketing import AdConnection, ClientWorkspace, Project
from app.models.messaging import MessagingChannel
from app.models.system import AppSetting
from app.models.telephony import TelephonyConnection

logger = logging.getLogger("uvicorn.error.monitor")
STATE_KEY = "monitor_state"
REPEAT_HOURS = 24
ERROR_BURST = 5           # server errors within ERROR_WINDOW minutes
ERROR_WINDOW = 15
STALE_SYNC_HOURS = 12
STALE_POLL_MINUTES = 30
DISK_FREE_MIN = 0.10
API_PLATFORMS = {"yandex": "Яндекс Директ", "vk_ads": "VK Реклама", "meta": "Meta Ads",
                 "avito_items": "Авито", "avito_ads": "Авито Реклама"}
CHANNELS = {"whatsapp": "WhatsApp", "telegram_bot": "Telegram-бот", "avito": "Чаты Авито"}

_errors: deque = deque(maxlen=200)
_tasks: dict[str, asyncio.Task] = {}


class ErrorCollector(logging.Handler):
    """Remembers ERROR records of the app (uvicorn.error and its children) for the burst check."""
    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR and not record.name.endswith(".monitor"):
            try:
                text = record.getMessage().splitlines()[0][:200]
            except Exception:
                text = record.msg if isinstance(record.msg, str) else "ошибка"
            _errors.append((time.time(), record.name, text))


def install() -> None:
    root = logging.getLogger("uvicorn.error")
    if not any(isinstance(h, ErrorCollector) for h in root.handlers):
        root.addHandler(ErrorCollector())


def register(name: str, task: asyncio.Task) -> None:
    _tasks[name] = task


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


async def problems(db: AsyncSession) -> list[dict]:
    names = {w.id: w.name for w in (await db.scalars(select(ClientWorkspace).where(ClientWorkspace.status != "deleted"))).all()}
    active = {p.id for p in (await db.scalars(select(Project).where(Project.status == "active"))).all()}
    found: list[dict] = []

    def add(key, title, detail="", workspace_id=None):
        found.append({"key": key, "title": title, "detail": (detail or "")[:400], "client": names.get(workspace_id)})

    for a in (await db.scalars(select(AdConnection).where(AdConnection.platform.in_(list(API_PLATFORMS)),
                                                          AdConnection.status != "disconnected"))).all():
        if a.project_id not in active or a.workspace_id not in names:
            continue
        label = f"{API_PLATFORMS[a.platform]} «{a.name}»"
        if a.status == "error":
            add(f"ad:{a.id}", f"Ошибка рекламного кабинета: {label}", a.last_error, a.workspace_id)
        elif a.status == "connected" and aware(a.last_synced_at) and aware(a.last_synced_at) < now() - timedelta(hours=STALE_SYNC_HOURS):
            add(f"adstale:{a.id}", f"Кабинет не обновлялся больше {STALE_SYNC_HOURS} ч: {label}", "", a.workspace_id)
    for c in (await db.scalars(select(MessagingChannel).where(MessagingChannel.active.is_(True)))).all():
        if c.project_id not in active or c.workspace_id not in names:
            continue
        label = f"{CHANNELS.get(c.kind, c.kind)} «{c.name}»"
        if c.status == "error":
            add(f"chan:{c.id}", f"Канал переписки не работает: {label}", c.last_error, c.workspace_id)
        elif c.status == "connected" and aware(c.last_polled_at) and aware(c.last_polled_at) < now() - timedelta(minutes=STALE_POLL_MINUTES):
            add(f"chanstale:{c.id}", f"Сообщения не забирались больше {STALE_POLL_MINUTES} мин: {label}", "", c.workspace_id)
    for t in (await db.scalars(select(TelephonyConnection).where(TelephonyConnection.active.is_(True)))).all():
        if t.project_id in active and t.workspace_id in names and t.status == "error":
            add(f"tel:{t.id}", f"Телефония не работает: «{t.name}»", t.last_error, t.workspace_id)
    for name, task in _tasks.items():
        if task.done():
            reason = "остановлена"
            if not task.cancelled() and task.exception():
                reason = str(task.exception())[:200]
            add(f"worker:{name}", f"Фоновая задача «{name}» не работает — нужен перезапуск бэкенда", reason)
    recent = [e for e in _errors if e[0] > time.time() - ERROR_WINDOW * 60]
    if len(recent) >= ERROR_BURST:
        sample = "\n".join(f"{e[1]}: {e[2]}" for e in recent[-3:])
        add("errors", f"Много ошибок сервера: {len(recent)} за {ERROR_WINDOW} мин", sample)
    try:
        usage = shutil.disk_usage(settings.recordings_dir if __import__("os").path.isdir(settings.recordings_dir) else "/")
        if usage.free / usage.total < DISK_FREE_MIN:
            add("disk", f"Заканчивается место на диске: свободно {usage.free / 1e9:.1f} ГБ из {usage.total / 1e9:.0f}")
    except OSError:
        pass
    return found


def _line(p: dict) -> str:
    head = f"<b>{html.escape(p['title'])}</b>" + (f"\nКлиент: {html.escape(p['client'])}" if p.get("client") else "")
    return head + (f"\n<i>{html.escape(p['detail'])}</i>" if p.get("detail") else "")


async def recipients(db: AsyncSession) -> list[str]:
    return [a.telegram_chat_id for a in (await db.scalars(select(AdminUser).where(AdminUser.telegram_chat_id.is_not(None)))).all()]


async def send(db: AsyncSession, text: str) -> int:
    from app.services.notifications import telegram_api, telegram_configured
    if not telegram_configured():
        return 0
    sent = 0
    for chat in await recipients(db):
        try:
            await telegram_api("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
            sent += 1
        except Exception as exc:
            logger.warning("monitor alert failed type=%s", type(exc).__name__)
    return sent


async def run_once(db: AsyncSession) -> dict:
    """Compare with the last state, alert on new / long / resolved problems. Commits."""
    current = {p["key"]: p for p in await problems(db)}
    row = await db.get(AppSetting, STATE_KEY)
    state = dict(row.value) if row else {}
    t = now()
    fresh, repeat, resolved = [], [], []
    for key, p in current.items():
        seen = state.get(key)
        if not seen:
            fresh.append(p); state[key] = {"since": t.isoformat(), "notified": t.isoformat(), "title": p["title"], "client": p.get("client")}
        elif datetime.fromisoformat(seen["notified"]) < t - timedelta(hours=REPEAT_HOURS):
            repeat.append(p); seen["notified"] = t.isoformat()
    for key in [k for k in state if k not in current]:
        resolved.append(state.pop(key))
    messages = []
    if fresh:
        messages.append("⚠️ <b>StepToLead: сбой</b>\n\n" + "\n\n".join(_line(p) for p in fresh))
    if repeat:
        messages.append("⏳ <b>Всё ещё не работает</b>\n\n" + "\n\n".join(_line(p) for p in repeat))
    if resolved:
        messages.append("✅ <b>Восстановлено</b>\n" + "\n".join(f"• {html.escape(r['title'])}" + (f" ({html.escape(r['client'])})" if r.get("client") else "") for r in resolved))
    for text in messages:
        await send(db, text)
    if row:
        row.value = state
    else:
        db.add(AppSetting(key=STATE_KEY, value=state))
    await db.commit()
    return {"problems": list(current.values()), "new": len(fresh), "resolved": len(resolved)}


async def run_worker() -> None:
    from app.db import SessionLocal
    await asyncio.sleep(120)
    while True:
        try:
            async with SessionLocal() as db:
                await run_once(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("monitor pass failed", exc_info=True)
        await asyncio.sleep(300)
