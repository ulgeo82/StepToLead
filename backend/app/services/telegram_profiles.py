"""Customer Telegram sessions: login and one Redis-fenced persistent connection owner."""
import asyncio
import json
import re
import secrets
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import func, select
from telethon import events, functions, types
from telethon.errors import FloodWaitError, PeerFloodError, SessionPasswordNeededError

from app.core.config import settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.db import SessionLocal
from app.models.crm import CrmContact
from app.models.messaging import MessagingChannel
from app.services import client_proxies
from app.services.telegram import make_client

LEASE = "steptolead:telegram-profiles:lease"
RENEW = "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('expire',KEYS[1],60) else return 0 end"
RELEASE = "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"
clients = {}


def client_for(channel, proxy):
    if proxy is None or proxy.status == "error":
        raise ValueError("Нет связи через прокси")
    client = make_client(decrypt_secret(channel.secret_encrypted), proxy, catch_up=True)
    state = (channel.config or {}).get("update_state")
    if state:
        client.session.set_update_state(0, types.updates.State(
            pts=state["pts"], qts=state["qts"], seq=state["seq"],
            date=datetime.fromisoformat(state["date"]), unread_count=state["unread_count"]))
    return client


async def login(db, channel, *, code=None, password=None):
    from app.services.messaging import ChannelError
    if (channel.config or {}).get("telegram_user_id"):
        async with Redis.from_url(settings.redis_url) as redis:
            if await redis.exists(f"steptolead:telegram-profiles:live:{channel.id}"):
                raise ChannelError("Выключите канал и подождите отключения профиля перед новым входом")
    proxy = await client_proxies.allocate(db, channel)
    if proxy is None:
        return
    config = dict(channel.config or {})
    client = client_for(channel, proxy)
    try:
        await client.connect()
        if code is None and password is None:
            sent = await client.send_code_request(config["phone"])
            config["phone_code_hash"] = sent.phone_code_hash
            channel.status = "code_required"
        else:
            try:
                if password:
                    await client.sign_in(password=password)
                else:
                    await client.sign_in(config["phone"], code, phone_code_hash=config.get("phone_code_hash"))
            except SessionPasswordNeededError:
                channel.status = "password_required"
            else:
                me = await client.get_me()
                config.update(telegram_user_id=me.id, username=me.username)
                config.pop("phone_code_hash", None)
                channel.status = "connected"
        channel.secret_encrypted = encrypt_secret(client.session.save())
        channel.config, channel.last_error = config, None
    except Exception as error:
        if isinstance(error, (FloodWaitError, PeerFloodError)):
            raise ChannelError(error_text(error)) from None
        raise ChannelError("Telegram не принял код или пароль. Запросите код заново.") from None
    finally:
        await client.disconnect()


def error_text(error):
    if isinstance(error, ValueError):
        text = str(error)
        return text if re.search("[а-яё]", text, re.I) else "Не удалось найти этот чат в Telegram-профиле"
    if isinstance(error, FloodWaitError):
        until = datetime.now(timezone.utc) + timedelta(seconds=error.seconds)
        return f"Telegram ограничил отправку до {until.astimezone(ZoneInfo('Europe/Samara')):%H:%M}"
    if isinstance(error, PeerFloodError):
        return "Telegram временно запретил писать новым людям с этого аккаунта (спамблок)"
    if type(error).__name__ in {"AuthKeyUnregisteredError", "SessionRevokedError"}:
        return "Сессия завершена — подключите профиль заново"
    if type(error).__name__ in {"UserIsBlockedError", "InputUserDeactivatedError", "UserPrivacyRestrictedError",
                                "PeerIdInvalidError", "ChatWriteForbiddenError"}:
        return "Telegram не даёт написать этому человеку (заблокировал, удалил аккаунт или ограничил сообщения)"
    if isinstance(error, (ConnectionError, OSError, asyncio.TimeoutError)):
        return "Нет связи через прокси"
    return "Telegram вернул ошибку — попробуйте ещё раз"


