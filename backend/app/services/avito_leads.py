"""Avito chats and calls → CRM inbound requests ("Входящие").

* Chats: Avito Messenger u2i chats about the account's own listings. A chat becomes one
  inbound request (external_id "chat:<id>"); later messages in the same chat are not new leads.
* Calls: Avito Calltracking (getCalls) or, if that is not connected, CPA calls (callsByTime).
  Each call becomes an inbound request with the buyer's phone ("call:<id>"); CRM shows
  duplicates by phone on acceptance.

Every request is attributed to the Avito connection (verified_connection_id) so CPL/ROMI
of the cabinet include it. Polling is cursor-based and idempotent (external_id dedup).
"""
import asyncio
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import token_digest
from app.models.marketing import AdConnection, LeadInboundSource
from app.services import avito
from app.services.avito import AvitoClient, AvitoError, config, set_config

logger = logging.getLogger("uvicorn.error.avito_leads")
POLL_SECONDS = 180
STATS_SYNC_HOURS = 6
CALLS_RETRY_HOURS = 24
_locks: dict[int, asyncio.Lock] = {}


def lock_for(connection_id: int) -> asyncio.Lock:
    return _locks.setdefault(connection_id, asyncio.Lock())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def normalize_phone(value) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        digits = "7" + digits
    return f"+{digits}" if 10 <= len(digits) <= 15 else None


def leads_state(row: AdConnection) -> dict:
    data = config(row)
    return {"chats": bool(data.get("leads_chats")), "calls": bool(data.get("leads_calls")),
            "calls_mode": data.get("calls_mode"), "calls_note": data.get("calls_note"),
            "last_check_at": data.get("leads_checked_at"), "last_error": data.get("leads_error"),
            "imported_chats": int(data.get("imported_chats") or 0),
            "imported_calls": int(data.get("imported_calls") or 0),
            "inbound_source_id": data.get("inbound_source_id")}


async def ensure_inbound_source(db: AsyncSession, row: AdConnection) -> LeadInboundSource:
    source_id = config(row).get("inbound_source_id")
    source = await db.get(LeadInboundSource, source_id) if source_id else None
    if source and source.project_id == row.project_id and source.workspace_id == row.workspace_id:
        if not source.active:
            source.active = True
        return source
    token = secrets.token_urlsafe(48)  # never shown; the source only receives server-side Avito leads
    source = LeadInboundSource(workspace_id=row.workspace_id, project_id=row.project_id,
                               name=f"Авито: {row.name}"[:180], token_hash=token_digest(token),
                               token_prefix=token[:12], active=True, auto_assign=True)
    db.add(source)
    await db.flush()
    set_config(row, inbound_source_id=source.id)
    return source


async def configure(db: AsyncSession, row: AdConnection, *, chats: bool, calls: bool) -> dict:
    data = config(row)
    now = _now()
    updates = {"leads_chats": chats, "leads_calls": calls, "leads_error": None}
    # Start from "now" when switching on, so historical chats/calls do not flood the CRM.
    if chats and not data.get("leads_chats"):
        updates["chats_since"] = int(now.timestamp())
    if calls and not data.get("leads_calls"):
        updates["calls_since"] = now.isoformat()
        updates["calls_mode"] = None
        updates["calls_note"] = None
    set_config(row, **updates)
    if chats or calls:
        await ensure_inbound_source(db, row)
    await db.commit()
    return leads_state(row)


async def _create(db: AsyncSession, row: AdConnection, source: LeadInboundSource, data: dict) -> bool:
    from app.api.routes.portal import InboundLead  # local import: routes import services, not the reverse
    from app.services.inbound_lead import create_inbound
    payload = InboundLead.model_validate(data)
    attribution = {"verified_connection_id": row.id, "platform": row.platform}
    if data.get("item_id"):
        attribution["avito_item_id"] = str(data["item_id"])
    result = await create_inbound(db, source, payload, commit=True, allow_raw_contact=True,
                                  attribution_extra=attribution)
    return not result.get("duplicate")


def _chat_text(message: dict | None) -> str | None:
    content = (message or {}).get("content") or {}
    text = content.get("text")
    if text:
        return str(text)[:3000]
    if content.get("call"):
        return "Звонок через Авито"
    if content.get("image"):
        return "Изображение"
    return None


