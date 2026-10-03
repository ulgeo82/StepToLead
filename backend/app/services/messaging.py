"""Omnichannel messaging: polling adapters (Avito Messenger, Telegram bot, WhatsApp via GREEN-API),
message ingest into conversations, links to contacts/deals/requests and outgoing replies.

No public webhook is required: everything is polled (Telegram getUpdates, GREEN-API
ReceiveNotification queue, Avito chat list by `updated`). Sending happens synchronously
from the cabinet so the manager sees delivery errors immediately.
"""
import asyncio
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import token_digest
from app.core.crypto import decrypt_secret
from app.models.crm import CrmContact, CrmDeal, CrmInbound
from app.models.marketing import AdConnection, LeadInboundSource, PortalProjectAccess, PortalUser, Project
from app.models.messaging import Conversation, Message, MessagingChannel
from app.services import avito
from app.services.notifications import direct as notify_direct, flush_telegram

logger = logging.getLogger("uvicorn.error.messaging")
KINDS = {"avito": "Авито", "telegram_bot": "Telegram", "whatsapp": "WhatsApp"}
POLL_SECONDS = 15
AVITO_POLL_SECONDS = 60


class ChannelError(ValueError):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def from_unix(value) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return now()


def norm_phone(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    return digits


def cfg(channel: MessagingChannel) -> dict:
    return dict(channel.config or {})


def set_cfg(channel: MessagingChannel, **values) -> None:
    channel.config = {**cfg(channel), **values}


def secret(channel: MessagingChannel) -> str:
    value = decrypt_secret(channel.secret_encrypted)
    if not value:
        raise ChannelError("Для канала не сохранён токен")
    return value


async def _json(method: str, url: str, **kwargs) -> dict | list | None:
    try:
        async with httpx.AsyncClient(timeout=kwargs.pop("timeout", 30)) as client:
            response = await client.request(method, url, **kwargs)
    except httpx.HTTPError:
        raise ChannelError("Сервис канала недоступен. Повторите позже.") from None
    try:
        data = response.json() if response.content else None
    except ValueError:
        data = None
    if response.status_code >= 400:
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("description") or data.get("message") or data.get("error") or "")[:200]
        raise ChannelError(f"Канал вернул ошибку {response.status_code}{': ' + detail if detail else ''}")
    return data


# --------------------------------------------------------------------------- adapters

class Incoming(dict):
    """chat_id, title, text, external_id, sent_at, direction, phone, meta, author"""


class TelegramBot:
    def __init__(self, channel: MessagingChannel):
        self.channel = channel
        self.base = f"https://api.telegram.org/bot{secret(channel)}"

    async def call(self, method: str, payload: dict | None = None):
        data = await _json("POST", f"{self.base}/{method}", json=payload or {})
        if not isinstance(data, dict) or not data.get("ok"):
            raise ChannelError(str((data or {}).get("description") or "Telegram отклонил запрос"))
        return data["result"]

    async def test(self) -> dict:
        from app.core.config import settings
        if settings.telegram_bot_token and secret(self.channel) == settings.telegram_bot_token:
            raise ChannelError("Это бот уведомлений сотрудников. Для клиентов создайте отдельного бота в @BotFather")
        me = await self.call("getMe")
        await self.call("deleteWebhook", {"drop_pending_updates": False})
        set_cfg(self.channel, bot_username=me.get("username"))
        return {"name": f"@{me.get('username')}"}

    async def poll(self) -> list[Incoming]:
        offset = cfg(self.channel).get("offset")
        updates = await self.call("getUpdates", {"timeout": 0, "allowed_updates": ["message"],
                                                 **({"offset": offset} if offset else {})})
        result = []
        for update_row in updates:
            set_cfg(self.channel, offset=int(update_row["update_id"]) + 1)
            message = update_row.get("message") or {}
            chat = message.get("chat") or {}
            if chat.get("type") != "private":
                continue
            sender = message.get("from") or {}
            name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])) or sender.get("username") or "Клиент Telegram"
            text = message.get("text") or message.get("caption")
            if text == "/start":
                text = "Клиент начал диалог с ботом"
            elif not text:
                kind = next((k for k in ("photo", "document", "voice", "video", "sticker", "contact", "location") if message.get(k)), "сообщение")
                text = {"photo": "📷 Фото", "document": "📎 Файл", "voice": "🎤 Голосовое сообщение", "video": "🎬 Видео",
                        "sticker": "Стикер", "contact": "Контакт", "location": "📍 Геопозиция"}.get(kind, "Сообщение")
            phone = (message.get("contact") or {}).get("phone_number")
            result.append(Incoming(chat_id=str(chat["id"]), title=name[:180], text=text, external_id=f"m{message.get('message_id')}",
                                   sent_at=from_unix(message.get("date")), direction="in", phone=phone,
                                   meta={"username": sender.get("username"), "telegram_id": sender.get("id")}))
        return result

    async def send(self, conversation: Conversation, text: str) -> str:
        sent = await self.call("sendMessage", {"chat_id": conversation.external_chat_id, "text": text})
        return f"m{sent.get('message_id')}"


