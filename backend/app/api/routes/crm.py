"""Client CRM. Existing ClientLead/ClientSale remain the reporting facts."""
import json
import re
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import check_origin, require_portal_user
from app.core.permissions import effective_permissions, require_permission
from app.db import get_db
from app.services.notifications import flush_telegram, notify
from app.services import crm_automation
from app.models.crm import (CrmActivity, CrmContact, CrmCustomFieldDefinition, CrmDeal, CrmInbound,
                            CrmPipeline, CrmStage, CrmStageHistory, CrmTask, CrmTaskType)
from app.models.marketing import (AdCampaignMetricDaily, AdConnection, AdHypothesisCampaign, ClientLead, ClientLeadAttribution, ClientLeadEvent, ClientSale,
                                  LeadInboundSource, PortalProjectAccess, PortalUser, Project, ProjectLostReason,
                                  ProjectSource)

router = APIRouter(prefix="/crm", tags=["crm"], dependencies=[Depends(require_portal_user)])
TASK_TYPES = {"CALL": "Позвонить", "MEETING": "Встреча", "MESSAGE": "Написать", "SEND": "Отправить",
              "FOLLOW_UP": "Связаться повторно", "OTHER": "Другое"}
REJECT_REASONS = {"SPAM", "DUPLICATE", "TEST", "INVALID", "NOT_TARGET", "OTHER"}


def now():
    return datetime.now(timezone.utc)


def json_value(value):
    if isinstance(value, Decimal): return float(value)
    if isinstance(value, datetime): return value.isoformat()
    return value


