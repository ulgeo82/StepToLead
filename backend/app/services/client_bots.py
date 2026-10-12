"""Свой Telegram-бот у клиента: токен от @BotFather вводится в портале, дальше всё как у бота агентства —
уведомления о заявках, SLA, еженедельный отчёт, меню «Заявки / Задачи / Отчёт».

Чей бот используется для человека — записано в PortalUser.telegram_state["bot_id"] при подключении:
пока сотрудник не нажал «Подключить Telegram» в новом боте, уведомления идут через бот агентства.
"""
import asyncio
import hashlib
import logging
import re
from contextlib import suppress

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret, encrypt_secret
from app.models.system import WorkspaceBot
from app.services.telegram_http import BotScope, bot_scope, bot_url, client_options

logger = logging.getLogger("uvicorn.error.client_bots")
TOKEN_RE = re.compile(r"^\d{5,16}:[A-Za-z0-9_-]{30,64}$")


class BotTokenError(ValueError):
    pass


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def scope_of(row: WorkspaceBot | None) -> BotScope | None:
    if not row or not row.active:
        return None
    try:
        token = decrypt_secret(row.token_encrypted)
    except ValueError:
        return None
    return BotScope(token=token, workspace_id=row.workspace_id, bot_row_id=row.id, username=row.username) if token else None


async def bot_for(db: AsyncSession, workspace_id: int) -> WorkspaceBot | None:
    return await db.scalar(select(WorkspaceBot).where(WorkspaceBot.workspace_id == workspace_id))


async def scope_for_user(db: AsyncSession, user) -> BotScope | None:
    """Бот, к которому сотрудник подключён. None — бот агентства."""
    bot_id = (getattr(user, "telegram_state", None) or {}).get("bot_id")
    if not bot_id:
        return None
    row = await db.get(WorkspaceBot, int(bot_id))
    return scope_of(row) if row and row.workspace_id == user.workspace_id else None


async def send_to_user(db: AsyncSession, user, method: str, payload: dict):
    from app.services.notifications import telegram_api
    reset = bot_scope.set(await scope_for_user(db, user))
    try:
        return await telegram_api(method, payload)
    finally:
        bot_scope.reset(reset)


async def _call(token: str, method: str, payload: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15, **client_options()) as client:
            data = (await client.post(bot_url(token, method), json=payload or {})).json()
    except (httpx.HTTPError, ValueError):
        raise BotTokenError("Telegram сейчас недоступен, попробуйте ещё раз через минуту") from None
    if not data.get("ok"):
        if data.get("error_code") in (401, 404):
            raise BotTokenError("Telegram не принял токен: скопируйте его из @BotFather целиком")
        raise BotTokenError(f"Telegram ответил ошибкой: {data.get('description') or data.get('error_code')}")
    return data.get("result")


async def connect(db: AsyncSession, workspace_id: int, token: str, company_name: str) -> WorkspaceBot:
    from app.core.config import settings
    from app.models.messaging import MessagingChannel
    token = (token or "").strip()
    if not TOKEN_RE.match(token):
        raise BotTokenError("Это не похоже на токен бота. Он выглядит так: 1234567890:AAH…")
    if settings.telegram_bot_token and token == settings.telegram_bot_token:
        raise BotTokenError("Это токен бота агентства — для клиента нужен отдельный бот")
    digest = token_hash(token)
    other = await db.scalar(select(WorkspaceBot).where(WorkspaceBot.token_hash == digest))
    if other and other.workspace_id != workspace_id:
        raise BotTokenError("Этот бот уже подключён к другой компании")
    from app.services.messaging import secret
    for channel in (await db.scalars(select(MessagingChannel).where(MessagingChannel.kind == "telegram_bot"))).all():
        try:
            same = secret(channel) == token
        except Exception:
            same = False
        if same:
            raise BotTokenError("Этот бот уже используется как канал переписки с клиентами. Для уведомлений создайте отдельного бота")
    me = await _call(token, "getMe")
    # Бот для уведомлений: webhook не нужен (опрос), меню команд — как у бота агентства.
    await _call(token, "deleteWebhook", {"drop_pending_updates": True})
    from app.services.tg_bot import COMMANDS
    with suppress(BotTokenError):
        await _call(token, "setMyCommands", {"commands": COMMANDS})
        await _call(token, "setMyDescription", {"description": f"Уведомления и отчёты {company_name}: новые заявки, задачи, результат рекламы."[:512]})
        await _call(token, "setMyShortDescription", {"short_description": f"Заявки и отчёты {company_name}"[:120]})
    row = await bot_for(db, workspace_id)
    if row is None:
        row = WorkspaceBot(workspace_id=workspace_id)
        db.add(row)
    row.token_encrypted, row.token_hash = encrypt_secret(token), digest
    row.bot_id, row.username, row.title = str(me["id"]), me.get("username") or "", (me.get("first_name") or "")[:128]
    row.active, row.last_error = True, None
    await db.commit()
    supervisor_wakeup()
    return row


