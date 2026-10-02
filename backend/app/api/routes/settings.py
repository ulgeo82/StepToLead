"""Client-only settings for one organization and one of its projects."""
import base64
import binascii
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import check_origin, require_portal_user
from app.core.permissions import effective_permissions, require_permission
from app.db import get_db
from app.models.access import GrowthCalculation
from app.models.marketing import (AdConnection, ClientWorkspace, PortalProjectAccess, PortalUser, Project,
                                  ProjectEconomics, ProjectLostReason, ProjectNotificationRule, ProjectSource)
from app.services.notifications import EVENTS, project_members, rule_state, telegram_configured

router = APIRouter(prefix="/settings", tags=["client-settings"])
DEFAULT_REASONS = ["Не отвечает", "Не подходит", "Не подходит по бюджету", "Передумал",
                   "Выбрал конкурента", "Нецелевой", "Другое"]
# Event catalogue lives with the delivery logic.


async def scoped(db, actor, project_id):
    project = await db.get(Project, project_id)
    if not project or project.workspace_id != actor.workspace_id:
        raise HTTPException(404, "Проект не найден")
    if actor.role != "client_owner":
        access = await db.scalar(select(PortalProjectAccess.id).where(
            PortalProjectAccess.user_id == actor.id, PortalProjectAccess.project_id == project.id))
        if not access:
            raise HTTPException(404, "Проект не найден")
    return project


async def reasons(db, project_id):
    rows = (await db.scalars(select(ProjectLostReason).where(ProjectLostReason.project_id == project_id)
                             .order_by(ProjectLostReason.position, ProjectLostReason.id))).all()
    if not rows:
        db.add_all(ProjectLostReason(project_id=project_id, label=label, is_system=True, position=index)
                   for index, label in enumerate(DEFAULT_REASONS))
        await db.commit()
        rows = (await db.scalars(select(ProjectLostReason).where(ProjectLostReason.project_id == project_id)
                                 .order_by(ProjectLostReason.position, ProjectLostReason.id))).all()
    return rows


def reason_payload(row):
    return {"id": row.id, "label": row.label, "is_system": row.is_system,
            "status": row.status, "position": row.position}


def source_payload(row):
    return {"id": f"source:{row.id}", "name": row.name, "category": row.category,
            "kind": row.kind, "method": row.method, "data_mode": row.data_mode or row.method,
            "status": row.status, "created_at": row.created_at, "connection_id": row.connection_id}


async def source_permission(db, project, actor):
    if actor.role == "client_owner":
        return
    access = await db.scalar(select(PortalProjectAccess).where(
        PortalProjectAccess.user_id == actor.id, PortalProjectAccess.project_id == project.id))
    allowed = "manage_sources" in effective_permissions(actor)
    if actor.permissions is None and access and access.manage_sources:
        allowed = True
    if not access or not allowed:
        raise HTTPException(403, "Для проекта не выдано право manage_sources")


def validate_zone(value):
    if value:
        try: ZoneInfo(value)
        except ZoneInfoNotFoundError as exc: raise HTTPException(422, "Неизвестный часовой пояс") from exc
    return value


def validate_logo(value):
    if value is None:
        return None
    match = re.fullmatch(r"data:image/(png|jpeg);base64,([A-Za-z0-9+/=]+)", value)
    if not match:
        raise HTTPException(422, "Допустимы только PNG или JPEG")
    try: raw = base64.b64decode(match.group(2), validate=True)
    except (ValueError, binascii.Error) as exc: raise HTTPException(422, "Некорректное изображение") from exc
    if len(raw) > 1024 * 1024 or not (raw.startswith(b"\x89PNG\r\n\x1a\n") if match.group(1) == "png" else raw.startswith(b"\xff\xd8\xff")):
        raise HTTPException(422, "Изображение должно быть PNG/JPEG до 1 МБ")
    return value


class CompanyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=180)
    legal_name: str | None = Field(default=None, max_length=240)
    contact_email: str | None = Field(default=None, max_length=254)
    contact_phone: str | None = Field(default=None, max_length=64)
    website: str | None = Field(default=None, max_length=500)
    timezone: str | None = Field(default=None, max_length=80)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    logo_data: str | None = Field(default=None, max_length=1_500_000)


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=180)
    website: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=3000)
    status: str | None = None
    timezone: str | None = Field(default=None, max_length=80)


class FunnelPatch(BaseModel):
    meeting_enabled: bool