async def accept_event(db, channel, event):
    from app.services import messaging
    sender = await event.get_chat() if event.out else await event.get_sender()
    if not event.is_private or not isinstance(sender, types.User) or sender.bot or sender.is_self:
        return False
    if sender.id == 777000:
        return False
    phone = messaging.norm_phone(sender.phone)
    username = (sender.username or "").lower().lstrip("@")
    # Look up only the matching contact: projects may hold thousands of contacts.
    scope = select(CrmContact).where(CrmContact.workspace_id == channel.workspace_id,
                                     CrmContact.project_id == channel.project_id)
    contact = await db.scalar(scope.where(CrmContact.phone_normalized == phone).limit(1)) if phone else None
    if contact is None and username:
        candidates = (await db.scalars(scope.where(func.lower(CrmContact.telegram).in_(
            [username, f"@{username}", f"https://t.me/{username}", f"t.me/{username}"])).limit(1))).all()
        contact = candidates[0] if candidates else None
    if (channel.config or {}).get("mode", "corporate") == "personal" and contact is None:
        return False
    title = " ".join(filter(None, [sender.first_name, sender.last_name])) or sender.username or "Клиент"
    item = messaging.Incoming(chat_id=str(event.chat_id), title=title, text=event.raw_text or "Сообщение",
                              external_id=f"m{event.id}", direction="out" if event.out else "in",
                              sent_at=event.date, phone=f"+{phone}" if phone else None,
                              meta={"username": sender.username, "telegram_id": sender.id})
    if contact:
        # The existing linking service handles phone; username-only contacts are linked before lead creation.
        from app.models.messaging import Conversation
        conversation = await db.scalar(select(Conversation).where(
            Conversation.channel_id == channel.id, Conversation.external_chat_id == item["chat_id"]))
        if conversation is None:
            conversation = Conversation(workspace_id=channel.workspace_id, project_id=channel.project_id,
                                        channel_id=channel.id, external_chat_id=item["chat_id"], title=title,
                                        contact_id=contact.id, unread_count=0, meta=item["meta"])
            db.add(conversation)
            await db.flush()
            await messaging._link_existing(db, channel, conversation)
    return await messaging.ingest(db, channel, item)


class TelegramProfile:
    def __init__(self, channel):
        self.channel = channel

    async def rpc(self, operation, **values):
        from app.services.messaging import ChannelError
        token = secrets.token_hex(16)
        async with Redis.from_url(settings.redis_url) as redis:
            if not await redis.get(LEASE):
                raise ChannelError("Менеджер Telegram-профилей недоступен")
            await redis.rpush("steptolead:telegram-profiles:commands", json.dumps({
                "channel_id": self.channel.id, "operation": operation, "token": token, **values}))
            result = await redis.blpop(f"steptolead:telegram-profiles:reply:{token}", timeout=10)
            if not result:
                raise ChannelError("Telegram не ответил за 10 секунд")
            payload = json.loads(result[1])
            if "error" in payload:
                raise ChannelError(payload["error"])
            return payload["result"]

    async def test(self):
        if self.channel.status != "connected":
            from app.services.messaging import ChannelError
            raise ChannelError("Подключите Telegram-профиль")
        return {"name": self.channel.name}

    async def send(self, conversation, text):
        return await self.rpc("send", chat_id=conversation.external_chat_id, text=text)

    async def poll(self):
        # Reception belongs exclusively to the leased persistent manager.
        await self.test()
        return []

    async def lookup(self, *, phone=None, username=None, email=None):
        return await self.rpc("lookup", phone=phone, username=username)

    async def history(self, external_chat_id, limit=50):
        result = await self.rpc("history", chat_id=external_chat_id, limit=min(limit, 50))
        for item in result:
            item["sent_at"] = datetime.fromisoformat(item["sent_at"])
        return result


async def execute(client, command):
    operation = command["operation"]
    if operation == "send":
        chat_id = int(command["chat_id"])
        try:
            sent = await client.send_message(chat_id, command["text"])
        except ValueError:
            # StringSession keeps no entity cache across restarts: load dialogs once, then resolve the chat again.
            await client.get_dialogs(limit=500)
            sent = await client.send_message(chat_id, command["text"])
        return f"m{sent.id}"
    if operation == "lookup":
        if command.get("username"):
            try:
                result = await client(functions.contacts.ResolveUsernameRequest(command["username"].lstrip("@")))
            except Exception as error:
                if type(error).__name__ in {"UsernameInvalidError", "UsernameNotOccupiedError"}:
                    return None
                raise
            user = result.users[0] if result.users else None
        elif command.get("phone"):
            result = await client(functions.contacts.ImportContactsRequest([
                types.InputPhoneContact(client_id=0, phone=command["phone"], first_name="Клиент", last_name="")]))
            user = result.users[0] if result.users else None
        else:
            return None
        if user is None or user.bot or user.is_self:
            return None
        history = await client.get_messages(user, limit=1)
        return {"external_chat_id": str(user.id), "title": user.first_name or user.username or "Клиент",
                "has_history": bool(history)}
    try:
        messages = await client.get_messages(int(command["chat_id"]), limit=command.get("limit", 50))
    except ValueError:
        await client.get_dialogs(limit=500)
        messages = await client.get_messages(int(command["chat_id"]), limit=command.get("limit", 50))
    return [{"external_id": f"m{m.id}", "direction": "out" if m.out else "in",
             "text": m.message or "Сообщение", "sent_at": m.date.isoformat()} for m in reversed(messages)]