async def _first_incoming(client: AvitoClient, user_id: str, chat_id: str) -> str | None:
    try:
        data = await client.get(f"/messenger/v3/accounts/{user_id}/chats/{chat_id}/messages/",
                                params={"limit": 50, "offset": 0})
    except AvitoError:
        return None
    messages = data.get("messages") or data.get("items") or []
    incoming = [m for m in messages if m.get("direction") == "in"]
    incoming.sort(key=lambda m: m.get("created") or 0)
    return _chat_text(incoming[0]) if incoming else None


async def poll_chats(db: AsyncSession, row: AdConnection, client: AvitoClient, source: LeadInboundSource) -> int:
    user_id = str(row.external_account_id)
    since = int(config(row).get("chats_since") or _now().timestamp())
    newest = since
    created = 0
    for page in range(10):
        data = await client.get(f"/messenger/v2/accounts/{user_id}/chats",
                                params={"chat_types": "u2i", "limit": 100, "offset": page * 100})
        chats = data.get("chats") or []
        for chat in chats:
            created_at = int(chat.get("created") or 0)
            if created_at < since:
                continue
            context = (chat.get("context") or {}).get("value") or {}
            if context.get("user_id") is not None and str(context.get("user_id")) != user_id:
                continue  # we are the buyer in this chat, not a lead
            buyer = next((u for u in chat.get("users") or [] if str(u.get("id")) != user_id), {})
            chat_id = str(chat.get("id"))
            last = chat.get("last_message") or {}
            first_text = await _first_incoming(client, user_id, chat_id)
            if first_text is None and last.get("direction") == "in":
                first_text = _chat_text(last)
            name = (buyer.get("name") or "").strip() or "Покупатель Авито"
            chat_url = f"https://www.avito.ru/profile/messenger/channel/{chat_id}"
            if await _create(db, row, source, {
                "external_id": f"chat:{chat_id}", "full_name": name[:180] if len(name) >= 2 else "Покупатель Авито",
                "source": "avito", "external_source": "avito", "contact_method": "Чат Авито",
                "contact": chat_url, "chat_url": chat_url, "notes": first_text,
                "item_id": str(context.get("id")) if context.get("id") else None,
                "item_title": context.get("title"), "item_url": context.get("url"),
                "item_price": context.get("price_string"),
                "buyer_profile_url": (buyer.get("public_user_profile") or {}).get("url"),
                "landing_url": context.get("url"), "contact_consent": False}):
                created += 1
            newest = max(newest, created_at)
        updated = [int(chat.get("updated") or 0) for chat in chats]
        if len(chats) < 100 or (updated and max(updated) < since):
            break
    set_config(row, chats_since=newest, imported_chats=int(config(row).get("imported_chats") or 0) + created)
    return created


async def _fetch_calls(client: AvitoClient, mode: str, since: datetime) -> list[dict]:
    calls = []
    for page in range(10):
        if mode == "calltracking":
            status, data = await client.request("POST", "/calltracking/v1/getCalls/", json={
                "dateTimeFrom": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 100, "offset": page * 100},
                allow=(400, 402, 403, 404))
        else:
            status, data = await client.request("POST", "/cpa/v2/callsByTime", json={
                "dateTimeFrom": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 100, "offset": page * 100},
                allow=(400, 402, 403, 404))
        error = data.get("error") if isinstance(data.get("error"), dict) else None
        if status >= 400 or (error and (error.get("code") or error.get("message") or error.get("error"))):
            raise AvitoError("unavailable", status)
        rows = data.get("calls") or []
        calls.extend(rows)
        if len(rows) < 100:
            return calls
        if mode == "cpa":
            await asyncio.sleep(61)  # CPA calls: 1 request per minute
    return calls


