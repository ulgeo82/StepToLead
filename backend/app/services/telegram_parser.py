import asyncio
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from telethon import functions
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.types import User, UserStatusLastMonth, UserStatusLastWeek, UserStatusOffline, UserStatusOnline, UserStatusRecently

from app.core.crypto import decrypt_secret
from app.db import SessionLocal
from app.models import TelegramAccount
from app.models.automation import CampaignAccount, CampaignRuntime
from app.models.telegram_parser import TelegramParsedContact, TelegramParseLog, TelegramParseTask
from app.services.telegram import make_client


def normalize_source(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^https?://", "", value, flags=re.I)
    value = re.sub(r"^(?:www\.)?(?:t\.me|telegram\.me)/", "", value, flags=re.I)
    value = value.split("?", 1)[0].strip("/")
    if value.startswith("+") or value.startswith("joinchat/"):
        return "https://t.me/" + value
    return value.lstrip("@")


def source_link(value: str) -> str:
    normalized = normalize_source(value)
    return normalized if normalized.startswith("https://") else f"https://t.me/{normalized}"


def user_activity(user: User) -> tuple[str, datetime | None]:
    status = getattr(user, "status", None)
    if isinstance(status, UserStatusOnline):
        return "online", None
    if isinstance(status, UserStatusRecently):
        return "recently", None
    if isinstance(status, UserStatusLastWeek):
        return "last_week", None
    if isinstance(status, UserStatusLastMonth):
        return "last_month", None
    if isinstance(status, UserStatusOffline):
        return "offline", status.was_online
    return "unknown", None


def activity_allowed(user: User, mode: str, max_offline_days: int | None, now: datetime | None = None) -> bool:
    activity, last_seen = user_activity(user)
    if mode == "online" and activity != "online":
        return False
    if mode == "recent" and activity not in {"online", "recently", "last_week", "offline"}:
        return False
    if max_offline_days is not None:
        if activity in {"online", "recently"}:
            return True
        if activity == "last_week":
            return max_offline_days >= 7
        if activity == "last_month":
            return max_offline_days >= 30
        if last_seen:
            current = now or datetime.now(timezone.utc)
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            return last_seen >= current - timedelta(days=max_offline_days)
        return False
    return True


async def add_log(db, task_id: int, message: str, level: str = "info") -> None:
    db.add(TelegramParseLog(task_id=task_id, level=level, message=message[:2000]))
    await db.commit()


async def task_cancelled(db, task_id: int) -> bool:
    return await db.scalar(select(TelegramParseTask.status).where(TelegramParseTask.id == task_id)) == "cancel_requested"


async def run_parse_task(task_id: int) -> None:
    async with SessionLocal() as db:
        task = await db.get(TelegramParseTask, task_id)
        if not task:
            return
        account = await db.scalar(
            select(TelegramAccount).options(selectinload(TelegramAccount.proxy)).where(TelegramAccount.id == task.account_id)
        )
        if not account or not account.session_encrypted:
            task.status, task.error, task.finished_at = "failed", "Telegram-аккаунт не подключён", datetime.now(timezone.utc)
            await db.commit()
            return
        busy_campaign = await db.scalar(
            select(CampaignAccount.campaign_id).join(CampaignRuntime, CampaignRuntime.campaign_id == CampaignAccount.campaign_id)
            .where(CampaignAccount.account_id == account.id, CampaignRuntime.status == "running")
        )
        if busy_campaign:
            task.status, task.error, task.finished_at = "failed", f"Аккаунт занят запущенной кампанией #{busy_campaign}", datetime.now(timezone.utc)
            await db.commit()
            return

        client = make_client(decrypt_secret(account.session_encrypted), account.proxy)
        seen: dict[int, TelegramParsedContact] = {}
        warnings = 0

        async def save_user(user, source: str, source_name: str, found_via: str, message_text: str | None = None) -> bool:
            nonlocal warnings
            if not isinstance(user, User) or user.bot or user.deleted:
                return False
            clean_message = (message_text or "").strip()
            if user.id in seen:
                contact = seen[user.id]
                if clean_message:
                    details = dict(contact.details or {})
                    messages = list(details.get("messages") or [])
                    if clean_message not in messages and len(messages) < 30:
                        messages.append(clean_message[:2000])
                    details["messages"] = messages
                    details["message_count"] = int(details.get("message_count") or 0) + 1
                    contact.details = details
                return len(seen) >= task.total_limit
            if not activity_allowed(user, task.activity_filter, task.max_offline_days):
                return False
            bio = None
            personal_channel = None
            if task.extended_profile:
                try:
                    full = await client(functions.users.GetFullUserRequest(user))
                    bio = full.full_user.about
                    channel_id = getattr(full.full_user, "personal_channel_id", None)
                    if channel_id:
                        try:
                            channel = await client.get_entity(channel_id)
                            personal_channel = f"https://t.me/{channel.username}" if getattr(channel, "username", None) else f"ID {channel_id}"
                        except (RPCError, ValueError):
                            personal_channel = f"ID {channel_id}"
                    await asyncio.sleep(0.12)
                except (RPCError, ValueError):
                    warnings += 1
            activity, last_seen = user_activity(user)
            contact = TelegramParsedContact(
                task_id=task.id, telegram_user_id=user.id, username=user.username,
                first_name=user.first_name, last_name=user.last_name,
                phone=user.phone or None, bio=bio, is_premium=bool(user.premium),
                activity=activity, last_seen_at=last_seen, source=source_link(source),
                source_name=source_name[:255], found_via=found_via,
                details={
                    "lang_code": user.lang_code,
                    "gender": "Не определён",
                    "personal_channel": personal_channel,
                    "messages": [clean_message[:2000]] if clean_message else [],
                    "message_count": 1 if clean_message else 0,
                },
            )
            db.add(contact)
            seen[user.id] = contact
            task.collected_count = len(seen)
            if len(seen) % 50 == 0:
                await db.commit()
            return len(seen) >= task.total_limit

        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError("Сессия Telegram больше не авторизована. Переподключите аккаунт.")
            await add_log(db, task.id, f"Аккаунт подключён. Источников: {len(task.sources)}. Общий лимит: {task.total_limit}.")
            stop = False
            for raw_source in task.sources:
                if stop or await task_cancelled(db, task.id):
                    break
                source = normalize_source(raw_source)
                try:
                    entity = await client.get_entity(source)
                    source_name = getattr(entity, "title", None) or getattr(entity, "username", None) or source
                    await add_log(db, task.id, f"Источник «{source_name}»: начинаю сбор.")

                    if task.collect_messages:
                        count = 0
                        async for message in client.iter_messages(entity, limit=task.per_source_limit):
                            sender = message.sender
                            if sender is None and message.sender_id:
                                try:
                                    sender = await message.get_sender()
                                except RPCError:
                                    sender = None
                            count += int(isinstance(sender, User))
                            if await save_user(sender, raw_source, str(source_name), "message", message.message):
                                stop = True
                                break
                        await add_log(db, task.id, f"Источник «{source_name}»: просмотрены авторы последних сообщений ({count}).")

                    if task.collect_members and not stop:
                        count = 0
                        try:
                            async for user in client.iter_participants(entity, limit=task.per_source_limit):
                                count += 1
                                if await save_user(user, raw_source, str(source_name), "member"):
                                    stop = True
                                    break
                            await add_log(db, task.id, f"Источник «{source_name}»: просмотрены участники ({count}).")
                        except RPCError as exc:
                            warnings += 1
                            await add_log(db, task.id, f"Источник «{source_name}»: Telegram не разрешил получить список участников ({type(exc).__name__}).", "warning")

                    if task.collect_comments and not stop:
                        replies_seen = 0
                        try:
                            async for post in client.iter_messages(entity, limit=min(task.per_source_limit, 100)):
                                if not post.replies or not post.replies.replies:
                                    continue
                                async for reply in client.iter_messages(entity, reply_to=post.id, limit=min(task.per_source_limit, 500)):
                                    replies_seen += 1
                                    sender = reply.sender or (await reply.get_sender() if reply.sender_id else None)
                                    if await save_user(sender, raw_source, str(source_name), "comment", reply.message):
                                        stop = True
                                        break
                                if stop or replies_seen >= task.per_source_limit:
                                    break
                            await add_log(db, task.id, f"Источник «{source_name}»: просмотрены комментарии ({replies_seen}).")
                        except RPCError as exc:
                            warnings += 1
                            await add_log(db, task.id, f"Источник «{source_name}»: комментарии недоступны ({type(exc).__name__}).", "warning")

                    task.processed_sources += 1
                    await db.commit()
                except FloodWaitError as exc:
                    warnings += 1
                    await add_log(db, task.id, f"Telegram установил паузу {exc.seconds} сек. Задача остановлена с сохранением результата.", "warning")
                    task.error = f"FloodWait: повторите позже, ожидание {exc.seconds} сек."
                    break
                except (RPCError, ValueError) as exc:
                    warnings += 1
                    await add_log(db, task.id, f"Не удалось обработать «{raw_source}»: {type(exc).__name__}: {str(exc)[:300]}", "warning")
                    task.processed_sources += 1
                    await db.commit()

            cancelled = await task_cancelled(db, task.id)
            task.status = "cancelled" if cancelled else ("partial" if task.error or warnings else "completed")
            task.finished_at = datetime.now(timezone.utc)
            await db.commit()
            await add_log(db, task.id, f"Сбор завершён. Уникальных контактов: {task.collected_count}. Обработано источников: {task.processed_sources}/{len(task.sources)}.", "success" if task.collected_count else "warning")
        except Exception as exc:
            await db.rollback()
            task = await db.get(TelegramParseTask, task_id)
            task.status, task.error, task.finished_at = "failed", f"{type(exc).__name__}: {str(exc)[:500]}", datetime.now(timezone.utc)
            await db.commit()
            await add_log(db, task.id, f"Задача остановлена: {task.error}", "error")
        finally:
            await client.disconnect()


async def run_parser_worker() -> None:
    async with SessionLocal() as db:
        rows = (await db.scalars(select(TelegramParseTask).where(TelegramParseTask.status == "running"))).all()
        for row in rows:
            row.status = "queued"
        await db.commit()
    while True:
        try:
            task_id = None
            async with SessionLocal() as db:
                task = await db.scalar(select(TelegramParseTask).where(TelegramParseTask.status == "queued").order_by(TelegramParseTask.id).limit(1).with_for_update(skip_locked=True))
                if task:
                    task.status, task.started_at, task.error = "running", datetime.now(timezone.utc), None
                    task_id = task.id
                    await db.commit()
            if task_id:
                await run_parse_task(task_id)
            else:
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(3)