async def run_leader(redis, owner, lost):
    tasks = {}
    locks = {}
    signatures = {}

    async def heartbeat():
        try:
            while not lost.is_set():
                await asyncio.sleep(10)
                if not await redis.eval(RENEW, 1, LEASE, owner):
                    break
        finally:
            lost.set()
            for task in tasks.values():
                task.cancel()

    heartbeat_task = asyncio.create_task(heartbeat())

    async def connect(channel_id):
        delay = 2
        while not lost.is_set():
            client = None
            try:
                async with SessionLocal() as db:
                    channel = await db.get(MessagingChannel, channel_id)
                    proxy = await client_proxies.allocate(db, channel)
                    client = client_for(channel, proxy)
                async def handler(event):
                    if lost.is_set() or await redis.get(LEASE) not in (owner, owner.encode()):
                        return
                    async with locks.setdefault(channel_id, asyncio.Lock()):
                        if event.out and await redis.exists(
                                f"steptolead:telegram-profiles:sent:{channel_id}:m{event.id}"):
                            return
                    # API sends and Telegram echoes may race; the database unique key is authoritative.
                    from sqlalchemy.exc import IntegrityError
                    for attempt in range(2):
                        try:
                            async with SessionLocal() as db:
                                channel = await db.get(MessagingChannel, channel_id)
                                if channel.active:
                                    await accept_event(db, channel, event)
                                    channel.last_polled_at = datetime.now(timezone.utc)
                                    await db.commit()
                            break
                        except IntegrityError:
                            if attempt:
                                raise
                            await asyncio.sleep(0.1)
                client.add_event_handler(handler, events.NewMessage())
                await client.connect()
                if not await client.get_me():
                    raise ValueError("Сессия завершена — подключите профиль заново")
                clients[channel_id] = client
                await redis.set(f"steptolead:telegram-profiles:live:{channel_id}", owner, ex=30)
                async with SessionLocal() as db:
                    channel = await db.get(MessagingChannel, channel_id)
                    channel.status, channel.last_error = "connected", None
                    channel.last_polled_at = datetime.now(timezone.utc)
                    await db.commit()
                await client.catch_up()
                delay = 2
                await client.disconnected
            except asyncio.CancelledError:
                raise
            except Exception as error:
                async with SessionLocal() as db:
                    channel = await db.get(MessagingChannel, channel_id)
                    channel.status, channel.last_error = "error", error_text(error)
                    await db.commit()
            finally:
                clients.pop(channel_id, None)
                await redis.eval(RELEASE, 1, f"steptolead:telegram-profiles:live:{channel_id}", owner)
                if client:
                    await client.disconnect()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

    try:
        while not lost.is_set():
            async with SessionLocal() as db:
                await client_proxies.check_due(db)
                channels = (await db.scalars(select(MessagingChannel).where(
                    MessagingChannel.kind == "telegram_user", MessagingChannel.active.is_(True)))).all()
                wanted = {c.id for c in channels if c.status in {"connected", "error"} and c.secret_encrypted
                          and c.last_error != "Сессия завершена — подключите профиль заново"
                          and c.id not in qr_logins}
                from app.models.proxy import Proxy
                proxies = {p.id: p for p in (await db.scalars(select(Proxy).where(Proxy.pool == "client"))).all()}
                current = {c.id: client_proxies.signature(proxies.get((c.config or {}).get("proxy_id")))
                           for c in channels}
                for channel in channels:
                    if channel.id in clients:
                        await redis.set(f"steptolead:telegram-profiles:live:{channel.id}", owner, ex=30)
                        channel.last_polled_at = datetime.now(timezone.utc)
                        state = clients[channel.id].session.get_update_state(0)
                        if state:
                            channel.config = {**(channel.config or {}), "update_state": {
                                "pts": state.pts, "qts": state.qts, "seq": state.seq,
                                "date": state.date.isoformat(), "unread_count": state.unread_count}}
                await db.commit()
            # The agency replaced a proxy in the portal: drop the old connection, the profile reconnects via the new IP.
            for channel_id in [cid for cid in tasks if cid in wanted and signatures.get(cid) != current.get(cid)]:
                task = tasks.pop(channel_id)
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            for channel_id in wanted - tasks.keys():
                signatures[channel_id] = current.get(channel_id)
                tasks[channel_id] = asyncio.create_task(connect(channel_id))
            for channel_id in tasks.keys() - wanted:
                task = tasks.pop(channel_id)
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            command = await redis.blpop("steptolead:telegram-profiles:commands", timeout=5)
            if not command:
                continue
            command = json.loads(command[1])
            try:
                if await redis.get(LEASE) not in (owner, owner.encode()):
                    lost.set()
                    break
                client = clients.get(command["channel_id"])
                if client is None:
                    raise ValueError("Профиль ещё не подключён")
                async with locks.setdefault(command["channel_id"], asyncio.Lock()):
                    result = {"result": await asyncio.wait_for(execute(client, command), 9)}
                    if command["operation"] == "send":
                        await redis.set(
                            f"steptolead:telegram-profiles:sent:{command['channel_id']}:{result['result']}",
                            "1", ex=86400)
            except Exception as error:
                result = {"error": error_text(error)}
                async with SessionLocal() as db:
                    from app.services.messaging import project_people
                    from app.services.notifications import direct, flush_telegram
                    channel = await db.get(MessagingChannel, command["channel_id"])
                    channel.last_error = result["error"]
                    if type(error).__name__ in {"AuthKeyUnregisteredError", "SessionRevokedError"}:
                        channel.status = "error"
                    people = await project_people(db, channel.project_id)
                    targets = [uid for uid, user in people.items() if user.role in {"client_owner", "sales_head"}]
                    direct(db, channel.workspace_id, targets, "Telegram-профиль", result["error"], people,
                           project_id=channel.project_id)
                    await db.commit()
                    flush_telegram(db)
            key = f"steptolead:telegram-profiles:reply:{command['token']}"
            await redis.rpush(key, json.dumps(result))
            await redis.expire(key, 30)
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


