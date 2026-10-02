from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import check_origin, portal_session_user, session_user
from app.core.permissions import effective_permissions, require_actor_permission
from app.db import get_db
from app.models.access import GrowthCalculation
from app.models.marketing import (AdCampaignMetricDaily, AdConnection, AdHypothesis, ClientLead, ClientLeadAttribution,
                                  ClientLeadEvent, ClientSale, ClientWorkspace, PortalUser,
                                  LeadInboundSource, PortalProjectAccess, Project, ProjectEconomics, ProjectSource,
                                  SourceMetricDaily)
from app.services.result_analytics import manual_source_key, result_facts
from app.services.business_economics import EconomicsInput, calculateBusinessEconomics

router = APIRouter(prefix="/result", tags=["result"])


def reporting_period(start: date | None, end: date | None):
    today = datetime.now(timezone.utc).date()
    end = end or today
    start = start or end - timedelta(days=29)
    if end < start or (end - start).days > 365:
        raise HTTPException(422, "Выберите период до 366 дней")
    return start, end


async def actor(request: Request, db: AsyncSession):
    user = await portal_session_user(request, db)
    if user:
        return "client", user
    admin = await session_user(request, db)
    if admin and admin.role == "admin":
        return "admin", admin
    raise HTTPException(401, "Требуется вход")


async def scoped_project(db: AsyncSession, request: Request, project_id: int | None):
    kind, user = await actor(request, db)
    if project_id is None:
        query = select(Project).order_by(Project.is_default.desc(), Project.id)
        if kind == "client":
            query = query.where(Project.workspace_id == user.workspace_id)
            if user.role != "client_owner":
                query = query.join(PortalProjectAccess, PortalProjectAccess.project_id == Project.id).where(
                    PortalProjectAccess.user_id == user.id)
        project = await db.scalar(query.limit(1))
    else:
        project = await db.get(Project, project_id)
    if not project or (kind == "client" and project.workspace_id != user.workspace_id):
        raise HTTPException(404, "Проект не найден")
    if kind == "client" and user.role != "client_owner":
        access = await db.scalar(select(PortalProjectAccess).where(PortalProjectAccess.user_id == user.id,
                                                                  PortalProjectAccess.project_id == project.id))
        if access is None:
            raise HTTPException(404, "Проект не найден")
    return project, kind, user


async def can_manage_sources(db, project, kind, user):
    if kind == "admin":
        return True
    if user.role == "client_owner":
        return True
    access = await db.scalar(select(PortalProjectAccess).where(PortalProjectAccess.user_id == user.id,
                                                              PortalProjectAccess.project_id == project.id))
    return bool(access and ("manage_sources" in effective_permissions(user) or
                            (user.permissions is None and access.manage_sources)))


@router.get("/projects")
async def projects(request: Request, db: AsyncSession = Depends(get_db)):
    kind, user = await actor(request, db)
    query = select(Project, ClientWorkspace.name).join(ClientWorkspace).order_by(ClientWorkspace.name, Project.name)
    if kind == "client":
        query = query.where(Project.workspace_id == user.workspace_id)
        if user.role != "client_owner":
            query = query.join(PortalProjectAccess, PortalProjectAccess.project_id == Project.id).where(
                PortalProjectAccess.user_id == user.id)
    rows = (await db.execute(query)).all()
    return [{"id": project.id, "name": project.name, "organization_id": project.workspace_id,
             "organization_name": name, "is_default": project.is_default} for project, name in rows]


@router.get("/growth-models")
async def growth_models(request: Request, db: AsyncSession = Depends(get_db)):
    admin = await session_user(request, db)
    if not admin or admin.role != "admin":
        raise HTTPException(403, "Экономические модели назначает администратор StepToLead")
    rows = (await db.scalars(select(GrowthCalculation).where(or_(GrowthCalculation.phone.is_not(None),
        GrowthCalculation.telegram.is_not(None), GrowthCalculation.email.is_not(None)))
        .order_by(GrowthCalculation.created_at.desc()).limit(100))).all()
    return [{"id": row.id, "name": row.name, "created_at": row.created_at,
             "average_order_value": row.inputs.get("average_order_value"),
             "gross_margin": row.inputs.get("gross_margin")} for row in rows]