class ReasonCreate(BaseModel):
    label: str = Field(min_length=2, max_length=160)


class ReasonPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, min_length=2, max_length=160)
    status: str | None = None
    position: int | None = Field(default=None, ge=0)


class ReasonOrder(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=100)


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=180)
    category: str | None = Field(default=None, max_length=80)
    kind: str
    method: str


class SourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=180)
    category: str | None = Field(default=None, max_length=80)
    status: str | None = None


class RulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    threshold: float | None = Field(default=None, ge=0, le=100000)
    in_app: bool = True
    telegram: bool = False
    recipient_user_ids: list[int] | None = Field(default=None, max_length=500)
    notify_assignee: bool = True


@router.get("")
async def get_settings(project_id: int, db: AsyncSession = Depends(get_db),
                       actor: PortalUser = Depends(require_portal_user)):
    if not effective_permissions(actor).intersection({"manage_settings", "manage_sources"}):
        raise HTTPException(403, "Недостаточно прав для просмотра настроек")
    project = await scoped(db, actor, project_id)
    company = await db.get(ClientWorkspace, actor.workspace_id)
    economy = await db.scalar(select(ProjectEconomics).where(ProjectEconomics.project_id == project.id))
    calculation = await db.get(GrowthCalculation, economy.growth_calculation_id) if economy else None
    reason_rows = await reasons(db, project.id)
    source_rows = (await db.scalars(select(ProjectSource).where(ProjectSource.project_id == project.id)
                                    .order_by(ProjectSource.name))).all()
    ad_rows = (await db.scalars(select(AdConnection).where(AdConnection.project_id == project.id,
                                                   AdConnection.platform != "telegram_ads")
                                .order_by(AdConnection.name))).all()
    source_connection_ids = {row.connection_id for row in source_rows if row.connection_id}
    source_list = [source_payload(row) for row in source_rows]
    for ad in ad_rows:
        if ad.id not in source_connection_ids:
            source_list.append({"id": f"ad:{ad.id}", "name": ad.name, "category": "Реклама",
                                "kind": "INTEGRATION", "method": "API", "data_mode": "API",
                                "status": "error" if ad.status == "error" else "archived" if ad.status == "disconnected" else "active",
                                "created_at": ad.created_at, "connection_id": ad.id})
    rule_rows = (await db.scalars(select(ProjectNotificationRule).where(ProjectNotificationRule.project_id == project.id))).all()
    rule_map = {row.event_key: row for row in rule_rows}
    notifications = [{"key": key, **config, **rule_state(key, row),
                      "threshold": float(row.threshold) if row and row.threshold is not None else None,
                      "email": False}
                     for key, config in EVENTS.items() for row in [rule_map.get(key)]]
    members = [{"id": member.id, "display_name": member.display_name, "role": member.role,
                "telegram_linked": bool(member.telegram_chat_id)} for member in await project_members(db, project)]
    return {"company": {"id": company.id, "name": company.name, "legal_name": company.legal_name,
                         "contact_email": company.contact_email, "contact_phone": company.contact_phone,
                         "website": company.website, "timezone": company.timezone, "currency": company.currency,
                         "logo_data": company.logo_data},
            "project": {"id": project.id, "name": project.name, "website": project.website,
                        "description": project.description, "status": project.status,
                        "timezone": project.timezone, "meeting_enabled": project.meeting_enabled},
            "economics": ({"name": calculation.name, "updated_at": economy.activated_at,
                           "average_check": calculation.inputs.get("average_order_value"),
                           "margin": calculation.inputs.get("gross_margin"),
                           "allowable_cac": float(economy.allowable_cac) if economy.allowable_cac is not None else None}
                          if calculation else None),
            "reasons": [reason_payload(row) for row in reason_rows], "sources": source_list,
            "notifications": notifications, "members": members,
            "telegram_bot_configured": telegram_configured(),
            "permissions": sorted(effective_permissions(actor))}