async def run_worker():
    while True:
        try:
            async with Redis.from_url(settings.redis_url) as redis:
                owner = secrets.token_hex(16)
                if await acquire(redis, owner):
                    try:
                        await run_leader(redis, owner, asyncio.Event())
                    finally:
                        await redis.eval(RELEASE, 1, LEASE, owner)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # Redis unavailable: fail closed, never start an unleased profile.
        await asyncio.sleep(5)


async def acquire(redis, owner):
    return bool(await redis.set(LEASE, owner, nx=True, ex=60))


# --------------------------------------------------------------------------- QR login
# The client scans a QR code in Telegram (Settings → Devices → Link Desktop Device): no SMS code, no phone number.
# A pending login keeps a live Telegram connection, so it lives in this backend process (the portal runs one
# backend process; the profile manager skips channels that are being logged in).
QR_TTL_SECONDS = 180
qr_logins: dict[int, dict] = {}


def _now():
    return datetime.now(timezone.utc)


def qr_svg(url: str) -> str:
    import io

    import segno
    buffer = io.BytesIO()
    segno.make(url, error="m").save(buffer, kind="svg", scale=6, border=2, dark="#101c31", xmldecl=False)
    return buffer.getvalue().decode()


def qr_state(channel_id: int) -> dict | None:
    state = qr_logins.get(channel_id)
    if state is None:
        return None
    result = {"status": state["status"], "error": state.get("error"),
              "deadline": state["deadline"].isoformat()}
    if state["status"] == "waiting" and state.get("url"):
        result.update(svg=qr_svg(state["url"]), expires_at=state["expires"].isoformat())
    return result


async def _close(channel_id: int, state: dict) -> None:
    if qr_logins.get(channel_id) is state:
        qr_logins.pop(channel_id, None)
    with suppress(Exception):
        await state["client"].disconnect()