@router.get("")
async def result(request: Request, project_id: int | None = None, start: date | None = None, end: date | None = None,
                 granularity: str = "day", db: AsyncSession = Depends(get_db)):
    if granularity not in {"day", "week", "month"}:
        raise HTTPException(422, "Доступна группировка по дням, неделям или месяцам")
    start, end = reporting_period(start, end)
    project, kind, user = await scoped_project(db, request, project_id)
    require_actor_permission(kind, user, "view_result")
    data = await result_facts(db, project, start, end, granularity)
    data["viewer"] = {"name": "Администратор" if kind == "admin" else user.display_name,
                      "role": "admin" if kind == "admin" else user.role,
                      "can_manage_sources": await can_manage_sources(db, project, kind, user),
                      "can_manage_economics": kind == "admin" or "manage_settings" in effective_permissions(user)}
    return data


class ProjectCreate(BaseModel):
    organization_id: int = Field(gt=0)
    name: str = Field(min_length=2, max_length=180)


@router.post("/projects", status_code=201)
async def create_project(payload: ProjectCreate, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    admin = await session_user(request, db)
    if not admin or admin.role != "admin":
        raise HTTPException(403, "Проекты создаёт администратор StepToLead")
    if not await db.get(ClientWorkspace, payload.organization_id):
        raise HTTPException(404, "Организация не найдена")
    project = Project(workspace_id=payload.organization_id, name=payload.name.strip(), is_default=False)
    db.add(project); await db.commit(); await db.refresh(project)
    return {"id": project.id, "name": project.name, "organization_id": project.workspace_id}


class ProjectAccessUpdate(BaseModel):
    user_id: int = Field(gt=0)
    manage_sources: bool | None = None
    manage_integrations: bool | None = None


@router.put("/projects/{project_id}/access")
async def grant_project_access(project_id: int, payload: ProjectAccessUpdate, request: Request,
                               db: AsyncSession = Depends(get_db)):
    check_origin(request)
    admin = await session_user(request, db)
    if not admin or admin.role != "admin":
        raise HTTPException(403, "Доступ к проектам назначает администратор StepToLead")
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    from app.models.marketing import PortalUser
    user = await db.get(PortalUser, payload.user_id)
    if not user or user.workspace_id != project.workspace_id:
        raise HTTPException(404, "Пользователь этой организации не найден")
    row = await db.scalar(select(PortalProjectAccess).where(PortalProjectAccess.user_id == user.id,
                                                              PortalProjectAccess.project_id == project.id))
    if row is None:
        row = PortalProjectAccess(user_id=user.id, project_id=project.id)
        db.add(row)
    if payload.manage_sources is not None:
        row.manage_sources = payload.manage_sources
    if payload.manage_integrations is not None:
        row.manage_integrations = payload.manage_integrations
    await db.commit()
    return {"ok": True}


@router.get("/leads")
async def result_leads(request: Request, project_id: int | None = None, start: date | None = None,
                       end: date | None = None, search: str = "", status: str = "", source: str = "",
                       campaign: str = "", owner_id: int | None = None, page: int = 1, page_size: int = 20,
                       db: AsyncSession = Depends(get_db)):
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "Некорректная страница")
    project, kind, user = await scoped_project(db, request, project_id)
    require_actor_permission(kind, user, "view_leads")
    sale_exists = select(ClientSale.id).where(ClientSale.lead_id == ClientLead.id).exists()
    query = select(ClientLead.id).outerjoin(ClientLeadAttribution, ClientLeadAttribution.lead_id == ClientLead.id).where(
        ClientLead.project_id == project.id)
    if kind == "client" and user.role == "sales_manager":
        query = query.where(ClientLead.assigned_to_id == user.id)
    if start:
        query = query.where(func.date(ClientLead.created_at) >= start)
    if end:
        query = query.where(func.date(ClientLead.created_at) <= end)
    if search.strip():
        pattern = f"%{search.strip().lower()}%"
        query = query.where(or_(func.lower(ClientLead.full_name).like(pattern), func.lower(ClientLead.phone).like(pattern),
                                func.lower(ClientLead.email).like(pattern), func.lower(ClientLead.telegram).like(pattern)))
    if source:
        query = query.where(ClientLead.source == source)
    if campaign:
        query = query.where(or_(ClientLeadAttribution.external_campaign_id == campaign,
                                ClientLeadAttribution.utm_campaign == campaign))
    if owner_id:
        query = query.where(ClientLead.assigned_to_id == owner_id)
    if status == "lost":
        query = query.where(ClientLead.status == "lost")
    elif status == "sale":
        query = query.where(ClientLead.status != "lost", sale_exists)
    elif status == "qualified":
        query = query.where(ClientLead.status != "lost", ~sale_exists, ClientLead.qualified_at.is_not(None))
    elif status == "lead":
        query = query.where(ClientLead.status != "lost", ~sale_exists, ClientLead.qualified_at.is_(None))
    elif status:
        raise HTTPException(422, "Неизвестный статус")
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    ids = (await db.scalars(query.order_by(ClientLead.created_at.desc(), ClientLead.id.desc())
                            .offset((page - 1) * page_size).limit(page_size))).all()
    if not ids:
        return {"project": {"id": project.id, "name": project.name}, "rows": [], "total": total,
                "page": page, "page_size": page_size}
    rows = (await db.execute(select(ClientLead, ClientLeadAttribution, PortalUser.display_name)
                             .outerjoin(ClientLeadAttribution, ClientLeadAttribution.lead_id == ClientLead.id)
                             .outerjoin(PortalUser, PortalUser.id == ClientLead.assigned_to_id)
                             .where(ClientLead.id.in_(ids)))).all()
    sales = (await db.execute(select(ClientSale.lead_id, func.sum(ClientSale.amount), func.count(ClientSale.id))
                              .where(ClientSale.lead_id.in_(ids)).group_by(ClientSale.lead_id))).all()
    sale_map = {lead_id: (float(amount) if amount is not None else None, count)
                for lead_id, amount, count in sales}
    campaign_keys = {(attr.connection_id, attr.external_campaign_id) for _, attr, _ in rows
                     if attr and attr.connection_id and attr.external_campaign_id}
    campaign_map = {}
    for connection_id, external_id in campaign_keys:
        name = await db.scalar(select(AdCampaignMetricDaily.campaign_name).join(AdConnection).where(
            AdConnection.project_id == project.id, AdCampaignMetricDaily.connection_id == connection_id,
            AdCampaignMetricDaily.external_campaign_id == external_id)
            .order_by(AdCampaignMetricDaily.date.desc()).limit(1))
        campaign_map[(connection_id, external_id)] = name
    hypothesis_ids = {attr.hypothesis_id for _, attr, _ in rows if attr and attr.hypothesis_id}
    hypotheses = {h.id: h.name for h in (await db.scalars(select(AdHypothesis).where(
        AdHypothesis.project_id == project.id, AdHypothesis.id.in_(hypothesis_ids)))).all()} if hypothesis_ids else {}
    ordered = {lead_id: index for index, lead_id in enumerate(ids)}
    rows = sorted(rows, key=lambda item: ordered[item[0].id])
    return {"project": {"id": project.id, "name": project.name},
            "total": total, "page": page, "page_size": page_size,
            "rows": [{"id": lead.id, "name": lead.full_name, "phone": lead.phone, "email": lead.email,
                      "telegram": lead.telegram, "assigned_to_id": lead.assigned_to_id,
                      "assigned_to_name": assignee_name, "notes": lead.notes, "lost_reason": lead.lost_reason,
                      "created_at": lead.created_at, "source": lead.source,
                      "campaign_id": (attribution.external_campaign_id or attribution.utm_campaign) if attribution else None,
                      "campaign": (campaign_map.get((attribution.connection_id, attribution.external_campaign_id))
                                   or attribution.utm_campaign or attribution.external_campaign_id) if attribution else None,
                      "hypothesis": hypotheses.get(attribution.hypothesis_id) if attribution else None,
                      "status": "lost" if lead.status == "lost" else "sale" if lead.id in sale_map
                      else "qualified" if lead.qualified_at else "lead",
                      "sales_count": sale_map.get(lead.id, (0, 0))[1],
                      "revenue": sale_map.get(lead.id, (None, 0))[0]}
                     for lead, attribution, assignee_name in rows]}


