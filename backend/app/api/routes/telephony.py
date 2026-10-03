"""Telephony API: PBX connection settings, Mango webhooks, call log, click-to-call, recordings."""
import secrets
from datetime import timedelta
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.crm import deal_for, project_for
from app.api.routes.messaging import can_manage_channels
from app.core.access import check_origin, require_portal_user
from app.core.crypto import encrypt_secret
from app.core.permissions import effective_permissions, require_permission
from app.db import get_db
from app.models.crm import CrmContact, CrmDeal, CrmStage, CrmTask
from app.models.marketing import AdConnection, PortalUser, ProjectSource
from app.models.telephony import Call, TelephonyConnection
from app.services import call_ai, telephony
from app.services.messaging import project_people
from app.services.telephony import TelephonyError

webhook_router = APIRouter(prefix="/telephony", tags=["telephony-webhooks"])
router = APIRouter(prefix="/crm", tags=["telephony"], dependencies=[Depends(require_portal_user)])


# --------------------------------------------------------------------------- webhooks (public, signed)

@webhook_router.get("/mango/{public_id}")
@webhook_router.get("/mango/{public_id}/{path:path}")
async def mango_ping(public_id: str, path: str = ""):
    return {"ok": True}


@webhook_router.post("/mango/{public_id}")
@webhook_router.post("/mango/{public_id}/{path:path}")
async def mango_webhook(public_id: str, request: Request, path: str = "", db: AsyncSession = Depends(get_db)):
    conn = await db.scalar(select(TelephonyConnection).where(TelephonyConnection.public_id == public_id,
                                                             TelephonyConnection.provider == "mango"))
    if not conn or not conn.active:
        raise HTTPException(404, "Not found")
    raw = (await request.body()).decode("utf-8", "replace")
    form = {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}
    try:
        data = telephony.Mango.for_connection(conn).verify(form)
    except TelephonyError as exc:
        raise HTTPException(403, str(exc)) from None
    parts = [p for p in path.strip("/").split("/") if p]
    kind = parts[-1] if len(parts) >= 2 and parts[0] == "events" else ""
    await telephony.handle(db, conn, kind, data)
    return {"ok": True}


# --------------------------------------------------------------------------- settings

def connection_json(conn: TelephonyConnection, manage: bool) -> dict:
    data = conn.config or {}
    result = {"id": conn.id, "provider": conn.provider, "provider_name": telephony.PROVIDERS.get(conn.provider, conn.provider),
              "name": conn.name, "active": conn.active, "status": conn.status, "last_error": conn.last_error,
              "last_event_at": conn.last_event_at, "retention_days": conn.retention_days,
              "create_leads": data.get("create_leads", True),
              "missed_task_minutes": int(data.get("missed_task_minutes") or telephony.MISSED_TASK_MINUTES)}
    if manage:
        result.update({"webhook_path": f"/api/telephony/{conn.provider}/{conn.public_id}",
                       "pbx_users": data.get("pbx_users") or [], "user_map": data.get("user_map") or {}})
    result["line_map"] = data.get("line_map") or {}
    return result


async def tracking_options(db: AsyncSession, conn: TelephonyConnection) -> dict:
    """Call tracking setup: numbers seen in calls plus the channels a number can be tied to."""
    seen = (await db.scalars(select(Call.line_number).where(Call.connection_id == conn.id, Call.line_number.is_not(None))
                             .distinct().limit(50))).all()
    lines = sorted({telephony.norm_phone(n) for n in seen if telephony.norm_phone(n)} | set((conn.config or {}).get("line_map") or {}))
    ads = (await db.scalars(select(AdConnection).where(AdConnection.project_id == conn.project_id))).all()
    sources = (await db.scalars(select(ProjectSource).where(ProjectSource.project_id == conn.project_id,
                                                            ProjectSource.inbound_source_id.is_(None)))).all()
    return {"lines": lines, "channels": [*({"kind": "ad", "id": a.id, "label": a.name} for a in ads),
                                         *({"kind": "source", "id": s.id, "label": s.name} for s in sources)]}