async def disconnect(db: AsyncSession, workspace_id: int) -> None:
    from app.models.marketing import PortalUser
    row = await bot_for(db, workspace_id)
    if not row:
        return
    for user in (await db.scalars(select(PortalUser).where(PortalUser.workspace_id == workspace_id))).all():
        state = dict(user.telegram_state or {})
        if state.get("bot_id") == row.id:
            # Подключение к удалённому боту больше не работает: человек подключит Telegram заново.
            state.pop("bot_id", None)
            user.telegram_state = state
            user.telegram_chat_id = user.telegram_username = None
    await db.delete(row)
    await db.commit()
    supervisor_wakeup()


async def invite_existing(db: AsyncSession, row: WorkspaceBot) -> int:
    """Сотрудникам, подключённым к боту агентства, — сообщение со ссылкой на новый бот компании (ссылка живёт 7 дней)."""
    import secrets
    from datetime import datetime, timedelta, timezone
    from app.core.access import token_digest
    from app.models.marketing import PortalUser
    from app.services.notifications import telegram_api, telegram_configured
    if not telegram_configured() or not row.username:
        return 0
    sent = 0
    users = (await db.scalars(select(PortalUser).where(PortalUser.workspace_id == row.workspace_id,
                                                        PortalUser.active.is_(True),
                                                        PortalUser.telegram_chat_id.is_not(None)))).all()
    for user in users:
        if (user.telegram_state or {}).get("bot_id"):
            continue
        code = secrets.token_urlsafe(24)
        user.telegram_link_code_hash = token_digest(code)
        user.telegram_link_expires_at = datetime.now(timezone.utc) + timedelta(days=7)
        await db.commit()
        with suppress(Exception):
            await telegram_api("sendMessage", {"chat_id": user.telegram_chat_id,
                "text": f"У вашей компании теперь свой бот для заявок и отчётов: @{row.username}.\n"
                        "Нажмите кнопку ниже — уведомления будут приходить туда.",
                "reply_markup": {"inline_keyboard": [[{"text": "Перейти в бот компании",
                                                       "url": f"https://t.me/{row.username}?start={code}"}]]}})
            sent += 1
    return sent


def public(row: WorkspaceBot | None) -> dict:
    if not row:
        return {"connected": False}
    return {"connected": True, "username": row.username, "title": row.title, "active": row.active,
            "last_error": row.last_error, "url": f"https://t.me/{row.username}" if row.username else None}


# ---------------------------------------------------------------- опрос ботов клиентов

_wake = asyncio.Event()


def supervisor_wakeup() -> None:
    with suppress(RuntimeError):
        _wake.set()


async def run_worker():
    """Один цикл опроса на каждого активного бота клиента; список сверяется раз в минуту или сразу после изменений."""
    from app.db import SessionLocal
    from app.services import tg_bot
    tasks: dict[int, tuple[str, asyncio.Task]] = {}
    while True:
        try:
            async with SessionLocal() as db:
                rows = (await db.scalars(select(WorkspaceBot).where(WorkspaceBot.active.is_(True)))).all()
            wanted = {row.id: scope_of(row) for row in rows}
            for row_id, (token, task) in list(tasks.items()):
                scope = wanted.get(row_id)
                if scope is None or scope.token != token or task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task
                    tasks.pop(row_id)
            for row_id, scope in wanted.items():
                if scope and row_id not in tasks:
                    tasks[row_id] = (scope.token, asyncio.create_task(_poll_scoped(scope, tg_bot)))
        except asyncio.CancelledError:
            for _, task in tasks.values():
                task.cancel()
            raise
        except Exception:
            logger.exception("client bots supervisor failed")
        _wake.clear()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_wake.wait(), timeout=60)


async def _poll_scoped(scope: BotScope, tg_bot):
    bot_scope.set(scope)  # задача получила свою копию контекста: все вызовы Telegram в ней идут через бот клиента
    await tg_bot.poll()