async def poll_calls(db: AsyncSession, row: AdConnection, client: AvitoClient, source: LeadInboundSource) -> int:
    data = config(row)
    since = _parse_time(data.get("calls_since")) or _now()
    mode = data.get("calls_mode")
    if mode == "unavailable":
        checked = _parse_time(data.get("calls_mode_checked_at"))
        if checked and checked > _now() - timedelta(hours=CALLS_RETRY_HOURS):
            return 0
        mode = None
    calls = None
    for candidate in ([mode] if mode else ["calltracking", "cpa"]):
        try:
            calls = await _fetch_calls(client, candidate, since)
            mode = candidate
            break
        except AvitoError as exc:
            if str(exc) != "unavailable" and exc.status not in {400, 402, 403, 404}:
                raise
    if calls is None:
        set_config(row, calls_mode="unavailable", calls_mode_checked_at=_now().isoformat(),
                   calls_note="Авито не отдаёт звонки по API для этого профиля: нужен подключённый "
                              "Коллтрекинг Авито или тариф с оплатой за целевые действия (CPA).")
        return 0
    set_config(row, calls_mode=mode, calls_note=None)
    created = 0
    newest = since
    for call in calls:
        call_time = _parse_time(call.get("createTime") or call.get("startTime"))
        if call_time and call_time < since:
            continue
        phone = normalize_phone(call.get("buyerPhone"))
        duration = int(float(call.get("duration") or 0))
        note = f"Звонок с Авито, разговор {duration} сек." if duration else "Пропущенный звонок с Авито"
        if await _create(db, row, source, {
            "external_id": f"call:{call.get('id')}", "full_name": f"Звонок {phone or 'с Авито'}",
            "phone": phone, "source": "avito", "external_source": "avito",
            "contact_method": "Звонок Авито", "contact": phone or "Номер скрыт", "notes": note,
            "item_id": str(call.get("itemId")) if call.get("itemId") else None,
            "call_duration": duration, "call_record_url": call.get("recordUrl"),
            "call_group": call.get("groupTitle"), "contact_consent": False}):
            created += 1
        if call_time:
            newest = max(newest, call_time)
    set_config(row, calls_since=newest.isoformat(),
               imported_calls=int(config(row).get("imported_calls") or 0) + created)
    return created


async def poll(db: AsyncSession, row: AdConnection) -> dict:
    """Import new chats/calls for one avito_items connection. Commits."""
    state = leads_state(row)
    if row.platform != avito.ITEMS or not (state["chats"] or state["calls"]):
        return {"chats": 0, "calls": 0}
    async with lock_for(row.id):
        result = {"chats": 0, "calls": 0}
        try:
            client = await avito.client_for(db, row)
            source = await ensure_inbound_source(db, row)
            await db.commit()
            if state["chats"]:
                result["chats"] = await poll_chats(db, row, client, source)
            if state["calls"]:
                result["calls"] = await poll_calls(db, row, client, source)
            set_config(row, leads_checked_at=_now().isoformat(), leads_error=None)
        except AvitoError as exc:
            set_config(row, leads_checked_at=_now().isoformat(), leads_error=str(exc)[:500])
        await db.commit()
        return result


async def _tick(connection_id: int) -> None:
    from app.api.routes.marketing import store_metrics
    from app.db import SessionLocal
    async with SessionLocal() as db:
        row = await db.get(AdConnection, connection_id)
        if row is None or row.status != "connected":
            return
        if row.platform == avito.ITEMS:
            await poll(db, row)
        synced = row.last_synced_at
        if synced and synced.tzinfo is None:
            synced = synced.replace(tzinfo=timezone.utc)
        if not synced or synced > _now() - timedelta(hours=STATS_SYNC_HOURS):
            return  # never-synced cabinets are synced by a person first (initial history is heavy)
        async with lock_for(row.id):
            try:
                await store_metrics(db, row, await avito.metrics(db, row))
                row.last_synced_at = _now()
                row.last_error = None
                await db.commit()
            except Exception as exc:  # keep the worker alive; show the error on the cabinet card
                await db.rollback()
                row = await db.get(AdConnection, connection_id, populate_existing=True)
                row.last_error = f"Автосинхронизация: {exc}"[:1500]
                await db.commit()


async def run_worker() -> None:
    """Background loop: poll leads every few minutes, refresh Avito statistics every few hours."""
    from app.db import SessionLocal
    await asyncio.sleep(20)
    while True:
        try:
            async with SessionLocal() as db:
                from app.models.marketing import Project
                ids = (await db.scalars(select(AdConnection.id).where(
                    AdConnection.platform.in_(avito.PLATFORMS), AdConnection.status == "connected",
                    AdConnection.project_id.not_in(select(Project.id).where(Project.status == "demo"))))).all()
            for connection_id in ids:
                try:
                    await _tick(connection_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("avito worker failed for connection %s", connection_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("avito worker iteration failed")
        await asyncio.sleep(POLL_SECONDS)