@router.get("/lead-filters")
async def lead_filters(request: Request, project_id: int, db: AsyncSession = Depends(get_db)):
    project, kind, user = await scoped_project(db, request, project_id)
    require_actor_permission(kind, user, "view_leads")
    scope = [ClientLead.project_id == project.id]
    if kind == "client" and user.role == "sales_manager":
        scope.append(ClientLead.assigned_to_id == user.id)
    sources = (await db.scalars(select(ClientLead.source).where(*scope).distinct().order_by(ClientLead.source))).all()
    campaigns = (await db.execute(select(ClientLeadAttribution.external_campaign_id, ClientLeadAttribution.utm_campaign,
                                         ClientLeadAttribution.connection_id).join(ClientLead).where(
        *scope, or_(ClientLeadAttribution.external_campaign_id.is_not(None),
                    ClientLeadAttribution.utm_campaign.is_not(None))).distinct())).all()
    campaign_options = []
    for external_id, utm_name, connection_id in campaigns:
        name = None
        if connection_id:
            name = await db.scalar(select(AdCampaignMetricDaily.campaign_name).join(AdConnection).where(
                AdConnection.project_id == project.id, AdCampaignMetricDaily.connection_id == connection_id,
                AdCampaignMetricDaily.external_campaign_id == external_id)
                .order_by(AdCampaignMetricDaily.date.desc()).limit(1))
        campaign_options.append({"id": external_id or utm_name, "name": name or utm_name or external_id})
    return {"sources": [value for value in sources if value], "campaigns": campaign_options}


