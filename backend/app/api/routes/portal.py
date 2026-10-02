import secrets
import re
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.routes.access import rate_limit
from app.core.access import PORTAL_COOKIE, check_origin, hash_password, portal_session_user, require_admin, require_portal_user, token_digest, verify_password
from app.core.config import settings
from app.core.permissions import effective_permissions, require_permission
from app.db import get_db
from app.models.marketing import (AdConnection, AdHypothesisCampaign, AdMetricDaily, ClientLead, ClientLeadAttribution, ClientSale, Project, PortalProjectAccess,
                                  ClientLeadEvent, ClientWorkspace, LeadInboundReceipt, LeadInboundSource,
                                  PortalNotification, PortalSession, PortalUser, ProjectSource, ProjectLostReason)
from app.services.project_scope import default_project
from app.api.routes.settings import in_app_rule_enabled, reasons as project_reasons
from app.models.crm import CrmInbound, CrmActivity, CrmContact, CrmDeal, CrmStage, CrmStageHistory
from app.models.website import WebsiteSession
from app.services.inbound_lead import create_inbound

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
    manage_sources: bool = False
    manage_integrations: bool = False

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
    manage_sources: bool | None = None
    manage_integrations: bool | None = None

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
    telegram: str | None = Field(default=None, max_length=120)
    source: str = Field(default="Вручную", min_length=2, max_length=120)
    assigned_to_id: int | None = Field(default=None, gt=0)
    value: float | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=4000)
    next_action_at: datetime | None = None
    project_id: int | None = Field(default=None, gt=0)

    @field_validator("full_name", "source")
    @classmethod
    def trim_lead_required(cls, value: str):
        return value.strip()

    @field_validator("phone", "email", "telegram", "notes")
    @classmethod
    def trim_lead_optional(cls, value: str | None):
        return value.strip() or None if value is not None else None


class LeadUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str | None = Field(default=None, min_length=2, max_length=180)
    phone: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=254)
    telegram: str | None = Field(default=None, max_length=120)
    source: str | None = Field(default=None, min_length=2, max_length=120)
    status: str | None = None
    assigned_to_id: int | None = Field(default=None, gt=0)
    value: float | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=4000)
    next_action_at: datetime | None = None
    meeting_at: datetime | None = None

    @field_validator("status")
    @classmethod
    def valid_lead_status(cls, value: str | None):
        if value is not None and value not in LEAD_STATUSES:
            raise ValueError("Неизвестный статус лида")
        return value

    @field_validator("full_name", "phone", "email", "telegram", "source", "notes")
    @classmethod
    def trim_updated_values(cls, value: str | None):
        return value.strip() or None if value is not None else None


class SaleCreate(BaseModel):
    amount: float = Field(gt=0, le=1e12)
    occurred_at: datetime | None = None
    comment: str | None = Field(default=None, max_length=4000)


class LostCreate(BaseModel):
    reason: str
    detail: str | None = Field(default=None, max_length=4000)


class CommentCreate(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


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
    model_config = ConfigDict(extra="allow")
    external_id: str | None = Field(default=None, max_length=180)
    full_name: str = Field(min_length=2, max_length=180)
    phone: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=254)
    source: str | None = Field(default=None, max_length=120)
    value: float | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=4000)
    external_campaign_id: str | None = Field(default=None, max_length=180)
    external_ad_id: str | None = Field(default=None, max_length=180)
    utm_source: str | None = Field(default=None, max_length=255)
    utm_medium: str | None = Field(default=None, max_length=255)
    utm_campaign: str | None = Field(default=None, max_length=500)
    utm_content: str | None = Field(default=None, max_length=500)
    utm_term: str | None = Field(default=None, max_length=500)
    landing_url: str | None = Field(default=None, max_length=1500)
    yclid: str | None = Field(default=None, max_length=255)
    gclid: str | None = Field(default=None, max_length=255)
    contact_consent: bool = False
    website_session_key: str | None = Field(default=None, min_length=16, max_length=64)

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
            "must_change_password": user.must_change_password, "manage_sources": user.manage_sources,
            "manage_integrations": user.manage_integrations,
            "phone": user.phone, "permissions": sorted(effective_permissions(user)),
            "status": "blocked" if not user.active else "invited" if user.must_change_password else "active",
            "last_activity_at": user.last_activity_at, "created_at": user.created_at}


