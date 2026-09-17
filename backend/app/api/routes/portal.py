import secrets
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.routes.access import rate_limit
from app.core.access import PORTAL_COOKIE, check_origin, hash_password, portal_session_user, require_admin, require_portal_user, token_digest, verify_password
from app.core.config import settings
from app.db import get_db
from app.models.marketing import (AdConnection, AdHypothesisCampaign, AdMetricDaily, ClientLead, ClientLeadAttribution,
                                  ClientLeadEvent, ClientWorkspace, LeadInboundReceipt, LeadInboundSource,
                                  PortalNotification, PortalSession, PortalUser)

auth_router = APIRouter(prefix="/portal/auth", tags=["portal-access"])
admin_router = APIRouter(prefix="/portal/admin", tags=["portal-admin"], dependencies=[Depends(require_admin)])
portal_router = APIRouter(prefix="/portal", tags=["portal"], dependencies=[Depends(require_portal_user)])
inbound_router = APIRouter(prefix="/portal/inbound", tags=["portal-inbound"])

ROLES = {
    "client_owner": "Собственник",
    "sales_head": "Руководитель продаж",
    "sales_manager": "Менеджер продаж",
    "client_marketer": "Маркетолог клиента",
    "viewer": "Наблюдатель",
}
LEAD_STATUSES = {
    "new": "Новый",
    "contacted": "Связались",
    "qualified": "Квалифицирован",
    "proposal": "Предложение",
    "won": "Сделка",
    "lost": "Отказ",
}


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: int = Field(gt=0)
    username: str = Field(min_length=3, max_length=254)
    display_name: str = Field(min_length=2, max_length=160)
    role: str

    @field_validator("username", "display_name")
    @classmethod
    def trim(cls, value: str):
        return value.strip()

    @field_validator("role")
    @classmethod
    def valid_role(cls, value: str):
        if value not in ROLES:
            raise ValueError("Неизвестная роль")
        return value


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str | None = None
    active: bool | None = None

    @field_validator("role")
    @classmethod
    def valid_update_role(cls, value: str | None):
        if value is not None and value not in ROLES:
            raise ValueError("Неизвестная роль")
        return value


class LeadCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str = Field(min_length=2, max_length=180)
    phone: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=254)
    source: str = Field(default="Вручную", min_length=2, max_length=120)
    assigned_to_id: int | None = Field(default=None, gt=0)
    value: float = Field(default=0, ge=0)
    notes: str | None = Field(default=None, max_length=4000)
    next_action_at: datetime | None = None

    @field_validator("full_name", "source")
    @classmethod
    def trim_lead_required(cls, value: str):
        return value.strip()

    @field_validator("phone", "email", "notes")
    @classmethod
    def trim_lead_optional(cls, value: str | None):
        return value.strip() or None if value is not None else None


class LeadUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str | None = Field(default=None, min_length=2, max_length=180)
    phone: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=254)
    source: str | None = Field(default=None, min_length=2, max_length=120)
    status: str | None = None
    assigned_to_id: int | None = Field(default=None, gt=0)
    value: float | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=4000)
    next_action_at: datetime | None = None

    @field_validator("status")
    @classmethod
    def valid_lead_status(cls, value: str | None):
        if value is not None and value not in LEAD_STATUSES:
            raise ValueError("Неизвестный статус лида")
        return value

    @field_validator("full_name", "phone", "email", "source", "notes")
    @classmethod
    def trim_updated_values(cls, value: str | None):
        return value.strip() or None if value is not None else None


class InboundSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: int = Field(gt=0)
    name: str = Field(min_length=2, max_length=180)
    auto_assign: bool = True

    @field_validator("name")
    @classmethod
    def trim_source_name(cls, value: str):
        return value.strip()