class WhatsAppGreen:
    """WhatsApp through GREEN-API (green-api.com): instance id + API token, polling queue."""

    def __init__(self, channel: MessagingChannel):
        self.channel = channel
        data = cfg(channel)
        self.api = str(data.get("api_url") or "https://api.green-api.com").rstrip("/")
        self.instance = str(data.get("instance_id") or "")
        if not self.instance.isdigit():
            raise ChannelError("Укажите idInstance из личного кабинета GREEN-API")
        self.token = secret(channel)

    def url(self, method: str, suffix: str = "") -> str:
        return f"{self.api}/waInstance{self.instance}/{method}/{self.token}{suffix}"

    async def test(self) -> dict:
        state = await _json("GET", self.url("getStateInstance"))
        status = (state or {}).get("stateInstance")
        if status != "authorized":
            raise ChannelError(f"Номер WhatsApp не авторизован в GREEN-API (статус: {status}). Отсканируйте QR-код в кабинете GREEN-API")
        return {"name": "WhatsApp подключён"}

    async def poll(self) -> list[Incoming]:
        result = []
        for _ in range(50):
            item = await _json("GET", self.url("receiveNotification", "?receiveTimeout=5"), timeout=20)
            if not item:
                break
            body = item.get("body") or {}
            try:
                parsed = self.parse(body)
                if parsed:
                    result.append(parsed)
            finally:
                await _json("DELETE", self.url("deleteNotification", f"/{item.get('receiptId')}"))
        return result

    @staticmethod
    def parse(body: dict) -> Incoming | None:
        kind = body.get("typeWebhook")
        if kind not in {"incomingMessageReceived", "outgoingMessageReceived"}:
            return None  # API-sent messages are stored when we send them
        sender = body.get("senderData") or {}
        chat_id = str(sender.get("chatId") or "")
        if not chat_id.endswith("@c.us"):
            return None  # groups and broadcasts are not client chats
        data = body.get("messageData") or {}
        text = ((data.get("textMessageData") or {}).get("textMessage") or (data.get("extendedTextMessageData") or {}).get("text")
                or (data.get("fileMessageData") or {}).get("caption"))
        if not text:
            text = {"imageMessage": "📷 Фото", "documentMessage": "📎 Файл", "audioMessage": "🎤 Голосовое сообщение",
                    "videoMessage": "🎬 Видео", "locationMessage": "📍 Геопозиция", "contactMessage": "Контакт"}.get(
                data.get("typeMessage"), "Сообщение")
        phone = chat_id.split("@")[0]
        name = sender.get("senderContactName") or sender.get("senderName") or sender.get("chatName") or f"+{phone}"
        return Incoming(chat_id=chat_id, title=str(name)[:180], text=str(text), external_id=str(body.get("idMessage")),
                        sent_at=from_unix(body.get("timestamp")), phone=f"+{phone}",
                        direction="in" if kind == "incomingMessageReceived" else "out", meta={})

    async def send(self, conversation: Conversation, text: str) -> str:
        sent = await _json("POST", self.url("sendMessage"), json={"chatId": conversation.external_chat_id, "message": text})
        return str((sent or {}).get("idMessage") or f"wa-{secrets.token_hex(6)}")