@router.get("/sales")
async def result_sales(request: Request, project_id: int, start: date | None = None, end: date | None = None,
                       search: str = "", source: str = "", owner_id: int | None = None,
                       min_amount: float | None = None, max_amount: float | None = None,
                       page: int = 1, page_size: int = 20, db: AsyncSession = Depends(get_db)):
    start, end = reporting_period(start, end)
    if page < 1 or page_size < 1 or page_size > 100 or (min_amount is not None and min_amount < 0) or (max_amount is not None and max_amount < 0):
        raise HTTPException(422, "Некорректные фильтры")
    if min_amount is not None and max_amount is not None and max_amount < min_amount:
        raise HTTPException(422, "Максимальная сумма меньше минимальной")
    project, kind, user = await scoped_project(db, request, project_id)
    require_actor_permission(kind, user, "view_sales")
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - (end - start)
    source_rows = (await db.scalars(select(ProjectSource).where(ProjectSource.project_id == project.id))).all()
    connections = (await db.scalars(select(AdConnection).where(AdConnection.project_id == project.id))).all()
    inbound = (await db.scalars(select(LeadInboundSource).where(LeadInboundSource.project_id == project.id))).all()
    source_names = {f"source:{row.id}": row.name for row in source_rows}
    source_names.update({f"ad:{row.id}": row.name for row in connections})
    source_names.update({f"webhook:{row.id}": row.name for row in inbound})
    manual_names = (await db.scalars(select(ClientLead.source).where(ClientLead.project_id == project.id).distinct())).all()
    source_names.update({manual_source_key(name): name.strip() for name in manual_names if manual_source_key(name)})
    source_names["unknown"] = "Не определено"

    def source_key(lead: ClientLead, attribution: ClientLeadAttribution | None):
        key = f"source:{lead.source_id}" if lead.source_id else None
        if key not in source_names:
            key = f"ad:{attribution.connection_id}" if attribution and attribution.connection_id else None
        if key not in source_names:
            key = f"webhook:{attribution.source_id}" if attribution and attribution.source_id else None
        if key not in source_names:
            key = manual_source_key(lead.source)
        return key if key in source_names else "unknown"

    # The cycle is based on confirmed Sale rows in each period, independent of table filters.
    cycle_rows = (await db.execute(select(ClientSale.occurred_at, ClientLead.created_at).join(ClientLead).where(
        ClientSale.project_id == project.id, func.date(ClientSale.occurred_at) >= prior_start,
        func.date(ClientSale.occurred_at) <= end))).all()
    def cycle(first: date, last: date):
        days = []
        for sold, created in cycle_rows:
            if first <= sold.date() <= last:
                duration = (sold.date() - created.date()).days
                if duration >= 0:
                    days.append(duration)
        return sum(days) / len(days) if days else None

    query = (select(ClientSale, ClientLead, ClientLeadAttribution, PortalUser.display_name)
             .join(ClientLead, ClientLead.id == ClientSale.lead_id)
             .outerjoin(ClientLeadAttribution, ClientLeadAttribution.lead_id == ClientLead.id)
             .outerjoin(PortalUser, PortalUser.id == ClientLead.assigned_to_id)
             .where(ClientSale.project_id == project.id,
                    func.date(ClientSale.occurred_at) >= start, func.date(ClientSale.occurred_at) <= end))
    if kind == "client" and user.role == "sales_manager":
        query = query.where(ClientLead.assigned_to_id == user.id)
    if search.strip():
        pattern = f"%{search.strip().lower()}%"
        query = query.where(or_(func.lower(ClientLead.full_name).like(pattern), func.lower(ClientLead.phone).like(pattern),
                                func.lower(ClientLead.email).like(pattern), func.lower(ClientLead.telegram).like(pattern)))
    if owner_id:
        query = query.where(ClientLead.assigned_to_id == owner_id)
    if min_amount is not None:
        query = query.where(ClientSale.amount >= min_amount)
    if max_amount is not None:
        query = query.where(ClientSale.amount <= max_amount)
    records = (await db.execute(query.order_by(ClientSale.occurred_at.desc(), ClientSale.id.desc()))).all()
    if source:
        records = [row for row in records if source_key(row[1], row[2]) == source]
    total = len(records)
    visible = records[(page - 1) * page_size:page * page_size]
    campaign_map = {}
    for _, _, attribution, _ in visible:
        if not attribution or not attribution.connection_id or not attribution.external_campaign_id:
            continue
        key = (attribution.connection_id, attribution.external_campaign_id)
        if key not in campaign_map:
            campaign_map[key] = await db.scalar(select(AdCampaignMetricDaily.campaign_name).join(AdConnection).where(
                AdConnection.project_id == project.id, AdCampaignMetricDaily.connection_id == key[0],
                AdCampaignMetricDaily.external_campaign_id == key[1]).order_by(AdCampaignMetricDaily.date.desc()).limit(1))
    owners = (await db.execute(select(PortalUser.id, PortalUser.display_name).join(
        ClientLead, ClientLead.assigned_to_id == PortalUser.id).where(ClientLead.project_id == project.id)
        .distinct().order_by(PortalUser.display_name))).all()
    return {"project_id": project.id, "total": total, "page": page, "page_size": page_size,
            "cycle_days": cycle(start, end), "previous_cycle_days": cycle(prior_start, prior_end),
            "sources": [{"id": key, "name": name} for key, name in source_names.items()],
            "owners": [{"id": id, "name": name} for id, name in owners],
            "rows": [{"id": sale.id, "lead_id": lead.id, "client": lead.full_name,
                      "phone": lead.phone, "email": lead.email, "source_id": source_key(lead, attribution),
                      "source": source_names[source_key(lead, attribution)],
                      "campaign": (campaign_map.get((attribution.connection_id, attribution.external_campaign_id))
                                   or attribution.utm_campaign or attribution.external_campaign_id) if attribution else None,
                      "lead_created_at": lead.created_at, "occurred_at": sale.occurred_at,
                      "amount": float(sale.amount) if sale.amount is not None else None,
                      "status": "Продажа", "owner_id": lead.assigned_to_id, "owner": owner_name,
                      "comment": sale.comment} for sale, lead, attribution, owner_name in visible]}