def norm_phone(value: str | None):
    """Digits only, Russian numbers unified to 7XXXXXXXXXX so 8 999… and +7 999… match."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits[0] == "9":
        digits = "7" + digits
    return digits


def task_state(task: CrmTask | None):
    if not task:
        return "NO_TASK"
    due = task.due_at
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    today = now().date()
    return "OVERDUE" if due.date() < today else "TODAY" if due.date() == today else "PLANNED"


async def validate_custom_values(db: AsyncSession, project_id: int, values: dict):
    definitions = (await db.scalars(select(CrmCustomFieldDefinition).where(
        CrmCustomFieldDefinition.project_id == project_id,
        CrmCustomFieldDefinition.archived_at.is_(None)))).all()
    known = {item.key: item for item in definitions}
    if set(values) - set(known):
        raise HTTPException(422, "Неизвестное пользовательское поле")
    for key, value in values.items():
        if value is None:
            continue
        kind = known[key].field_type
        valid = (kind in {"TEXT", "PHONE", "EMAIL", "DATE", "DATETIME"} and isinstance(value, str)) or (
            kind in {"NUMBER", "MONEY"} and isinstance(value, (int, float)) and not isinstance(value, bool)) or (
            kind == "BOOLEAN" and isinstance(value, bool)) or (
            kind == "SELECT" and isinstance(value, str) and value in (known[key].options or [])) or (
            kind == "MULTISELECT" and isinstance(value, list) and all(isinstance(v, str) and v in (known[key].options or []) for v in value))
        if not valid:
            raise HTTPException(422, f"Некорректное значение поля {key}")


async def validate_required_fields(db: AsyncSession, project_id: int, keys: list[str]):
    custom = set((await db.scalars(select(CrmCustomFieldDefinition.key).where(
        CrmCustomFieldDefinition.project_id == project_id,
        CrmCustomFieldDefinition.archived_at.is_(None)))).all())
    if set(keys) - custom - {"name", "amount", "responsible_user_id", "source_id"}:
        raise HTTPException(422, "Обязательное поле не существует")


async def project_for(db: AsyncSession, user: PortalUser, project_id: int):
    from app.models.marketing import ClientWorkspace
    workspace = await db.get(ClientWorkspace, user.workspace_id)
    if not user.active or not workspace or workspace.status == "deleted":
        raise HTTPException(403, "Доступ отключён")
    project = await db.get(Project, project_id)
    if not project or project.workspace_id != user.workspace_id:
        raise HTTPException(404, "Проект не найден")
    if user.role != "client_owner" and not await db.scalar(select(PortalProjectAccess.id).where(
        PortalProjectAccess.user_id == user.id, PortalProjectAccess.project_id == project_id)):
        raise HTTPException(403, "Нет доступа к проекту")
    return project


def own_filter(query, user: PortalUser):
    if "view_all_deals" not in effective_permissions(user):
        if "view_own_deals" not in effective_permissions(user):
            raise HTTPException(403, "Нет доступа к сделкам")
        query = query.where(CrmDeal.responsible_user_id == user.id)
    return query


async def deal_for(db: AsyncSession, user: PortalUser, deal_id: int):
    deal = await db.get(CrmDeal, deal_id)
    if not deal or deal.workspace_id != user.workspace_id:
        raise HTTPException(404, "Сделка не найдена")
    await project_for(db, user, deal.project_id)
    if "view_all_deals" not in effective_permissions(user) and deal.responsible_user_id != user.id:
        raise HTTPException(403, "Сделка назначена другому сотруднику")
    return deal


HUMAN_TOUCH = {"COMMENT_ADDED", "TASK_COMPLETED", "STAGE_CHANGED", "CALL_LOGGED", "MESSAGE_SENT", "MEETING_HELD",
               "DEAL_WON", "DEAL_LOST", "SALE_CREATED"}


def activity(db, deal: CrmDeal | None, user: PortalUser | None, kind: str, payload: dict,
             inbound: CrmInbound | None = None, touch: bool = True):
    if deal is not None and touch:
        deal.last_activity_at = now()
        if user is not None and deal.first_response_at is None and kind in HUMAN_TOUCH:
            deal.first_response_at = now()  # speed-to-lead: first real action by a person
    db.add(CrmActivity(workspace_id=(deal or inbound).workspace_id, project_id=(deal or inbound).project_id,
                       deal_id=deal.id if deal else None, inbound_id=inbound.id if inbound else None,
                       actor_id=user.id if user else None, actor_name=user.display_name if user else "Система",
                       event_type=kind, payload=payload))


async def pipeline_for(db: AsyncSession, project_id: int, pipeline_id: int | None = None, commit: bool = True):
    query = select(CrmPipeline).where(CrmPipeline.project_id == project_id, CrmPipeline.archived_at.is_(None))
    if pipeline_id:
        query = query.where(CrmPipeline.id == pipeline_id)
    else:
        query = query.order_by(CrmPipeline.is_default.desc(), CrmPipeline.id)
    pipeline = await db.scalar(query.limit(1))
    if not pipeline and pipeline_id is None:
        project = await db.get(Project, project_id)
        pipeline = CrmPipeline(workspace_id=project.workspace_id, project_id=project.id,
                               name="Основная воронка", is_default=True)
        db.add(pipeline); await db.flush()
        for position, (name, kind, color) in enumerate((
            ("Новый лид", "LEAD", "#006BFD"), ("Квалифицирован", "QUALIFIED", "#8555E8"),
            ("Продажа", "WON", "#15A86B"), ("Отказ", "LOST", "#EF4A59"))):
            db.add(CrmStage(pipeline_id=pipeline.id, name=name, analytics_type=kind, color=color,
                            position=position, required_fields=[]))
        await db.flush()
        if commit:  # GET screens persist the default pipeline; write paths commit themselves
            await db.commit()
    if not pipeline:
        raise HTTPException(404, "Воронка не найдена")
    return pipeline


async def stage_for(db: AsyncSession, pipeline: CrmPipeline, stage_id: int):
    stage = await db.get(CrmStage, stage_id)
    if not stage or stage.pipeline_id != pipeline.id or stage.archived_at:
        raise HTTPException(422, "Этап недоступен")
    return stage


async def contact_for(db: AsyncSession, project_id: int, contact_id: int):
    contact = await db.get(CrmContact, contact_id)
    if not contact or contact.project_id != project_id:
        raise HTTPException(404, "Контакт не найден")
    return contact


async def validate_owner(db: AsyncSession, project_id: int, workspace_id: int, owner_id: int | None):
    if owner_id is None:
        return
    owner = await db.get(PortalUser, owner_id)
    if not owner or owner.workspace_id != workspace_id or not owner.active:
        raise HTTPException(422, "Ответственный недоступен")
    if owner.role != "client_owner" and not await db.scalar(select(PortalProjectAccess.id).where(
        PortalProjectAccess.project_id == project_id, PortalProjectAccess.user_id == owner_id)):
        raise HTTPException(422, "У ответственного нет доступа к проекту")


async def validate_source(db: AsyncSession, project_id: int, source_id: int | None):
    if source_id is not None:
        source = await db.get(ProjectSource, source_id)
        if not source or source.project_id != project_id:
            raise HTTPException(422, "Источник недоступен")


async def duplicate_contacts(db: AsyncSession, project_id: int, phone: str | None, email: str | None):
    if not phone and not email:
        return []
    digits = norm_phone(phone)
    lowered = (email or "").lower()
    conditions = []
    if digits: conditions.append(CrmContact.phone_normalized == digits)
    if lowered: conditions.append(CrmContact.email_normalized == lowered)
    contacts = (await db.scalars(select(CrmContact).where(CrmContact.project_id == project_id,
                     or_(*conditions)).limit(10))).all()
    return [{"id": c.id, "name": c.name} for c in contacts if
            (digits and any(norm_phone(p) == digits for p in c.phones or [])) or
            (lowered and any(e.lower() == lowered for e in c.emails or []))][:10]


def inbound_telegram(payload: dict):
    if payload.get("external_source") == "telegram_bot":
        contact = str(payload.get("contact") or "").strip()
        return contact[:120] if contact.startswith("@") else None
    if payload.get("external_source") != "tilda":
        return None
    contact = str(payload.get("contact") or "").strip()
    method = str(payload.get("contact_method") or "").strip().casefold()
    if method in {"telegram", "телеграм", "телеграмма", "телеграмм", "tg"} or (
            not method and re.fullmatch(r"@[A-Za-z0-9_]{5,32}", contact)):
        return contact[:120] or None
    return None


def contact_json(contact: CrmContact, payload: dict | None = None):
    return {"id": contact.id, "name": contact.name, "phones": contact.phones or [], "emails": contact.emails or [],
            "telegram": contact.telegram or inbound_telegram(payload or {}), "company": contact.company, "custom_fields": contact.custom_fields or {},
            "created_at": contact.created_at}


def deal_json(deal: CrmDeal, contact: CrmContact, stage: CrmStage, task: CrmTask | None = None,
              source_name: str | None = None, owner_name: str | None = None):
    return {"id": deal.id, "name": deal.name, "amount": float(deal.amount) if deal.amount is not None else None,
            "project_id": deal.project_id, "contact": contact_json(contact), "lead_id": deal.lead_id,
            "inbound_id": deal.inbound_id, "pipeline_id": deal.pipeline_id, "stage_id": deal.stage_id,
            "stage_name": stage.name, "analytics_type": stage.analytics_type, "responsible_user_id": deal.responsible_user_id,
            "source_id": deal.source_id, "source_name": source_name, "responsible_name": owner_name,
            "origin": deal.origin, "custom_fields": deal.custom_fields or {},
            "attribution_snapshot": deal.attribution_snapshot or {}, "lost_reason_id": deal.lost_reason_id,
            "archived_at": deal.archived_at, "created_at": deal.created_at, "updated_at": deal.updated_at,
            "tags": deal.tags or [], "stage_entered_at": deal.stage_entered_at,
            "days_in_stage": days_since(deal.stage_entered_at or deal.created_at),
            "last_activity_at": deal.last_activity_at,
            "idle_days": days_since(deal.last_activity_at or deal.updated_at or deal.created_at),
            "first_response_at": deal.first_response_at, "lost_comment": deal.lost_comment,
            "closed_at": deal.closed_at,
            "next_task": task_json(task) if task else None, "task_state": task_state(task)}


def days_since(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0, (now() - value).days)


def task_json(task: CrmTask):
    return {"id": task.id, "project_id": task.project_id, "deal_id": task.deal_id, "contact_id": task.contact_id,
            "type_code": task.type_code, "title": task.title, "description": task.description,
            "responsible_user_id": task.responsible_user_id, "due_at": task.due_at,
            "duration_minutes": task.duration_minutes, "priority": task.priority, "status": task.status,
            "result": task.result, "completed_at": task.completed_at, "created_at": task.created_at}


class ContactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int
    name: str = Field(min_length=2, max_length=180)
    phone: str | None = None
    email: str | None = None
    telegram: str | None = None
    company: str | None = None


class DealCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int
    contact_id: int | None = None
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    name: str = Field(min_length=2, max_length=220)
    pipeline_id: int | None = None
    stage_id: int | None = None
    responsible_user_id: int | None = None
    amount: float | None = Field(default=None, ge=0)
    source_id: int | None = None
    origin: str = "MANUAL"
    created_at: datetime | None = None
    custom_fields: dict = Field(default_factory=dict)
    comment: str | None = None


class DealUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=220)
    tags: list[str] | None = Field(default=None, max_length=20)
    amount: float | None = Field(default=None, ge=0)
    responsible_user_id: int | None = None
    source_id: int | None = None
    custom_fields: dict | None = None


class StageMove(BaseModel):
    stage_id: int
    lost_reason_id: int | None = None
    lost_comment: str | None = None


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int
    deal_id: int | None = None
    contact_id: int | None = None
    type_code: str = "OTHER"
    title: str = Field(min_length=2, max_length=220)
    description: str | None = None
    responsible_user_id: int | None = None
    due_at: datetime
    duration_minutes: int | None = Field(default=None, ge=1)
    priority: str = "NORMAL"


class NextTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type_code: str = "CALL"
    title: str = Field(min_length=2, max_length=220)
    due_at: datetime
    responsible_user_id: int | None = None


class TaskComplete(BaseModel):
    result: str = Field(min_length=1, max_length=4000)
    next_task: NextTask | None = None


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type_code: str | None = None
    title: str | None = Field(default=None, min_length=2, max_length=220)
    description: str | None = None
    responsible_user_id: int | None = None
    due_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=1)
    priority: str | None = None
    status: str | None = None


class InboundAction(BaseModel):
    contact_id: int | None = None
    deal_name: str | None = None
    responsible_user_id: int | None = None
    rejection_reason: str | None = None


class StageCreate(BaseModel):
    name: str = Field(min_length=2, max_length=180)
    analytics_type: str
    color: str = "#006BFD"
    position: int = 0
    required_fields: list[str] = Field(default_factory=list)


class StageUpdate(BaseModel):
    name: str | None = None
    color: str | None = None
    position: int | None = None
    required_fields: list[str] | None = None
    archived: bool | None = None


class CommentCreate(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class SaleCreate(BaseModel):
    amount: float = Field(gt=0)
    occurred_at: datetime | None = None
    comment: str | None = None


async def create_deal_fact(db: AsyncSession, project: Project, contact: CrmContact, pipeline: CrmPipeline,
                           stage: CrmStage, name: str, owner_id: int | None, amount: float | None,
                           source_id: int | None, origin: str, custom_fields: dict | None = None,
                           inbound: CrmInbound | None = None, actor: PortalUser | None = None,
                           created_at: datetime | None = None):
    if not (contact.phones or contact.emails or contact.telegram or
            (inbound and (inbound.raw_payload.get("contact_consent") or (
                inbound.raw_payload.get("external_source") in {"tilda", "avito", "telegram_bot", "whatsapp", "phone"} and inbound.raw_payload.get("contact"))))):
        raise HTTPException(422, "Для лида нужен контакт или подтверждённое согласие на связь")
    # A real contact is required before this point. Lead remains the shared analytics fact.
    source = await db.get(ProjectSource, source_id) if source_id else None
    lead = ClientLead(workspace_id=project.workspace_id, project_id=project.id, source_id=source_id,
                      full_name=contact.name, phone=(contact.phones or [None])[0], email=(contact.emails or [None])[0],
                      telegram=contact.telegram, source=source.name if source else "Не определено",
                      assigned_to_id=owner_id, value=amount, status="new",
                      **({"created_at": created_at} if created_at else {}))
    db.add(lead); await db.flush()
    snapshot = dict(inbound.attribution or {}) if inbound else {}
    deal = CrmDeal(workspace_id=project.workspace_id, project_id=project.id, contact_id=contact.id,
                   lead_id=lead.id, inbound_id=inbound.id if inbound else None, pipeline_id=pipeline.id,
                   stage_id=stage.id, responsible_user_id=owner_id, name=name, amount=amount,
                   source_id=source_id, origin=origin, custom_fields=custom_fields or {}, attribution_snapshot=snapshot or {},
                   tags=[], stage_entered_at=now(), last_activity_at=now(), automation_state={},
                   **({"created_at": created_at} if created_at else {}))
    db.add(deal); await db.flush()
    db.add(ClientLeadEvent(workspace_id=project.workspace_id, lead_id=lead.id,
                           actor_id=actor.id if actor else None, event_type="LEAD_CREATED",
                           description="Создана сделка CRM"))
    if inbound:
        campaign_id = (snapshot or {}).get("external_campaign_id")
        connection_id = None
        hypothesis_id = None
        # Server-side integrations (Avito chats/calls) know their cabinet for sure; a payload cannot set this key.
        verified = (snapshot or {}).get("verified_connection_id")
        if verified and str(verified).isdigit():
            verified_row = await db.get(AdConnection, int(verified))
            if verified_row and verified_row.project_id == project.id:
                connection_id = verified_row.id
                snapshot = {**snapshot, "connection_id": verified_row.id, "platform": verified_row.platform,
                            "ad_account": verified_row.name}
        if campaign_id and connection_id is None:
            metric_candidates = (await db.scalars(select(AdConnection.id).join(
                AdCampaignMetricDaily, AdCampaignMetricDaily.connection_id == AdConnection.id).where(
                AdConnection.project_id == project.id,
                AdCampaignMetricDaily.external_campaign_id == campaign_id).distinct().limit(2))).all()
            linked_candidates = (await db.scalars(select(AdConnection.id).join(
                AdHypothesisCampaign, AdHypothesisCampaign.connection_id == AdConnection.id).where(
                AdConnection.project_id == project.id,
                AdHypothesisCampaign.external_campaign_id == campaign_id).distinct().limit(2))).all()
            candidates = set(metric_candidates) | set(linked_candidates)
            hint = snapshot.get("connection_id")
            if hint and str(hint).isdigit() and int(hint) in candidates:
                connection_id = int(hint)
            elif len(candidates) == 1:
                connection_id = next(iter(candidates))
            if connection_id:
                hypothesis_id = await db.scalar(select(AdHypothesisCampaign.hypothesis_id).where(
                    AdHypothesisCampaign.connection_id == connection_id,
                    AdHypothesisCampaign.external_campaign_id == campaign_id))
                connection = await db.get(AdConnection, connection_id)
                snapshot = {**snapshot, "connection_id": connection_id,
                            "hypothesis_id": hypothesis_id,
                            "platform": connection.platform if connection else None,
                            "ad_account": connection.name if connection else None}
            else:
                snapshot.pop("connection_id", None)
        deal.attribution_snapshot = snapshot
        db.add(ClientLeadAttribution(lead_id=lead.id, source_id=inbound.inbound_source_id,
                                     connection_id=connection_id,
                                     hypothesis_id=hypothesis_id,
                                     external_campaign_id=(snapshot or {}).get("external_campaign_id"),
                                     external_ad_id=(snapshot or {}).get("external_ad_id"),
                                     utm_source=(snapshot or {}).get("utm_source"),
                                     utm_medium=(snapshot or {}).get("utm_medium"),
                                     utm_campaign=(snapshot or {}).get("utm_campaign"),
                                     utm_content=(snapshot or {}).get("utm_content"),
                                     utm_term=(snapshot or {}).get("utm_term"),
                                     landing_url=(snapshot or {}).get("landing_url")))
    db.add(CrmStageHistory(deal_id=deal.id, from_stage_id=None, to_stage_id=stage.id,
                           actor_id=actor.id if actor else None))
    activity(db, deal, actor, "DEAL_CREATED", {"stage_id": stage.id, "lead_id": lead.id}, inbound)
    await crm_automation.on_stage_enter(db, deal, stage, created=True)
    return deal


@router.get("/projects/{project_id}/board")
async def board(project_id: int, pipeline_id: int | None = None, page: int = Query(1, ge=1),
                per_stage: int = Query(20, ge=1, le=100), state: str | None = None,
                search: str | None = None, archived: bool = False, responsible_user_id: int | None = None,
                source_id: int | None = None, tag: str | None = None, mine: bool = False,
                idle_days: int | None = Query(None, ge=1, le=365), db: AsyncSession = Depends(get_db),
                user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm")
    await project_for(db, user, project_id)
    pipeline = await pipeline_for(db, project_id, pipeline_id)
    stages = (await db.scalars(select(CrmStage).where(CrmStage.pipeline_id == pipeline.id,
                    CrmStage.archived_at.is_(None)).order_by(CrmStage.position, CrmStage.id))).all()
    due = select(func.min(CrmTask.due_at)).where(CrmTask.deal_id == CrmDeal.id,
                                                CrmTask.status == "OPEN").scalar_subquery()
    midnight = datetime.combine(now().date(), time.min, tzinfo=timezone.utc)
    tomorrow = midnight + timedelta(days=1)
    count_base = own_filter(select(CrmDeal.id).where(CrmDeal.project_id == project_id,
        CrmDeal.pipeline_id == pipeline.id, (CrmDeal.archived_at.is_not(None) if archived else CrmDeal.archived_at.is_(None)),
        CrmDeal.stage_id.in_([stage.id for stage in stages])), user)
    def narrow(query):
        if search:
            term = f"%{search.strip()}%"
            query = query.where(or_(CrmDeal.name.ilike(term), CrmDeal.contact_id.in_(select(CrmContact.id).where(
                CrmContact.project_id == project_id, or_(CrmContact.name.ilike(term), CrmContact.phones.cast(String).ilike(term),
                                                         CrmContact.emails.cast(String).ilike(term), CrmContact.company.ilike(term))))))
        owner = user.id if mine else responsible_user_id
        if owner: query = query.where(CrmDeal.responsible_user_id == owner)
        if source_id: query = query.where(CrmDeal.source_id == source_id)
        if tag:
            # JSON may be stored with escaped non-ASCII; match both spellings, LIKE-escaped.
            label = tag.strip()
            query = query.where(or_(CrmDeal.tags.cast(String).contains(json.dumps(label), autoescape=True),
                                    CrmDeal.tags.cast(String).contains(json.dumps(label, ensure_ascii=False), autoescape=True)))
        if idle_days: query = query.where(CrmDeal.last_activity_at <= now() - timedelta(days=idle_days))
        return query
    count_base = narrow(count_base)
    counts = {}
    state_conditions = {"OVERDUE": due < midnight, "TODAY": (due >= midnight) & (due < tomorrow),
                        "PLANNED": due >= tomorrow, "NO_TASK": due.is_(None)}
    for key, condition in state_conditions.items():
        counts[key] = await db.scalar(select(func.count()).select_from(count_base.where(condition).subquery())) or 0
    columns = []
    for stage in stages:
        base = own_filter(select(CrmDeal, CrmContact, ProjectSource.name, PortalUser.display_name)
                          .join(CrmContact, CrmContact.id == CrmDeal.contact_id)
                          .outerjoin(ProjectSource, ProjectSource.id == CrmDeal.source_id)
                          .outerjoin(PortalUser, PortalUser.id == CrmDeal.responsible_user_id)
                          .where(CrmDeal.project_id == project_id, CrmDeal.stage_id == stage.id,
                                 (CrmDeal.archived_at.is_not(None) if archived else CrmDeal.archived_at.is_(None))), user)
        base = narrow(base)
        if state in state_conditions:
            base = base.where(state_conditions[state])
        total = await db.scalar(select(func.count()).select_from(base.subquery())) or 0
        amount_total = await db.scalar(select(func.coalesce(func.sum(base.subquery().c.amount), 0))) or 0
        rows = (await db.execute(base.order_by(CrmDeal.updated_at.desc(), CrmDeal.id.desc())
                                 .offset((page - 1) * per_stage).limit(per_stage))).all()
        ids = [deal.id for deal, *_ in rows]
        task_map = {}
        if ids:
            tasks = (await db.scalars(select(CrmTask).where(CrmTask.deal_id.in_(ids), CrmTask.status == "OPEN")
                                      .order_by(CrmTask.due_at, CrmTask.id))).all()
            for task in tasks:
                task_map.setdefault(task.deal_id, task)
        items = []
        for deal, contact, source_name, owner_name in rows:
            item = deal_json(deal, contact, stage, task_map.get(deal.id), source_name, owner_name)
            items.append(item)
        columns.append({"stage": {"id": stage.id, "name": stage.name, "analytics_type": stage.analytics_type,
                                     "color": stage.color, "required_fields": stage.required_fields or []},
                        "total": total, "amount": float(amount_total), "deals": items, "has_more": page * per_stage < total})
    inbound_count = await db.scalar(select(func.count(CrmInbound.id)).where(
        CrmInbound.project_id == project_id, CrmInbound.status == "NEW")) or 0
    return {"pipeline": {"id": pipeline.id, "name": pipeline.name}, "columns": columns,
            "control": counts, "inbound_count": inbound_count, "page": page, "per_stage": per_stage}


@router.get("/projects/{project_id}/pipelines")
async def pipelines(project_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm")
    await project_for(db, user, project_id)
    await pipeline_for(db, project_id)
    rows = (await db.scalars(select(CrmPipeline).where(CrmPipeline.project_id == project_id,
                  CrmPipeline.archived_at.is_(None)).order_by(CrmPipeline.is_default.desc(), CrmPipeline.id))).all()
    return [{"id": p.id, "name": p.name, "is_default": p.is_default, "stages": [
        {"id": s.id, "name": s.name, "analytics_type": s.analytics_type, "color": s.color,
         "position": s.position, "required_fields": s.required_fields or []}
        for s in (await db.scalars(select(CrmStage).where(CrmStage.pipeline_id == p.id,
                CrmStage.archived_at.is_(None)).order_by(CrmStage.position, CrmStage.id))).all()]} for p in rows]


@router.post("/pipelines/{pipeline_id}/stages", status_code=201)
async def add_stage(pipeline_id: int, payload: StageCreate, request: Request, db: AsyncSession = Depends(get_db),
                    user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "manage_pipeline")
    pipeline = await db.get(CrmPipeline, pipeline_id)
    if not pipeline:
        raise HTTPException(404, "Воронка не найдена")
    await project_for(db, user, pipeline.project_id)
    if payload.analytics_type not in {"LEAD", "QUALIFIED", "WON", "LOST"}:
        raise HTTPException(422, "Неверный аналитический тип")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", payload.color):
        raise HTTPException(422, "Неверный цвет")
    await validate_required_fields(db, pipeline.project_id, payload.required_fields)
    stage = CrmStage(pipeline_id=pipeline_id, **payload.model_dump())
    db.add(stage); await db.commit(); await db.refresh(stage)
    return {"id": stage.id}


@router.patch("/stages/{stage_id}")
async def edit_stage(stage_id: int, payload: StageUpdate, request: Request, db: AsyncSession = Depends(get_db),
                     user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "manage_pipeline")
    stage = await db.get(CrmStage, stage_id)
    if not stage:
        raise HTTPException(404, "Этап не найден")
    pipeline = await db.get(CrmPipeline, stage.pipeline_id)
    await project_for(db, user, pipeline.project_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("color") and not re.fullmatch(r"#[0-9a-fA-F]{6}", changes["color"]):
        raise HTTPException(422, "Неверный цвет")
    if changes.get("required_fields") is not None:
        await validate_required_fields(db, pipeline.project_id, changes["required_fields"])
    if changes.pop("archived", False):
        if await db.scalar(select(CrmDeal.id).where(CrmDeal.stage_id == stage.id, CrmDeal.archived_at.is_(None)).limit(1)):
            raise HTTPException(409, "Сначала переместите активные сделки")
        replacement = await db.scalar(select(CrmStage.id).where(
            CrmStage.pipeline_id == pipeline.id, CrmStage.analytics_type == stage.analytics_type,
            CrmStage.id != stage.id, CrmStage.archived_at.is_(None)).limit(1))
        if not replacement:
            raise HTTPException(409, "Сначала добавьте другой этап с тем же аналитическим типом")
        stage.archived_at = now()
    for key, value in changes.items():
        setattr(stage, key, value)
    await db.commit()
    return {"ok": True}


@router.get("/projects/{project_id}/contacts")
async def contacts(project_id: int, search: str | None = None, page: int = Query(1, ge=1),
                   limit: int = Query(30, ge=1, le=100), db: AsyncSession = Depends(get_db),
                   user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm")
    await project_for(db, user, project_id)
    query = select(CrmContact).where(CrmContact.project_id == project_id)
    if search:
        query = query.where(or_(CrmContact.name.ilike(f"%{search}%"),
                                CrmContact.phones.cast(String).ilike(f"%{search}%"),
                                CrmContact.emails.cast(String).ilike(f"%{search}%"),
                                CrmContact.telegram.ilike(f"%{search}%")))
    rows = (await db.scalars(query.order_by(CrmContact.id.desc()).offset((page-1)*limit).limit(limit))).all()
    # Recover older Tilda contacts for display from their original, scoped request.
    originals = {}
    if rows:
        linked = (await db.execute(select(CrmDeal.contact_id, CrmInbound.raw_payload)
            .join(CrmInbound, CrmInbound.id == CrmDeal.inbound_id)
            .where(CrmDeal.project_id == project_id, CrmInbound.project_id == project_id,
                   CrmDeal.contact_id.in_([c.id for c in rows]))
            .order_by(CrmDeal.id.desc()))).all()
        for contact_id, payload in linked:
            if inbound_telegram(payload or {}):
                originals.setdefault(contact_id, payload)
    return {"items": [contact_json(c, originals.get(c.id)) for c in rows], "page": page}


@router.get("/projects/{project_id}/sources")
async def crm_sources(project_id: int, db: AsyncSession = Depends(get_db),
                      user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm"); await project_for(db, user, project_id)
    rows = (await db.scalars(select(ProjectSource).where(ProjectSource.project_id == project_id,
                     ProjectSource.status == "active").order_by(ProjectSource.name))).all()
    return [{"id": row.id, "name": row.name} for row in rows]


@router.get("/projects/{project_id}/contacts/duplicates")
async def contact_duplicates(project_id: int, phone: str | None = None, email: str | None = None,
                             db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm"); await project_for(db, user, project_id)
    return {"items": await duplicate_contacts(db, project_id, phone, email)}


@router.post("/contacts", status_code=201)
async def create_contact(payload: ContactCreate, request: Request, db: AsyncSession = Depends(get_db),
                         user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "create_deal")
    project = await project_for(db, user, payload.project_id)
    duplicates = await duplicate_contacts(db, project.id, payload.phone, payload.email)
    contact = CrmContact(workspace_id=project.workspace_id, project_id=project.id, name=payload.name.strip(),
                         phones=[payload.phone] if payload.phone else [], emails=[payload.email.lower()] if payload.email else [],
                         phone_normalized=norm_phone(payload.phone) or None,
                         email_normalized=payload.email.lower() if payload.email else None,
                         telegram=payload.telegram, company=payload.company)
    db.add(contact); await db.commit(); await db.refresh(contact)
    return {**contact_json(contact), "potential_duplicates": duplicates}


@router.post("/deals", status_code=201)
async def create_deal(payload: DealCreate, request: Request, db: AsyncSession = Depends(get_db),
                      user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "create_deal")
    project = await project_for(db, user, payload.project_id)
    pipeline = await pipeline_for(db, project.id, payload.pipeline_id)
    stage = await stage_for(db, pipeline, payload.stage_id) if payload.stage_id else await db.scalar(
        select(CrmStage).where(CrmStage.pipeline_id == pipeline.id, CrmStage.analytics_type == "LEAD",
                               CrmStage.archived_at.is_(None)).order_by(CrmStage.position).limit(1))
    if not stage or stage.analytics_type != "LEAD":
        raise HTTPException(422, "Новая сделка должна начинаться с этапа Lead")
    await validate_owner(db, project.id, project.workspace_id, payload.responsible_user_id)
    await validate_source(db, project.id, payload.source_id)
    if payload.origin not in {"AUTO", "MANUAL", "IMPORT", "API", "INTEGRATION"}:
        raise HTTPException(422, "Неверное происхождение")
    await validate_custom_values(db, project.id, payload.custom_fields)
    if payload.contact_id:
        contact = await contact_for(db, project.id, payload.contact_id)
        duplicates = []
    else:
        if not payload.contact_name or not (payload.phone or payload.email):
            raise HTTPException(422, "Укажите контакт и телефон или email")
        duplicates = await duplicate_contacts(db, project.id, payload.phone, payload.email)
        contact = CrmContact(workspace_id=project.workspace_id, project_id=project.id,
                             name=payload.contact_name, phones=[payload.phone] if payload.phone else [],
                             emails=[payload.email.lower()] if payload.email else [],
                             phone_normalized=norm_phone(payload.phone) or None,
                             email_normalized=payload.email.lower() if payload.email else None)
        db.add(contact); await db.flush()
    deal = await create_deal_fact(db, project, contact, pipeline, stage, payload.name,
                                   payload.responsible_user_id or user.id, payload.amount,
                                   payload.source_id, payload.origin, payload.custom_fields, actor=user,
                                   created_at=payload.created_at)
    if payload.comment:
        activity(db, deal, user, "COMMENT_ADDED", {"text": payload.comment})
    from app.services.tg_preferences import lead_buttons
    await notify(db, project.id, "new_lead", "Новый лид", f"{contact.name} · создан вручную ({user.display_name})",
                 assignee_id=deal.responsible_user_id, actor_id=user.id,
                 details=[*(contact.phones or [])[:1], *(contact.emails or [])[:1]], reply_markup=lead_buttons(deal_id=deal.id))
    await db.commit(); await db.refresh(deal)
    flush_telegram(db)
    return {"id": deal.id, "lead_id": deal.lead_id, "potential_duplicates": duplicates}


@router.get("/deals/{deal_id}")
async def deal_detail(deal_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm")
    deal = await deal_for(db, user, deal_id)
    contact = await db.get(CrmContact, deal.contact_id)
    stage = await db.get(CrmStage, deal.stage_id)
    source = await db.get(ProjectSource, deal.source_id) if deal.source_id else None
    owner = await db.get(PortalUser, deal.responsible_user_id) if deal.responsible_user_id else None
    tasks = (await db.scalars(select(CrmTask).where(CrmTask.deal_id == deal.id)
                              .order_by(CrmTask.status, CrmTask.due_at))).all()
    events = (await db.scalars(select(CrmActivity).where(CrmActivity.deal_id == deal.id)
                               .order_by(CrmActivity.created_at.desc(), CrmActivity.id.desc()).limit(100))).all()
    inbound_events = (await db.scalars(select(CrmActivity).where(CrmActivity.inbound_id == deal.inbound_id,
                             CrmActivity.deal_id.is_(None)).order_by(CrmActivity.created_at.desc()).limit(100))).all() if deal.inbound_id else []
    legacy_events = (await db.scalars(select(ClientLeadEvent).where(ClientLeadEvent.lead_id == deal.lead_id)
                                      .order_by(ClientLeadEvent.created_at.desc()).limit(100))).all() if deal.lead_id else []
    inbound = await db.get(CrmInbound, deal.inbound_id) if deal.inbound_id else None
    lead = await db.get(ClientLead, deal.lead_id) if deal.lead_id else None
    sales = (await db.scalars(select(ClientSale).where(ClientSale.deal_id == deal.id)
                              .order_by(ClientSale.occurred_at.desc()))).all()
    return {**deal_json(deal, contact, stage, next((t for t in tasks if t.status == "OPEN"), None),
                        source.name if source else None, owner.display_name if owner else None),
            "tasks": [task_json(t) for t in tasks],
            "activities": sorted(
                [{"id": f"crm-{e.id}", "event_type": e.event_type, "actor_name": e.actor_name,
                  "payload": e.payload, "created_at": e.created_at} for e in [*events, *inbound_events]] +
                [{"id": f"lead-{e.id}", "event_type": e.event_type, "actor_name": None,
                  "payload": {"text": e.description}, "created_at": e.created_at} for e in legacy_events],
                key=lambda item: item["created_at"], reverse=True),
            "form_data": inbound.raw_payload if inbound else None,
            "quality": lead.quality if lead else None, "quality_reason": lead.quality_reason if lead else None,
            "contact": contact_json(contact, inbound.raw_payload if inbound else None),
            "sales": [{"id": s.id, "amount": float(s.amount) if s.amount is not None else None,
                       "occurred_at": s.occurred_at} for s in sales]}


@router.get("/leads/{lead_id}/deal")
async def deal_by_lead(lead_id: int, db: AsyncSession = Depends(get_db),
                       user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm")
    deal = await db.scalar(select(CrmDeal).where(CrmDeal.lead_id == lead_id,
                                                 CrmDeal.workspace_id == user.workspace_id))
    if not deal:
        raise HTTPException(404, "Сделка не найдена")
    await deal_for(db, user, deal.id)
    return {"deal_id": deal.id}


@router.patch("/deals/{deal_id}")
async def update_deal(deal_id: int, payload: DealUpdate, request: Request, db: AsyncSession = Depends(get_db),
                      user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    return await update_deal_core(deal_id, payload, db, user)


async def update_deal_core(deal_id: int, payload: DealUpdate, db: AsyncSession, user: PortalUser, *, commit: bool = True):
    require_permission(user, "edit_deal")
    deal = await deal_for(db, user, deal_id)
    changes = payload.model_dump(exclude_unset=True)
    if "source_id" in changes and changes["source_id"] != deal.source_id:
        require_permission(user, "change_attribution")
        await validate_source(db, deal.project_id, changes["source_id"])
    if "responsible_user_id" in changes:
        await validate_owner(db, deal.project_id, deal.workspace_id, changes["responsible_user_id"])
    lead = await db.get(ClientLead, deal.lead_id) if deal.lead_id else None
    if "tags" in changes:
        changes["tags"] = crm_automation.normalize_tags(changes["tags"])
    for key, value in changes.items():
        old = getattr(deal, key)
        if old == value:
            continue
        if key == "custom_fields":
            await validate_custom_values(db, deal.project_id, value or {})
        setattr(deal, key, value)
        activity(db, deal, user, "ATTRIBUTION_CHANGED" if key == "source_id" else
                 "OWNER_CHANGED" if key == "responsible_user_id" else "TAGS_CHANGED" if key == "tags" else "FIELD_CHANGED",
                 {"field": key, "old": json_value(old), "new": json_value(value)})
        if lead and key == "amount": lead.value = value
        if lead and key == "responsible_user_id": lead.assigned_to_id = value
        if lead and key == "source_id":
            lead.source_id = value
            source = await db.get(ProjectSource, value) if value else None
            lead.source = source.name if source else "Не определено"
            attribution = await db.scalar(select(ClientLeadAttribution).where(ClientLeadAttribution.lead_id == lead.id))
            if attribution:
                attribution.source_id = source.inbound_source_id if source else None
                if not source or source.connection_id != attribution.connection_id:
                    attribution.connection_id = None
                    attribution.hypothesis_id = None
                    attribution.external_campaign_id = None
                    attribution.external_ad_id = None
    if commit:
        await db.commit()
    return {"ok": True}


@router.post("/deals/{deal_id}/move")
async def move_deal(deal_id: int, payload: StageMove, request: Request, db: AsyncSession = Depends(get_db),
                    user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "move_deal")
    deal = await deal_for(db, user, deal_id)
    pipeline = await pipeline_for(db, deal.project_id, deal.pipeline_id)
    stage = await stage_for(db, pipeline, payload.stage_id)
    old_stage = await db.get(CrmStage, deal.stage_id)
    if old_stage.id == stage.id:
        return {"ok": True}
    if old_stage.analytics_type in {"WON", "LOST"} and stage.analytics_type != old_stage.analytics_type:
        raise HTTPException(409, "Закрытую сделку нельзя перевести в другой аналитический тип без отдельной процедуры")
    if old_stage.analytics_type == "QUALIFIED" and stage.analytics_type == "LEAD":
        raise HTTPException(409, "Квалифицированную сделку нельзя понизить до лида без отдельной процедуры")
    def field_value(key: str):
        if key in {"name", "amount", "responsible_user_id", "source_id"}:
            return getattr(deal, key)
        return (deal.custom_fields or {}).get(key)
    missing = [key for key in stage.required_fields or [] if field_value(key) is None or field_value(key) == ""]
    if stage.analytics_type == "WON" and deal.amount is None:
        missing.append("amount")
    if stage.analytics_type == "LOST":
        reason = await db.get(ProjectLostReason, payload.lost_reason_id) if payload.lost_reason_id else None
        if not reason or reason.project_id != deal.project_id or reason.status != "active":
            missing.append("lost_reason")
    if missing:
        raise HTTPException(422, {"message": "Заполните обязательные поля", "fields": missing})
    old_id = deal.stage_id
    deal.stage_id = stage.id
    deal.stage_entered_at = now()
    if stage.analytics_type == "LOST":
        deal.lost_reason_id = payload.lost_reason_id
        deal.lost_comment = payload.lost_comment
        deal.lost_at = now(); deal.closed_at = now()
    elif stage.analytics_type == "WON":
        deal.closed_at = now()
    db.add(CrmStageHistory(deal_id=deal.id, from_stage_id=old_id, to_stage_id=stage.id, actor_id=user.id))
    activity(db, deal, user, "STAGE_CHANGED", {"from": old_stage.name, "to": stage.name,
                                            "analytics_type": stage.analytics_type})
    lead = await db.get(ClientLead, deal.lead_id) if deal.lead_id else None
    if lead:
        if stage.analytics_type in {"QUALIFIED", "WON"} and lead.quality is None:
            lead.quality = "target"  # reaching qualification means the lead was a real client
        if stage.analytics_type in {"QUALIFIED", "WON"}:
            from app.services import conversions
            await conversions.record(db, deal, "qualified")
        if stage.analytics_type == "QUALIFIED" and lead.qualified_at is None:
            lead.qualified_at = now()
            db.add(ClientLeadEvent(workspace_id=lead.workspace_id, lead_id=lead.id, actor_id=user.id,
                                   event_type="LEAD_QUALIFIED", description="Квалифицировано в CRM"))
        if stage.analytics_type == "LOST":
            lead.status = "lost"
            lead.lost_reason = reason.label
        elif stage.analytics_type == "WON":
            # A won stage alone is not a confirmed Sale. Reporting stays tied to Sales.
            lead.status = "qualified"
        elif stage.analytics_type == "QUALIFIED": lead.status = "qualified"
        elif lead.status not in {"qualified", "won"}: lead.status = "new"
    if stage.analytics_type == "WON": activity(db, deal, user, "DEAL_WON", {"sale_required": True})
    if stage.analytics_type == "LOST": activity(db, deal, user, "DEAL_LOST", {"reason_id": payload.lost_reason_id})
    automations = await crm_automation.on_stage_enter(db, deal, stage)
    await db.commit()
    flush_telegram(db)
    return {"ok": True, "sale_required": stage.analytics_type == "WON", "automations": automations}


@router.post("/deals/{deal_id}/sales", status_code=201)
async def create_deal_sale(deal_id: int, payload: SaleCreate, request: Request, db: AsyncSession = Depends(get_db),
                           user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "create_sale")
    deal = await deal_for(db, user, deal_id)
    if not deal.lead_id:
        raise HTTPException(422, "У сделки нет подтверждённого лида")
    stage = await db.get(CrmStage, deal.stage_id)
    if stage.analytics_type == "LOST":
        raise HTTPException(409, "Нельзя подтвердить продажу по потерянной сделке")
    sale = ClientSale(lead_id=deal.lead_id, deal_id=deal.id, project_id=deal.project_id,
                      amount=payload.amount, occurred_at=payload.occurred_at or now(),
                      comment=payload.comment, confirmed_by_id=user.id)
    db.add(sale)
    lead = await db.get(ClientLead, deal.lead_id)
    lead.status = "won"
    if not lead.qualified_at: lead.qualified_at = now()
    lead.quality, lead.quality_reason = "target", None
    if stage.analytics_type != "WON":
        won = await db.scalar(select(CrmStage).where(CrmStage.pipeline_id == deal.pipeline_id,
                        CrmStage.analytics_type == "WON", CrmStage.archived_at.is_(None)).limit(1))
        if won:
            db.add(CrmStageHistory(deal_id=deal.id, from_stage_id=deal.stage_id, to_stage_id=won.id, actor_id=user.id))
            deal.stage_id = won.id
    deal.closed_at = now()
    activity(db, deal, user, "SALE_CREATED", {"amount": payload.amount})
    from app.services import conversions
    await conversions.record(db, deal, "qualified")
    await conversions.record(db, deal, "sale", float(payload.amount), payload.occurred_at or now())
    db.add(ClientLeadEvent(workspace_id=lead.workspace_id, lead_id=lead.id, actor_id=user.id,
                           event_type="SALE_CREATED", description=f"Продажа {payload.amount} ₽"))
    await notify(db, deal.project_id, "new_sale", "Новая продажа", f"{deal.name} · {payload.amount:,.2f} ₽",
                 assignee_id=deal.responsible_user_id, actor_id=user.id)
    await db.commit(); await db.refresh(sale)
    flush_telegram(db)
    return {"id": sale.id, "deal_id": deal.id}


QUALITY_REASONS = ["Спам или ошибка", "Не та услуга", "Не наш регион", "Нет бюджета", "Дубль", "Не выходит на связь", "Другое"]


class QualityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quality: str | None = Field(default=None, pattern="^(target|non_target)$")
    reason: str | None = Field(default=None, max_length=120)


@router.post("/deals/{deal_id}/quality")
async def set_quality(deal_id: int, payload: QualityIn, request: Request, db: AsyncSession = Depends(get_db),
                      user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    return await set_quality_core(deal_id, payload, db, user)


async def set_quality_core(deal_id: int, payload: QualityIn, db: AsyncSession, user: PortalUser, *, commit: bool = True):
    """Целевой / нецелевой: feedback from sales to the marketer, counted per campaign in analytics."""
    require_permission(user, "edit_deal")
    deal = await deal_for(db, user, deal_id)
    lead = await db.get(ClientLead, deal.lead_id) if deal.lead_id else None
    if lead is None:
        raise HTTPException(422, "У сделки нет лида для аналитики")
    if payload.quality == "non_target" and not (payload.reason or "").strip():
        raise HTTPException(422, "Укажите, почему заявка нецелевая — это увидит маркетолог")
    lead.quality = payload.quality
    lead.quality_reason = ((payload.reason or "").strip()[:120] or None) if payload.quality == "non_target" else None
    text = {"target": "Заявка отмечена как целевая", "non_target": f"Заявка нецелевая: {lead.quality_reason}",
            None: "Отметка качества снята"}[payload.quality]
    activity(db, deal, user, "QUALITY_CHANGED", {"quality": payload.quality, "reason": lead.quality_reason, "text": text})
    if commit:
        await db.commit()
    return {"quality": lead.quality, "quality_reason": lead.quality_reason}


@router.get("/quality-reasons")
async def quality_reasons():
    return QUALITY_REASONS


@router.post("/deals/{deal_id}/comments", status_code=201)
async def add_comment(deal_id: int, payload: CommentCreate, request: Request, db: AsyncSession = Depends(get_db),
                      user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "edit_deal")
    deal = await deal_for(db, user, deal_id)
    activity(db, deal, user, "COMMENT_ADDED", {"text": payload.text.strip()})
    await db.commit()
    return {"ok": True}


@router.post("/deals/{deal_id}/archive")
async def archive_deal(deal_id: int, request: Request, db: AsyncSession = Depends(get_db),
                       user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "archive_deal")
    deal = await deal_for(db, user, deal_id)
    deal.archived_at = now()
    activity(db, deal, user, "SYSTEM", {"action": "archived"})
    await db.commit()
    return {"ok": True}


@router.post("/deals/{deal_id}/restore")
async def restore_deal(deal_id: int, request: Request, db: AsyncSession = Depends(get_db),
                       user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "archive_deal")
    deal = await deal_for(db, user, deal_id)
    if deal.archived_at:
        deal.archived_at = None
        activity(db, deal, user, "SYSTEM", {"action": "restored"})
    await db.commit()
    return {"ok": True}


@router.get("/projects/{project_id}/inbound")
async def inbound_list(project_id: int, page: int = Query(1, ge=1), limit: int = Query(30, ge=1, le=100),
                       status: str = "NEW", db: AsyncSession = Depends(get_db),
                       user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm"); await project_for(db, user, project_id)
    rows = (await db.scalars(select(CrmInbound).where(CrmInbound.project_id == project_id,
              CrmInbound.status == status).order_by(CrmInbound.received_at.desc(), CrmInbound.id.desc())
              .offset((page - 1) * limit).limit(limit))).all()
    return {"items": [{"id": item.id, "name": item.name, "phone": item.phone, "email": item.email,
                       "origin": item.origin, "status": item.status, "raw_payload": item.raw_payload,
                       "attribution": item.attribution, "received_at": item.received_at,
                       "potential_duplicates": await duplicate_contacts(db, project_id, item.phone, item.email)}
                      for item in rows], "page": page}


async def round_robin_owner(db: AsyncSession, project: Project, inbound: CrmInbound) -> int | None:
    if not inbound.inbound_source_id:
        return None
    inbound_source = await db.get(LeadInboundSource, inbound.inbound_source_id)
    if not inbound_source or not inbound_source.auto_assign:
        return None
    managers = (await db.scalars(select(PortalUser).where(
        PortalUser.workspace_id == project.workspace_id, PortalUser.active.is_(True),
        PortalUser.role == "sales_manager",
        or_(PortalUser.id.in_(select(PortalProjectAccess.user_id).where(
            PortalProjectAccess.project_id == project.id)), PortalUser.role == "client_owner"))
        .order_by(PortalUser.id))).all()
    if not managers:
        managers = (await db.scalars(select(PortalUser).where(
            PortalUser.workspace_id == project.workspace_id, PortalUser.active.is_(True),
            PortalUser.role.in_(["sales_head", "client_owner"]),
            or_(PortalUser.role == "client_owner", PortalUser.id.in_(select(PortalProjectAccess.user_id).where(
                PortalProjectAccess.project_id == project.id))))
            .order_by(PortalUser.id))).all()
    if not managers:
        return None
    last = next((index for index, member in enumerate(managers)
                 if member.id == inbound_source.last_assigned_to_id), -1)
    owner_id = managers[(last + 1) % len(managers)].id
    inbound_source.last_assigned_to_id = owner_id
    return owner_id


async def accept_core(db: AsyncSession, inbound: CrmInbound, project: Project, actor: PortalUser | None, *,
                      contact_id: int | None = None, responsible_user_id: int | None = None,
                      deal_name: str | None = None, merge_existing: bool = False) -> CrmDeal:
    """Turn a request into a deal (manual accept or a trusted source). Does not commit.

    merge_existing (automatic path): a known phone reuses its contact, and a repeat request from a client
    with an open deal is attached to that deal instead of creating a duplicate."""
    contact = None
    if contact_id:
        contact = await contact_for(db, project.id, contact_id)
    elif merge_existing and norm_phone(inbound.phone):
        contact = await db.scalar(select(CrmContact).where(CrmContact.project_id == project.id,
                                                           CrmContact.phone_normalized == norm_phone(inbound.phone)).limit(1))
    if contact is None:
        contact = CrmContact(workspace_id=project.workspace_id, project_id=project.id,
                             name=inbound.name or "Без имени", phones=[inbound.phone] if inbound.phone else [],
                             emails=[inbound.email] if inbound.email else [],
                             phone_normalized=norm_phone(inbound.phone) or None,
                             email_normalized=inbound.email.lower() if inbound.email else None,
                             telegram=inbound_telegram(inbound.raw_payload))
        db.add(contact); await db.flush()
    deal = None
    if merge_existing:
        deal = await db.scalar(select(CrmDeal).where(CrmDeal.contact_id == contact.id, CrmDeal.archived_at.is_(None),
                                                     CrmDeal.closed_at.is_(None)).order_by(CrmDeal.created_at.desc()).limit(1))
        if deal:
            activity(db, deal, None, "INBOUND_REPEAT", {"inbound_id": inbound.id, "text": "Повторное обращение клиента",
                                                        "source": (inbound.raw_payload or {}).get("source")}, inbound, touch=False)
    if deal is None:
        pipeline = await pipeline_for(db, project.id, commit=False)
        stage = await db.scalar(select(CrmStage).where(CrmStage.pipeline_id == pipeline.id,
                        CrmStage.analytics_type == "LEAD", CrmStage.archived_at.is_(None))
                        .order_by(CrmStage.position).limit(1))
        owner_id = responsible_user_id or await round_robin_owner(db, project, inbound)
        deal = await create_deal_fact(db, project, contact, pipeline, stage,
                                       deal_name or f"Заявка — {contact.name}", owner_id,
                                       inbound.raw_payload.get("value"), inbound.source_id, inbound.origin,
                                       inbound=inbound, actor=actor)
    inbound.status = "ACCEPTED"; inbound.contact_id = contact.id; inbound.deal_id = deal.id; inbound.processed_at = now()
    activity(db, deal, actor, "INBOUND_ACCEPTED", {"inbound_id": inbound.id, "auto": actor is None}, inbound,
             touch=actor is not None)
    from app.services.messaging import link_inbound
    await link_inbound(db, inbound, contact.id, deal)
    from app.services.telephony import link_inbound as link_inbound_calls
    await link_inbound_calls(db, inbound, contact.id, deal)
    return deal


@router.post("/inbound/{inbound_id}/accept")
async def accept_inbound(inbound_id: int, payload: InboundAction, request: Request,
                         db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    return await accept_inbound_core(inbound_id, payload, db, user)


async def accept_inbound_core(inbound_id: int, payload: InboundAction, db: AsyncSession, user: PortalUser, *, commit: bool = True):
    require_permission(user, "create_deal")
    inbound = await db.get(CrmInbound, inbound_id, with_for_update=True)
    if not inbound or inbound.workspace_id != user.workspace_id:
        raise HTTPException(404, "Заявка не найдена")
    project = await project_for(db, user, inbound.project_id)
    if inbound.status != "NEW":
        raise HTTPException(409, "Заявка уже обработана")
    await validate_owner(db, project.id, project.workspace_id, payload.responsible_user_id)
    deal = await accept_core(db, inbound, project, user, contact_id=payload.contact_id,
                             responsible_user_id=payload.responsible_user_id, deal_name=payload.deal_name)
    if commit:
        await db.commit()
        flush_telegram(db)
    return {"deal_id": deal.id, "contact_id": deal.contact_id, "lead_id": deal.lead_id}


@router.post("/inbound/{inbound_id}/reject")
async def reject_inbound(inbound_id: int, payload: InboundAction, request: Request,
                         db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "edit_deal")
    inbound = await db.get(CrmInbound, inbound_id)
    if not inbound or inbound.workspace_id != user.workspace_id:
        raise HTTPException(404, "Заявка не найдена")
    await project_for(db, user, inbound.project_id)
    if inbound.status != "NEW":
        raise HTTPException(409, "Заявка уже обработана")
    if payload.rejection_reason not in REJECT_REASONS:
        raise HTTPException(422, "Укажите причину отклонения")
    inbound.status = "REJECTED"; inbound.rejection_reason = payload.rejection_reason; inbound.processed_at = now()
    activity(db, None, user, "INBOUND_REJECTED", {"reason": payload.rejection_reason}, inbound)
    await db.commit()
    return {"ok": True}


@router.get("/projects/{project_id}/tasks")
async def tasks(project_id: int, status: str | None = None, responsible_user_id: int | None = None,
                type_code: str | None = None, due_start: datetime | None = None,
                due_end: datetime | None = None, pipeline_id: int | None = None,
                stage_id: int | None = None, source_id: int | None = None,
                page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=100),
                db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm"); await project_for(db, user, project_id)
    query = select(CrmTask).where(CrmTask.project_id == project_id)
    if "view_all_deals" not in effective_permissions(user):
        query = query.where(CrmTask.responsible_user_id == user.id)
    if status: query = query.where(CrmTask.status == status)
    if responsible_user_id: query = query.where(CrmTask.responsible_user_id == responsible_user_id)
    if type_code: query = query.where(CrmTask.type_code == type_code)
    if due_start: query = query.where(CrmTask.due_at >= due_start)
    if due_end: query = query.where(CrmTask.due_at <= due_end)
    if pipeline_id or stage_id or source_id:
        query = query.join(CrmDeal, CrmDeal.id == CrmTask.deal_id)
        if pipeline_id: query = query.where(CrmDeal.pipeline_id == pipeline_id)
        if stage_id: query = query.where(CrmDeal.stage_id == stage_id)
        if source_id: query = query.where(CrmDeal.source_id == source_id)
    rows = (await db.scalars(query.order_by(CrmTask.due_at, CrmTask.id)
                              .offset((page - 1) * limit).limit(limit))).all()
    deal_ids = {t.deal_id for t in rows if t.deal_id}
    contact_ids = {t.contact_id for t in rows if t.contact_id}
    owner_ids = {t.responsible_user_id for t in rows if t.responsible_user_id}
    deals = {d.id: d for d in (await db.scalars(select(CrmDeal).where(CrmDeal.id.in_(deal_ids)))).all()} if deal_ids else {}
    contacts = {c.id: c for c in (await db.scalars(select(CrmContact).where(CrmContact.id.in_(contact_ids)))).all()} if contact_ids else {}
    owners = {o.id: o for o in (await db.scalars(select(PortalUser).where(PortalUser.id.in_(owner_ids)))).all()} if owner_ids else {}
    return {"items": [{**task_json(t), "deal_name": deals[t.deal_id].name if t.deal_id in deals else None,
                       "contact_name": contacts[t.contact_id].name if t.contact_id in contacts else None,
                       "responsible_name": owners[t.responsible_user_id].display_name if t.responsible_user_id in owners else None}
                      for t in rows], "page": page}


@router.post("/tasks", status_code=201)
async def create_task(payload: TaskCreate, request: Request, db: AsyncSession = Depends(get_db),
                      user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    return await create_task_core(payload, db, user)


async def create_task_core(payload: TaskCreate, db: AsyncSession, user: PortalUser, *, commit: bool = True):
    require_permission(user, "manage_tasks")
    project = await project_for(db, user, payload.project_id)
    deal = await deal_for(db, user, payload.deal_id) if payload.deal_id else None
    if deal and deal.project_id != project.id:
        raise HTTPException(422, "Сделка из другого проекта")
    contact = await contact_for(db, project.id, payload.contact_id) if payload.contact_id else None
    if not deal and not contact:
        raise HTTPException(422, "Укажите сделку или контакт")
    if payload.type_code not in TASK_TYPES and not await db.scalar(select(CrmTaskType.id).where(
            CrmTaskType.project_id == project.id, CrmTaskType.code == payload.type_code,
            CrmTaskType.archived_at.is_(None))):
        raise HTTPException(422, "Неизвестный тип задачи")
    if payload.priority not in {"LOW", "NORMAL", "HIGH"}:
        raise HTTPException(422, "Неверный приоритет")
    owner_id = payload.responsible_user_id or (deal.responsible_user_id if deal else user.id)
    await validate_owner(db, project.id, project.workspace_id, owner_id)
    task = CrmTask(workspace_id=project.workspace_id, project_id=project.id, deal_id=deal.id if deal else None,
                   contact_id=contact.id if contact else deal.contact_id if deal else None,
                   type_code=payload.type_code, title=payload.title, description=payload.description,
                   responsible_user_id=owner_id, due_at=payload.due_at,
                   duration_minutes=payload.duration_minutes, priority=payload.priority, created_by_id=user.id)
    db.add(task); await db.flush()
    if deal: activity(db, deal, user, "TASK_CREATED", {"task_id": task.id, "title": task.title})
    from app.services.notifications import direct
    from app.services.tg_preferences import task_buttons
    owner = await db.get(PortalUser, owner_id) if owner_id else None
    if owner:
        direct(db, project.workspace_id, [owner.id], "Новая задача", task.title, {owner.id: owner},
               reply_markup=task_buttons(task.id, task.deal_id), project_id=project.id)
    if commit:
        await db.commit()
        flush_telegram(db)
    return task_json(task)


@router.post("/tasks/{task_id}/complete")
async def complete_task(task_id: int, payload: TaskComplete, request: Request,
                        db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    return await complete_task_core(task_id, payload, db, user)


async def complete_task_core(task_id: int, payload: TaskComplete, db: AsyncSession, user: PortalUser, *, commit: bool = True):
    require_permission(user, "manage_tasks")
    task = await db.get(CrmTask, task_id)
    if not task or task.workspace_id != user.workspace_id:
        raise HTTPException(404, "Задача не найдена")
    await project_for(db, user, task.project_id)
    if "view_all_deals" not in effective_permissions(user) and task.responsible_user_id != user.id:
        raise HTTPException(403, "Задача назначена другому сотруднику")
    if task.status != "OPEN":
        raise HTTPException(409, "Задача уже закрыта")
    task.status = "COMPLETED"; task.result = payload.result; task.completed_at = now()
    deal = await deal_for(db, user, task.deal_id) if task.deal_id else None
    if deal:
        activity(db, deal, user, "TASK_COMPLETED", {"task_id": task.id, "title": task.title, "result": payload.result})
    next_task = None
    if payload.next_task:
        follow = payload.next_task
        if follow.type_code not in TASK_TYPES:
            raise HTTPException(422, "Неизвестный тип задачи")
        owner_id = follow.responsible_user_id or task.responsible_user_id or user.id
        await validate_owner(db, task.project_id, task.workspace_id, owner_id)
        next_task = CrmTask(workspace_id=task.workspace_id, project_id=task.project_id, deal_id=task.deal_id,
                            contact_id=task.contact_id, type_code=follow.type_code, title=follow.title,
                            responsible_user_id=owner_id, due_at=follow.due_at, created_by_id=user.id)
        db.add(next_task); await db.flush()
        from app.services.notifications import direct
        from app.services.tg_preferences import task_buttons
        owner = await db.get(PortalUser, owner_id) if owner_id else None
        if owner:
            direct(db, task.workspace_id, [owner.id], "Новая задача", next_task.title, {owner.id: owner},
                   reply_markup=task_buttons(next_task.id, next_task.deal_id), project_id=task.project_id)
        if deal:
            activity(db, deal, user, "TASK_CREATED", {"task_id": next_task.id, "title": next_task.title}, touch=False)
    if commit:
        await db.commit()
        flush_telegram(db)
    return {"ok": True, "next_task": task_json(next_task) if next_task else None}


@router.patch("/tasks/{task_id}")
async def update_task(task_id: int, payload: TaskUpdate, request: Request,
                      db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    return await update_task_core(task_id, payload, db, user)


async def update_task_core(task_id: int, payload: TaskUpdate, db: AsyncSession, user: PortalUser, *, commit: bool = True):
    require_permission(user, "manage_tasks")
    task = await db.get(CrmTask, task_id)
    if not task or task.workspace_id != user.workspace_id:
        raise HTTPException(404, "Задача не найдена")
    await project_for(db, user, task.project_id)
    if "view_all_deals" not in effective_permissions(user) and task.responsible_user_id != user.id:
        raise HTTPException(403, "Задача назначена другому сотруднику")
    changes = payload.model_dump(exclude_unset=True)
    if "responsible_user_id" in changes:
        await validate_owner(db, task.project_id, task.workspace_id, changes["responsible_user_id"])
    if changes.get("priority") and changes["priority"] not in {"LOW", "NORMAL", "HIGH"}:
        raise HTTPException(422, "Неверный приоритет")
    if changes.get("type_code") and changes["type_code"] not in TASK_TYPES and not await db.scalar(
        select(CrmTaskType.id).where(CrmTaskType.project_id == task.project_id,
                                     CrmTaskType.code == changes["type_code"])):
        raise HTTPException(422, "Неизвестный тип задачи")
    if "status" in changes and changes["status"] != "CANCELLED":
        raise HTTPException(422, "Для завершения задачи используйте отдельное действие")
    def serializable(value):
        return value.isoformat() if isinstance(value, datetime) else value
    old = {key: serializable(getattr(task, key)) for key in changes}
    for key, value in changes.items(): setattr(task, key, value)
    if task.deal_id:
        deal = await deal_for(db, user, task.deal_id)
        activity(db, deal, user, "TASK_UPDATED", {"task_id": task.id, "old": old,
                                                  "new": {key: serializable(value) for key, value in changes.items()}})
    if commit:
        await db.commit()
    return task_json(task)


@router.get("/projects/{project_id}/custom-fields")
async def fields(project_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm"); await project_for(db, user, project_id)
    rows = (await db.scalars(select(CrmCustomFieldDefinition).where(
        CrmCustomFieldDefinition.project_id == project_id, CrmCustomFieldDefinition.archived_at.is_(None)))).all()
    return [{"id": x.id, "key": x.key, "name": x.name, "field_type": x.field_type,
             "options": x.options} for x in rows]


class FieldCreate(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,79}$")
    name: str = Field(min_length=2, max_length=140)
    field_type: str
    options: list[str] | None = None


@router.post("/projects/{project_id}/custom-fields", status_code=201)
async def add_field(project_id: int, payload: FieldCreate, request: Request, db: AsyncSession = Depends(get_db),
                    user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "manage_custom_fields")
    project = await project_for(db, user, project_id)
    if payload.field_type not in {"TEXT", "NUMBER", "MONEY", "DATE", "DATETIME", "SELECT", "MULTISELECT", "BOOLEAN", "PHONE", "EMAIL"}:
        raise HTTPException(422, "Неверный тип поля")
    if await db.scalar(select(CrmCustomFieldDefinition.id).where(CrmCustomFieldDefinition.project_id == project.id,
                                                                    CrmCustomFieldDefinition.key == payload.key)):
        raise HTTPException(409, "Ключ поля уже используется")
    field = CrmCustomFieldDefinition(workspace_id=project.workspace_id, project_id=project.id, **payload.model_dump())
    db.add(field); await db.commit(); await db.refresh(field)
    return {"id": field.id}