async def connection_for(db: AsyncSession, user: PortalUser, connection_id: int, manage: bool = False) -> TelephonyConnection:
    conn = await db.get(TelephonyConnection, connection_id)
    if not conn or conn.workspace_id != user.workspace_id:
        raise HTTPException(404, "Подключение телефонии не найдено")
    await project_for(db, user, conn.project_id)
    if manage and not can_manage_channels(user):
        raise HTTPException(403, "Настраивать телефонию может руководитель или владелец")
    return conn


async def project_connection(db: AsyncSession, project_id: int) -> TelephonyConnection | None:
    return await db.scalar(select(TelephonyConnection).where(TelephonyConnection.project_id == project_id)
                           .order_by(TelephonyConnection.id).limit(1))


def auto_map(pbx_users: list[dict], people: dict[int, PortalUser], current: dict) -> dict:
    """Keep the manual mapping; match the rest by the employee's name."""
    taken = {int(v) for v in current.values() if v}
    result = {k: v for k, v in current.items() if any(u["extension"] == k for u in pbx_users)}
    by_name = {u.display_name.strip().lower(): u.id for u in people.values() if u.display_name}
    for row in pbx_users:
        uid = by_name.get(row["name"].strip().lower())
        if row["extension"] not in result and uid and uid not in taken:
            result[row["extension"]] = uid; taken.add(uid)
    return result


@router.get("/projects/{project_id}/telephony")
async def telephony_settings(project_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm"); await project_for(db, user, project_id)
    manage = can_manage_channels(user)
    conn = await project_connection(db, project_id)
    members = []
    if manage:
        members = [{"id": u.id, "name": u.display_name} for u in (await project_people(db, project_id)).values()]
    return {"connection": connection_json(conn, manage) if conn else None, "can_manage": manage, "members": members,
            "tracking": await tracking_options(db, conn) if conn and manage else None,
            "my_extension": telephony.extension_for_user(conn, user.id) if conn else None,
            "providers": [{"code": k, "name": v} for k, v in telephony.PROVIDERS.items()]}


class ConnectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = "mango"
    name: str = Field(default="Mango Office", min_length=2, max_length=180)
    api_key: str = Field(min_length=4, max_length=200)
    api_salt: str = Field(min_length=4, max_length=200)


class ConnectionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=180)
    active: bool | None = None
    api_key: str | None = Field(default=None, min_length=4, max_length=200)
    api_salt: str | None = Field(default=None, min_length=4, max_length=200)
    user_map: dict[str, int | None] | None = None
    create_leads: bool | None = None
    missed_task_minutes: int | None = Field(default=None, ge=1, le=1440)
    line_map: dict[str, dict | None] | None = None  # tracking number → {"kind": "ad"|"source", "id": int}