class InboundLead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_id: str | None = Field(default=None, max_length=180)
    full_name: str = Field(min_length=2, max_length=180)
    phone: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=254)
    source: str | None = Field(default=None, max_length=120)
    value: float = Field(default=0, ge=0)
    notes: str | None = Field(default=None, max_length=4000)
    external_campaign_id: str | None = Field(default=None, max_length=180)
    external_ad_id: str | None = Field(default=None, max_length=180)
    utm_source: str | None = Field(default=None, max_length=255)
    utm_medium: str | None = Field(default=None, max_length=255)
    utm_campaign: str | None = Field(default=None, max_length=500)
    utm_content: str | None = Field(default=None, max_length=500)
    utm_term: str | None = Field(default=None, max_length=500)
    landing_url: str | None = Field(default=None, max_length=1500)

    @field_validator("full_name")
    @classmethod
    def trim_inbound_name(cls, value: str):
        return value.strip()

    @field_validator("external_id", "phone", "email", "source", "notes", "external_campaign_id", "external_ad_id",
                     "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "landing_url")
    @classmethod
    def trim_inbound_values(cls, value: str | None):
        return value.strip() or None if value is not None else None


def user_payload(user: PortalUser, workspace_name: str | None = None):
    return {"id": user.id, "workspace_id": user.workspace_id, "workspace_name": workspace_name,
            "username": user.username, "display_name": user.display_name, "role": user.role,
            "role_name": ROLES.get(user.role, user.role), "active": user.active,
            "must_change_password": user.must_change_password, "created_at": user.created_at}


def lead_payload(lead: ClientLead, assignee_name: str | None = None):
    return {"id": lead.id, "workspace_id": lead.workspace_id, "assigned_to_id": lead.assigned_to_id,
            "assigned_to_name": assignee_name, "full_name": lead.full_name, "phone": lead.phone,
            "email": lead.email, "source": lead.source, "status": lead.status,
            "status_name": LEAD_STATUSES.get(lead.status, lead.status), "value": float(lead.value or 0),
            "notes": lead.notes, "next_action_at": lead.next_action_at, "created_at": lead.created_at,
            "updated_at": lead.updated_at}


def inbound_source_payload(source: LeadInboundSource):
    return {"id": source.id, "workspace_id": source.workspace_id, "name": source.name,
            "token_prefix": source.token_prefix, "active": source.active,
            "auto_assign": source.auto_assign, "created_at": source.created_at}


@auth_router.post("/login")
async def login(payload: Login, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    await rate_limit(request, "portal-login", 12, 900)
    username = payload.username.strip().lower()
    user = await db.scalar(select(PortalUser).where(PortalUser.username == username))
    valid = user is not None and user.active and await run_in_threadpool(verify_password, payload.password, user.password_hash)
    if not valid:
        raise HTTPException(401, "Неверный логин или пароль")
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    await db.execute(delete(PortalSession).where(PortalSession.expires_at < now))
    db.add(PortalSession(token_hash=token_digest(token), user_id=user.id, expires_at=now + timedelta(hours=12)))
    await db.commit()
    response.set_cookie(PORTAL_COOKIE, token, httponly=True, secure=settings.session_cookie_secure, samesite="strict", max_age=43200, path="/")
    workspace = await db.get(ClientWorkspace, user.workspace_id)
    return user_payload(user, workspace.name if workspace else None)


@auth_router.get("/me")
async def me(db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    workspace = await db.get(ClientWorkspace, user.workspace_id)
    return user_payload(user, workspace.name if workspace else None)


@auth_router.post("/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    token = request.cookies.get(PORTAL_COOKIE, "")
    if token:
        await db.execute(delete(PortalSession).where(PortalSession.token_hash == token_digest(token)))
        await db.commit()
    response.delete_cookie(PORTAL_COOKIE, path="/", secure=settings.session_cookie_secure, httponly=True, samesite="strict")
    return {"ok": True}


@auth_router.post("/change-password")
async def change_password(payload: PasswordChange, request: Request, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    if not await run_in_threadpool(verify_password, payload.current_password, user.password_hash):
        raise HTTPException(422, "Текущий пароль указан неверно")
    user.password_hash = await run_in_threadpool(hash_password, payload.new_password)
    user.must_change_password = False
    await db.commit()
    return {"ok": True}


@admin_router.get("/users")
async def list_users(workspace_id: int | None = None, db: AsyncSession = Depends(get_db)):
    query = select(PortalUser, ClientWorkspace.name).join(ClientWorkspace).order_by(ClientWorkspace.name, PortalUser.display_name)
    if workspace_id:
        query = query.where(PortalUser.workspace_id == workspace_id)
    rows = (await db.execute(query)).all()
    return [user_payload(user, name) for user, name in rows]


@admin_router.post("/users", status_code=201)
async def create_user(payload: UserCreate, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    if not await db.get(ClientWorkspace, payload.workspace_id):
        raise HTTPException(404, "Компания не найдена")
    username = payload.username.lower()
    if await db.scalar(select(PortalUser.id).where(PortalUser.username == username)):
        raise HTTPException(409, "Пользователь с таким логином уже существует")
    temporary_password = secrets.token_urlsafe(12)
    user = PortalUser(workspace_id=payload.workspace_id, username=username, display_name=payload.display_name,
                      role=payload.role, password_hash=await run_in_threadpool(hash_password, temporary_password))
    db.add(user)
    db.add(PortalNotification(workspace_id=payload.workspace_id, user_id=None, level="success", title="Пользователь добавлен",
                              body=f"{payload.display_name} получил роль «{ROLES[payload.role]}»."))
    await db.commit(); await db.refresh(user)
    return {**user_payload(user), "temporary_password": temporary_password}


@admin_router.patch("/users/{user_id}")
async def update_user(user_id: int, payload: UserUpdate, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    user = await db.get(PortalUser, user_id)
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if payload.role is not None:
        user.role = payload.role
    if payload.active is not None:
        user.active = payload.active
        if not payload.active:
            await db.execute(delete(PortalSession).where(PortalSession.user_id == user.id))
    await db.commit()
    workspace = await db.get(ClientWorkspace, user.workspace_id)
    return user_payload(user, workspace.name if workspace else None)


@admin_router.post("/users/{user_id}/reset-password")
async def reset_user_password(user_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    user = await db.get(PortalUser, user_id)
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    temporary_password = secrets.token_urlsafe(12)
    user.password_hash = await run_in_threadpool(hash_password, temporary_password)
    user.must_change_password = True
    await db.execute(delete(PortalSession).where(PortalSession.user_id == user.id))
    await db.commit()
    return {**user_payload(user), "temporary_password": temporary_password}


@admin_router.get("/lead-sources")
async def list_inbound_sources(workspace_id: int, db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(LeadInboundSource).where(LeadInboundSource.workspace_id == workspace_id)
                             .order_by(LeadInboundSource.created_at.desc()))).all()
    return [inbound_source_payload(row) for row in rows]


@admin_router.post("/lead-sources", status_code=201)
async def create_inbound_source(payload: InboundSourceCreate, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    if not await db.get(ClientWorkspace, payload.workspace_id):
        raise HTTPException(404, "Компания не найдена")
    token = secrets.token_urlsafe(32)
    source = LeadInboundSource(workspace_id=payload.workspace_id, name=payload.name,
                               token_hash=token_digest(token), token_prefix=token[:8],
                               auto_assign=payload.auto_assign)
    db.add(source); await db.commit(); await db.refresh(source)
    return {**inbound_source_payload(source), "webhook_token": token,
            "webhook_path": f"/api/portal/inbound/{token}"}


@admin_router.delete("/lead-sources/{source_id}", status_code=204)
async def revoke_inbound_source(source_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    source = await db.get(LeadInboundSource, source_id)
    if not source:
        raise HTTPException(404, "Источник не найден")
    source.active = False
    await db.commit()


async def crm_lead_or_404(lead_id: int, user: PortalUser, db: AsyncSession) -> ClientLead:
    lead = await db.get(ClientLead, lead_id)
    if not lead or lead.workspace_id != user.workspace_id:
        raise HTTPException(404, "Лид не найден")
    if user.role == "sales_manager" and lead.assigned_to_id != user.id:
        raise HTTPException(403, "Этот лид назначен другому менеджеру")
    return lead


async def validate_assignee(assignee_id: int | None, workspace_id: int, db: AsyncSession) -> PortalUser | None:
    if assignee_id is None:
        return None
    assignee = await db.get(PortalUser, assignee_id)
    if not assignee or assignee.workspace_id != workspace_id or not assignee.active:
        raise HTTPException(422, "Выбранный сотрудник недоступен")
    if assignee.role not in {"sales_manager", "sales_head", "client_owner"}:
        raise HTTPException(422, "Лиды можно назначать сотрудникам отдела продаж")
    return assignee


@inbound_router.post("/{token}", status_code=202)
async def receive_inbound_lead(token: str, payload: InboundLead, request: Request, db: AsyncSession = Depends(get_db)):
    if len(token) < 40 or len(token) > 128:
        raise HTTPException(404, "Источник не найден")
    source = await db.scalar(select(LeadInboundSource).where(LeadInboundSource.token_hash == token_digest(token),
                                                              LeadInboundSource.active.is_(True)))
    if not source:
        raise HTTPException(404, "Источник не найден")
    await rate_limit(request, f"lead-inbound:{source.id}", 120, 60)
    phone_digits = re.sub(r"\D", "", payload.phone or "")
    phone = f"+{phone_digits}" if phone_digits else None
    email = payload.email.lower() if payload.email else None
    if not payload.external_id and not phone and not email:
        raise HTTPException(422, "Укажите external_id, телефон или email")
    if payload.external_id:
        receipt = await db.scalar(select(LeadInboundReceipt).where(LeadInboundReceipt.source_id == source.id,
                                                                    LeadInboundReceipt.external_id == payload.external_id))
        if receipt:
            existing = await db.get(ClientLead, receipt.lead_id)
            return {"ok": True, "lead_id": receipt.lead_id, "duplicate": True,
                    "assigned_to_id": existing.assigned_to_id if existing else None}
    contact_conditions = []
    if phone:
        contact_conditions.append(ClientLead.phone == phone)
    if email:
        contact_conditions.append(func.lower(ClientLead.email) == email)
    existing = await db.scalar(select(ClientLead).where(ClientLead.workspace_id == source.workspace_id,
                                                          or_(*contact_conditions)).order_by(ClientLead.id.desc())) if contact_conditions else None
    if existing:
        db.add(ClientLeadEvent(workspace_id=source.workspace_id, lead_id=existing.id, actor_id=None,
                               event_type="duplicate", description=f"Повторное обращение из источника «{source.name}»."))
        if payload.external_id:
            db.add(LeadInboundReceipt(source_id=source.id, lead_id=existing.id, external_id=payload.external_id))
        await db.commit()
        return {"ok": True, "lead_id": existing.id, "duplicate": True, "assigned_to_id": existing.assigned_to_id}
    assignee = None
    if source.auto_assign:
        managers = list((await db.scalars(select(PortalUser).where(PortalUser.workspace_id == source.workspace_id,
                                                                    PortalUser.active.is_(True),
                                                                    PortalUser.role == "sales_manager")
                                           .order_by(PortalUser.id))).all())
        if not managers:
            managers = list((await db.scalars(select(PortalUser).where(PortalUser.workspace_id == source.workspace_id,
                                                                        PortalUser.active.is_(True),
                                                                        PortalUser.role.in_(["sales_head", "client_owner"]))
                                               .order_by(PortalUser.id))).all())
        if managers:
            last_index = next((index for index, member in enumerate(managers) if member.id == source.last_assigned_to_id), -1)
            assignee = managers[(last_index + 1) % len(managers)]
            source.last_assigned_to_id = assignee.id
    lead = ClientLead(workspace_id=source.workspace_id, assigned_to_id=assignee.id if assignee else None,
                      full_name=payload.full_name or "Без имени", phone=phone, email=email,
                      source=payload.source or source.name, value=payload.value, notes=payload.notes)
    db.add(lead); await db.flush()
    hypothesis_id = None; connection_id = None
    if payload.external_campaign_id:
        matched = (await db.execute(
            select(AdHypothesisCampaign.hypothesis_id, AdHypothesisCampaign.connection_id)
            .join(AdConnection, AdConnection.id == AdHypothesisCampaign.connection_id)
            .where(AdConnection.workspace_id == source.workspace_id,
                   AdHypothesisCampaign.external_campaign_id == payload.external_campaign_id)
            .limit(1)
        )).first()
        if matched:
            hypothesis_id, connection_id = matched
    db.add(ClientLeadAttribution(
        lead_id=lead.id, source_id=source.id, connection_id=connection_id, hypothesis_id=hypothesis_id,
        external_campaign_id=payload.external_campaign_id, external_ad_id=payload.external_ad_id,
        utm_source=payload.utm_source, utm_medium=payload.utm_medium, utm_campaign=payload.utm_campaign,
        utm_content=payload.utm_content, utm_term=payload.utm_term, landing_url=payload.landing_url,
    ))
    db.add(ClientLeadEvent(workspace_id=source.workspace_id, lead_id=lead.id, actor_id=None,
                           event_type="inbound", description=f"Лид автоматически получен из источника «{source.name}»."))
    db.add(LeadInboundReceipt(source_id=source.id, lead_id=lead.id,
                              external_id=payload.external_id or f"lead-{lead.id}"))
    recipients = [assignee] if assignee else list((await db.scalars(select(PortalUser).where(
        PortalUser.workspace_id == source.workspace_id, PortalUser.active.is_(True),
        PortalUser.role.in_(["sales_head", "client_owner"])))).all())
    for recipient in recipients:
        db.add(PortalNotification(workspace_id=source.workspace_id, user_id=recipient.id, level="success",
                                  title="Поступил новый лид", body=f"{lead.full_name} · {lead.source}"))
    await db.commit()
    return {"ok": True, "lead_id": lead.id, "duplicate": False,
            "assigned_to_id": assignee.id if assignee else None}


@portal_router.get("/crm/team")
async def crm_team(db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    members = (await db.scalars(select(PortalUser).where(PortalUser.workspace_id == user.workspace_id,
                                                         PortalUser.active.is_(True),
                                                         PortalUser.role.in_(["sales_manager", "sales_head", "client_owner"]))
                                .order_by(PortalUser.display_name))).all()
    return [user_payload(member) for member in members]


@portal_router.get("/crm/leads")
async def crm_leads(db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    query = (select(ClientLead, PortalUser.display_name)
             .outerjoin(PortalUser, ClientLead.assigned_to_id == PortalUser.id)
             .where(ClientLead.workspace_id == user.workspace_id)
             .order_by(ClientLead.updated_at.desc(), ClientLead.id.desc()))
    if user.role == "sales_manager":
        query = query.where(ClientLead.assigned_to_id == user.id)
    rows = (await db.execute(query)).all()
    return [lead_payload(lead, assignee_name) for lead, assignee_name in rows]


@portal_router.post("/crm/leads", status_code=201)
async def create_crm_lead(payload: LeadCreate, request: Request, db: AsyncSession = Depends(get_db),
                          user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    if user.role not in {"client_owner", "sales_head"}:
        raise HTTPException(403, "Создавать лиды может собственник или руководитель продаж")
    assignee = await validate_assignee(payload.assigned_to_id, user.workspace_id, db)
    lead = ClientLead(workspace_id=user.workspace_id, assigned_to_id=payload.assigned_to_id,
                      full_name=payload.full_name, phone=payload.phone, email=payload.email,
                      source=payload.source, value=payload.value, notes=payload.notes,
                      next_action_at=payload.next_action_at)
    db.add(lead); await db.flush()
    db.add(ClientLeadEvent(workspace_id=user.workspace_id, lead_id=lead.id, actor_id=user.id,
                           event_type="created", description=f"Лид создан. Источник: {payload.source}."))
    recipients: list[PortalUser] = []
    if assignee:
        recipients = [assignee]
    else:
        recipients = list((await db.scalars(select(PortalUser).where(PortalUser.workspace_id == user.workspace_id,
                                                                      PortalUser.active.is_(True),
                                                                      PortalUser.role.in_(["client_owner", "sales_head"])))).all())
    for recipient in recipients:
        db.add(PortalNotification(workspace_id=user.workspace_id, user_id=recipient.id, level="success",
                                  title="Новый лид", body=f"{payload.full_name} · {payload.source}"))
    await db.commit(); await db.refresh(lead)
    return lead_payload(lead, assignee.display_name if assignee else None)


@portal_router.patch("/crm/leads/{lead_id}")
async def update_crm_lead(lead_id: int, payload: LeadUpdate, request: Request, db: AsyncSession = Depends(get_db),
                          user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    if user.role in {"viewer", "client_marketer"}:
        raise HTTPException(403, "У вашей роли нет права изменять лиды")
    lead = await crm_lead_or_404(lead_id, user, db)
    changes: list[str] = []
    assignee_name: str | None = None
    supplied = payload.model_fields_set
    if payload.full_name is not None and payload.full_name != lead.full_name:
        lead.full_name = payload.full_name; changes.append("Обновлено имя контакта")
    if "phone" in supplied and payload.phone != lead.phone:
        lead.phone = payload.phone; changes.append("Обновлён телефон")
    if "email" in supplied and payload.email != lead.email:
        lead.email = payload.email.lower() if payload.email else None; changes.append("Обновлён email")
    if payload.source is not None and payload.source != lead.source:
        lead.source = payload.source; changes.append("Обновлён источник")
    if "assigned_to_id" in supplied:
        if user.role not in {"client_owner", "sales_head"}:
            raise HTTPException(403, "Назначать менеджеров может руководитель продаж или собственник")
        assignee = await validate_assignee(payload.assigned_to_id, user.workspace_id, db)
        lead.assigned_to_id = payload.assigned_to_id
        assignee_name = assignee.display_name if assignee else None
        changes.append(f"Назначен сотрудник: {assignee_name or 'не назначен'}")
        if assignee:
            db.add(PortalNotification(workspace_id=user.workspace_id, user_id=assignee.id, level="success",
                                      title="Вам назначен лид", body=f"{lead.full_name} · {lead.source}"))
    elif lead.assigned_to_id:
        assignee = await db.get(PortalUser, lead.assigned_to_id)
        assignee_name = assignee.display_name if assignee else None
    if payload.status is not None and payload.status != lead.status:
        lead.status = payload.status; changes.append(f"Статус: {LEAD_STATUSES[payload.status]}")
    if payload.value is not None:
        lead.value = payload.value; changes.append("Обновлена сумма сделки")
    if "notes" in supplied:
        lead.notes = payload.notes.strip() or None if payload.notes else None; changes.append("Обновлён комментарий")
    if "next_action_at" in supplied:
        lead.next_action_at = payload.next_action_at; changes.append("Обновлён срок следующего действия")
    if changes:
        db.add(ClientLeadEvent(workspace_id=user.workspace_id, lead_id=lead.id, actor_id=user.id,
                               event_type="updated", description="; ".join(changes)))
    await db.commit(); await db.refresh(lead)
    return lead_payload(lead, assignee_name)


@portal_router.get("/crm/leads/{lead_id}/events")
async def crm_lead_events(lead_id: int, db: AsyncSession = Depends(get_db),
                          user: PortalUser = Depends(require_portal_user)):
    await crm_lead_or_404(lead_id, user, db)
    rows = (await db.execute(select(ClientLeadEvent, PortalUser.display_name)
                             .outerjoin(PortalUser, ClientLeadEvent.actor_id == PortalUser.id)
                             .where(ClientLeadEvent.lead_id == lead_id,
                                    ClientLeadEvent.workspace_id == user.workspace_id)
                             .order_by(ClientLeadEvent.created_at.desc()))).all()
    return [{"id": event.id, "event_type": event.event_type, "description": event.description,
             "actor_name": actor_name, "created_at": event.created_at} for event, actor_name in rows]


@portal_router.get("/crm/leads/{lead_id}/attribution")
async def crm_lead_attribution(lead_id: int, db: AsyncSession = Depends(get_db),
                               user: PortalUser = Depends(require_portal_user)):
    await crm_lead_or_404(lead_id, user, db)
    row = await db.scalar(select(ClientLeadAttribution).where(ClientLeadAttribution.lead_id == lead_id))
    if not row:
        return None
    return {"external_campaign_id": row.external_campaign_id, "external_ad_id": row.external_ad_id,
            "utm_source": row.utm_source, "utm_medium": row.utm_medium, "utm_campaign": row.utm_campaign,
            "utm_content": row.utm_content, "utm_term": row.utm_term, "landing_url": row.landing_url,
            "hypothesis_id": row.hypothesis_id, "connection_id": row.connection_id}


@portal_router.get("/dashboard")
async def dashboard(db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    workspace = await db.get(ClientWorkspace, user.workspace_id)
    connection_ids = select(AdConnection.id).where(AdConnection.workspace_id == user.workspace_id)
    totals = (await db.execute(select(func.coalesce(func.sum(AdMetricDaily.spend), 0),
                                      func.coalesce(func.sum(AdMetricDaily.impressions), 0),
                                      func.coalesce(func.sum(AdMetricDaily.clicks), 0),
                                      func.coalesce(func.sum(AdMetricDaily.leads), 0))
                               .where(AdMetricDaily.connection_id.in_(connection_ids)))).one()
    connections = await db.scalar(select(func.count(AdConnection.id)).where(AdConnection.workspace_id == user.workspace_id)) or 0
    applications = await db.scalar(
        select(func.count(func.distinct(LeadInboundReceipt.lead_id)))
        .join(LeadInboundSource, LeadInboundSource.id == LeadInboundReceipt.source_id)
        .where(LeadInboundSource.workspace_id == user.workspace_id)
    ) or 0
    team = await db.scalar(select(func.count(PortalUser.id)).where(PortalUser.workspace_id == user.workspace_id, PortalUser.active.is_(True))) or 0
    crm_scope = [ClientLead.workspace_id == user.workspace_id]
    if user.role == "sales_manager":
        crm_scope.append(ClientLead.assigned_to_id == user.id)
    new_leads = await db.scalar(select(func.count(ClientLead.id)).where(*crm_scope, ClientLead.status == "new")) or 0
    in_progress = await db.scalar(select(func.count(ClientLead.id)).where(*crm_scope, ClientLead.status.in_(["contacted", "qualified", "proposal"]))) or 0
    deals = await db.scalar(select(func.count(ClientLead.id)).where(*crm_scope, ClientLead.status == "won")) or 0
    overdue = await db.scalar(select(func.count(ClientLead.id)).where(*crm_scope, ClientLead.next_action_at < datetime.now(timezone.utc),
                                                                      ClientLead.status.notin_(["won", "lost"]))) or 0
    return {"user": user_payload(user, workspace.name if workspace else None),
            "marketing": {"spend": float(totals[0]), "impressions": int(totals[1]), "clicks": int(totals[2]),
                          "applications": applications,
                          "cpc": float(totals[0]) / int(totals[2]) if totals[2] else None,
                          "conversion_rate": applications / int(totals[2]) * 100 if totals[2] else None,
                          "cost_per_application": float(totals[0]) / applications if applications else None,
                          "connections": connections},
            "sales": {"new_leads": new_leads, "in_progress": in_progress, "deals": deals, "overdue": overdue}, "team_count": team, "brief_progress": 0}


@portal_router.get("/notifications")
async def notifications(db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    rows = (await db.scalars(select(PortalNotification).where(PortalNotification.workspace_id == user.workspace_id,
                                                               (PortalNotification.user_id.is_(None) | (PortalNotification.user_id == user.id)))
                             .order_by(PortalNotification.created_at.desc()).limit(30))).all()
    return rows


@portal_router.post("/notifications/{notification_id}/read")
async def read_notification(notification_id: int, request: Request, db: AsyncSession = Depends(get_db),
                            user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    row = await db.get(PortalNotification, notification_id)
    if not row or row.workspace_id != user.workspace_id or row.user_id not in {None, user.id}:
        raise HTTPException(404, "Уведомление не найдено")
    row.read_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True}