@router.patch("/company")
async def patch_company(project_id: int, payload: CompanyPatch, request: Request,
                        db: AsyncSession = Depends(get_db), actor: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(actor, "manage_settings")
    await scoped(db, actor, project_id)
    company = await db.get(ClientWorkspace, actor.workspace_id)
    values = payload.model_dump(exclude_unset=True)
    if "name" in values:
        if values["name"] is None:
            raise HTTPException(422, "Укажите название компании")
        values["name"] = values["name"].strip()
        if len(values["name"]) < 2:
            raise HTTPException(422, "Название компании слишком короткое")
        if await db.scalar(select(ClientWorkspace.id).where(ClientWorkspace.name == values["name"], ClientWorkspace.id != company.id)):
            raise HTTPException(409, "Название компании уже используется")
    if "timezone" in values:
        if not values["timezone"]: raise HTTPException(422, "Укажите часовой пояс")
        validate_zone(values["timezone"])
    if "currency" in values:
        if not values["currency"] or not re.fullmatch(r"[A-Za-z]{3}", values["currency"]):
            raise HTTPException(422, "Валюта должна быть кодом ISO из 3 букв")
        values["currency"] = values["currency"].upper()
    if "logo_data" in values: validate_logo(values["logo_data"])
    for key, value in values.items(): setattr(company, key, value or None if key not in {"name", "timezone", "currency"} else value)
    await db.commit()
    return {"ok": True}


@router.patch("/project")
async def patch_project(project_id: int, payload: ProjectPatch, request: Request,
                        db: AsyncSession = Depends(get_db), actor: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(actor, "manage_settings")
    project = await scoped(db, actor, project_id)
    values = payload.model_dump(exclude_unset=True)
    if "name" in values:
        if not values["name"] or len(values["name"].strip()) < 2:
            raise HTTPException(422, "Укажите название проекта")
        values["name"] = values["name"].strip()
    if "status" in values and values["status"] not in {"active", "archived"}:
        raise HTTPException(422, "Неизвестный статус проекта")
    if "timezone" in values: validate_zone(values["timezone"])
    for key, value in values.items(): setattr(project, key, value)
    await db.commit()
    return {"ok": True}


@router.patch("/funnel")
async def patch_funnel(project_id: int, payload: FunnelPatch, request: Request,
                       db: AsyncSession = Depends(get_db), actor: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(actor, "manage_settings")
    project = await scoped(db, actor, project_id)
    project.meeting_enabled = payload.meeting_enabled
    await db.commit()
    return {"ok": True}


@router.get("/reasons")
async def get_reasons(project_id: int, db: AsyncSession = Depends(get_db),
                      actor: PortalUser = Depends(require_portal_user)):
    require_permission(actor, "view_leads")
    project = await scoped(db, actor, project_id)
    return [reason_payload(row) for row in await reasons(db, project.id) if row.status == "active"]


@router.post("/reasons", status_code=201)
async def add_reason(project_id: int, payload: ReasonCreate, request: Request,
                     db: AsyncSession = Depends(get_db), actor: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(actor, "manage_settings")
    project = await scoped(db, actor, project_id)
    rows = await reasons(db, project.id)
    label = payload.label.strip()
    if any(row.label.casefold() == label.casefold() for row in rows):
        raise HTTPException(409, "Такая причина уже существует")
    row = ProjectLostReason(project_id=project.id, label=label, position=max((r.position for r in rows), default=-1) + 1)
    db.add(row); await db.commit(); await db.refresh(row)
    return reason_payload(row)


@router.patch("/reasons/{reason_id}")
async def patch_reason(project_id: int, reason_id: int, payload: ReasonPatch, request: Request,
                       db: AsyncSession = Depends(get_db), actor: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(actor, "manage_settings")
    project = await scoped(db, actor, project_id)
    row = await db.get(ProjectLostReason, reason_id)
    if not row or row.project_id != project.id: raise HTTPException(404, "Причина не найдена")
    if payload.label is not None:
        if row.is_system: raise HTTPException(422, "Системную причину нельзя переименовать")
        label = payload.label.strip()
        if len(label) < 2: raise HTTPException(422, "Укажите название причины")
        existing = await reasons(db, project.id)
        if any(item.id != row.id and item.label.casefold() == label.casefold() for item in existing):
            raise HTTPException(409, "Такая причина уже существует")
        row.label = label
    if payload.status is not None:
        if payload.status not in {"active", "archived"}: raise HTTPException(422, "Неизвестный статус")
        row.status = payload.status
    if payload.position is not None: row.position = payload.position
    await db.commit()
    return reason_payload(row)


@router.put("/reasons/order")
async def reorder_reasons(project_id: int, payload: ReasonOrder, request: Request,
                          db: AsyncSession = Depends(get_db), actor: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(actor, "manage_settings")
    project = await scoped(db, actor, project_id)
    rows = await reasons(db, project.id)
    if len(payload.ids) != len(rows) or set(payload.ids) != {row.id for row in rows}:
        raise HTTPException(422, "Передайте все причины проекта ровно один раз")
    order = {reason_id: index for index, reason_id in enumerate(payload.ids)}
    for row in rows: row.position = order[row.id]
    await db.commit()
    return {"ok": True}


@router.post("/sources", status_code=201)
async def add_source(project_id: int, payload: SourceCreate, request: Request,
                     db: AsyncSession = Depends(get_db), actor: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    project = await scoped(db, actor, project_id)
    await source_permission(db, project, actor)
    if payload.kind not in {"CUSTOM", "MANUAL", "INTERNAL"} or payload.method not in {"API", "WEBHOOK", "BOT", "FILE", "MANUAL", "INTERNAL"}:
        raise HTTPException(422, "Рекламные интеграции подключаются в /ads; укажите допустимый тип и способ")
    if payload.kind == "MANUAL" and payload.method not in {"MANUAL", "FILE"}:
        raise HTTPException(422, "Ручной источник не может выдавать себя за активный коннектор")
    if len(payload.name.strip()) < 2:
        raise HTTPException(422, "Укажите название источника")
    row = ProjectSource(project_id=project.id, name=payload.name.strip(), category=payload.category,
                        kind=payload.kind, method=payload.method, data_mode=payload.method,
                        status="active", created_by_id=actor.id)
    db.add(row); await db.commit(); await db.refresh(row)
    return source_payload(row)


@router.patch("/sources/{source_id}")
async def patch_source(project_id: int, source_id: int, payload: SourcePatch, request: Request,
                       db: AsyncSession = Depends(get_db), actor: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    project = await scoped(db, actor, project_id)
    await source_permission(db, project, actor)
    row = await db.get(ProjectSource, source_id)
    if not row or row.project_id != project.id: raise HTTPException(404, "Источник не найден")
    if row.connection_id: raise HTTPException(409, "Рекламным подключением управляйте в /ads")
    if payload.status is not None and payload.status not in {"active", "archived"}:
        raise HTTPException(422, "Неизвестный статус")
    if payload.name is not None and len(payload.name.strip()) < 2:
        raise HTTPException(422, "Укажите название источника")
    for key, value in payload.model_dump(exclude_unset=True).items(): setattr(row, key, value)
    await db.commit()
    return source_payload(row)


@router.patch("/notifications/{event_key}")
async def patch_rule(project_id: int, event_key: str, payload: RulePatch, request: Request,
                     db: AsyncSession = Depends(get_db), actor: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(actor, "manage_settings")
    project = await scoped(db, actor, project_id)
    if event_key not in EVENTS: raise HTTPException(404, "Событие не найдено")
    if payload.threshold is not None and event_key not in {"lead_idle", "cpl_growth", "ad_budget"}:
        raise HTTPException(422, "Для события порог не требуется")
    if event_key in {"lead_idle", "cpl_growth", "ad_budget"} and payload.enabled and payload.threshold is None:
        raise HTTPException(422, "Укажите порог")
    row = await db.scalar(select(ProjectNotificationRule).where(ProjectNotificationRule.project_id == project.id,
                                                                 ProjectNotificationRule.event_key == event_key))
    if row is None:
        row = ProjectNotificationRule(project_id=project.id, event_key=event_key)
        db.add(row)
    recipients = None
    if payload.recipient_user_ids is not None:
        if not EVENTS[event_key].get("recipients"):
            raise HTTPException(422, "Для этого события получатели не настраиваются")
        member_ids = {member.id for member in await project_members(db, project)}
        unknown = set(payload.recipient_user_ids) - member_ids
        if unknown:
            raise HTTPException(422, "Получатели должны быть участниками проекта")
        recipients = sorted(set(payload.recipient_user_ids))
    row.enabled = payload.enabled; row.threshold = payload.threshold; row.in_app = payload.in_app
    row.telegram = payload.telegram; row.recipient_user_ids = recipients; row.notify_assignee = payload.notify_assignee
    await db.commit()
    return {"ok": True, "delivery_active": EVENTS[event_key]["active"] and payload.enabled and (payload.in_app or payload.telegram)}


async def in_app_rule_enabled(db, project_id, event_key):
    if event_key not in EVENTS or not EVENTS[event_key]["active"]:
        return False
    row = await db.scalar(select(ProjectNotificationRule).where(ProjectNotificationRule.project_id == project_id,
                                                                 ProjectNotificationRule.event_key == event_key))
    return (row.enabled and row.in_app) if row else EVENTS[event_key].get("default", False)