@router.post("/projects/{project_id}/telephony", status_code=201)
async def connect_telephony(project_id: int, payload: ConnectionIn, request: Request, db: AsyncSession = Depends(get_db),
                            user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    if not can_manage_channels(user):
        raise HTTPException(403, "Подключать телефонию может руководитель или владелец")
    project = await project_for(db, user, project_id)
    if payload.provider not in telephony.PROVIDERS:
        raise HTTPException(422, "Этот провайдер пока не поддерживается")
    if await project_connection(db, project.id):
        raise HTTPException(409, "Телефония в проекте уже подключена — измените ключи в её настройках")
    try:
        pbx_users = await telephony.Mango(payload.api_key, payload.api_salt).users()
    except TelephonyError as exc:
        raise HTTPException(422, str(exc)) from None
    people = await project_people(db, project.id)
    conn = TelephonyConnection(workspace_id=project.workspace_id, project_id=project.id, provider=payload.provider,
                               name=payload.name.strip(), public_id=secrets.token_urlsafe(24), status="connected",
                               api_key_encrypted=encrypt_secret(payload.api_key.strip()),
                               api_salt_encrypted=encrypt_secret(payload.api_salt.strip()), active=True, retention_days=90,
                               config={"pbx_users": pbx_users, "user_map": auto_map(pbx_users, people, {}),
                                       "create_leads": True, "missed_task_minutes": telephony.MISSED_TASK_MINUTES})
    db.add(conn); await db.commit()
    return connection_json(conn, True)


@router.patch("/telephony/{connection_id}")
async def update_telephony(connection_id: int, payload: ConnectionPatch, request: Request, db: AsyncSession = Depends(get_db),
                           user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    conn = await connection_for(db, user, connection_id, manage=True)
    changes = payload.model_dump(exclude_unset=True)
    if "api_key" in changes or "api_salt" in changes:
        current = telephony.Mango.for_connection(conn)
        key, salt = changes.get("api_key") or current.key, changes.get("api_salt") or current.salt
        try:
            pbx_users = await telephony.Mango(key, salt).users()
        except TelephonyError as exc:
            raise HTTPException(422, str(exc)) from None
        conn.api_key_encrypted, conn.api_salt_encrypted = encrypt_secret(key.strip()), encrypt_secret(salt.strip())
        conn.status, conn.last_error = "connected", None
        telephony.set_cfg(conn, pbx_users=pbx_users)
    if changes.get("name"):
        conn.name = changes["name"].strip()
    if changes.get("active") is not None:
        conn.active = changes["active"]
    if payload.user_map is not None:
        people = await project_people(db, conn.project_id)
        extensions = {u["extension"] for u in telephony.cfg(conn).get("pbx_users") or []}
        mapping, seen = {}, set()
        for extension, uid in payload.user_map.items():
            if not uid:
                continue
            if extension not in extensions:
                raise HTTPException(422, f"Внутреннего номера {extension} нет в АТС — обновите список сотрудников")
            if uid not in people:
                raise HTTPException(422, "Сотрудник не участвует в проекте")
            if uid in seen:
                raise HTTPException(422, "Одному сотруднику можно назначить только один внутренний номер")
            seen.add(uid); mapping[extension] = uid
        telephony.set_cfg(conn, user_map=mapping)
    if payload.line_map is not None:
        options = await tracking_options(db, conn)
        allowed = {(c["kind"], c["id"]): c["label"] for c in options["channels"]}
        routes = {}
        for number, route in payload.line_map.items():
            digits = telephony.norm_phone(number)
            if not digits:
                raise HTTPException(422, f"Некорректный номер: {number}")
            if not route:
                continue
            key = (route.get("kind"), route.get("id"))
            if key not in allowed:
                raise HTTPException(422, "Канал не найден в проекте")
            routes[digits] = {"kind": key[0], "id": key[1], "label": allowed[key]}
        telephony.set_cfg(conn, line_map=routes)
    if payload.create_leads is not None:
        telephony.set_cfg(conn, create_leads=payload.create_leads)
    if payload.missed_task_minutes is not None:
        telephony.set_cfg(conn, missed_task_minutes=payload.missed_task_minutes)
    await db.commit()
    return connection_json(conn, True)


@router.post("/telephony/{connection_id}/sync-users")
async def sync_telephony_users(connection_id: int, request: Request, db: AsyncSession = Depends(get_db),
                               user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    conn = await connection_for(db, user, connection_id, manage=True)
    try:
        pbx_users = await telephony.client(conn).users()
    except TelephonyError as exc:
        conn.status, conn.last_error = "error", str(exc)[:500]
        await db.commit()
        raise HTTPException(422, str(exc)) from None
    people = await project_people(db, conn.project_id)
    telephony.set_cfg(conn, pbx_users=pbx_users, user_map=auto_map(pbx_users, people, telephony.cfg(conn).get("user_map") or {}))
    conn.status, conn.last_error = "connected", None
    await db.commit()
    return connection_json(conn, True)


# --------------------------------------------------------------------------- calls

def visible_calls(query, user: PortalUser):
    if "view_all_deals" in effective_permissions(user):
        return query
    own_deals = select(CrmDeal.id).where(CrmDeal.responsible_user_id == user.id)
    return query.where(or_(Call.user_id == user.id, Call.deal_id.in_(own_deals),
                           (Call.user_id.is_(None) & Call.deal_id.is_(None))))


async def calls_json(db: AsyncSession, rows: list[Call]) -> list[dict]:
    contacts = {c.id: c for c in (await db.scalars(select(CrmContact).where(
        CrmContact.id.in_({r.contact_id for r in rows if r.contact_id})))).all()} if rows else {}
    deals = {d.id: d for d in (await db.scalars(select(CrmDeal).where(
        CrmDeal.id.in_({r.deal_id for r in rows if r.deal_id})))).all()} if rows else {}
    users = {u.id: u for u in (await db.scalars(select(PortalUser).where(
        PortalUser.id.in_({r.user_id for r in rows if r.user_id})))).all()} if rows else {}
    routes = {}
    for conn in (await db.scalars(select(TelephonyConnection).where(
            TelephonyConnection.id.in_({r.connection_id for r in rows})))).all() if rows else []:
        routes[conn.id] = conn
    tasks = {t.id: t for t in (await db.scalars(select(CrmTask).where(
        CrmTask.id.in_({r.task_id for r in rows if r.task_id})))).all()} if rows else {}
    result = []
    for r in rows:
        task = tasks.get(r.task_id)
        result.append({
            "id": r.id, "direction": r.direction, "status": r.status, "phone": r.client_phone, "line_number": r.line_number,
            "extension": r.extension, "user_id": r.user_id, "user_name": users[r.user_id].display_name if r.user_id in users else None,
            "started_at": r.started_at, "answered_at": r.answered_at, "ended_at": r.ended_at,
            "duration_sec": r.duration_sec, "wait_sec": r.wait_sec,
            "contact_id": r.contact_id, "contact_name": contacts[r.contact_id].name if r.contact_id in contacts else None,
            "deal_id": r.deal_id, "deal_name": deals[r.deal_id].name if r.deal_id in deals else None,
            "inbound_id": r.inbound_id,
            "has_recording": bool((r.recording_path or r.recording_id) and not r.recording_deleted_at),
            "recording_deleted": bool(r.recording_deleted_at),
            "callback_status": task.status if task else None, "callback_task_id": r.task_id,
            "channel": (telephony.line_route(routes[r.connection_id], r.line_number) or {}).get("label")
            if r.connection_id in routes else None,
            "ai": call_ai.public(r)})
    return result


def call_stats(rows: list[Call], names: dict[int, str]) -> dict:
    def empty():
        return {"total": 0, "incoming": 0, "outgoing": 0, "answered_in": 0, "missed_in": 0, "answered_out": 0,
                "talk_sec": 0, "wait_sum": 0, "ai_sum": 0, "ai_n": 0}
    total, per_user = empty(), {}
    for r in rows:
        for bucket in (total, per_user.setdefault(r.user_id, empty())):
            bucket["total"] += 1
            if r.direction == "in":
                bucket["incoming"] += 1
                if r.status == "answered":
                    bucket["answered_in"] += 1; bucket["wait_sum"] += r.wait_sec
                elif r.status == "missed":
                    bucket["missed_in"] += 1
            else:
                bucket["outgoing"] += 1
                bucket["answered_out"] += r.status == "answered"
            bucket["talk_sec"] += r.duration_sec
            score = ((r.meta or {}).get("ai") or {}).get("score") if r.ai_status == "done" else None
            if score is not None:
                bucket["ai_sum"] += score; bucket["ai_n"] += 1

    def finish(bucket):
        answered = bucket.pop("wait_sum") / bucket["answered_in"] if bucket["answered_in"] else None
        ai_sum, ai_n = bucket.pop("ai_sum"), bucket.pop("ai_n")
        return {**bucket, "avg_wait_sec": round(answered) if answered is not None else None,
                "ai_score": round(ai_sum / ai_n) if ai_n else None, "ai_calls": ai_n}
    return {"total": finish(total),
            "users": [{"user_id": uid, "name": names.get(uid) or "Не назначен", **finish(b)}
                      for uid, b in sorted(per_user.items(), key=lambda item: -item[1]["total"])]}


@router.get("/projects/{project_id}/calls")
async def call_log(project_id: int, direction: str | None = None, status: str | None = None,
                   user_id: int | None = None, days: int = Query(30, ge=1, le=365), search: str | None = None,
                   page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=200),
                   db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm"); await project_for(db, user, project_id)
    since = telephony.now() - timedelta(days=days)
    base = visible_calls(select(Call).where(Call.project_id == project_id, Call.started_at >= since,
                                            Call.processed.is_(True)), user)
    period = (await db.scalars(base)).all()
    period = [c for c in period if not (c.meta or {}).get("internal")]
    names = {u.id: u.display_name for u in (await db.scalars(select(PortalUser).where(
        PortalUser.id.in_({c.user_id for c in period if c.user_id})))).all()} if period else {}
    query = base
    if direction in {"in", "out"}:
        query = query.where(Call.direction == direction)
    if status in {"answered", "missed"}:
        query = query.where(Call.status == status)
    if status == "callback":
        query = query.where(Call.task_id.in_(select(CrmTask.id).where(CrmTask.status == "OPEN")))
    if user_id:
        query = query.where(Call.user_id == user_id)
    digits = "".join(ch for ch in (search or "") if ch.isdigit())
    if digits:
        query = query.where(Call.client_phone.contains(digits[-10:]))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (await db.scalars(query.order_by(Call.started_at.desc(), Call.id.desc()).offset((page - 1) * limit).limit(limit))).all()
    open_callbacks = await db.scalar(select(func.count(CrmTask.id)).where(
        CrmTask.id.in_([c.task_id for c in period if c.task_id] or [0]), CrmTask.status == "OPEN"))
    return {"items": await calls_json(db, list(rows)), "total": total, "page": page,
            "stats": {**call_stats(period, names), "open_callbacks": open_callbacks or 0}}


@router.get("/deals/{deal_id}/calls")
async def deal_calls(deal_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm")
    deal = await deal_for(db, user, deal_id)
    rows = (await db.scalars(select(Call).where(Call.deal_id == deal.id, Call.processed.is_(True))
                             .order_by(Call.started_at.desc()).limit(100))).all()
    return await calls_json(db, list(rows))


@router.get("/projects/{project_id}/calls/active")
async def active_calls(project_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    """Incoming calls ringing right now for this user (or not yet routed to anyone): the pop-up card."""
    require_permission(user, "view_crm"); await project_for(db, user, project_id)
    if telephony.dialed_recently(project_id, user.id):
        return []  # the manager's own click-to-call: Mango rings him first, that is not a client call
    rows = (await db.scalars(select(Call).where(
        Call.project_id == project_id, Call.processed.is_(False), Call.direction == "in",
        Call.started_at >= telephony.now() - timedelta(minutes=10),
        or_(Call.user_id == user.id, Call.user_id.is_(None))).order_by(Call.started_at.desc()).limit(3))).all()
    rows = [r for r in rows if not (r.meta or {}).get("internal")]
    items = await calls_json(db, rows)
    for item, row in zip(items, rows):
        if row.deal_id:
            deal = await db.get(CrmDeal, row.deal_id)
            stage = await db.get(CrmStage, deal.stage_id) if deal else None
            owner = await db.get(PortalUser, deal.responsible_user_id) if deal and deal.responsible_user_id else None
            item.update({"stage_name": stage.name if stage else None, "amount": float(deal.amount) if deal and deal.amount else None,
                         "responsible_name": owner.display_name if owner else None})
    return items


class DialIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(min_length=5, max_length=40)
    deal_id: int | None = None


@router.post("/projects/{project_id}/calls/dial")
async def dial(project_id: int, payload: DialIn, request: Request, db: AsyncSession = Depends(get_db),
               user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "view_crm")
    await project_for(db, user, project_id)
    if payload.deal_id:
        await deal_for(db, user, payload.deal_id)
    conn = await project_connection(db, project_id)
    if not conn or not conn.active:
        raise HTTPException(422, "Телефония в проекте не подключена")
    extension = telephony.extension_for_user(conn, user.id)
    if not extension:
        raise HTTPException(422, "Вам не назначен внутренний номер АТС — попросите руководителя указать его в настройках телефонии")
    digits = telephony.norm_phone(payload.phone)
    if not digits:
        raise HTTPException(422, "Некорректный номер телефона")
    try:
        await telephony.client(conn).callback(extension, digits)
    except TelephonyError as exc:
        raise HTTPException(422, str(exc)) from None
    telephony.remember_dial(project_id, user.id)
    return {"ok": True, "message": "Звоним вам на телефон — снимите трубку, затем АТС соединит с клиентом"}


@router.get("/calls/{call_id}/recording")
async def call_recording(call_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm")
    call = await db.get(Call, call_id)
    if not call or call.workspace_id != user.workspace_id:
        raise HTTPException(404, "Звонок не найден")
    await project_for(db, user, call.project_id)
    if not await db.scalar(visible_calls(select(Call.id).where(Call.id == call.id), user)):
        raise HTTPException(403, "Звонок другого сотрудника")
    if call.recording_deleted_at:
        raise HTTPException(410, "Запись удалена по сроку хранения")
    path = telephony.recording_file(call)
    if path is None and call.recording_id:
        try:
            path = await telephony.fetch_recording(db, call)
            await db.commit()
        except TelephonyError as exc:
            raise HTTPException(502, str(exc)) from None
    if path is None:
        raise HTTPException(404, "Записи нет")
    return FileResponse(path, media_type="audio/mpeg", filename=f"call-{call.id}.mp3",
                        headers={"Cache-Control": "private, max-age=3600"})


async def visible_call(db: AsyncSession, user: PortalUser, call_id: int) -> Call:
    call = await db.get(Call, call_id)
    if not call or call.workspace_id != user.workspace_id:
        raise HTTPException(404, "Звонок не найден")
    await project_for(db, user, call.project_id)
    if not await db.scalar(visible_calls(select(Call.id).where(Call.id == call.id), user)):
        raise HTTPException(403, "Звонок другого сотрудника")
    return call


@router.get("/calls/{call_id}/ai")
async def call_analysis(call_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    """Full AI analysis of a call: summary, checklist, advice and the transcript."""
    require_permission(user, "view_crm")
    call = await visible_call(db, user, call_id)
    return {"call": (await calls_json(db, [call]))[0], "analysis": call_ai.public(call, full=True),
            "available": call_ai.available()}


@router.post("/calls/{call_id}/ai")
async def queue_call_analysis(call_id: int, request: Request, db: AsyncSession = Depends(get_db),
                              user: PortalUser = Depends(require_portal_user)):
    """Analyze (or re-analyze) a call on demand; the worker picks it up within a minute."""
    check_origin(request); require_permission(user, "view_crm")
    call = await visible_call(db, user, call_id)
    if not call_ai.available():
        raise HTTPException(422, "Разбор звонков не подключён: нужен ключ ИИ и распознавания речи в настройках сервера")
    if call.status != "answered" or call.duration_sec < 10:
        raise HTTPException(422, "Разбирать можно только состоявшийся разговор")
    if call.recording_deleted_at or not (call.recording_path or call.recording_id):
        raise HTTPException(422, "Записи разговора нет")
    if call.ai_status in {"queued", "stt"}:
        return {"status": call.ai_status}
    if not call.recording_path:
        try:
            await telephony.fetch_recording(db, call)
        except TelephonyError as exc:
            raise HTTPException(502, str(exc)) from None
    call.meta = {**(call.meta or {}), "ai": {}}
    call.ai_status = "queued"
    await db.commit()
    return {"status": "queued"}
