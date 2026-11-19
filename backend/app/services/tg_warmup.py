"""Прогрев Telegram-аккаунтов: подготовка, безопасная фоновая активность и переписка «свои ↔ свои».

Проект и обоснование — docs/TELEGRAM_WARMUP.md. Коротко: Telegram ограничивает аккаунт прежде всего за жалобы
на холодные сообщения, поэтому прогрев (1) готовит аккаунт (скрыть номер, облачный пароль, завершить чужие
сессии), (2) каждый день даёт живую активность короткими сессиями днём по часовому поясу компании — заходы в
сеть, чтение каналов с просмотрами, реакции, переписку со своими аккаунтами из общего пула — и (3) раз в сутки
спрашивает @SpamBot. Лимит холодных первых касаний регулирует человек (лимит компании/аккаунта); прогрев
показывает рекомендуемый лимит по возрасту и не ограничивает его. Всё — только через прокси аккаунта и только
когда аккаунт не занят рассылкой (account_locks).
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import TelegramAccount
from app.models.marketing import ClientWorkspace
from app.models.system import AppSetting
from app.models.telegram_warmup import TelegramWarmupChat, TelegramWarmupEvent
from app.services import tg_space, tg_warmup_bank as bank

log = logging.getLogger(__name__)

STAGE_NAMES = {"prep": "Подготовка", "rest": "Отлёжка", "passive": "Каналы и чтение", "social": "Общение",
               "ready": "Готов"}
# Действий в день по этапам (мягкий режим — ×0.6). «Готов» — поддерживающий фон рядом с рассылкой.
BUDGET = {"prep": 2, "rest": 4, "passive": 10, "social": 16, "ready": 6}
RECOMMENDED = ((0, 0), (4, 2), (6, 3), (8, 5), (11, 8), (14, 12))   # день прогрева -> холодных в день (подсказка)
DEFAULT_CHANNELS = ["rbc_news", "kommersant", "forbesrussia", "tass_agency", "rian_ru", "habr_com", "vcnews", "banksta"]
WAKE_FROM, WAKE_TO = 9, 22       # «бодрствует» по часовому поясу компании, у каждого аккаунта сдвиг −2…+2 ч
CONCURRENCY = 3

session_factory = None           # подмена в тестах
spawn = asyncio.create_task


def sessions():
    from app.db import SessionLocal
    return session_factory or SessionLocal


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(value) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- настройки компании

def _key(ws: int | None) -> str:
    return f"telegram:warmup:{int(ws or tg_space.home() or 0)}"


async def company(db: AsyncSession, ws: int | None) -> dict:
    row = await db.get(AppSetting, _key(ws))
    value = dict(row.value) if row and isinstance(row.value, dict) else {}
    return {"intensity": value.get("intensity", "normal"), "channels": value.get("channels") or list(DEFAULT_CHANNELS),
            "pool": value.get("pool", True)}


async def save_company(db: AsyncSession, ws: int | None, value: dict) -> dict:
    current = await company(db, ws)
    current.update({k: v for k, v in value.items() if k in ("intensity", "channels", "pool") and v is not None})
    current["channels"] = [c.strip().lstrip("@").split("/")[-1] for c in current["channels"] if c and c.strip()][:30]
    row = await db.get(AppSetting, _key(ws))
    if row is None:
        db.add(AppSetting(key=_key(ws), value=current))
    else:
        row.value = current
    await db.flush()
    return current


# ---------------------------------------------------------------- этап и расписание

def state(account) -> dict:
    return dict(account.warmup or {})


def day_of(st: dict, now: datetime) -> int:
    start = _dt(st.get("started_at"))
    return max(0, (now - start).days) if start else 0


def stage_of(st: dict, now: datetime) -> str | None:
    if not st.get("enabled"):
        return None
    if not st.get("prepared_at"):
        return "prep"
    if st.get("warmed"):
        return "ready"
    d = day_of(st, now)
    return "rest" if d < 2 else "passive" if d < 5 else "social" if d < 14 else "ready"


def recommended(st: dict, now: datetime, company_limit: int) -> int:
    if st.get("warmed"):
        return company_limit
    d = day_of(st, now)
    if d >= 18:
        return company_limit
    value = 0
    for start, limit in RECOMMENDED:
        if d >= start:
            value = limit
    return min(value, company_limit)


def _shift(account_id: int) -> int:
    return (account_id * 7) % 5 - 2


def awake(when: datetime, tz: str, account_id: int, rnd: random.Random | None = None) -> datetime:
    """Перенести момент на утро, если он попал на ночь аккаунта."""
    rnd = rnd or random
    local = when.astimezone(ZoneInfo(tz or "Europe/Moscow"))
    start, end = WAKE_FROM + _shift(account_id), WAKE_TO + _shift(account_id)
    if start <= local.hour < end:
        return when
    morning = local.replace(hour=start, minute=0, second=0, microsecond=0)
    if local.hour >= end:
        morning += timedelta(days=1)
    return morning.astimezone(timezone.utc) + timedelta(minutes=rnd.randint(0, 75))


def next_session(now: datetime, stage: str, intensity: str, tz: str, account_id: int,
                 rnd: random.Random | None = None) -> datetime:
    """Следующая сессия: бюджет дня делится на сессии по 1–4 действия, разнесённые по дню; ночью — ничего."""
    rnd = rnd or random
    budget = BUDGET.get(stage, 4) * (0.6 if intensity == "soft" else 1.0)
    sessions_per_day = max(2, math.ceil(budget / 2.5))
    gap = (WAKE_TO - WAKE_FROM) * 60 / sessions_per_day
    when = now + timedelta(minutes=gap * rnd.uniform(0.5, 1.5))
    return awake(when, tz, account_id, rnd)


def view(account, now: datetime, company_limit: int) -> dict:
    st = state(account)
    stage = stage_of(st, now)
    today = st.get("today") or {}
    return {"enabled": bool(st.get("enabled")), "paused": bool(st.get("paused")), "stage": stage,
            "stage_name": STAGE_NAMES.get(stage or "", "Выключен"), "day": day_of(st, now) if stage else None,
            "warmed": bool(st.get("warmed")), "recommended_limit": recommended(st, now, company_limit) if stage else None,
            "today": today if today.get("date") == now.date().isoformat() else {}, "next_at": st.get("next_at"),
            "last": st.get("last"), "prep": st.get("prep") or {}, "has_twofa": bool(account.twofa_encrypted)}


async def event(db: AsyncSession, account_id: int, kind: str, text: str) -> None:
    db.add(TelegramWarmupEvent(account_id=account_id, kind=kind, text=text[:500]))


def _bump(st: dict, now: datetime, key: str, n: int = 1) -> None:
    today = st.get("today") or {}
    if today.get("date") != now.date().isoformat():
        today = {"date": now.date().isoformat()}
    today[key] = today.get(key, 0) + n
    st["today"] = today


# ---------------------------------------------------------------- управление

async def start(db: AsyncSession, accounts: list, now: datetime | None = None, *, warmed: bool = False) -> int:
    now = now or now_utc()
    for a in accounts:
        st = state(a)
        st.update(enabled=True, paused=False, warmed=warmed or st.get("warmed", False))
        st.setdefault("started_at", _iso(now))
        st["next_at"] = _iso(now + timedelta(minutes=random.randint(1, 20)))
        a.warmup = st
        await event(db, a.id, "start", "Аккаунт уже прогрет: сразу этап «Готов»" if warmed else "Прогрев запущен")
    await db.flush()
    return len(accounts)


async def pause(db: AsyncSession, accounts: list, paused: bool = True) -> int:
    for a in accounts:
        st = state(a)
        st["paused"] = paused
        a.warmup = st
        await event(db, a.id, "pause" if paused else "resume", "Прогрев на паузе" if paused else "Прогрев продолжен")
    await db.flush()
    return len(accounts)


async def reset(db: AsyncSession, accounts: list, now: datetime | None = None) -> int:
    now = now or now_utc()
    for a in accounts:
        st = state(a)
        st.update(started_at=_iso(now), warmed=False, enabled=True, paused=False)
        a.warmup = st
        await event(db, a.id, "reset", "Прогрев начат заново")
    await db.flush()
    return len(accounts)


# ---------------------------------------------------------------- действия в Telegram

async def prepare(client, account, st: dict, now: datetime) -> list[str]:
    """Скрыть номер, звонки — только контакты, облачный пароль (если его нет), завершить чужие сессии."""
    from telethon import errors
    from telethon.tl import functions, types
    from app.core.crypto import encrypt_secret
    prep = dict(st.get("prep") or {})
    notes = []
    if not prep.get("privacy"):
        await client(functions.account.SetPrivacyRequest(key=types.InputPrivacyKeyPhoneNumber(),
                                                         rules=[types.InputPrivacyValueDisallowAll()]))
        await client(functions.account.SetPrivacyRequest(key=types.InputPrivacyKeyPhoneCall(),
                                                         rules=[types.InputPrivacyValueAllowContacts()]))
        prep["privacy"] = _iso(now)
        notes.append("номер скрыт, звонки — только от контактов")
    if not prep.get("twofa"):
        pw = await client(functions.account.GetPasswordRequest())
        if pw.has_password:
            prep["twofa"] = "had"
            notes.append("облачный пароль уже был")
        else:
            password = secrets.token_urlsafe(12)
            await client.edit_2fa(new_password=password, hint="")
            account.twofa_encrypted = encrypt_secret(password)
            prep["twofa"] = _iso(now)
            notes.append("поставлен облачный пароль")
    if not prep.get("sessions"):
        try:
            await client(functions.auth.ResetAuthorizationsRequest())
            prep["sessions"] = _iso(now)
            notes.append("чужие сессии завершены")
        except errors.RPCError as exc:   # новой сессии Telegram даёт это сделать только через ~24 ч
            prep["sessions_retry"] = _iso(now + timedelta(hours=24))
            notes.append(f"чужие сессии завершим позже ({type(exc).__name__})")
    st["prep"] = prep
    if prep.get("privacy") and prep.get("twofa"):
        st["prepared_at"] = st.get("prepared_at") or _iso(now)
    return notes


async def read_channel(client, st: dict, channels: list[str], rnd: random.Random, react: bool) -> str | None:
    from telethon.tl import functions, types
    joined = list(st.get("channels") or [])
    import zlib
    target = 3 + zlib.crc32(str(st.get("started_at")).encode()) % 6   # 3–8 каналов, у каждого аккаунта своё
    candidates = [c for c in channels if c not in joined]
    if len(joined) < target and candidates:
        name = rnd.choice(candidates)
        entity = await client.get_entity(name)
        await client(functions.channels.JoinChannelRequest(entity))
        joined.append(name)
        st["channels"] = joined
        return f"вступил в @{name}"
    if not joined:
        return None
    name = rnd.choice(joined)
    entity = await client.get_entity(name)
    posts = await client.get_messages(entity, limit=rnd.randint(3, 10))
    ids = [m.id for m in posts]
    if ids:
        await client(functions.messages.GetMessagesViewsRequest(peer=entity, id=ids, increment=True))
        await client.send_read_acknowledge(entity, max_id=max(ids))
        if react and rnd.random() < 0.35:
            await client(functions.messages.SendReactionRequest(
                peer=entity, msg_id=rnd.choice(ids[:3]), reaction=[types.ReactionEmoji(emoticon=rnd.choice(bank.REACTIONS))]))
            return f"прочитал @{name} ({len(ids)}) и поставил реакцию"
    return f"прочитал @{name} ({len(ids)})"


async def _peer(client, partner):
    from telethon.tl import functions, types
    result = await client(functions.contacts.ImportContactsRequest(contacts=[types.InputPhoneContact(
        client_id=random.randint(1, 2 ** 31), phone=partner.phone, first_name=partner.first_name or "Коллега",
        last_name=partner.last_name or "")]))
    users = [u for u in result.users if not partner.telegram_user_id or u.id == partner.telegram_user_id]
    if not users:
        raise LookupError("собеседник не найден по номеру")
    return users[0]


async def chat_turn(client, chat: TelegramWarmupChat, me, partner, now: datetime, rnd: random.Random,
                    tz: str = "Europe/Moscow") -> str:
    from telethon.tl import functions, types
    user = await _peer(client, partner)
    last = await client.get_messages(user, limit=1)
    if last and not last[0].out:
        await client.send_read_acknowledge(user, max_id=last[0].id)
        if rnd.random() < 0.25:
            await client(functions.messages.SendReactionRequest(
                peer=user, msg_id=last[0].id, reaction=[types.ReactionEmoji(emoticon=rnd.choice(bank.REACTIONS))]))
    await asyncio.sleep(rnd.uniform(2, 6))     # «печатает»
    await client.send_message(user, bank.line(chat.topic, chat.turn, rnd))
    chat.turn += 1
    if chat.turn >= chat.turns:
        chat.finished_at = now
    else:
        chat.next_from_id = partner.id
        chat.next_at = awake(now + (timedelta(minutes=rnd.randint(1, 40)) if rnd.random() < 0.8
                                    else timedelta(hours=rnd.uniform(2, 6))), tz, partner.id, rnd)   # иногда «ответил позже»
    return f"написал {partner.first_name or partner.phone[-4:]} ({chat.topic}, реплика {chat.turn}/{chat.turns})"


async def pick_partner(db: AsyncSession, me, now: datetime, rnd: random.Random):
    """Собеседник из общего пула: прогрев включён, этап «Общение»/«Готов», другой прокси, без недавнего диалога."""
    rows = (await db.scalars(select(TelegramAccount).options(selectinload(TelegramAccount.proxy)).where(
        TelegramAccount.id != me.id, TelegramAccount.session_encrypted.is_not(None),
        TelegramAccount.proxy_id.is_not(None), TelegramAccount.warmup.is_not(None)))).all()
    recent = set((await db.scalars(select(TelegramWarmupChat.b_id).where(
        TelegramWarmupChat.a_id == me.id, TelegramWarmupChat.started_at > now - timedelta(hours=48)))).all()) | set(
        (await db.scalars(select(TelegramWarmupChat.a_id).where(
            TelegramWarmupChat.b_id == me.id, TelegramWarmupChat.started_at > now - timedelta(hours=48)))).all())
    ok = [p for p in rows if stage_of(state(p), now) in ("social", "ready") and not state(p).get("paused")
          and p.proxy_id != me.proxy_id and p.proxy is not None and p.proxy.status != "error"
          and p.id not in recent and p.status not in ("spamblock", "flood_wait")]
    return rnd.choice(ok) if ok else None


# ---------------------------------------------------------------- сессия

async def run_session(account_id: int, rnd: random.Random | None = None, now: datetime | None = None) -> None:
    from telethon.errors import FloodWaitError, PeerFloodError
    from telethon.tl import functions
    from app.services import account_health, account_locks, telegram_jobs
    rnd = rnd or random.Random()
    now = now or now_utc()
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if not account_locks.try_take(account_id):
        return
    try:
        async with sessions()() as db:
            account = await db.scalar(select(TelegramAccount).options(selectinload(TelegramAccount.proxy))
                                      .where(TelegramAccount.id == account_id))
            if account is None:
                return
            st = state(account)
            stage = stage_of(st, now)
            ws = account.workspace_id or tg_space.home()
            row = await db.get(ClientWorkspace, ws) if ws else None
            tz = row.timezone if row is not None and getattr(row, "timezone", None) else "Europe/Moscow"
            conf = await company(db, ws)
            if not stage or st.get("paused") or (row is not None and row.status != "active"):
                return
            if account.proxy is None or account.proxy.status == "error" or not account.session_encrypted:
                st["last"] = "нет рабочего прокси — ждём"
                st["next_at"] = _iso(now + timedelta(hours=1))
                account.warmup = st
                await db.commit()
                return
            due_chats = list((await db.scalars(select(TelegramWarmupChat).where(
                TelegramWarmupChat.next_from_id == account.id, TelegramWarmupChat.finished_at.is_(None),
                TelegramWarmupChat.next_at <= now).limit(2))).all())
            notes: list[str] = []

            async def go(client):
                await client(functions.account.UpdateStatusRequest(offline=False))
                if stage == "prep" or (st.get("prep", {}).get("sessions_retry")
                                       and _dt(st["prep"]["sessions_retry"]) <= now):
                    if st.get("prep", {}).get("sessions_retry"):
                        st["prep"].pop("sessions_retry", None)
                    notes.extend(await prepare(client, account, st, now))
                spam = (account.health or {}).get("spamblock") or {}
                if not spam.get("checked_at") or _dt(spam["checked_at"]) < now - timedelta(hours=24):
                    result = await account_health.ask_spambot(client)
                    account_health.apply_spamblock(account, result, now)
                    notes.append("спам-блок: " + ("есть ограничение" if result["state"] == "limited" else "нет"))
                    _bump(st, now, "actions")
                for chat in due_chats:
                    partner_id = chat.b_id if chat.a_id == account.id else chat.a_id
                    partner = await db.get(TelegramAccount, partner_id)
                    if partner is None or not state(partner).get("enabled"):
                        chat.finished_at = now
                        continue
                    notes.append(await chat_turn(client, chat, account, partner, now, rnd, tz))
                    _bump(st, now, "messages")
                if stage in ("passive", "social", "ready"):
                    for _ in range(rnd.randint(1, 2 if stage == "ready" else 3)):
                        await asyncio.sleep(rnd.uniform(3, 20))
                        note = await read_channel(client, st, conf["channels"], rnd, react=True)
                        if note:
                            notes.append(note)
                            _bump(st, now, "channels")
                today = (st.get("today") or {})
                started_today = today.get("chats_started", 0) if today.get("date") == now.date().isoformat() else 0
                if stage in ("social", "ready") and conf.get("pool", True) and started_today < (2 if stage == "social" else 1) \
                        and rnd.random() < 0.6:
                    partner = await pick_partner(db, account, now, rnd)
                    if partner is not None:
                        topic = rnd.choice(list(bank.TOPICS))
                        chat = TelegramWarmupChat(a_id=account.id, b_id=partner.id, topic=topic, turn=0,
                                                  turns=rnd.randint(2, bank.turns(topic)), next_from_id=account.id,
                                                  next_at=now, started_at=now)
                        db.add(chat)
                        await db.flush()
                        notes.append(await chat_turn(client, chat, account, partner, now, rnd, tz))
                        _bump(st, now, "chats_started")
                        _bump(st, now, "messages")
                await asyncio.sleep(rnd.uniform(2, 8))
                await client(functions.account.UpdateStatusRequest(offline=True))

            try:
                await telegram_jobs._with_account_unlocked(account, go)
                _bump(st, now, "sessions")
                st["last"] = "; ".join(notes) or "был в сети"
                await event(db, account.id, "session", st["last"])
            except (FloodWaitError, PeerFloodError) as exc:
                seconds = getattr(exc, "seconds", 3600) or 3600
                st["paused_until"] = _iso(now + timedelta(seconds=seconds))
                st["last"] = "Telegram попросил паузу — прогрев ждёт"
                await event(db, account.id, "flood", f"Пауза Telegram {seconds} с")
            except Exception as exc:  # noqa: BLE001
                from app.services import tg_errors
                kind, text = tg_errors.classify(exc)
                if kind == "dead":       # мёртвую сессию прогревом не дёргаем — ставим на паузу до переподключения
                    st["paused"] = True
                    account.status = "unauthorized" if "Сессия" in text else "banned"
                    account.last_error = text
                elif kind == "limited":
                    st["paused_until"] = _iso(now + timedelta(hours=24))
                st["last"] = text if kind else f"ошибка: {type(exc).__name__}"
                await event(db, account.id, "error", f"{type(exc).__name__}: {str(exc)[:200]}")
                log.warning("warmup account %s: %s", account_id, type(exc).__name__)
            stage = stage_of(st, now) or stage
            nxt = next_session(now, stage, conf["intensity"], tz, account.id, rnd)
            paused_until = _dt(st.get("paused_until"))
            if paused_until and paused_until > nxt:
                nxt = paused_until
            st["next_at"] = _iso(nxt)
            account.warmup = st
            await db.commit()
    finally:
        account_locks.release(account_id)


async def due_accounts(db: AsyncSession, now: datetime) -> list[int]:
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    rows = (await db.scalars(select(TelegramAccount).where(TelegramAccount.warmup.is_not(None)))).all()
    chat_due = set((await db.scalars(select(TelegramWarmupChat.next_from_id).where(
        TelegramWarmupChat.finished_at.is_(None), TelegramWarmupChat.next_at <= now))).all())
    out = []
    for a in rows:
        st = state(a)
        if not st.get("enabled") or st.get("paused"):
            continue
        nxt = _dt(st.get("next_at"))
        if a.id in chat_due or nxt is None or nxt <= now:
            out.append(a.id)
    return out


async def run_worker() -> None:
    from app.services import account_locks
    gate = asyncio.Semaphore(CONCURRENCY)
    running: set[int] = set()

    async def one(account_id: int):
        async with gate:
            try:
                await run_session(account_id)
            finally:
                running.discard(account_id)

    while True:
        try:
            async with sessions()() as db:
                ids = await due_accounts(db, now_utc())
            for account_id in ids:
                if account_id in running or account_locks.busy(account_id):
                    continue
                running.add(account_id)
                spawn(one(account_id))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("warmup worker cycle failed")
        await asyncio.sleep(20)