def lead_payload(lead: ClientLead, assignee_name: str | None = None):
    return {"id": lead.id, "workspace_id": lead.workspace_id, "assigned_to_id": lead.assigned_to_id,
            "assigned_to_name": assignee_name, "full_name": lead.full_name, "phone": lead.phone,
            "email": lead.email, "telegram": lead.telegram, "project_id": lead.project_id,
            "lost_reason": lead.lost_reason, "source": lead.source, "status": lead.status,
            "meeting_at": lead.meeting_at,
            "status_name": LEAD_STATUSES.get(lead.status, lead.status), "value": float(lead.value) if lead.value is not None else None,
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
    user.last_activity_at = now
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
                      role=payload.role, manage_sources=payload.manage_sources,
                      manage_integrations=payload.manage_integrations,
                      password_hash=await run_in_threadpool(hash_password, temporary_password))
    db.add(user)
    await db.flush()
    project = await default_project(db, payload.workspace_id)
    db.add(PortalProjectAccess(user_id=user.id, project_id=project.id, manage_sources=payload.manage_sources,
                               manage_integrations=payload.manage_integrations))
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
    if user.role == "client_owner" and user.active and (payload.role is not None and payload.role != "client_owner" or payload.active is False):
        others = await db.scalar(select(func.count()).select_from(PortalUser).where(
            PortalUser.workspace_id == user.workspace_id, PortalUser.role == "client_owner",
            PortalUser.active.is_(True), PortalUser.id != user.id))
        if not others:
            raise HTTPException(409, "Нельзя заблокировать или разжаловать последнего владельца")
    if payload.role is not None:
        user.role = payload.role
        user.permissions = None
    if payload.manage_sources is not None:
        user.manage_sources = payload.manage_sources
        access_rows = (await db.scalars(select(PortalProjectAccess).where(PortalProjectAccess.user_id == user.id))).all()
        for access in access_rows:
            access.manage_sources = payload.manage_sources
    if payload.manage_integrations is not None:
        user.manage_integrations = payload.manage_integrations
        access_rows = (await db.scalars(select(PortalProjectAccess).where(PortalProjectAccess.user_id == user.id))).all()
        for access in access_rows:
            access.manage_integrations = payload.manage_integrations
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
    project = await default_project(db, payload.workspace_id)
    source = LeadInboundSource(workspace_id=payload.workspace_id, project_id=project.id, name=payload.name,
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
    if user.role != "client_owner":
        access = await db.scalar(select(PortalProjectAccess.id).where(
            PortalProjectAccess.user_id == user.id,
            PortalProjectAccess.project_id == lead.project_id))
        if access is None:
            raise HTTPException(404, "Лид не найден")
    if user.role == "sales_manager" and lead.assigned_to_id != user.id:
        raise HTTPException(403, "Этот лид назначен другому менеджеру")
    return lead


async def validate_assignee(assignee_id: int | None, workspace_id: int, db: AsyncSession, project_id: int | None = None) -> PortalUser | None:
    if assignee_id is None:
        return None
    assignee = await db.get(PortalUser, assignee_id)
    if not assignee or assignee.workspace_id != workspace_id or not assignee.active:
        raise HTTPException(422, "Выбранный сотрудник недоступен")
    if assignee.role not in {"sales_manager", "sales_head", "client_owner"}:
        raise HTTPException(422, "Лиды можно назначать сотрудникам отдела продаж")
    if project_id and assignee.role != "client_owner" and not await db.scalar(select(PortalProjectAccess.id).where(
        PortalProjectAccess.user_id == assignee.id, PortalProjectAccess.project_id == project_id)):
        raise HTTPException(422, "У сотрудника нет доступа к проекту")
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
    if len(json.dumps(payload.model_dump(mode="json"), ensure_ascii=False)) > 65536:
        raise HTTPException(413, "Заявка слишком большая")
    return await create_inbound(db, source, payload)


@portal_router.get("/crm/team")
async def crm_team(project_id: int | None = None, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_leads")
    query = select(PortalUser).where(PortalUser.workspace_id == user.workspace_id, PortalUser.active.is_(True),
                                     PortalUser.role.in_(["sales_manager", "sales_head", "client_owner"]))
    if project_id:
        project = await db.get(Project, project_id)
        if not project or project.workspace_id != user.workspace_id:
            raise HTTPException(404, "Проект не найден")
        query = query.where(or_(PortalUser.role == "client_owner", PortalUser.id.in_(select(PortalProjectAccess.user_id).where(
            PortalProjectAccess.project_id == project_id))))
    members = (await db.scalars(query.order_by(PortalUser.display_name))).all()
    return [user_payload(member) for member in members]


@portal_router.get("/crm/leads")
async def crm_leads(project_id: int | None = None, db: AsyncSession = Depends(get_db),
                    user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_leads")
    query = (select(ClientLead, PortalUser.display_name)
             .outerjoin(PortalUser, ClientLead.assigned_to_id == PortalUser.id)
             .where(ClientLead.workspace_id == user.workspace_id)
             .order_by(ClientLead.updated_at.desc(), ClientLead.id.desc()))
    if project_id is not None:
        project = await db.get(Project, project_id)
        if not project or project.workspace_id != user.workspace_id:
            raise HTTPException(404, "Проект не найден")
        query = query.where(ClientLead.project_id == project_id)
    if user.role != "client_owner":
        permitted = select(PortalProjectAccess.project_id).where(PortalProjectAccess.user_id == user.id)
        query = query.where(ClientLead.project_id.in_(permitted))
    if user.role == "sales_manager":
        query = query.where(ClientLead.assigned_to_id == user.id)
    rows = (await db.execute(query)).all()
    return [lead_payload(lead, assignee_name) for lead, assignee_name in rows]


@portal_router.post("/crm/leads", status_code=201)
async def create_crm_lead(payload: LeadCreate, request: Request, db: AsyncSession = Depends(get_db),
                          user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    require_permission(user, "manage_leads")
    project = await (db.get(Project, payload.project_id) if payload.project_id else default_project(db, user.workspace_id))
    if project is None or project.workspace_id != user.workspace_id:
        raise HTTPException(404, "Проект не найден")
    if user.role != "client_owner" and not await db.scalar(select(PortalProjectAccess.id).where(
        PortalProjectAccess.user_id == user.id, PortalProjectAccess.project_id == project.id)):
        raise HTTPException(404, "Проект не найден")
    assignee = await validate_assignee(payload.assigned_to_id, user.workspace_id, db, project.id)
    if not payload.phone and not payload.email and not payload.telegram:
        raise HTTPException(422, "Для лида нужен телефон, email или Telegram")
    lead = ClientLead(workspace_id=user.workspace_id, project_id=project.id, assigned_to_id=payload.assigned_to_id,
                      full_name=payload.full_name, phone=payload.phone, email=payload.email, telegram=payload.telegram,
                      source=payload.source, value=payload.value, notes=payload.notes,
                      next_action_at=payload.next_action_at)
    db.add(lead); await db.flush()
    from app.api.routes.crm import pipeline_for
    pipeline = await pipeline_for(db, project.id)
    stage = await db.scalar(select(CrmStage).where(CrmStage.pipeline_id == pipeline.id,
                       CrmStage.analytics_type == "LEAD").order_by(CrmStage.position).limit(1))
    contact = CrmContact(workspace_id=user.workspace_id, project_id=project.id, name=lead.full_name,
                         phones=[lead.phone] if lead.phone else [], emails=[lead.email] if lead.email else [],
                         phone_normalized=re.sub(r"\D", "", lead.phone) if lead.phone else None,
                         email_normalized=lead.email.lower() if lead.email else None,
                         telegram=lead.telegram)
    db.add(contact); await db.flush()
    deal = CrmDeal(workspace_id=user.workspace_id, project_id=project.id, contact_id=contact.id,
                   lead_id=lead.id, pipeline_id=pipeline.id, stage_id=stage.id,
                   responsible_user_id=lead.assigned_to_id, name=lead.full_name, amount=lead.value,
                   origin="MANUAL", custom_fields={}, attribution_snapshot={})
    db.add(deal); await db.flush()
    db.add(CrmStageHistory(deal_id=deal.id, from_stage_id=None, to_stage_id=stage.id, actor_id=user.id))
    db.add(CrmActivity(workspace_id=user.workspace_id, project_id=project.id, deal_id=deal.id,
                       actor_id=user.id, actor_name=user.display_name, event_type="DEAL_CREATED", payload={"lead_id": lead.id}))
    db.add(ClientLeadEvent(workspace_id=user.workspace_id, lead_id=lead.id, actor_id=user.id,
                           event_type="LEAD_CREATED", description=f"Лид создан. Источник: {payload.source}."))
    if payload.notes:
        db.add(ClientLeadEvent(workspace_id=user.workspace_id, lead_id=lead.id, actor_id=user.id,
                               event_type="COMMENT_ADDED", description=payload.notes))
    recipients: list[PortalUser] = []
    if assignee:
        recipients = [assignee]
    else:
        recipients = list((await db.scalars(select(PortalUser).where(PortalUser.workspace_id == user.workspace_id,
                                                                      PortalUser.active.is_(True),
                                                                      PortalUser.role.in_(["client_owner", "sales_head"])))).all())
    if await in_app_rule_enabled(db, project.id, "new_lead"):
        for recipient in recipients:
            db.add(PortalNotification(workspace_id=user.workspace_id, user_id=recipient.id, level="success",
                                      title="Новый лид", body=f"{payload.full_name} · {payload.source}"))
    await db.commit(); await db.refresh(lead)
    return lead_payload(lead, assignee.display_name if assignee else None)


@portal_router.patch("/crm/leads/{lead_id}")
async def update_crm_lead(lead_id: int, payload: LeadUpdate, request: Request, db: AsyncSession = Depends(get_db),
                          user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    require_permission(user, "manage_leads")
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
    if "telegram" in supplied and payload.telegram != lead.telegram:
        lead.telegram = payload.telegram; changes.append("Обновлён Telegram")
    if payload.source is not None and payload.source != lead.source:
        lead.source = payload.source; changes.append("Обновлён источник")
    if "assigned_to_id" in supplied:
        if user.role not in {"client_owner", "sales_head"}:
            raise HTTPException(403, "Назначать менеджеров может руководитель продаж или собственник")
        assignee = await validate_assignee(payload.assigned_to_id, user.workspace_id, db, lead.project_id)
        old_owner = lead.assigned_to_id
        lead.assigned_to_id = payload.assigned_to_id
        assignee_name = assignee.display_name if assignee else None
        changes.append(f"Назначен сотрудник: {assignee_name or 'не назначен'}")
        if old_owner != lead.assigned_to_id:
            db.add(ClientLeadEvent(workspace_id=user.workspace_id, lead_id=lead.id, actor_id=user.id,
                                   event_type="OWNER_CHANGED", description=f"Ответственный: {assignee_name or 'не назначен'}."))
        if assignee:
            db.add(PortalNotification(workspace_id=user.workspace_id, user_id=assignee.id, level="success",
                                      title="Вам назначен лид", body=f"{lead.full_name} · {lead.source}"))
    elif lead.assigned_to_id:
        assignee = await db.get(PortalUser, lead.assigned_to_id)
        assignee_name = assignee.display_name if assignee else None
    if payload.status is not None and payload.status != lead.status:
        if lead.status in {"won", "lost"}:
            raise HTTPException(422, "Конечный статус лида нельзя изменить через эту форму")
        if payload.status in {"won", "lost"}:
            raise HTTPException(422, "Продажа и потеря требуют подтверждения в карточке лида")
        lead.status = payload.status; changes.append(f"Статус: {LEAD_STATUSES[payload.status]}")
        if payload.status in {"qualified", "proposal", "won"} and lead.qualified_at is None:
            lead.qualified_at = datetime.now(timezone.utc)
        if payload.status == "qualified":
            db.add(ClientLeadEvent(workspace_id=user.workspace_id, lead_id=lead.id, actor_id=user.id,
                                   event_type="LEAD_QUALIFIED", description="Лид квалифицирован отделом продаж."))
    if payload.value is not None:
        lead.value = payload.value; changes.append("Обновлена сумма сделки")
    if "notes" in supplied:
        lead.notes = payload.notes.strip() or None if payload.notes else None; changes.append("Обновлён комментарий")
        if lead.notes:
            db.add(ClientLeadEvent(workspace_id=user.workspace_id, lead_id=lead.id, actor_id=user.id,
                                   event_type="COMMENT_ADDED", description=lead.notes))
    if "next_action_at" in supplied:
        lead.next_action_at = payload.next_action_at; changes.append("Обновлён срок следующего действия")
    if "meeting_at" in supplied:
        project = await db.get(Project, lead.project_id) if lead.project_id else None
        if not project or not project.meeting_enabled:
            raise HTTPException(422, "Этап встречи не включён для проекта")
        lead.meeting_at = payload.meeting_at
        changes.append("Встреча отмечена" if payload.meeting_at else "Отметка встречи снята")
    if changes:
        db.add(ClientLeadEvent(workspace_id=user.workspace_id, lead_id=lead.id, actor_id=user.id,
                               event_type="updated", description="; ".join(changes)))
        deal = await db.scalar(select(CrmDeal).where(CrmDeal.lead_id == lead.id))
        if deal:
            deal.amount = lead.value; deal.responsible_user_id = lead.assigned_to_id
            if payload.status in {"qualified", "proposal"}:
                target = await db.scalar(select(CrmStage).where(CrmStage.pipeline_id == deal.pipeline_id,
                    CrmStage.analytics_type == "QUALIFIED").order_by(CrmStage.position).limit(1))
                if target and deal.stage_id != target.id:
                    db.add(CrmStageHistory(deal_id=deal.id, from_stage_id=deal.stage_id,
                                           to_stage_id=target.id, actor_id=user.id))
                    deal.stage_id = target.id
            db.add(CrmActivity(workspace_id=user.workspace_id, project_id=lead.project_id, deal_id=deal.id,
                               actor_id=user.id, actor_name=user.display_name, event_type="FIELD_CHANGED",
                               payload={"changes": changes}))
    await db.commit(); await db.refresh(lead)
    return lead_payload(lead, assignee_name)


@portal_router.get("/crm/leads/{lead_id}/events")
async def crm_lead_events(lead_id: int, db: AsyncSession = Depends(get_db),
                          user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_leads")
    await crm_lead_or_404(lead_id, user, db)
    rows = (await db.execute(select(ClientLeadEvent, PortalUser.display_name)
                             .outerjoin(PortalUser, ClientLeadEvent.actor_id == PortalUser.id)
                             .where(ClientLeadEvent.lead_id == lead_id,
                                    ClientLeadEvent.workspace_id == user.workspace_id)
                             .order_by(ClientLeadEvent.created_at.desc()))).all()
    return [{"id": event.id, "event_type": event.event_type, "description": event.description,
             "actor_name": actor_name, "created_at": event.created_at} for event, actor_name in rows]


@portal_router.post("/crm/leads/{lead_id}/sales", status_code=201)
async def add_crm_sale(lead_id: int, payload: SaleCreate, request: Request,
                       db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    require_permission(user, "manage_sales")
    lead = await crm_lead_or_404(lead_id, user, db)
    if lead.status == "lost":
        raise HTTPException(422, "Нельзя добавить продажу к потерянному лиду")
    if lead.project_id is None:
        lead.project_id = (await default_project(db, user.workspace_id)).id
    occurred_at = payload.occurred_at or datetime.now(timezone.utc)
    deal = await db.scalar(select(CrmDeal).where(CrmDeal.lead_id == lead.id))
    sale = ClientSale(lead_id=lead.id, deal_id=deal.id if deal else None, project_id=lead.project_id, amount=payload.amount,
                      confirmed_by_id=user.id, occurred_at=occurred_at,
                      comment=payload.comment.strip() or None if payload.comment else None)
    db.add(sale)
    if deal:
        won = await db.scalar(select(CrmStage).where(CrmStage.pipeline_id == deal.pipeline_id,
                            CrmStage.analytics_type == "WON").order_by(CrmStage.position).limit(1))
        if won and deal.stage_id != won.id:
            db.add(CrmStageHistory(deal_id=deal.id, from_stage_id=deal.stage_id, to_stage_id=won.id, actor_id=user.id))
            deal.stage_id = won.id
        db.add(CrmActivity(workspace_id=user.workspace_id, project_id=lead.project_id, deal_id=deal.id,
                           actor_id=user.id, actor_name=user.display_name, event_type="SALE_CREATED",
                           payload={"amount": payload.amount}))
    lead.status = "won"
    if lead.qualified_at is None:
        lead.qualified_at = occurred_at
        db.add(ClientLeadEvent(workspace_id=user.workspace_id, lead_id=lead.id, actor_id=user.id,
                               event_type="LEAD_QUALIFIED", description="Лид квалифицирован при подтверждении продажи."))
    lead.value = payload.amount
    db.add(ClientLeadEvent(workspace_id=user.workspace_id, lead_id=lead.id, actor_id=user.id,
                           event_type="SALE_CREATED", description=f"Подтверждена продажа на {payload.amount:,.2f} ₽."
                           + (f" {payload.comment.strip()}" if payload.comment and payload.comment.strip() else "")))
    if await in_app_rule_enabled(db, lead.project_id, "new_sale"):
        recipients = (await db.scalars(select(PortalUser).where(PortalUser.workspace_id == user.workspace_id,
            PortalUser.active.is_(True), PortalUser.role.in_(["client_owner", "sales_head"])))).all()
        for recipient in recipients:
            db.add(PortalNotification(workspace_id=user.workspace_id, user_id=recipient.id, level="success",
                                      title="Новая продажа", body=f"{lead.full_name} · {payload.amount:,.2f} ₽"))
    await db.commit(); await db.refresh(sale)
    return {"id": sale.id, "lead_id": lead.id, "amount": float(sale.amount) if sale.amount is not None else None,
            "occurred_at": sale.occurred_at, "comment": sale.comment}


@portal_router.get("/crm/leads/{lead_id}/sales")
async def list_crm_sales(lead_id: int, db: AsyncSession = Depends(get_db),
                         user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_sales")
    await crm_lead_or_404(lead_id, user, db)
    rows = (await db.scalars(select(ClientSale).where(ClientSale.lead_id == lead_id)
                             .order_by(ClientSale.occurred_at.desc()))).all()
    return [{"id": row.id, "lead_id": lead_id, "amount": float(row.amount) if row.amount is not None else None,
             "occurred_at": row.occurred_at, "comment": row.comment} for row in rows]


@portal_router.post("/crm/leads/{lead_id}/lost")
async def lose_crm_lead(lead_id: int, payload: LostCreate, request: Request, db: AsyncSession = Depends(get_db),
                        user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    require_permission(user, "manage_leads")
    lead = await crm_lead_or_404(lead_id, user, db)
    if lead.project_id is None:
        lead.project_id = (await default_project(db, user.workspace_id)).id
    active_reasons = {row.label for row in await project_reasons(db, lead.project_id) if row.status == "active"}
    if payload.reason not in active_reasons or (payload.reason == "Другое" and not (payload.detail or "").strip()):
        raise HTTPException(422, "Укажите причину потери; для «Другое» нужен текст")
    if lead.status == "won":
        raise HTTPException(422, "Проданный лид нельзя пометить потерянным")
    if lead.status == "lost":
        raise HTTPException(422, "Лид уже помечен потерянным")
    lead.status = "lost"
    lead.lost_reason = payload.reason + (f": {payload.detail.strip()}" if payload.detail and payload.detail.strip() else "")
    deal = await db.scalar(select(CrmDeal).where(CrmDeal.lead_id == lead.id))
    if deal:
        stage = await db.scalar(select(CrmStage).where(CrmStage.pipeline_id == deal.pipeline_id,
                            CrmStage.analytics_type == "LOST").order_by(CrmStage.position).limit(1))
        reason = await db.scalar(select(ProjectLostReason).where(ProjectLostReason.project_id == lead.project_id,
                                                                  ProjectLostReason.label == payload.reason))
        if stage and deal.stage_id != stage.id:
            db.add(CrmStageHistory(deal_id=deal.id, from_stage_id=deal.stage_id,
                                   to_stage_id=stage.id, actor_id=user.id))
            deal.stage_id = stage.id
        deal.lost_reason_id = reason.id if reason else None
        deal.lost_comment = payload.detail
        deal.lost_at = datetime.now(timezone.utc)
        db.add(CrmActivity(workspace_id=user.workspace_id, project_id=lead.project_id, deal_id=deal.id,
                           actor_id=user.id, actor_name=user.display_name, event_type="DEAL_LOST",
                           payload={"reason": payload.reason}))
    db.add(ClientLeadEvent(workspace_id=user.workspace_id, lead_id=lead.id, actor_id=user.id,
                           event_type="LEAD_LOST", description=lead.lost_reason))
    await db.commit()
    return {"ok": True, "status": "lost", "lost_reason": lead.lost_reason}


@portal_router.post("/crm/leads/{lead_id}/comments", status_code=201)
async def add_crm_comment(lead_id: int, payload: CommentCreate, request: Request, db: AsyncSession = Depends(get_db),
                          user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    require_permission(user, "manage_leads")
    lead = await crm_lead_or_404(lead_id, user, db)
    text = payload.text.strip()
    if not text:
        raise HTTPException(422, "Комментарий не может быть пустым")
    lead.notes = text
    event = ClientLeadEvent(workspace_id=user.workspace_id, lead_id=lead.id, actor_id=user.id,
                            event_type="COMMENT_ADDED", description=text)
    db.add(event)
    await db.commit()
    return {"ok": True}


@portal_router.get("/crm/leads/{lead_id}/attribution")
async def crm_lead_attribution(lead_id: int, db: AsyncSession = Depends(get_db),
                               user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_leads")
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
    require_permission(user, "view_result")
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
