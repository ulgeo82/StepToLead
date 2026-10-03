"""Telephony: Mango Office VPBX API → call log, CRM links, missed-call tasks, recordings.

How it works
* The manager pastes two keys from Mango (Настройки → Интеграции → API): "уникальный код АТС"
  (vpbx_api_key) and "ключ для создания подписи" (salt), and puts our address
  https://<portal>/api/telephony/mango/<public_id> into "Адрес внешней системы".
* Mango POSTs form fields vpbx_api_key, sign, json to <address>/events/call (live call states),
  /events/summary (final result of a call) and /events/recording (a record is ready).
  sign = sha256(vpbx_api_key + json + salt).
* Commands go the other way, signed the same: commands/callback (click-to-call: Mango rings the
  manager first, then the client), config/users/request (employees and extensions),
  queries/recording/post (download a record).

The summary event is the source of truth: it creates/links the contact, deal or a new request in
«Неразобранное», writes the call into the deal timeline and sets a «Перезвонить» task for a missed call.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import token_digest
from app.core.config import settings
from app.core.crypto import decrypt_secret
from app.models.crm import CrmActivity, CrmContact, CrmDeal, CrmInbound, CrmTask
from app.models.marketing import LeadInboundSource, PortalUser
from app.models.telephony import Call, TelephonyConnection
from app.services.notifications import direct as notify_direct, flush_telegram

logger = logging.getLogger("uvicorn.error.telephony")
PROVIDERS = {"mango": "Mango Office"}
MANGO_URL = "https://app.mango-office.ru/vpbx/"
MISSED_TASK_MINUTES = 15
CALLBACK_PREFIX = "Перезвонить"
WORKER_SECONDS = 60
MANGO_ERRORS = {
    3100: "Mango Office: неверные параметры запроса", 3101: "Mango Office: неверный метод запроса",
    3102: "Mango Office: неверная подпись — проверьте «ключ для создания подписи»",
    3103: "Mango Office: интеграция по API не подключена в кабинете Mango",
    3105: "Mango Office: неверный уникальный код АТС", 4001: "Mango Office: неверный номер",
    3104: "Mango Office: команда не выполнена", 5001: "Mango Office: сотрудник не найден",
    4101: "Mango Office: недостаточно средств на счёте",
}
_locks: dict[int, asyncio.Lock] = {}
_recent_dials: dict[tuple[int, int], float] = {}  # (project_id, user_id) → time of click-to-call


class TelephonyError(ValueError):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def from_unix(value) -> datetime | None:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(number, tz=timezone.utc) if number > 0 else None


def norm_phone(value: str | None) -> str:
    """Digits of a real phone number (7XXXXXXXXXX for Russia); '' for SIP addresses and short numbers."""
    raw = str(value or "")
    if raw.startswith("sip:") or "@" in raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        digits = "7" + digits
    return digits if len(digits) >= 10 else ""


def pretty_phone(digits: str) -> str:
    if len(digits) == 11 and digits[0] == "7":
        return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:]}"
    return f"+{digits}" if digits else "номер скрыт"


def cfg(conn: TelephonyConnection) -> dict:
    return dict(conn.config or {})


def set_cfg(conn: TelephonyConnection, **values) -> None:
    conn.config = {**cfg(conn), **values}


def lock_for(connection_id: int) -> asyncio.Lock:
    return _locks.setdefault(connection_id, asyncio.Lock())


def user_for_extension(conn: TelephonyConnection, extension: str | None) -> int | None:
    if not extension:
        return None
    value = (cfg(conn).get("user_map") or {}).get(str(extension))
    return int(value) if value else None


def extension_for_user(conn: TelephonyConnection, user_id: int) -> str | None:
    return next((ext for ext, uid in (cfg(conn).get("user_map") or {}).items() if uid and int(uid) == user_id), None)


def remember_dial(project_id: int, user_id: int) -> None:
    _recent_dials[(project_id, user_id)] = time.monotonic()


def dialed_recently(project_id: int, user_id: int, seconds: int = 90) -> bool:
    started = _recent_dials.get((project_id, user_id))
    return started is not None and time.monotonic() - started < seconds


def duration_text(seconds: int) -> str:
    minutes, sec = divmod(max(0, int(seconds)), 60)
    return f"{minutes} мин {sec} сек" if minutes else f"{sec} сек"


# --------------------------------------------------------------------------- Mango client

class Mango:
    def __init__(self, api_key: str, salt: str):
        self.key, self.salt = (api_key or "").strip(), (salt or "").strip()
        if not self.key or not self.salt:
            raise TelephonyError("Укажите уникальный код АТС и ключ для создания подписи из кабинета Mango Office")

    @classmethod
    def for_connection(cls, conn: TelephonyConnection) -> "Mango":
        return cls(decrypt_secret(conn.api_key_encrypted) or "", decrypt_secret(conn.api_salt_encrypted) or "")

    def sign(self, body: str) -> str:
        return hashlib.sha256(f"{self.key}{body}{self.salt}".encode()).hexdigest()

    def verify(self, form: dict) -> dict:
        body = str(form.get("json") or "")
        if not hmac.compare_digest(str(form.get("vpbx_api_key") or ""), self.key) or \
                not hmac.compare_digest(str(form.get("sign") or "").lower(), self.sign(body)):
            raise TelephonyError("Неверная подпись запроса Mango Office")
        try:
            data = json.loads(body) if body else {}
        except ValueError:
            raise TelephonyError("Некорректный JSON от Mango Office") from None
        return data if isinstance(data, dict) else {}

    def form(self, data: dict) -> dict:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        return {"vpbx_api_key": self.key, "sign": self.sign(body), "json": body}

    async def request(self, path: str, data: dict, *, raw: bool = False):
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                response = await client.post(MANGO_URL + path, data=self.form(data))
        except httpx.HTTPError:
            raise TelephonyError("Mango Office недоступен. Повторите позже.") from None
        if response.status_code in {401, 403}:
            raise TelephonyError("Mango Office отклонил ключи: проверьте уникальный код АТС и ключ подписи")
        if response.status_code >= 400:
            raise TelephonyError(f"Mango Office вернул ошибку {response.status_code}")
        is_json = "json" in response.headers.get("content-type", "") or response.content[:1] in {b"{", b"["}
        payload = None
        if is_json:
            try:
                payload = response.json()
            except ValueError:
                payload = None
        if isinstance(payload, dict) and payload.get("result") is not None:
            try:
                code = int(payload["result"])
            except (TypeError, ValueError):
                code = 0
            if code != 1000:
                raise TelephonyError(MANGO_ERRORS.get(code, f"Mango Office: код ошибки {payload['result']}"))
        if raw:
            if is_json:
                raise TelephonyError("Mango Office не отдал файл записи")
            return response.content
        return payload if isinstance(payload, dict) else {}

    async def users(self) -> list[dict]:
        data = await self.request("config/users/request", {})
        result = []
        for row in data.get("users") or []:
            general, telephony = row.get("general") or {}, row.get("telephony") or {}
            extension = str(telephony.get("extension") or "").strip()
            if not extension:
                continue
            numbers = [str(n.get("number")) for n in telephony.get("numbers") or [] if isinstance(n, dict) and n.get("number")]
            result.append({"extension": extension, "name": str(general.get("name") or f"Сотрудник {extension}")[:120],
                           "numbers": numbers[:5]})
        return sorted(result, key=lambda u: (len(u["extension"]), u["extension"]))

    async def callback(self, extension: str, phone_digits: str) -> None:
        await self.request("commands/callback", {"command_id": f"stl-{secrets.token_hex(8)}",
                                                 "from": {"extension": str(extension)}, "to_number": phone_digits})

    async def recording(self, recording_id: str) -> bytes:
        return await self.request("queries/recording/post", {"recording_id": recording_id, "action": "download"}, raw=True)


def client(conn: TelephonyConnection):
    if conn.provider == "mango":
        return Mango.for_connection(conn)
    raise TelephonyError("Неизвестный провайдер телефонии")


# --------------------------------------------------------------------------- events

async def get_call(db: AsyncSession, conn: TelephonyConnection, entry_id: str, started: datetime | None) -> Call:
    call = await db.scalar(select(Call).where(Call.connection_id == conn.id, Call.entry_id == entry_id))
    if call is None:
        call = Call(workspace_id=conn.workspace_id, project_id=conn.project_id, connection_id=conn.id,
                    entry_id=entry_id[:180], status="ringing", direction="in", started_at=started or now(), meta={})
        db.add(call); await db.flush()
    return call


async def attach_contact(db: AsyncSession, call: Call) -> None:
    digits = norm_phone(call.client_phone)
    if call.contact_id is None and digits:
        contact = await db.scalar(select(CrmContact).where(CrmContact.project_id == call.project_id,
                                                           CrmContact.phone_normalized == digits).limit(1))
        call.contact_id = contact.id if contact else None
    if call.contact_id and call.deal_id is None:
        deal = await db.scalar(select(CrmDeal).where(CrmDeal.contact_id == call.contact_id, CrmDeal.archived_at.is_(None),
                                                     CrmDeal.closed_at.is_(None)).order_by(CrmDeal.created_at.desc()).limit(1))
        call.deal_id = deal.id if deal else None


async def on_call_event(db: AsyncSession, conn: TelephonyConnection, data: dict) -> Call | None:
    """Live state of a call: drives the incoming-call card."""
    entry = str(data.get("entry_id") or "")
    if not entry:
        return None
    stamp = from_unix(data.get("timestamp")) or now()
    call = await get_call(db, conn, entry, stamp)
    if call.processed:
        return call
    source, target = data.get("from") or {}, data.get("to") or {}
    from_ext, to_ext = str(source.get("extension") or ""), str(target.get("extension") or "")
    if from_ext and to_ext:
        call.meta = {**(call.meta or {}), "internal": True}
        return call
    if from_ext:
        call.direction, call.extension, phone = "out", from_ext, target.get("number")
    else:
        call.direction, phone = "in", source.get("number")
        if to_ext:
            call.extension = to_ext
    digits = norm_phone(phone)
    if digits and not call.client_phone:
        call.client_phone = f"+{digits}"
    if target.get("line_number") and not call.line_number:
        call.line_number = str(target["line_number"])[:32]
    if call.extension:
        call.user_id = user_for_extension(conn, call.extension) or call.user_id
    state = str(data.get("call_state") or "")
    if state == "Connected" and call.extension:
        call.status = "talking"
        call.answered_at = call.answered_at or stamp
    elif state == "Appeared" and call.status != "talking":
        call.status = "ringing"
    await attach_contact(db, call)
    return call


async def on_recording(db: AsyncSession, conn: TelephonyConnection, data: dict) -> Call | None:
    entry, recording = str(data.get("entry_id") or ""), str(data.get("recording_id") or "")
    if not entry or not recording or str(data.get("recording_state") or "Completed") != "Completed":
        return None
    call = await get_call(db, conn, entry, from_unix(data.get("timestamp")))
    if not call.recording_id:
        call.recording_id = recording[:255]
    elif call.recording_id != recording:
        extra = list((call.meta or {}).get("extra_recordings") or [])
        if recording not in extra:
            call.meta = {**(call.meta or {}), "extra_recordings": [*extra, recording][:10]}
    return call


async def on_summary(db: AsyncSession, conn: TelephonyConnection, data: dict) -> Call | None:
    entry = str(data.get("entry_id") or "")
    if not entry:
        return None
    created = from_unix(data.get("create_time"))
    call = await get_call(db, conn, entry, created)
    try:
        direction = int(data.get("call_direction"))
    except (TypeError, ValueError):
        direction = 1
    source, target = data.get("from") or {}, data.get("to") or {}
    if direction == 0:  # internal call between employees: not a client call
        call.meta = {**(call.meta or {}), "internal": True}
        call.processed, call.status = True, "answered"
        return call
    if direction == 1:
        call.direction, phone, extension = "in", source.get("number"), target.get("extension")
    else:
        call.direction, phone, extension = "out", target.get("number"), source.get("extension")
    digits = norm_phone(phone)
    call.client_phone = f"+{digits}" if digits else call.client_phone
    if extension:
        call.extension = str(extension)
        call.user_id = user_for_extension(conn, call.extension) or call.user_id
    line = data.get("line_number") or target.get("line_number")
    call.line_number = str(line)[:32] if line else call.line_number
    talk, end = int(float(data.get("talk_time") or 0)), int(float(data.get("end_time") or 0))
    start = int(float(data.get("create_time") or 0))
    call.started_at = created or call.started_at
    call.answered_at = from_unix(talk) if talk else None
    call.ended_at = from_unix(end) or now()
    call.duration_sec = max(0, end - talk) if talk and end else 0
    call.wait_sec = max(0, (talk or end) - start) if start else 0
    call.disconnect_reason = str(data.get("disconnect_reason") or "")[:32] or None
    answered = bool(talk) and str(data.get("entry_result", 1)) == "1"
    call.status = "answered" if answered else "missed"
    if call.processed:
        return call
    call.processed = True
    await link_crm(db, conn, call)
    return call


async def ensure_source(db: AsyncSession, conn: TelephonyConnection) -> LeadInboundSource:
    source = await db.get(LeadInboundSource, conn.inbound_source_id) if conn.inbound_source_id else None
    if source:
        return source
    token = secrets.token_urlsafe(48)
    source = LeadInboundSource(workspace_id=conn.workspace_id, project_id=conn.project_id,
                               name=f"Звонки: {conn.name}"[:180], token_hash=token_digest(token),
                               token_prefix=token[:12], active=True, auto_assign=True)
    db.add(source); await db.flush()
    conn.inbound_source_id = source.id
    return source


def call_text(call: Call, user: PortalUser | None) -> str:
    who = f" · {user.display_name}" if user else ""
    if call.direction == "in":
        return (f"Входящий, {duration_text(call.duration_sec)}{who}" if call.status == "answered"
                else f"Пропущенный входящий{f', ждал {duration_text(call.wait_sec)}' if call.wait_sec else ''}")
    return f"Исходящий, {duration_text(call.duration_sec)}{who}" if call.status == "answered" \
        else f"Исходящий, не дозвонились{who}"


async def link_crm(db: AsyncSession, conn: TelephonyConnection, call: Call) -> None:
    """Final call: link to contact/deal/request, timeline entry, missed-call task, notification."""
    from app.api.routes.crm import activity
    from app.services.messaging import project_people
    digits = norm_phone(call.client_phone)
    phone = f"+{digits}" if digits else None
    await attach_contact(db, call)
    inbound = await db.get(CrmInbound, call.inbound_id) if call.inbound_id else None
    if call.contact_id is None and inbound is None and phone:
        inbound = await db.scalar(select(CrmInbound).where(CrmInbound.project_id == call.project_id, CrmInbound.status == "NEW",
                                                           CrmInbound.phone == phone).order_by(CrmInbound.id.desc()).limit(1))
        if inbound is None and call.direction == "in" and cfg(conn).get("create_leads", True):
            from app.api.routes.portal import InboundLead
            from app.services.inbound_lead import create_inbound
            note = (f"Входящий звонок, разговор {duration_text(call.duration_sec)}" if call.status == "answered"
                    else "Пропущенный входящий звонок")
            payload = InboundLead.model_validate({
                "external_id": f"call:{conn.id}:{call.entry_id}"[:180], "full_name": f"Звонок {pretty_phone(digits)}",
                "phone": phone, "source": "phone", "external_source": "phone", "contact_method": "Телефон",
                "contact": phone, "notes": note, "call_id": call.id, "line_number": call.line_number, "contact_consent": False})
            result = await create_inbound(db, await ensure_source(db, conn), payload, commit=False, allow_raw_contact=True)
            inbound = await db.get(CrmInbound, result.get("inbound_id")) if result.get("inbound_id") else None
        call.inbound_id = inbound.id if inbound else None
    deal = await db.get(CrmDeal, call.deal_id) if call.deal_id else None
    user = await db.get(PortalUser, call.user_id) if call.user_id else None
    answered = call.status == "answered"
    payload = {"text": call_text(call, user), "call_id": call.id, "direction": call.direction, "status": call.status,
               "duration": call.duration_sec, "phone": phone}
    human = answered or call.direction == "out"
    if deal:
        activity(db, deal, user if human else None, "CALL_LOGGED", payload, touch=human)
    elif inbound:
        activity(db, None, user if human else None, "CALL_LOGGED", payload, inbound=inbound, touch=False)
    callback_title = f"{CALLBACK_PREFIX} {pretty_phone(digits)}"[:220] if digits else None
    if answered and callback_title:
        # The client was reached: earlier «Перезвонить» tasks for this number are done.
        for task in (await db.scalars(select(CrmTask).where(CrmTask.project_id == call.project_id, CrmTask.status == "OPEN",
                                                             CrmTask.title == callback_title))).all():
            task.status, task.completed_at = "COMPLETED", now()
            task.result = f"Связались по телефону ({'входящий' if call.direction == 'in' else 'исходящий'} звонок)"
            if deal and task.deal_id == deal.id:
                activity(db, deal, user, "TASK_COMPLETED", {"task_id": task.id, "title": task.title, "result": task.result}, touch=False)
    if call.direction != "in" or answered:
        return
    people = await project_people(db, call.project_id)
    responsible = deal.responsible_user_id if deal and deal.responsible_user_id in people else \
        call.user_id if call.user_id in people else None
    name = None
    if call.contact_id:
        contact = await db.get(CrmContact, call.contact_id)
        name = contact.name if contact else None
    if callback_title:
        exists = await db.scalar(select(CrmTask.id).where(CrmTask.project_id == call.project_id, CrmTask.status == "OPEN",
                                                          CrmTask.title == callback_title).limit(1))
        if not exists:
            minutes = int(cfg(conn).get("missed_task_minutes") or MISSED_TASK_MINUTES)
            task = CrmTask(workspace_id=call.workspace_id, project_id=call.project_id, deal_id=deal.id if deal else None,
                           contact_id=call.contact_id, type_code="CALL", title=callback_title,
                           description=f"Пропущенный входящий звонок{f' от {name}' if name else ''}"
                                       f"{f' на линию {call.line_number}' if call.line_number else ''}.",
                           responsible_user_id=responsible, due_at=now() + timedelta(minutes=minutes), priority="HIGH",
                           status="OPEN")
            db.add(task); await db.flush()
            call.task_id = task.id
            if deal:
                activity(db, deal, None, "TASK_CREATED", {"task_id": task.id, "title": task.title}, touch=False)
    targets = [responsible] if responsible else [uid for uid, u in people.items() if u.role in {"client_owner", "sales_head"}]
    notify_direct(db, call.workspace_id, targets, "Пропущенный звонок",
                  f"{name + ' · ' if name else ''}{pretty_phone(digits)} — перезвоните клиенту", people)


async def handle(db: AsyncSession, conn: TelephonyConnection, kind: str, data: dict) -> Call | None:
    """Process one webhook event. Commits."""
    async with lock_for(conn.id):
        handler = {"call": on_call_event, "summary": on_summary, "recording": on_recording}.get(kind)
        call = await handler(db, conn, data) if handler else None
        conn.last_event_at = now()
        if conn.status != "connected":
            conn.status, conn.last_error = "connected", None
        await db.commit()
        flush_telegram(db)
        return call


async def link_inbound(db: AsyncSession, inbound: CrmInbound, contact_id: int, deal: CrmDeal) -> None:
    """A call-made request was accepted: its calls follow the contact and deal (and land in the timeline)."""
    calls = (await db.scalars(select(Call).where(Call.project_id == inbound.project_id, Call.inbound_id == inbound.id))).all()
    for call in calls:
        call.contact_id, call.deal_id = contact_id, deal.id
    if calls:
        await db.execute(CrmActivity.__table__.update().where(
            CrmActivity.inbound_id == inbound.id, CrmActivity.deal_id.is_(None),
            CrmActivity.event_type == "CALL_LOGGED").values(deal_id=deal.id))
        for task in (await db.scalars(select(CrmTask).where(CrmTask.id.in_([c.task_id for c in calls if c.task_id]))) ).all():
            task.deal_id, task.contact_id = deal.id, contact_id


# --------------------------------------------------------------------------- recordings

def recordings_root() -> Path:
    return Path(settings.recordings_dir)


def recording_file(call: Call) -> Path | None:
    if not call.recording_path:
        return None
    path = (recordings_root() / call.recording_path).resolve()
    root = recordings_root().resolve()
    return path if root in path.parents and path.is_file() else None


async def fetch_recording(db: AsyncSession, call: Call) -> Path:
    """Download the record from the PBX into local storage. Does not commit."""
    conn = await db.get(TelephonyConnection, call.connection_id)
    if not conn or not call.recording_id:
        raise TelephonyError("Запись звонка недоступна")
    content = await client(conn).recording(call.recording_id)
    if len(content) < 100:
        raise TelephonyError("Mango Office вернул пустую запись")
    relative = Path(str(call.project_id)) / f"{call.id}.mp3"
    path = recordings_root() / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_bytes, content)
    call.recording_path, call.recording_error = str(relative), None
    return path


async def download_pending(db: AsyncSession, limit: int = 10) -> int:
    rows = (await db.scalars(select(Call).where(Call.recording_id.is_not(None), Call.recording_path.is_(None),
                                                Call.recording_deleted_at.is_(None), Call.recording_error.is_(None),
                                                Call.started_at > now() - timedelta(days=7))
                             .order_by(Call.id).limit(limit))).all()
    done = 0
    for call in rows:
        try:
            await fetch_recording(db, call); done += 1
        except TelephonyError as exc:
            tries = int((call.meta or {}).get("record_tries") or 0) + 1
            call.meta = {**(call.meta or {}), "record_tries": tries}
            if tries >= 5:
                call.recording_error = str(exc)[:300]
        await db.commit()
    return done


async def purge_expired(db: AsyncSession) -> int:
    """Delete recordings older than the connection's retention (3 months by default)."""
    rows = (await db.execute(select(Call, TelephonyConnection.retention_days).join(
        TelephonyConnection, TelephonyConnection.id == Call.connection_id).where(
        Call.recording_deleted_at.is_(None), or_(Call.recording_path.is_not(None), Call.recording_id.is_not(None)),
        Call.started_at < now() - timedelta(days=30)).limit(500))).all()
    purged = 0
    for call, days in rows:
        if aware(call.started_at) > now() - timedelta(days=days or 90):
            continue
        path = recording_file(call)
        if path:
            await asyncio.to_thread(path.unlink, True)
        call.recording_path, call.recording_deleted_at = None, now()
        purged += 1
    if purged:
        await db.commit()
    return purged


async def close_stale(db: AsyncSession) -> None:
    """A summary never came (lost webhook): close the live state so it does not hang as «звонит»."""
    rows = (await db.scalars(select(Call).where(Call.processed.is_(False), Call.started_at < now() - timedelta(hours=2)).limit(200))).all()
    for call in rows:
        call.processed = True
        call.status = "answered" if call.answered_at else "missed"
        call.ended_at = call.ended_at or call.started_at
    if rows:
        await db.commit()


async def run_worker() -> None:
    from app.db import SessionLocal
    await asyncio.sleep(40)
    while True:
        try:
            async with SessionLocal() as db:
                await download_pending(db)
                await purge_expired(db)
                await close_stale(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("telephony worker iteration failed")
        await asyncio.sleep(WORKER_SECONDS)