async def cancel_qr(channel_id: int) -> None:
    state = qr_logins.get(channel_id)
    if state:
        task = state.get("task")
        if task and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        await _close(channel_id, state)


async def _finish(channel_id: int, state: dict) -> None:
    """Telegram accepted the login: store the encrypted session and hand the profile to the manager."""
    from app.services import messaging
    client = state["client"]
    me = await client.get_me()
    session = client.session.save()
    async with SessionLocal() as db:
        channel = await db.get(MessagingChannel, channel_id)
        others = (await db.scalars(select(MessagingChannel).where(
            MessagingChannel.workspace_id == channel.workspace_id, MessagingChannel.kind == "telegram_user",
            MessagingChannel.id != channel_id))).all()
        if any((other.config or {}).get("telegram_user_id") == me.id for other in others):
            state.update(status="error", error="Этот Telegram-аккаунт уже подключён в компании")
            channel.status, channel.last_error = "error", state["error"]
            await db.commit()
            return
        phone = messaging.norm_phone(me.phone)
        channel.config = {**(channel.config or {}), "telegram_user_id": me.id, "username": me.username,
                          "phone": f"+{phone}" if phone else None}
        channel.config.pop("update_state", None)
        channel.secret_encrypted = encrypt_secret(session)
        channel.status, channel.last_error, channel.active = "connected", None, True
        await db.commit()
    state.update(status="connected", error=None)


async def _wait(channel_id: int, state: dict) -> None:
    qr = state["qr"]
    try:
        while _now() < state["deadline"]:
            left = (state["deadline"] - _now()).total_seconds()
            try:
                await qr.wait(timeout=max(1.0, min(left, (qr.expires - _now()).total_seconds())))
            except asyncio.TimeoutError:
                if _now() >= state["deadline"]:
                    break
                await qr.recreate()  # Telegram tokens live ~30 s; the page shows the fresh code on its next poll.
                state.update(url=qr.url, expires=qr.expires)
                continue
            except SessionPasswordNeededError:
                state["status"] = "password_required"
                await asyncio.sleep(max(0.0, (state["deadline"] - _now()).total_seconds()))
                if state["status"] == "password_required":
                    state.update(status="expired", error="Время на ввод пароля истекло")
                return
            await _finish(channel_id, state)
            return
        state.update(status="expired", error="QR-код устарел — покажите новый")
    except asyncio.CancelledError:
        raise
    except Exception as error:
        state.update(status="error", error=error_text(error))
    finally:
        if state["status"] != "password_required":
            await _close(channel_id, state)


async def start_qr(db, channel) -> dict:
    from app.services.messaging import ChannelError
    await cancel_qr(channel.id)
    proxy = await client_proxies.allocate(db, channel)
    if proxy is None:
        raise ChannelError("Для Telegram-профилей компании не настроен прокси — обратитесь к агентству")
    if proxy.status == "error":
        raise ChannelError("Прокси компании не отвечает — агентство может заменить его в карточке клиента")
    client = make_client(None, proxy, catch_up=True)
    try:
        await asyncio.wait_for(client.connect(), 15)
        qr = await client.qr_login()
    except Exception as error:
        with suppress(Exception):
            await client.disconnect()
        raise ChannelError(error_text(error)) from None
    state = {"client": client, "qr": qr, "status": "waiting", "url": qr.url, "expires": qr.expires,
             "deadline": _now() + timedelta(seconds=QR_TTL_SECONDS), "error": None}
    qr_logins[channel.id] = state
    state["task"] = asyncio.create_task(_wait(channel.id, state))
    if channel.status != "connected":
        channel.status = "qr_pending"
    return qr_state(channel.id)


async def qr_password(channel_id: int, password: str) -> dict:
    from telethon.errors import PasswordHashInvalidError

    from app.services.messaging import ChannelError
    state = qr_logins.get(channel_id)
    if not state or state["status"] != "password_required":
        raise ChannelError("Пароль сейчас не нужен — покажите QR-код заново")
    try:
        await state["client"].sign_in(password=password)
    except PasswordHashInvalidError:
        raise ChannelError("Неверный облачный пароль Telegram") from None
    except Exception as error:
        raise ChannelError(error_text(error)) from None
    state["status"] = "finishing"
    try:
        await _finish(channel_id, state)
    finally:
        task = state.get("task")
        if task:
            task.cancel()
        await _close(channel_id, state)
    return {"status": state["status"], "error": state.get("error")}