class AvitoMessenger:
    """Avito chats of an «Авито · Объявления» cabinet; credentials come from the ad connection."""

    def __init__(self, channel: MessagingChannel, db: AsyncSession):
        self.channel, self.db = channel, db

    async def connection(self) -> AdConnection:
        row = await self.db.get(AdConnection, cfg(self.channel).get("connection_id"))
        if not row or row.platform != avito.ITEMS or row.project_id != self.channel.project_id:
            raise ChannelError("Кабинет Авито не найден в проекте")
        if row.status != "connected":
            raise ChannelError("Сначала проверьте подключение кабинета Авито в разделе «Реклама»")
        return row

    async def client(self):
        return await avito.client_for(self.db, await self.connection())

    async def test(self) -> dict:
        row = await self.connection()
        client = await self.client()
        await client.get(f"/messenger/v2/accounts/{row.external_account_id}/chats", params={"limit": 1, "chat_types": "u2i"})
        return {"name": row.name}

    async def poll(self) -> list[Incoming]:
        row = await self.connection()
        client = await self.client()
        uid = str(row.external_account_id)
        since = int(cfg(self.channel).get("since") or (now() - timedelta(hours=1)).timestamp())
        newest = since
        result = []
        for page in range(5):
            data = await client.get(f"/messenger/v2/accounts/{uid}/chats", params={"chat_types": "u2i", "limit": 100, "offset": page * 100})
            chats = data.get("chats") or []
            for chat in chats:
                updated = int(chat.get("updated") or 0)
                if updated <= since:
                    continue
                context = (chat.get("context") or {}).get("value") or {}
                if context.get("user_id") is not None and str(context.get("user_id")) != uid:
                    continue
                buyer = next((u for u in chat.get("users") or [] if str(u.get("id")) != uid), {})
                messages = await client.get(f"/messenger/v3/accounts/{uid}/chats/{chat['id']}/messages/", params={"limit": 30, "offset": 0})
                items = messages.get("messages") or messages.get("items") or []
                for message in items:
                    created = int(message.get("created") or 0)
                    if created <= since:
                        continue
                    content = message.get("content") or {}
                    text = content.get("text") or ("📷 Фото" if content.get("image") else "📞 Звонок через Авито" if content.get("call")
                                                    else "Сообщение")
                    result.append(Incoming(chat_id=str(chat["id"]), title=(buyer.get("name") or "Покупатель Авито")[:180], text=str(text),
                                           external_id=str(message.get("id")), sent_at=from_unix(created),
                                           direction="in" if message.get("direction") == "in" else "out",
                                           meta={"item_id": context.get("id"), "item_title": context.get("title"),
                                                 "item_url": context.get("url"),
                                                 "profile_url": (buyer.get("public_user_profile") or {}).get("url")}))
                newest = max(newest, updated)
            if len(chats) < 100 or all(int(c.get("updated") or 0) <= since for c in chats):
                break
        set_cfg(self.channel, since=newest)
        return result

    async def send(self, conversation: Conversation, text: str) -> str:
        row = await self.connection()
        client = await self.client()
        uid = row.external_account_id
        sent = await client.post(f"/messenger/v1/accounts/{uid}/chats/{conversation.external_chat_id}/messages",
                                 {"message": {"text": text[:1000]}, "type": "text"})
        return str(sent.get("id") or f"av-{secrets.token_hex(6)}")

    async def mark_read(self, conversation: Conversation) -> None:
        row = await self.connection()
        client = await self.client()
        await client.post(f"/messenger/v1/accounts/{row.external_account_id}/chats/{conversation.external_chat_id}/read", {})


def adapter(channel: MessagingChannel, db: AsyncSession):
    if channel.kind == "telegram_bot":
        return TelegramBot(channel)
    if channel.kind == "whatsapp":
        return WhatsAppGreen(channel)
    if channel.kind == "avito":
        return AvitoMessenger(channel, db)
    raise ChannelError("Неизвестный тип канала")


# --------------------------------------------------------------------------- ingest

async def project_people(db: AsyncSession, project_id: int) -> dict[int, PortalUser]:
    project = await db.get(Project, project_id)
    rows = (await db.scalars(select(PortalUser).where(
        PortalUser.workspace_id == project.workspace_id, PortalUser.active.is_(True),
        or_(PortalUser.role == "client_owner", PortalUser.id.in_(
            select(PortalProjectAccess.user_id).where(PortalProjectAccess.project_id == project_id)))))).all()
    return {u.id: u for u in rows}


async def ensure_source(db: AsyncSession, channel: MessagingChannel) -> LeadInboundSource:
    source = await db.get(LeadInboundSource, channel.inbound_source_id) if channel.inbound_source_id else None
    if source:
        return source
    token = secrets.token_urlsafe(48)
    source = LeadInboundSource(workspace_id=channel.workspace_id, project_id=channel.project_id,
                               name=f"{KINDS.get(channel.kind, channel.kind)}: {channel.name}"[:180],
                               token_hash=token_digest(token), token_prefix=token[:12], active=True, auto_assign=True)
    db.add(source); await db.flush()
    channel.inbound_source_id = source.id
    return source


