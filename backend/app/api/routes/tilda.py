"""Authenticated Tilda form adapter over the existing CRM inbound queue."""
import hmac
import logging
import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.api.routes.ads import require_manage_integrations
from app.api.routes.portal import InboundLead
from app.api.routes.result import scoped_project
from app.core.access import check_origin, token_digest
from app.core.permissions import require_actor_permission
from app.db import get_db
from app.models.marketing import LeadInboundSource, Project
from app.models.crm import CrmInbound
from app.models.tilda import TildaConnection, TildaReceipt
from app.models.website import WebsiteSite
from app.services.inbound_lead import create_inbound
from app.services.notifications import discard_telegram, flush_telegram

router = APIRouter(prefix="/integrations/tilda", tags=["tilda"])
SECRET_HEADER = "X-StepToLead-Tilda-Secret"
logger = logging.getLogger("uvicorn.error.tilda")


class ConnectTilda(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int = Field(gt=0)
    website_id: int = Field(gt=0)


class UpdateTilda(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_active: bool


def _public(row: TildaConnection) -> dict:
    return {"id": row.id, "public_id": row.public_id, "organization_id": row.organization_id,
            "project_id": row.project_id, "website_id": row.website_id, "is_active": row.is_active,
            "last_received_at": row.last_received_at, "created_at": row.created_at,
            "updated_at": row.updated_at, "form_id": row.allowed_form_id, "form_name": row.form_name,
            "secret_header": SECRET_HEADER, "webhook_path": f"/api/integrations/tilda/{row.public_id}"}


async def _managed(db: AsyncSession, request: Request, row: TildaConnection) -> None:
    project, kind, user = await scoped_project(db, request, row.project_id)
    if project.workspace_id != row.organization_id:
        raise HTTPException(404, "Подключение не найдено")
    await require_manage_integrations(db, project, kind, user)


@router.get("/connections")
async def list_connections(project_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    project, kind, user = await scoped_project(db, request, project_id)
    require_actor_permission(kind, user, "view_analytics")
    rows = (await db.scalars(select(TildaConnection).where(TildaConnection.project_id == project.id,
        TildaConnection.organization_id == project.workspace_id).order_by(TildaConnection.id))).all()
    return [_public(row) for row in rows]


@router.post("/connections", status_code=201)
async def create_connection(payload: ConnectTilda, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    project, kind, user = await scoped_project(db, request, payload.project_id)
    await require_manage_integrations(db, project, kind, user)
    site = await db.get(WebsiteSite, payload.website_id)
    if not site or site.project_id != project.id or site.workspace_id != project.workspace_id:
        raise HTTPException(404, "Сайт не найден в этом проекте")
    secret = secrets.token_urlsafe(32)
    source_token = secrets.token_urlsafe(48)
    source = LeadInboundSource(workspace_id=project.workspace_id, project_id=project.id,
        name=f"Tilda: {site.name}"[:180], token_hash=token_digest(source_token),
        token_prefix=source_token[:12], active=True, auto_assign=True)
    db.add(source)
    await db.flush()
    row = TildaConnection(public_id=secrets.token_urlsafe(24), secret_hash=token_digest(secret),
        organization_id=project.workspace_id, project_id=project.id, website_id=site.id,
        inbound_source_id=source.id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {**_public(row), "secret": secret}


@router.post("/connections/{connection_id}/rotate-secret")
async def rotate_secret(connection_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    row = await db.get(TildaConnection, connection_id)
    if not row:
        raise HTTPException(404, "Подключение не найдено")
    await _managed(db, request, row)
    secret = secrets.token_urlsafe(32)
    row.secret_hash = token_digest(secret)
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {**_public(row), "secret": secret}


@router.patch("/connections/{connection_id}")
async def update_connection(connection_id: int, payload: UpdateTilda, request: Request,
                            db: AsyncSession = Depends(get_db)):
    check_origin(request)
    row = await db.get(TildaConnection, connection_id)
    if not row:
        raise HTTPException(404, "Подключение не найдено")
    await _managed(db, request, row)
    row.is_active = payload.is_active
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return _public(row)


def _fields(form) -> dict[str, str]:
    result = {}
    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            raise HTTPException(422, "Файлы не поддерживаются")
        normalized = re.sub(r"[^a-z0-9а-я]", "", key.casefold())
        if normalized and normalized not in result:
            result[normalized] = str(value).strip()
    return result


def _value(fields: dict[str, str], *names: str) -> str | None:
    return next((fields[name] for name in names if fields.get(name)), None)


def _value_like(fields: dict[str, str], parts: tuple[str, ...], skip: tuple[str, ...] = ()) -> str | None:
    """Fallback for Tilda variable names generated from field titles, e.g. "Как_удобнее_связаться?"."""
    return next((value for key, value in fields.items()
                 if value and any(part in key for part in parts) and not any(bad in key for bad in skip)), None)


CONTACT_METHOD_FIELDS = ("contactmethod", "способсвязи", "какнастроитьконтакт", "какудобнеесвязаться",
                         "каксвамисвязаться", "удобныйспособсвязи", "messenger", "мессенджер")
WEBSITE_FIELDS = ("website", "site", "сайт", "ссылканасайт", "сайткомпании", "адрессайта")


def _response(inbound: CrmInbound, duplicate: bool) -> dict:
    return {"ok": True, "lead_id": inbound.id, "inbound_id": inbound.id,
            "duplicate": duplicate, "project_id": inbound.project_id}


async def _receipt_inbound(db, connection_id, tranid, organization_id, project_id):
    receipt = await db.scalar(select(TildaReceipt).where(
        TildaReceipt.tilda_connection_id == connection_id, TildaReceipt.tranid == tranid))
    if receipt is None:
        return None
    inbound = await db.get(CrmInbound, receipt.inbound_id)
    if inbound is None or (inbound.workspace_id, inbound.project_id) != (organization_id, project_id):
        raise HTTPException(500, "Запись обработки Tilda не соответствует заявке CRM")
    return inbound


@router.post("/{token}")
async def receive_tilda(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        response = await _receive_tilda(token, request, db)
        logger.info("tilda accepted integration_id=%s tranid=%s organization_id=%s project_id=%s response=%s",
                    getattr(request.state, "tilda_id", None), getattr(request.state, "tilda_tranid", None),
                    getattr(request.state, "tilda_organization", None), getattr(request.state, "tilda_project", None),
                    response if isinstance(response, dict) else "probe ok")
        return response
    except HTTPException as exc:
        await db.rollback()
        logger.warning("tilda rejected integration_id=%s tranid=%s status=%s error=%s",
                       getattr(request.state, "tilda_id", None), getattr(request.state, "tilda_tranid", None),
                       exc.status_code, exc.detail)
        raise
    except Exception:
        await db.rollback()
        logger.exception("tilda failed integration_id=%s tranid=%s organization_id=%s project_id=%s",
                         getattr(request.state, "tilda_id", None), getattr(request.state, "tilda_tranid", None),
                         getattr(request.state, "tilda_organization", None), getattr(request.state, "tilda_project", None))
        raise HTTPException(500, "Не удалось записать заявку Tilda в CRM; ошибка сохранена в серверном логе") from None


async def _receive_tilda(public_id: str, request: Request, db: AsyncSession):
    # Serialize deliveries for a connection, including receipt creation, in PostgreSQL.
    row = await db.scalar(select(TildaConnection).where(TildaConnection.public_id == public_id).with_for_update())
    if not row or not row.is_active:
        raise HTTPException(404, "Подключение не найдено или выключено")
    request.state.tilda_id = row.id
    request.state.tilda_organization = row.organization_id
    request.state.tilda_project = row.project_id
    supplied = request.headers.get(SECRET_HEADER, "")
    if not supplied or len(supplied) > 512 or not hmac.compare_digest(token_digest(supplied), row.secret_hash):
        raise HTTPException(401, "Неверный секрет")
    media_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if media_type not in {"application/x-www-form-urlencoded", "multipart/form-data"}:
        raise HTTPException(415, "Требуется form-urlencoded или multipart/form-data")
    if len(await request.body()) > 65536:
        raise HTTPException(413, "Заявка слишком большая")
    form = await request.form(max_files=0, max_fields=100, max_part_size=65536)
    fields = _fields(form)
    request.state.tilda_tranid = _value(fields, "tranid", "transactionid", "externalid")
    if fields.get("test", "").casefold() == "test":
        return PlainTextResponse("ok")
    form_id = _value(fields, "formid", "blockid")
    if form_id and form_id.isdigit():
        form_id = f"form{form_id}"
    if form_id != row.allowed_form_id:
        raise HTTPException(422, "Форма не разрешена для подключения")
    tranid = _value(fields, "tranid", "transactionid", "externalid")
    if not tranid or len(tranid) > 180:
        raise HTTPException(422, "Отсутствует tranid")
    contact = _value(fields, "contact", "phone", "tel", "telephone", "email", "почта", "телефон")
    if not contact:
        raise HTTPException(422, "Отсутствует контакт")
    site = await db.get(WebsiteSite, row.website_id)
    project = await db.get(Project, row.project_id)
    source = await db.get(LeadInboundSource, row.inbound_source_id)
    if not source or not source.active:
        raise HTTPException(404, "Источник выключен")
    if not project or project.workspace_id != row.organization_id or not site or (site.workspace_id, site.project_id) != (row.organization_id, row.project_id) or (
        source.workspace_id, source.project_id) != (row.organization_id, row.project_id):
        raise HTTPException(500, "Некорректная привязка интеграции Tilda к организации, проекту или сайту")
    existing = await _receipt_inbound(db, row.id, tranid, row.organization_id, row.project_id)
    if existing:
        return _response(existing, True)
    method = _value(fields, *CONTACT_METHOD_FIELDS) or _value_like(
        fields, ("связ", "contactmethod", "способ"), skip=("соглас", "consent", "agree", "policy"))
    method = method[:120] if method else None
    phone = contact if re.fullmatch(r"[+\d\s()\-]{7,64}", contact) else None
    email = contact if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", contact) else None
    name = _value(fields, "name", "fullname", "имя") or contact
    if len(name) < 2:
        name = contact
    session_key = _value(fields, "websitesessionkey", "sessionkey")
    if session_key and not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", session_key):
        session_key = None
    normalized = {"source": "tilda", "external_source": "tilda", "external_id": tranid,
        "form_id": form_id, "form_name": row.form_name, "name": name, "contact_method": method,
        "contact": contact, "website": (_value(fields, *WEBSITE_FIELDS) or _value_like(fields, ("сайт", "website")) or "")[:500] or None,
        "comment": _value(fields, "comments", "comment", "message", "комментарий"),
        "website_session_key": session_key,
        "utm_source": _value(fields, "utmsource"), "utm_medium": _value(fields, "utmmedium"),
        "utm_campaign": _value(fields, "utmcampaign"), "utm_content": _value(fields, "utmcontent"),
        "utm_term": _value(fields, "utmterm"), "page_url": _value(fields, "pageurl", "page", "formurl", "url"),
        "referer": _value(fields, "referer", "referrer")}
    try:
        payload = InboundLead.model_validate({**normalized, "full_name": name[:180], "phone": phone,
            "email": email, "notes": normalized["comment"], "landing_url": normalized["page_url"],
            "contact_consent": False})
    except ValidationError as exc:
        logger.warning("tilda validation failed integration_id=%s tranid=%s errors=%s",
                       row.id, tranid, exc.errors(include_input=False))
        raise HTTPException(422, "Некорректные поля заявки") from exc
    connection_id, organization_id, project_id = row.id, row.organization_id, row.project_id
    try:
        result = await create_inbound(db, source, payload, site_id=row.website_id,
                                      commit=False, allow_raw_contact=True)
        inbound_id = result.get("inbound_id")
        if inbound_id is None:
            raise HTTPException(409, "Входящая заявка уже обработана")
        inbound = await db.get(CrmInbound, inbound_id)
        if not inbound or (inbound.workspace_id, inbound.project_id) != (organization_id, project_id):
            raise HTTPException(500, "Сервис не создал заявку CRM в нужном проекте")
        db.add(TildaReceipt(tilda_connection_id=row.id, tranid=tranid, inbound_id=inbound_id))
        row.last_received_at = datetime.now(timezone.utc)
        await db.commit()
        flush_telegram(db)
    except IntegrityError:
        await db.rollback()
        discard_telegram(db)
        existing = await _receipt_inbound(db, connection_id, tranid, organization_id, project_id)
        if existing:
            return _response(existing, True)
        raise
    return _response(inbound, result["duplicate"])
