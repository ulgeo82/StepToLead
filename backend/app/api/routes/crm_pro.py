"""CRM power features: pipelines, contact cards and merge, bulk actions, CSV export,
digital-pipeline automations, quick touches and the sales analytics report."""
import csv
import io
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.crm import (activity, contact_json, deal_for, deal_json, days_since, norm_phone, now,
                                pipeline_for, project_for, stage_for, task_json, validate_owner)
from app.core.access import check_origin, require_portal_user
from app.core.permissions import effective_permissions, require_permission
from app.db import get_db
from app.models.crm import (CrmAutomation, CrmContact, CrmDeal, CrmInbound, CrmPipeline, CrmStage,
                            CrmStageHistory, CrmTask)
from app.models.marketing import ClientLead, ClientSale, PortalUser, ProjectLostReason, ProjectSource
from app.services import crm_automation
from app.services.notifications import flush_telegram

router = APIRouter(prefix="/crm", tags=["crm-pro"], dependencies=[Depends(require_portal_user)])
DEFAULT_STAGES = (("Новый лид", "LEAD", "#006BFD"), ("Квалифицирован", "QUALIFIED", "#8555E8"),
                  ("Продажа", "WON", "#15A86B"), ("Отказ", "LOST", "#EF4A59"))


# --------------------------------------------------------------------------- pipelines

class PipelineCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=180)
    copy_from_pipeline_id: int | None = None


class PipelineUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=180)
    is_default: bool | None = None
    archived: bool | None = None


@router.post("/projects/{project_id}/pipelines", status_code=201)
async def create_pipeline(project_id: int, payload: PipelineCreate, request: Request,
                          db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "manage_pipeline")
    project = await project_for(db, user, project_id)
    await pipeline_for(db, project.id)  # make sure the default pipeline exists first
    template = None
    if payload.copy_from_pipeline_id:
        template = await db.get(CrmPipeline, payload.copy_from_pipeline_id)
        if not template or template.project_id != project.id:
            raise HTTPException(404, "Воронка-шаблон не найдена")
    pipeline = CrmPipeline(workspace_id=project.workspace_id, project_id=project.id, name=payload.name.strip(),
                           is_default=False)
    db.add(pipeline); await db.flush()
    stages = [(s.name, s.analytics_type, s.color, s.required_fields or []) for s in (await db.scalars(
        select(CrmStage).where(CrmStage.pipeline_id == template.id, CrmStage.archived_at.is_(None))
        .order_by(CrmStage.position, CrmStage.id))).all()] if template else [(n, k, c, []) for n, k, c in DEFAULT_STAGES]
    for position, (name, kind, color, required) in enumerate(stages):
        db.add(CrmStage(pipeline_id=pipeline.id, name=name, analytics_type=kind, color=color,
                        position=position, required_fields=required))
    await db.commit()
    return {"id": pipeline.id}


@router.patch("/pipelines/{pipeline_id}")
async def edit_pipeline(pipeline_id: int, payload: PipelineUpdate, request: Request,
                        db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "manage_pipeline")
    pipeline = await db.get(CrmPipeline, pipeline_id)
    if not pipeline or pipeline.workspace_id != user.workspace_id:
        raise HTTPException(404, "Воронка не найдена")
    await project_for(db, user, pipeline.project_id)
    if payload.name:
        pipeline.name = payload.name.strip()
    if payload.is_default:
        await db.execute(update(CrmPipeline).where(CrmPipeline.project_id == pipeline.project_id)
                         .values(is_default=False))
        pipeline.is_default = True
    if payload.archived:
        if pipeline.is_default:
            raise HTTPException(409, "Основную воронку нельзя архивировать — сначала сделайте основной другую")
        if await db.scalar(select(CrmDeal.id).where(CrmDeal.pipeline_id == pipeline.id,
                                                    CrmDeal.archived_at.is_(None)).limit(1)):
            raise HTTPException(409, "В воронке есть активные сделки — перенесите или архивируйте их")
        pipeline.archived_at = now()
    await db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- contacts

class ContactUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=180)
    phones: list[str] | None = Field(default=None, max_length=10)
    emails: list[str] | None = Field(default=None, max_length=10)
    telegram: str | None = Field(default=None, max_length=120)
    company: str | None = Field(default=None, max_length=180)
    position: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=5000)
    tags: list[str] | None = Field(default=None, max_length=20)


class ContactMerge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_contact_id: int


async def contact_in_scope(db: AsyncSession, user: PortalUser, contact_id: int) -> CrmContact:
    contact = await db.get(CrmContact, contact_id)
    if not contact or contact.workspace_id != user.workspace_id:
        raise HTTPException(404, "Контакт не найден")
    await project_for(db, user, contact.project_id)
    return contact


def _clean_list(values: list[str] | None, lower: bool = False) -> list[str]:
    result = []
    for value in values or []:
        item = " ".join(str(value).split())
        item = item.lower() if lower else item
        if item and item not in result:
            result.append(item[:254])
    return result


@router.get("/contacts/{contact_id}")
async def contact_card(contact_id: int, db: AsyncSession = Depends(get_db),
                       user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm")
    contact = await contact_in_scope(db, user, contact_id)
    query = select(CrmDeal, CrmStage).join(CrmStage, CrmStage.id == CrmDeal.stage_id).where(CrmDeal.contact_id == contact.id)
    if "view_all_deals" not in effective_permissions(user):
        query = query.where(CrmDeal.responsible_user_id == user.id)
    rows = (await db.execute(query.order_by(CrmDeal.created_at.desc()))).all()
    deals = [deal_json(deal, contact, stage) for deal, stage in rows]
    sales = (await db.scalars(select(ClientSale).where(ClientSale.deal_id.in_([d["id"] for d in deals])))).all() if deals else []
    tasks = (await db.scalars(select(CrmTask).where(CrmTask.contact_id == contact.id, CrmTask.status == "OPEN")
                              .order_by(CrmTask.due_at))).all()
    duplicates = []
    for phone in contact.phones or []:
        digits = norm_phone(phone)
        if digits:
            duplicates += (await db.scalars(select(CrmContact).where(
                CrmContact.project_id == contact.project_id, CrmContact.id != contact.id,
                CrmContact.phone_normalized == digits))).all()
    for email in contact.emails or []:
        duplicates += (await db.scalars(select(CrmContact).where(
            CrmContact.project_id == contact.project_id, CrmContact.id != contact.id,
            CrmContact.email_normalized == email.lower()))).all()
    unique = {d.id: d for d in duplicates}
    return {**contact_json(contact), "position": contact.position, "notes": contact.notes, "tags": contact.tags or [],
            "deals": deals, "open_tasks": [task_json(t) for t in tasks],
            "lifetime_value": sum(float(s.amount or 0) for s in sales), "purchases": len(sales),
            "duplicates": [{"id": d.id, "name": d.name, "phones": d.phones or [], "emails": d.emails or []}
                           for d in unique.values()]}


@router.patch("/contacts/{contact_id}")
async def update_contact(contact_id: int, payload: ContactUpdate, request: Request,
                         db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "edit_deal")
    contact = await contact_in_scope(db, user, contact_id)
    changes = payload.model_dump(exclude_unset=True)
    if "phones" in changes:
        contact.phones = _clean_list(changes.pop("phones"))
        contact.phone_normalized = norm_phone(contact.phones[0]) if contact.phones else None
    if "emails" in changes:
        contact.emails = _clean_list(changes.pop("emails"), lower=True)
        contact.email_normalized = contact.emails[0] if contact.emails else None
    if "tags" in changes:
        contact.tags = crm_automation.normalize_tags(changes.pop("tags"))
    for key, value in changes.items():
        setattr(contact, key, value.strip() if isinstance(value, str) else value)
    for deal in (await db.scalars(select(CrmDeal).where(CrmDeal.contact_id == contact.id))).all():
        lead = await db.get(ClientLead, deal.lead_id) if deal.lead_id else None
        if lead:  # keep reporting facts in sync with the contact
            lead.full_name = contact.name
            lead.phone = (contact.phones or [None])[0]
            lead.email = (contact.emails or [None])[0]
            lead.telegram = contact.telegram
        activity(db, deal, user, "CONTACT_UPDATED", {"contact": contact.name}, touch=False)
    await db.commit()
    return {"ok": True}


@router.post("/contacts/{contact_id}/merge")
async def merge_contacts(contact_id: int, payload: ContactMerge, request: Request,
                         db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    """Merge a duplicate (source) into this contact: deals, tasks and requests move, data is united."""
    check_origin(request); require_permission(user, "edit_deal")
    target = await contact_in_scope(db, user, contact_id)
    source = await contact_in_scope(db, user, payload.source_contact_id)
    if source.id == target.id or source.project_id != target.project_id:
        raise HTTPException(422, "Выберите другой контакт этого проекта")
    if "view_all_deals" not in effective_permissions(user):
        raise HTTPException(403, "Склеивать контакты может руководитель")
    target.phones = _clean_list([*(target.phones or []), *(source.phones or [])])
    target.emails = _clean_list([*(target.emails or []), *(source.emails or [])], lower=True)
    target.phone_normalized = norm_phone(target.phones[0]) if target.phones else None
    target.email_normalized = target.emails[0] if target.emails else None
    target.telegram = target.telegram or source.telegram
    target.company = target.company or source.company
    target.position = target.position or source.position
    target.notes = "\n\n".join(filter(None, [target.notes, source.notes])) or None
    target.tags = crm_automation.normalize_tags([*(target.tags or []), *(source.tags or [])])
    target.custom_fields = {**(source.custom_fields or {}), **(target.custom_fields or {})}
    moved = (await db.scalars(select(CrmDeal).where(CrmDeal.contact_id == source.id))).all()
    for deal in moved:
        deal.contact_id = target.id
        activity(db, deal, user, "CONTACT_MERGED", {"from": source.name, "to": target.name}, touch=False)
    await db.execute(update(CrmTask).where(CrmTask.contact_id == source.id).values(contact_id=target.id))
    await db.execute(update(CrmInbound).where(CrmInbound.contact_id == source.id).values(contact_id=target.id))
    await db.delete(source)
    await db.commit()
    return {"ok": True, "moved_deals": len(moved)}


# --------------------------------------------------------------------------- deals: bulk, export, touches

class BulkAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deal_ids: list[int] = Field(min_length=1, max_length=500)
    action: str  # move | responsible | tag_add | tag_remove | archive
    stage_id: int | None = None
    responsible_user_id: int | None = None
    tag: str | None = Field(default=None, max_length=40)


@router.post("/deals/bulk")
async def bulk_deals(payload: BulkAction, request: Request, db: AsyncSession = Depends(get_db),
                     user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    permission = {"move": "move_deal", "responsible": "edit_deal", "tag_add": "edit_deal",
                  "tag_remove": "edit_deal", "archive": "archive_deal"}.get(payload.action)
    if not permission:
        raise HTTPException(422, "Неизвестное действие")
    require_permission(user, permission)
    done, skipped = 0, []
    for deal_id in dict.fromkeys(payload.deal_ids):
        deal = await deal_for(db, user, deal_id)
        stage = await db.get(CrmStage, deal.stage_id)
        if payload.action == "move":
            pipeline = await pipeline_for(db, deal.project_id, deal.pipeline_id)
            target = await stage_for(db, pipeline, payload.stage_id or 0)
            if target.analytics_type in {"WON", "LOST"} or stage.analytics_type in {"WON", "LOST"}:
                skipped.append(deal.id)  # closing needs a sale amount or a loss reason: do it in the card
                continue
            if stage.analytics_type == "QUALIFIED" and target.analytics_type == "LEAD":
                skipped.append(deal.id)
                continue
            if target.id == deal.stage_id:
                continue
            db.add(CrmStageHistory(deal_id=deal.id, from_stage_id=deal.stage_id, to_stage_id=target.id, actor_id=user.id))
            activity(db, deal, user, "STAGE_CHANGED", {"from": stage.name, "to": target.name,
                                                    "analytics_type": target.analytics_type, "bulk": True})
            deal.stage_id = target.id
            deal.stage_entered_at = now()
            lead = await db.get(ClientLead, deal.lead_id) if deal.lead_id else None
            if lead and target.analytics_type == "QUALIFIED":
                lead.status = "qualified"
                lead.qualified_at = lead.qualified_at or now()
            await crm_automation.on_stage_enter(db, deal, target)
        elif payload.action == "responsible":
            await validate_owner(db, deal.project_id, deal.workspace_id, payload.responsible_user_id)
            if deal.responsible_user_id == payload.responsible_user_id:
                continue
            activity(db, deal, user, "OWNER_CHANGED", {"field": "responsible_user_id", "old": deal.responsible_user_id,
                                                    "new": payload.responsible_user_id, "bulk": True})
            deal.responsible_user_id = payload.responsible_user_id
            lead = await db.get(ClientLead, deal.lead_id) if deal.lead_id else None
            if lead: lead.assigned_to_id = payload.responsible_user_id
            await db.execute(update(CrmTask).where(CrmTask.deal_id == deal.id, CrmTask.status == "OPEN")
                             .values(responsible_user_id=payload.responsible_user_id))
        elif payload.action in {"tag_add", "tag_remove"}:
            tag = (crm_automation.normalize_tags([payload.tag]) or [None])[0]
            if not tag:
                raise HTTPException(422, "Укажите тег")
            tags = [t for t in deal.tags or [] if t.casefold() != tag.casefold()]
            if payload.action == "tag_add":
                tags.append(tag)
            if tags != (deal.tags or []):
                deal.tags = crm_automation.normalize_tags(tags)
                activity(db, deal, user, "TAGS_CHANGED", {"field": "tags", "new": deal.tags, "bulk": True}, touch=False)
        elif payload.action == "archive":
            if stage.analytics_type not in {"LOST", "WON"} and not deal.archived_at:
                skipped.append(deal.id)  # only closed deals go to the archive
                continue
            deal.archived_at = deal.archived_at or now()
            activity(db, deal, user, "DEAL_ARCHIVED", {"bulk": True}, touch=False)
        done += 1
    await db.commit()
    flush_telegram(db)
    return {"ok": True, "updated": done, "skipped": skipped}


class Touch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str  # CALL | MESSAGE | MEETING
    text: str | None = Field(default=None, max_length=4000)


@router.post("/deals/{deal_id}/touch")
async def touch_deal(deal_id: int, payload: Touch, request: Request, db: AsyncSession = Depends(get_db),
                     user: PortalUser = Depends(require_portal_user)):
    """One-click log of a call / message / meeting: feeds speed-to-lead and the timeline."""
    check_origin(request); require_permission(user, "edit_deal")
    deal = await deal_for(db, user, deal_id)
    kinds = {"CALL": "CALL_LOGGED", "MESSAGE": "MESSAGE_SENT", "MEETING": "MEETING_HELD"}
    if payload.kind not in kinds:
        raise HTTPException(422, "Неизвестный тип касания")
    activity(db, deal, user, kinds[payload.kind], {"text": (payload.text or "").strip()})
    if payload.kind == "MEETING":
        lead = await db.get(ClientLead, deal.lead_id) if deal.lead_id else None
        if lead and lead.meeting_at is None:
            lead.meeting_at = now()
    await db.commit()
    return {"ok": True}


@router.get("/projects/{project_id}/deals.csv")
async def export_deals(project_id: int, pipeline_id: int | None = None, db: AsyncSession = Depends(get_db),
                       user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm")
    await project_for(db, user, project_id)
    query = (select(CrmDeal, CrmContact, CrmStage, CrmPipeline.name, ProjectSource.name, PortalUser.display_name)
             .join(CrmContact, CrmContact.id == CrmDeal.contact_id).join(CrmStage, CrmStage.id == CrmDeal.stage_id)
             .join(CrmPipeline, CrmPipeline.id == CrmDeal.pipeline_id)
             .outerjoin(ProjectSource, ProjectSource.id == CrmDeal.source_id)
             .outerjoin(PortalUser, PortalUser.id == CrmDeal.responsible_user_id)
             .where(CrmDeal.project_id == project_id))
    if pipeline_id:
        query = query.where(CrmDeal.pipeline_id == pipeline_id)
    if "view_all_deals" not in effective_permissions(user):
        query = query.where(CrmDeal.responsible_user_id == user.id)
    rows = (await db.execute(query.order_by(CrmDeal.id))).all()
    buffer = io.StringIO()
    buffer.write("﻿")  # Excel opens UTF-8 correctly
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["ID", "Сделка", "Воронка", "Этап", "Сумма", "Ответственный", "Источник", "Теги", "Контакт",
                     "Телефоны", "Email", "Компания", "UTM source", "UTM campaign", "Создана", "Закрыта", "В архиве"])
    for deal, contact, stage, pipeline_name, source_name, owner in rows:
        snap = deal.attribution_snapshot or {}
        writer.writerow([deal.id, deal.name, pipeline_name, stage.name, deal.amount or "", owner or "", source_name or "",
                         ", ".join(deal.tags or []), contact.name, ", ".join(contact.phones or []),
                         ", ".join(contact.emails or []), contact.company or "", snap.get("utm_source") or "",
                         snap.get("utm_campaign") or "", deal.created_at.isoformat() if deal.created_at else "",
                         deal.closed_at.isoformat() if deal.closed_at else "", "да" if deal.archived_at else ""])
    buffer.seek(0)
    return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="deals-{project_id}.csv"'})


# --------------------------------------------------------------------------- automations

class AutomationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pipeline_id: int
    stage_id: int | None = None
    name: str = Field(min_length=2, max_length=180)
    trigger: str
    delay_minutes: int = Field(default=0, ge=0, le=60 * 24 * 90)
    conditions: dict = Field(default_factory=dict)
    action: str
    params: dict = Field(default_factory=dict)
    active: bool = True


def automation_json(rule: CrmAutomation) -> dict:
    return {"id": rule.id, "pipeline_id": rule.pipeline_id, "stage_id": rule.stage_id, "name": rule.name,
            "trigger": rule.trigger, "delay_minutes": rule.delay_minutes, "conditions": rule.conditions or {},
            "action": rule.action, "params": {k: v for k, v in (rule.params or {}).items() if k != "rr_index"},
            "active": rule.active, "fired_count": rule.fired_count or 0, "last_fired_at": rule.last_fired_at,
            "summary": crm_automation.describe(rule)}


async def validate_automation(db: AsyncSession, project_id: int, payload: AutomationIn) -> CrmPipeline:
    pipeline = await db.get(CrmPipeline, payload.pipeline_id)
    if not pipeline or pipeline.project_id != project_id:
        raise HTTPException(404, "Воронка не найдена")
    if payload.trigger not in crm_automation.TRIGGERS or payload.action not in crm_automation.ACTIONS:
        raise HTTPException(422, "Неизвестный триггер или действие")
    if payload.stage_id:
        await stage_for(db, pipeline, payload.stage_id)
    elif payload.trigger == "STAGE_ENTER":
        raise HTTPException(422, "Выберите этап")
    if payload.trigger == "NO_ACTIVITY" and payload.delay_minutes < 15:
        raise HTTPException(422, "Минимальное время без активности — 15 минут")
    params = payload.params
    if payload.action == "CREATE_TASK" and not str(params.get("title") or "").strip():
        raise HTTPException(422, "Укажите название задачи")
    if payload.action == "ADD_TAG" and not str(params.get("tag") or "").strip():
        raise HTTPException(422, "Укажите тег")
    if payload.action == "SET_RESPONSIBLE":
        ids = [params.get("user_id")] if params.get("mode") != "round_robin" else params.get("user_ids") or []
        if not ids or any(not uid for uid in ids):
            raise HTTPException(422, "Выберите сотрудников")
        for uid in ids:
            await validate_owner(db, project_id, pipeline.workspace_id, int(uid))
    return pipeline


@router.get("/projects/{project_id}/automations")
async def list_automations(project_id: int, db: AsyncSession = Depends(get_db),
                           user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm"); await project_for(db, user, project_id)
    rows = (await db.scalars(select(CrmAutomation).where(CrmAutomation.project_id == project_id)
                             .order_by(CrmAutomation.pipeline_id, CrmAutomation.id))).all()
    return [automation_json(row) for row in rows]


@router.post("/projects/{project_id}/automations", status_code=201)
async def create_automation(project_id: int, payload: AutomationIn, request: Request,
                            db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "manage_pipeline")
    project = await project_for(db, user, project_id)
    await validate_automation(db, project.id, payload)
    rule = CrmAutomation(workspace_id=project.workspace_id, project_id=project.id, **payload.model_dump())
    db.add(rule); await db.commit(); await db.refresh(rule)
    return automation_json(rule)


@router.put("/automations/{rule_id}")
async def edit_automation(rule_id: int, payload: AutomationIn, request: Request,
                          db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "manage_pipeline")
    rule = await db.get(CrmAutomation, rule_id)
    if not rule or rule.workspace_id != user.workspace_id:
        raise HTTPException(404, "Правило не найдено")
    await project_for(db, user, rule.project_id)
    await validate_automation(db, rule.project_id, payload)
    for key, value in payload.model_dump().items():
        setattr(rule, key, value)
    await db.commit()
    return automation_json(rule)


@router.delete("/automations/{rule_id}", status_code=204)
async def delete_automation(rule_id: int, request: Request, db: AsyncSession = Depends(get_db),
                            user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "manage_pipeline")
    rule = await db.get(CrmAutomation, rule_id)
    if not rule or rule.workspace_id != user.workspace_id:
        raise HTTPException(404, "Правило не найдено")
    await project_for(db, user, rule.project_id)
    await db.delete(rule); await db.commit()


@router.post("/projects/{project_id}/automations/recommended", status_code=201)
async def install_recommended(project_id: int, pipeline_id: int, request: Request,
                              db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "manage_pipeline")
    project = await project_for(db, user, project_id)
    pipeline = await pipeline_for(db, project.id, pipeline_id)
    stages = (await db.scalars(select(CrmStage).where(CrmStage.pipeline_id == pipeline.id,
                                                      CrmStage.archived_at.is_(None)))).all()
    existing = {row.name for row in (await db.scalars(select(CrmAutomation).where(
        CrmAutomation.pipeline_id == pipeline.id))).all()}
    created = 0
    for rule in crm_automation.recommended(pipeline.id, list(stages)):
        if rule["name"] in existing:
            continue
        db.add(CrmAutomation(workspace_id=project.workspace_id, project_id=project.id, active=True,
                             conditions={}, delay_minutes=rule.pop("delay_minutes", 0), **rule))
        created += 1
    await db.commit()
    return {"created": created}


# --------------------------------------------------------------------------- sales analytics

def _aware(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _ratio(top, bottom):
    return round(top / bottom * 100, 1) if bottom else None


@router.get("/projects/{project_id}/report")
async def sales_report(project_id: int, start: date | None = None, end: date | None = None,
                       pipeline_id: int | None = None, db: AsyncSession = Depends(get_db),
                       user: PortalUser = Depends(require_portal_user)):
    """Sales analytics for deals created in the period (cohort view, like amoCRM «Анализ продаж»)."""
    require_permission(user, "view_crm")
    project = await project_for(db, user, project_id)
    end = end or now().date()
    start = start or end - timedelta(days=29)
    if end < start or (end - start).days > 366:
        raise HTTPException(422, "Выберите период до 367 дней")
    pipeline = await pipeline_for(db, project.id, pipeline_id)
    stages = (await db.scalars(select(CrmStage).where(CrmStage.pipeline_id == pipeline.id)
                               .order_by(CrmStage.position, CrmStage.id))).all()
    stage_map = {s.id: s for s in stages}
    lower = datetime.combine(start, time.min, tzinfo=timezone.utc)
    upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc)
    query = select(CrmDeal).where(CrmDeal.pipeline_id == pipeline.id, CrmDeal.created_at >= lower,
                                  CrmDeal.created_at < upper)
    if "view_all_deals" not in effective_permissions(user):
        query = query.where(CrmDeal.responsible_user_id == user.id)
    deals = (await db.scalars(query.limit(10000))).all()
    ids = [d.id for d in deals]
    history = (await db.scalars(select(CrmStageHistory).where(CrmStageHistory.deal_id.in_(ids)))).all() if ids else []
    visited = defaultdict(set)
    for row in history:
        visited[row.deal_id].add(row.to_stage_id)
    sales = (await db.scalars(select(ClientSale).where(ClientSale.deal_id.in_(ids)))).all() if ids else []
    revenue_by_deal = defaultdict(float)
    for sale in sales:
        revenue_by_deal[sale.deal_id] += float(sale.amount or 0)
    users = {u.id: u.display_name for u in (await db.scalars(select(PortalUser).where(
        PortalUser.workspace_id == project.workspace_id))).all()}
    sources = {s.id: s.name for s in (await db.scalars(select(ProjectSource).where(
        ProjectSource.project_id == project.id))).all()}
    reasons = {r.id: r.label for r in (await db.scalars(select(ProjectLostReason).where(
        ProjectLostReason.project_id == project.id))).all()}

    def kind(deal):
        return stage_map[deal.stage_id].analytics_type if deal.stage_id in stage_map else "LEAD"

    def won(deal):
        return deal.id in revenue_by_deal or kind(deal) == "WON"

    # Funnel: a deal "reached" a stage if it visited it or any later open/won stage.
    progress = [s for s in stages if s.analytics_type != "LOST" and s.archived_at is None]
    order = {s.id: index for index, s in enumerate(progress)}
    reach = []
    for deal in deals:
        steps = [order[sid] for sid in visited[deal.id] | {deal.stage_id} if sid in order]
        reach.append(max(steps) if steps else 0)
    funnel = []
    for index, stage in enumerate(progress):
        count = sum(1 for r in reach if r >= index)
        previous = funnel[-1]["count"] if funnel else None
        funnel.append({"stage_id": stage.id, "name": stage.name, "analytics_type": stage.analytics_type,
                       "color": stage.color, "count": count, "conversion_from_previous": _ratio(count, previous),
                       "conversion_from_start": _ratio(count, len(deals))})
    won_deals = [d for d in deals if won(d)]
    lost_deals = [d for d in deals if kind(d) == "LOST" and not won(d)]
    open_deals = [d for d in deals if d not in won_deals and d not in lost_deals]
    revenue = sum(revenue_by_deal.values())
    cycles = [(_aware(d.closed_at) - _aware(d.created_at)).total_seconds() / 86400
              for d in won_deals if d.closed_at and d.created_at]
    responses = [(_aware(d.first_response_at) - _aware(d.created_at)).total_seconds() / 60
                 for d in deals if d.first_response_at and d.created_at]
    no_response_open = [d for d in open_deals if not d.first_response_at]

    # Historical stage-to-win probability for the forecast (all closed deals of the pipeline).
    closed_all = (await db.scalars(select(CrmDeal).where(CrmDeal.pipeline_id == pipeline.id,
                                                         CrmDeal.closed_at.is_not(None)).limit(20000))).all()
    closed_ids = [d.id for d in closed_all]
    closed_hist = defaultdict(set)
    if closed_ids:
        for row in (await db.scalars(select(CrmStageHistory).where(CrmStageHistory.deal_id.in_(closed_ids)))).all():
            closed_hist[row.deal_id].add(row.to_stage_id)
    probability = {}
    for stage in progress:
        through = [d for d in closed_all if stage.id in closed_hist[d.id] | {d.stage_id}]
        wins = sum(1 for d in through if stage_map.get(d.stage_id) and stage_map[d.stage_id].analytics_type == "WON")
        default = {"LEAD": 0.1, "QUALIFIED": 0.3, "WON": 1.0}.get(stage.analytics_type, 0.2)
        probability[stage.id] = wins / len(through) if len(through) >= 10 else default
    pipeline_open = (await db.scalars(select(CrmDeal).where(CrmDeal.pipeline_id == pipeline.id,
                                                            CrmDeal.archived_at.is_(None), CrmDeal.closed_at.is_(None)))).all()
    forecast = sum(float(d.amount or 0) * probability.get(d.stage_id, 0) for d in pipeline_open)

    def group(key_fn, names):
        buckets = defaultdict(list)
        for deal in deals:
            buckets[key_fn(deal)].append(deal)
        rows = []
        for key, items in buckets.items():
            w = [d for d in items if won(d)]
            rev = sum(revenue_by_deal.get(d.id, 0) for d in w)
            resp = [(_aware(d.first_response_at) - _aware(d.created_at)).total_seconds() / 60
                    for d in items if d.first_response_at and d.created_at]
            rows.append({"id": key, "name": names.get(key) or "Не указан", "deals": len(items), "won": len(w),
                         "lost": sum(1 for d in items if kind(d) == "LOST" and not won(d)),
                         "revenue": rev, "conversion": _ratio(len(w), len(items)),
                         "average_check": rev / len(w) if w else None,
                         "median_response_minutes": round(statistics.median(resp), 1) if resp else None})
        return sorted(rows, key=lambda r: (-r["revenue"], -r["deals"]))

    managers = group(lambda d: d.responsible_user_id, users)
    open_tasks = (await db.scalars(select(CrmTask).where(CrmTask.project_id == project.id, CrmTask.status == "OPEN",
                                                         CrmTask.due_at < now()))).all()
    overdue = Counter(t.responsible_user_id for t in open_tasks)
    for row in managers:
        row["overdue_tasks"] = overdue.get(row["id"], 0)
    lost_reasons = Counter(reasons.get(d.lost_reason_id, "Без причины") for d in lost_deals)
    idle = [d for d in pipeline_open if days_since(d.last_activity_at or d.updated_at) is not None
            and days_since(d.last_activity_at or d.updated_at) >= 3]
    return {"period": {"start": start, "end": end}, "pipeline": {"id": pipeline.id, "name": pipeline.name},
            "totals": {"deals": len(deals), "won": len(won_deals), "lost": len(lost_deals), "open": len(open_deals),
                       "revenue": revenue, "win_rate": _ratio(len(won_deals), len(won_deals) + len(lost_deals)),
                       "conversion": _ratio(len(won_deals), len(deals)),
                       "average_check": revenue / len(won_deals) if won_deals else None,
                       "average_cycle_days": round(statistics.mean(cycles), 1) if cycles else None,
                       "median_response_minutes": round(statistics.median(responses), 1) if responses else None,
                       "responded_in_15_min": _ratio(sum(1 for m in responses if m <= 15), len(deals)),
                       "open_without_response": len(no_response_open),
                       "pipeline_value": sum(float(d.amount or 0) for d in pipeline_open),
                       "forecast": round(forecast, 2), "idle_3_days": len(idle)},
            "funnel": funnel, "managers": managers,
            "sources": group(lambda d: d.source_id, sources),
            "lost_reasons": [{"name": name, "count": count} for name, count in lost_reasons.most_common()],
            "stage_probability": [{"stage_id": s.id, "name": s.name, "probability": round(probability[s.id] * 100, 1)}
                                  for s in progress]}