async def _link_existing(db: AsyncSession, channel: MessagingChannel, conversation: Conversation) -> None:
    """Attach a new conversation to a known client: Avito request by chat id, or a contact by phone."""
    if channel.kind == "avito":
        inbound = await db.scalar(select(CrmInbound).where(CrmInbound.project_id == channel.project_id,
                                                           CrmInbound.external_id == f"chat:{conversation.external_chat_id}"))
        if inbound:
            conversation.inbound_id = inbound.id
            conversation.contact_id = inbound.contact_id
            conversation.deal_id = inbound.deal_id
    digits = norm_phone(conversation.phone)
    if conversation.contact_id is None and digits:
        contact = await db.scalar(select(CrmContact).where(CrmContact.project_id == channel.project_id,
                                                           CrmContact.phone_normalized == digits).limit(1))
        if contact:
            conversation.contact_id = contact.id
    if conversation.contact_id and conversation.deal_id is None:
        deal = await db.scalar(select(CrmDeal).where(CrmDeal.contact_id == conversation.contact_id,
                                                     CrmDeal.archived_at.is_(None), CrmDeal.closed_at.is_(None))
                               .order_by(CrmDeal.created_at.desc()).limit(1))
        if deal:
            conversation.deal_id = deal.id
    if conversation.deal_id and conversation.assigned_user_id is None:
        deal = await db.get(CrmDeal, conversation.deal_id)
        conversation.assigned_user_id = deal.responsible_user_id if deal else None


async def _new_lead(db: AsyncSession, channel: MessagingChannel, conversation: Conversation, first_text: str) -> None:
    """A first message from an unknown person becomes a request in «Неразобранное» (Avito has its own importer)."""
    if channel.kind == "avito" or conversation.contact_id or conversation.inbound_id:
        return
    from app.api.routes.portal import InboundLead
    from app.services.inbound_lead import create_inbound
    source = await ensure_source(db, channel)
    username = (conversation.meta or {}).get("username")
    contact = conversation.phone or (f"@{username}" if username else f"{KINDS[channel.kind]} · {conversation.title}")
    title = conversation.title if len(conversation.title) >= 2 else f"Клиент {KINDS[channel.kind]}"
    payload = InboundLead.model_validate({
        "external_id": f"conv:{conversation.id}", "full_name": title[:180], "phone": conversation.phone,
        "source": channel.kind, "external_source": channel.kind, "contact_method": KINDS[channel.kind],
        "contact": contact, "notes": first_text[:3000], "conversation_id": conversation.id, "contact_consent": False})
    result = await create_inbound(db, source, payload, commit=False, allow_raw_contact=True)
    conversation.inbound_id = result.get("inbound_id")
    if result.get("deal_id"):  # trusted channel: already a deal
        deal = await db.get(CrmDeal, result["deal_id"])
        conversation.deal_id, conversation.contact_id = deal.id, deal.contact_id
        conversation.assigned_user_id = conversation.assigned_user_id or deal.responsible_user_id


async def ingest(db: AsyncSession, channel: MessagingChannel, item: Incoming) -> Message | None:
    """Store one message. Returns the Message when it is new. Does not commit."""
    conversation = await db.scalar(select(Conversation).where(Conversation.channel_id == channel.id,
                                                              Conversation.external_chat_id == item["chat_id"]))
    created = conversation is None
    if created:
        conversation = Conversation(workspace_id=channel.workspace_id, project_id=channel.project_id, channel_id=channel.id,
                                    external_chat_id=item["chat_id"], title=item.get("title") or "Клиент",
                                    phone=item.get("phone"), meta=item.get("meta") or {}, status="open", unread_count=0)
        db.add(conversation); await db.flush()
        await _link_existing(db, channel, conversation)
    elif item.get("meta"):
        conversation.meta = {**(conversation.meta or {}), **{k: v for k, v in item["meta"].items() if v}}
    if item.get("phone") and not conversation.phone:
        conversation.phone = item["phone"]
    if item.get("external_id") and await db.scalar(select(Message.id).where(
            Message.conversation_id == conversation.id, Message.external_id == item["external_id"])):
        return None
    sent_at = aware(item.get("sent_at")) or now()
    message = Message(conversation_id=conversation.id, direction=item.get("direction", "in"), text=item.get("text"),
                      external_id=item.get("external_id"), author_name=conversation.title if item.get("direction", "in") == "in" else None,
                      status="received" if item.get("direction", "in") == "in" else "sent", sent_at=sent_at, attachments=[])
    db.add(message)
    was_waiting = conversation.waiting_since is not None
    if conversation.last_message_at is None or sent_at >= aware(conversation.last_message_at):
        conversation.last_message_at = sent_at
        conversation.last_message_preview = (item.get("text") or "")[:300]
        conversation.last_direction = message.direction
    if message.direction == "in":
        conversation.unread_count = (conversation.unread_count or 0) + 1
        conversation.waiting_since = conversation.waiting_since or sent_at
        conversation.status = "open"
        if created:
            await _new_lead(db, channel, conversation, item.get("text") or "")
        if not was_waiting and not created:
            people = await project_people(db, conversation.project_id)
            targets = [conversation.assigned_user_id] if conversation.assigned_user_id in people else \
                [uid for uid, u in people.items() if u.role in {"client_owner", "sales_head"}]
            notify_direct(db, conversation.workspace_id, targets, f"Новое сообщение · {KINDS.get(channel.kind)}",
                          f"{conversation.title}: {(item.get('text') or '')[:200]}", people)
    else:
        conversation.waiting_since = None
    await db.flush()
    return message