@router.get("/leads/{lead_id}")
async def result_lead_detail(lead_id: int, request: Request, project_id: int,
                             db: AsyncSession = Depends(get_db)):
    project, kind, user = await scoped_project(db, request, project_id)
    require_actor_permission(kind, user, "view_leads")
    lead = await db.get(ClientLead, lead_id)
    if not lead or lead.project_id != project.id or (kind == "client" and user.role == "sales_manager" and lead.assigned_to_id != user.id):
        raise HTTPException(404, "Лид не найден")
    events = (await db.execute(select(ClientLeadEvent, PortalUser.display_name).outerjoin(
        PortalUser, PortalUser.id == ClientLeadEvent.actor_id).where(ClientLeadEvent.lead_id == lead.id)
        .order_by(ClientLeadEvent.created_at.desc(), ClientLeadEvent.id.desc()))).all()
    sales = (await db.scalars(select(ClientSale).where(ClientSale.lead_id == lead.id)
                              .order_by(ClientSale.occurred_at.desc()))).all()
    attribution = await db.scalar(select(ClientLeadAttribution).where(ClientLeadAttribution.lead_id == lead.id))
    owner_name = await db.scalar(select(PortalUser.display_name).where(PortalUser.id == lead.assigned_to_id)) if lead.assigned_to_id else None
    campaign_name = None
    hypothesis_name = None
    if attribution and attribution.connection_id and attribution.external_campaign_id:
        campaign_name = await db.scalar(select(AdCampaignMetricDaily.campaign_name).join(AdConnection).where(
            AdConnection.project_id == project.id, AdCampaignMetricDaily.connection_id == attribution.connection_id,
            AdCampaignMetricDaily.external_campaign_id == attribution.external_campaign_id)
            .order_by(AdCampaignMetricDaily.date.desc()).limit(1))
    if attribution and attribution.hypothesis_id:
        hypothesis_name = await db.scalar(select(AdHypothesis.name).where(
            AdHypothesis.project_id == project.id, AdHypothesis.id == attribution.hypothesis_id))
    sale_total = sum(float(s.amount) for s in sales if s.amount is not None) if any(s.amount is not None for s in sales) else None
    return {"lead": {"id": lead.id, "name": lead.full_name, "phone": lead.phone, "email": lead.email,
                     "telegram": lead.telegram, "created_at": lead.created_at, "source": lead.source,
                     "campaign_id": (attribution.external_campaign_id or attribution.utm_campaign) if attribution else None,
                     "campaign": (campaign_name or attribution.utm_campaign or attribution.external_campaign_id) if attribution else None,
                     "hypothesis": hypothesis_name, "status": "lost" if lead.status == "lost" else "sale" if sales
                     else "qualified" if lead.qualified_at else "lead", "sales_count": len(sales), "revenue": sale_total,
                     "assigned_to_id": lead.assigned_to_id, "assigned_to_name": owner_name,
                     "notes": lead.notes, "lost_reason": lead.lost_reason, "meeting_at": lead.meeting_at},
            "events": [{"id": e.id, "event_type": e.event_type, "description": e.description,
                        "actor_name": actor, "created_at": e.created_at} for e, actor in events],
            "sales": [{"id": s.id, "amount": float(s.amount) if s.amount is not None else None,
                       "occurred_at": s.occurred_at} for s in sales]}


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int = Field(gt=0)
    name: str = Field(min_length=2, max_length=180)
    kind: str
    method: str