async def link_inbound(db: AsyncSession, inbound: CrmInbound, contact_id: int, deal: CrmDeal) -> None:
    """Called when a request is accepted: its conversations follow the new contact and deal."""
    conditions = [Conversation.inbound_id == inbound.id]
    external = str(inbound.external_id or "")
    if external.startswith("chat:"):
        conditions.append(Conversation.external_chat_id == external[5:])
    await db.execute(update(Conversation).where(Conversation.project_id == inbound.project_id, or_(*conditions))
                     .values(contact_id=contact_id, deal_id=deal.id, inbound_id=inbound.id,
                             assigned_user_id=deal.responsible_user_id))


# --------------------------------------------------------------------------- sending

async def send(db: AsyncSession, conversation: Conversation, text: str, user: PortalUser | None, *, is_ai: bool = False) -> Message:
    """Send a reply through the channel and store it. Commits. Raises ChannelError on delivery failure."""
    text = text.strip()
    if not text:
        raise ChannelError("Пустое сообщение")
    channel = await db.get(MessagingChannel, conversation.channel_id)
    if not channel or not channel.active:
        raise ChannelError("Канал выключен")
    limit = 1000 if channel.kind == "avito" else 4096
    if len(text) > limit:
        raise ChannelError(f"Сообщение длиннее {limit} символов — разбейте его на части")
    message = Message(conversation_id=conversation.id, direction="out", text=text, author_user_id=user.id if user else None,
                      author_name=user.display_name if user else ("ИИ-ассистент" if is_ai else "Автоматически"), status="sent", sent_at=now(), is_ai=is_ai,
                      attachments=[])
    try:
        message.external_id = await adapter(channel, db).send(conversation, text)
    except (ChannelError, avito.AvitoError) as exc:
        message.status, message.error = "failed", str(exc)[:500]
    db.add(message)
    if message.status == "sent":
        conversation.last_message_at = message.sent_at
        conversation.last_message_preview = text[:300]
        conversation.last_direction = "out"
        conversation.waiting_since = None
        conversation.unread_count = 0
        if conversation.assigned_user_id is None and user is not None:
            conversation.assigned_user_id = user.id
        if conversation.deal_id and user is not None:
            from app.api.routes.crm import activity
            deal = await db.get(CrmDeal, conversation.deal_id)
            if deal:
                activity(db, deal, user, "MESSAGE_SENT", {"text": text[:500], "channel": KINDS.get(channel.kind)})
    await db.commit()
    if message.status == "failed":
        raise ChannelError(message.error or "Не удалось отправить сообщение")
    return message


# --------------------------------------------------------------------------- worker

async def poll_channel(db: AsyncSession, channel: MessagingChannel) -> int:
    """Fetch and store new messages of one channel. Commits. Returns the number of new messages."""
    count = 0
    try:
        for item in await adapter(channel, db).poll():
            if await ingest(db, channel, item):
                count += 1
        channel.status, channel.last_error = "connected", None
    except (ChannelError, avito.AvitoError) as exc:
        await db.rollback()
        channel = await db.get(MessagingChannel, channel.id, populate_existing=True)
        channel.status, channel.last_error = "error", str(exc)[:500]
    channel.last_polled_at = now()
    await db.commit()
    flush_telegram(db)
    return count


async def run_worker() -> None:
    from app.db import SessionLocal
    await asyncio.sleep(25)
    while True:
        try:
            async with SessionLocal() as db:
                channels = (await db.scalars(select(MessagingChannel).where(MessagingChannel.active.is_(True)))).all()
                ids = [(c.id, c.kind, aware(c.last_polled_at)) for c in channels]
            for channel_id, kind, polled in ids:
                if kind == "avito" and polled and polled > now() - timedelta(seconds=AVITO_POLL_SECONDS):
                    continue
                try:
                    async with SessionLocal() as db:
                        channel = await db.get(MessagingChannel, channel_id)
                        if channel and channel.active:
                            await poll_channel(db, channel)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("messaging poll failed channel=%s", channel_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("messaging worker iteration failed")
        await asyncio.sleep(POLL_SECONDS)