@router.post("/sources", status_code=201)
async def create_source(payload: SourceCreate, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    project, kind, user = await scoped_project(db, request, payload.project_id)
    if not await can_manage_sources(db, project, kind, user):
        raise HTTPException(403, "Недостаточно прав для управления источниками")
    if payload.kind not in {"CUSTOM", "MANUAL", "INTERNAL"} or payload.method not in {"API", "WEBHOOK", "BOT", "FILE", "MANUAL", "INTERNAL"}:
        raise HTTPException(422, "Неизвестный тип или способ получения данных")
    row = ProjectSource(project_id=project.id, name=payload.name.strip(), kind=payload.kind, method=payload.method)
    db.add(row); await db.commit(); await db.refresh(row)
    return {"id": f"source:{row.id}", "name": row.name, "kind": row.kind, "method": row.method}


class SourceMetricInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: date
    spend: float | None = Field(default=None, ge=0)
    impressions: int | None = Field(default=None, ge=0)
    clicks: int | None = Field(default=None, ge=0)
    aggregated_leads: int | None = Field(default=None, ge=0)
    aggregated_qualified: int | None = Field(default=None, ge=0)
    aggregated_sales: int | None = Field(default=None, ge=0)
    aggregated_revenue: float | None = Field(default=None, ge=0)


@router.put("/sources/{source_id}/daily")
async def set_source_daily(source_id: int, payload: SourceMetricInput, request: Request,
                           db: AsyncSession = Depends(get_db)):
    check_origin(request)
    source = await db.get(ProjectSource, source_id)
    if not source:
        raise HTTPException(404, "Источник не найден")
    project, kind, user = await scoped_project(db, request, source.project_id)
    if not await can_manage_sources(db, project, kind, user):
        raise HTTPException(403, "Недостаточно прав для управления источниками")
    if kind == "client" and user.permissions is not None:
        require_actor_permission(kind, user, "edit_manual_metrics")
    row = await db.scalar(select(SourceMetricDaily).where(SourceMetricDaily.source_id == source_id,
                                                           SourceMetricDaily.date == payload.date))
    if row is None:
        row = SourceMetricDaily(source_id=source_id, date=payload.date)
        db.add(row)
    for field, value in payload.model_dump(exclude={"date"}, exclude_unset=True).items():
        setattr(row, field, value)
    await db.commit()
    return {"ok": True}


class EconomicsActivation(BaseModel):
    growth_calculation_id: int = Field(gt=0)
    allowable_cac: float | None = Field(default=None, ge=0)


class ProjectEconomicsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    average_order_value: float = Field(ge=0, le=1e12)
    purchases_per_customer: float = Field(ge=0, le=1e6)
    gross_margin: float = Field(ge=0, le=100)
    fixed_costs: float = Field(ge=0, le=1e12)
    current_customers: int = Field(ge=0, le=1_000_000_000)
    target_customers: int = Field(ge=0, le=1_000_000_000)
    growth_budget: float = Field(ge=0, le=1e12)
    allowable_cac: float | None = Field(default=None, ge=0, le=1e12)

    @model_validator(mode="after")
    def validate_capacity(self):
        if self.target_customers < self.current_customers:
            raise ValueError("Целевое число клиентов не может быть меньше текущего")
        return self


@router.get("/projects/{project_id}/economics/model")
async def project_economics_model(project_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    project, kind, user = await scoped_project(db, request, project_id)
    require_actor_permission(kind, user, "view_result")
    row = await db.scalar(select(ProjectEconomics).where(ProjectEconomics.project_id == project.id))
    if row is None:
        return {"inputs": None, "allowable_cac": None}
    calculation = await db.get(GrowthCalculation, row.growth_calculation_id)
    return {"inputs": calculation.inputs if calculation else None,
            "allowable_cac": float(row.allowable_cac) if row.allowable_cac is not None else None}


@router.put("/projects/{project_id}/economics/model")
async def save_project_economics_model(project_id: int, payload: ProjectEconomicsInput, request: Request,
                                       db: AsyncSession = Depends(get_db)):
    check_origin(request)
    project, kind, user = await scoped_project(db, request, project_id)
    require_actor_permission(kind, user, "manage_settings")
    values = payload.model_dump(exclude={"allowable_cac"})
    inputs = EconomicsInput(**values)
    result = calculateBusinessEconomics(inputs)
    row = await db.scalar(select(ProjectEconomics).where(ProjectEconomics.project_id == project.id))
    calculation = await db.get(GrowthCalculation, row.growth_calculation_id) if row else None
    # Public /growth submissions and models attached elsewhere must never be mutated.
    shared = bool(calculation and await db.scalar(select(ProjectEconomics.id).where(
        ProjectEconomics.growth_calculation_id == calculation.id,
        ProjectEconomics.project_id != project.id).limit(1)))
    if calculation is None or shared or any((calculation.phone, calculation.email, calculation.telegram)):
        calculation = GrowthCalculation(name=f"Экономика проекта: {project.name}"[:120],
                                        inputs=values, results=result.model_dump(), status=result.status)
        db.add(calculation)
        await db.flush()
    else:
        calculation.inputs = values
        calculation.results = result.model_dump()
        calculation.status = result.status
    if row is None:
        row = ProjectEconomics(project_id=project.id, growth_calculation_id=calculation.id)
        db.add(row)
    row.growth_calculation_id = calculation.id
    row.allowable_cac = payload.allowable_cac
    row.activated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True}


@router.put("/projects/{project_id}/economics")
async def activate_economics(project_id: int, payload: EconomicsActivation, request: Request,
                             db: AsyncSession = Depends(get_db)):
    check_origin(request)
    admin = await session_user(request, db)
    if not admin or admin.role != "admin":
        raise HTTPException(403, "Экономическую модель проекта назначает администратор StepToLead")
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    if not await db.get(GrowthCalculation, payload.growth_calculation_id):
        raise HTTPException(404, "Сохранённый расчёт не найден")
    row = await db.scalar(select(ProjectEconomics).where(ProjectEconomics.project_id == project.id))
    if row is None:
        row = ProjectEconomics(project_id=project.id, growth_calculation_id=payload.growth_calculation_id)
        db.add(row)
    row.growth_calculation_id = payload.growth_calculation_id
    row.allowable_cac = payload.allowable_cac
    row.activated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True}
