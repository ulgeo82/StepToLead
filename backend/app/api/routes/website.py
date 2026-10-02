"""First-party, consent-gated site events and project-scoped reporting."""

import secrets
import re
import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.access import rate_limit
from app.api.routes.ads import require_manage_integrations
from app.api.routes.result import reporting_period, scoped_project
from app.core.access import check_origin
from app.core.permissions import require_actor_permission
from app.db import get_db
from app.models.access import GrowthCalculation
from app.models.crm import CrmDeal, CrmInbound
from app.models.marketing import AdCampaignMetricDaily, AdConnection, AdHypothesisCampaign, ClientLead, ClientSale, ProjectEconomics
from app.models.website import WebsiteEvent, WebsiteEventDaily, WebsiteSession, WebsiteSite

router = APIRouter(prefix="/website", tags=["website"])
EVENTS = {
    "page_view", "section_view", "scroll_depth", "cta_view", "cta_click", "phone_click",
    "messenger_click", "service_view", "case_view", "pricing_view", "service_click", "case_open", "pricing_open", "form_view",
    "form_start", "form_error", "form_submit", "form_success",
}
_ELEMENT_EVENTS = EVENTS - {"page_view", "scroll_depth"}


def clean_origin(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password or parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise HTTPException(422, "Укажите адрес сайта в формате https://example.ru")
    if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1"}:
        raise HTTPException(422, "Для сайта требуется HTTPS")
    return f"{parts.scheme}://{parts.netloc.lower()}"


def safe_page(value: str | None, origin: str) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    parts = urlsplit(value[:1200])
    if f"{parts.scheme}://{parts.netloc.lower()}" != origin:
        return None, None
    path = (parts.path or "/")[:700]
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")), path


def safe_label(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if "@" in value or re.search(r"\d{10,}", value):
        return None
    return value or None


class SiteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int = Field(gt=0)
    name: str = Field(min_length=2, max_length=180)
    origin: str = Field(max_length=500)


class SiteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool


class CollectedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=16, max_length=64)
    name: str
    page: str | None = Field(default=None, max_length=1200)
    element_id: str | None = Field(default=None, max_length=100)
    element_name: str | None = Field(default=None, max_length=180)
    depth: int | None = None


class EventBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visitor: str = Field(min_length=16, max_length=64)
    session: str = Field(min_length=16, max_length=64)
    new_visitor: bool = False
    referrer: str | None = Field(default=None, max_length=1200)
    device: str | None = Field(default=None, max_length=20)
    browser: str | None = Field(default=None, max_length=40)
    os: str | None = Field(default=None, max_length=40)
    utm_source: str | None = Field(default=None, max_length=255)
    utm_medium: str | None = Field(default=None, max_length=255)
    utm_campaign: str | None = Field(default=None, max_length=500)
    utm_content: str | None = Field(default=None, max_length=500)
    utm_term: str | None = Field(default=None, max_length=500)
    click_id: str | None = Field(default=None, max_length=255)
    connection_id: int | None = Field(default=None, gt=0)
    external_campaign_id: str | None = Field(default=None, max_length=180)
    events: list[CollectedEvent] = Field(min_length=1, max_length=20)


def site_json(site: WebsiteSite) -> dict:
    return {"id": site.id, "name": site.name, "origin": site.origin, "public_key": site.public_key,
            "active": site.active, "last_event_at": site.last_event_at, "created_at": site.created_at}


@router.get("/sites")
async def list_sites(request: Request, project_id: int, db: AsyncSession = Depends(get_db)):
    project, kind, user = await scoped_project(db, request, project_id)
    require_actor_permission(kind, user, "view_analytics")
    sites = (await db.scalars(select(WebsiteSite).where(WebsiteSite.project_id == project.id).order_by(WebsiteSite.id))).all()
    return [site_json(site) for site in sites]


@router.post("/sites", status_code=201)
async def create_site(payload: SiteCreate, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    project, kind, user = await scoped_project(db, request, payload.project_id)
    await require_manage_integrations(db, project, kind, user)
    origin = clean_origin(payload.origin)
    existing = await db.scalar(select(WebsiteSite).where(WebsiteSite.project_id == project.id, WebsiteSite.origin == origin))
    if existing:
        raise HTTPException(409, "Этот сайт уже подключён к проекту")
    site = WebsiteSite(workspace_id=project.workspace_id, project_id=project.id, name=payload.name.strip(),
                       origin=origin, public_key=secrets.token_urlsafe(32))
    db.add(site)
    await db.commit()
    await db.refresh(site)
    return site_json(site)


@router.patch("/sites/{site_id}")
async def update_site(site_id: int, payload: SiteUpdate, request: Request, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    site = await db.get(WebsiteSite, site_id)
    if not site:
        raise HTTPException(404, "Сайт не найден")
    project, kind, user = await scoped_project(db, request, site.project_id)
    await require_manage_integrations(db, project, kind, user)
    site.active = payload.active
    await db.commit()
    return site_json(site)


@router.post("/collect/{public_key}", status_code=202)
async def collect(public_key: str, request: Request, db: AsyncSession = Depends(get_db)):
    site = await db.scalar(select(WebsiteSite).where(WebsiteSite.public_key == public_key, WebsiteSite.active.is_(True)))
    if not site:
        raise HTTPException(404, "Сайт не найден")
    origin = request.headers.get("origin")
    if origin != site.origin:
        raise HTTPException(403, "Адрес сайта не совпадает")
    try:
        if int(request.headers.get("content-length", "0")) > 16384:
            raise HTTPException(413, "Пакет слишком большой")
    except ValueError:
        raise HTTPException(400, "Некорректный размер пакета") from None
    # Next.js proxies collector traffic, so request.client may be shared by all visitors.
    await rate_limit(request, f"website:{site.id}", 3000, 60)
    raw = await request.body()
    if len(raw) > 16384:
        raise HTTPException(413, "Пакет слишком большой")
    try:
        payload = EventBatch.model_validate_json(raw)
    except ValueError:
        raise HTTPException(422, "Некорректный пакет событий") from None
    if not all(event.name in EVENTS and (event.depth in {25, 50, 75, 90, 100} if event.name == "scroll_depth" else event.depth is None) for event in payload.events):
        raise HTTPException(422, "Неизвестное событие")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", payload.visitor) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", payload.session) or any(
        not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", event.key) for event in payload.events
    ):
        raise HTTPException(422, "Некорректный идентификатор")
    now = datetime.now(timezone.utc)
    session = await db.scalar(select(WebsiteSession).where(WebsiteSession.site_id == site.id,
                                                            WebsiteSession.session_key == payload.session))
    if session and session.visitor_key != payload.visitor:
        raise HTTPException(409, "Сессия не соответствует посетителю")
    first_page, first_path = safe_page(payload.events[0].page, site.origin)
    if not session:
        verified_connection = None
        if payload.connection_id and payload.external_campaign_id:
            verified_connection = await db.scalar(select(AdConnection.id).join(
                AdCampaignMetricDaily, AdCampaignMetricDaily.connection_id == AdConnection.id).where(
                AdConnection.id == payload.connection_id, AdConnection.project_id == site.project_id,
                AdCampaignMetricDaily.external_campaign_id == payload.external_campaign_id).limit(1))
            if not verified_connection:
                verified_connection = await db.scalar(select(AdConnection.id).join(
                    AdHypothesisCampaign, AdHypothesisCampaign.connection_id == AdConnection.id).where(
                    AdConnection.id == payload.connection_id, AdConnection.project_id == site.project_id,
                    AdHypothesisCampaign.external_campaign_id == payload.external_campaign_id).limit(1))
        referrer = payload.referrer
        if referrer:
            parts = urlsplit(referrer)
            referrer = f"{parts.scheme}://{parts.netloc}" if parts.scheme in {"http", "https"} and parts.netloc else None
        session = WebsiteSession(site_id=site.id, workspace_id=site.workspace_id, project_id=site.project_id,
                                 visitor_key=payload.visitor, session_key=payload.session, is_new_visitor=payload.new_visitor,
                                 landing_url=first_page, landing_path=first_path, referrer=referrer,
                                 device_type=payload.device if payload.device in {"desktop", "mobile", "tablet"} else None,
                                 browser=payload.browser if payload.browser in {"firefox", "edge", "chrome", "safari", "other"} else None,
                                 os=payload.os if payload.os in {"android", "ios", "windows", "macos", "linux", "other"} else None,
                                 utm_source=safe_label(payload.utm_source), utm_medium=safe_label(payload.utm_medium),
                                 utm_campaign=safe_label(payload.utm_campaign), utm_content=safe_label(payload.utm_content),
                                 utm_term=safe_label(payload.utm_term), click_id=payload.click_id,
                                 connection_id=verified_connection,
                                 external_campaign_id=payload.external_campaign_id if verified_connection else None)
        db.add(session)
        await db.flush()
    existing = set((await db.scalars(select(WebsiteEvent.event_key).where(WebsiteEvent.site_id == site.id,
                                WebsiteEvent.event_key.in_([event.key for event in payload.events])))).all())
    prior_semantics = (await db.execute(select(WebsiteEvent.event_name, WebsiteEvent.element_name,
        WebsiteEvent.page_path, WebsiteEvent.properties).where(WebsiteEvent.session_id == session.id))).all()
    semantic_seen = {(name, label, None if name == "scroll_depth" or name.startswith("form_") else path,
                      (properties or {}).get("depth") if name == "scroll_depth" else None)
                     for name, label, path, properties in prior_semantics}
    rollup: dict[str, dict] = {}
    dialect_insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    for event in payload.events:
        if event.key in existing:
            continue
        page, path = safe_page(event.page, site.origin)
        label = safe_label(event.element_name) if event.name in _ELEMENT_EVENTS else None
        raw_insert = dialect_insert(WebsiteEvent).values(site_id=site.id, workspace_id=site.workspace_id, project_id=site.project_id,
                            session_id=session.id, event_key=event.key, event_name=event.name,
                            event_category="navigation" if event.name in {"page_view", "section_view", "scroll_depth"} else "interaction",
                            page_url=page, page_path=path,
                            element_id=safe_label(event.element_id) if event.name in _ELEMENT_EVENTS else None,
                            element_name=label,
                            properties={"depth": event.depth} if event.depth is not None else {}, occurred_at=now)
        inserted = await db.execute(raw_insert.on_conflict_do_nothing(index_elements=["site_id", "event_key"]))
        if not inserted.rowcount:
            continue
        dimensions = [event.name, path or "", session.landing_path or "", label or "",
                      session.device_type or "", session.utm_source or "", session.utm_campaign or "",
                      bool(session.is_new_visitor), event.depth or 0]
        group_key = hashlib.sha256(json.dumps(dimensions, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        if group_key not in rollup:
            rollup[group_key] = {"site_id": site.id, "project_id": site.project_id, "day": session.started_at.date(),
                "group_key": group_key, "event_name": event.name, "page_path": path or "",
                "landing_path": session.landing_path or "", "element_name": label or "",
                "device_type": session.device_type or "", "utm_source": session.utm_source or "",
                "utm_campaign": session.utm_campaign or "", "is_new_visitor": bool(session.is_new_visitor),
                "depth": event.depth or 0, "events_count": 0, "sessions_count": 0}
        rollup[group_key]["events_count"] += 1
        semantic = (event.name, label, None if event.name == "scroll_depth" or event.name.startswith("form_") else path,
                    event.depth if event.name == "scroll_depth" else None)
        if semantic not in semantic_seen:
            rollup[group_key]["sessions_count"] += 1
            semantic_seen.add(semantic)
        if event.name == "page_view":
            session.page_views += 1
        if event.name == "cta_click":
            session.cta_clicked = True
        if event.name == "form_start":
            session.form_started = True
        if event.name == "form_success":
            session.form_succeeded = True
        if event.name in {"cta_click", "phone_click", "messenger_click", "form_start", "form_submit",
                          "form_success", "service_click", "case_open", "pricing_open"}:
            session.engaged = True
        existing.add(event.key)
    for values in rollup.values():
        statement = dialect_insert(WebsiteEventDaily).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["site_id", "day", "group_key"],
            set_={"events_count": WebsiteEventDaily.events_count + values["events_count"],
                  "sessions_count": WebsiteEventDaily.sessions_count + values["sessions_count"]})
        await db.execute(statement)
    session.last_activity_at = now
    site.last_event_at = now
    await db.commit()
    return Response(status_code=202, headers={"Access-Control-Allow-Origin": site.origin, "Vary": "Origin"})


@router.options("/collect/{public_key}")
async def preflight(public_key: str, request: Request, db: AsyncSession = Depends(get_db)):
    site = await db.scalar(select(WebsiteSite).where(WebsiteSite.public_key == public_key, WebsiteSite.active.is_(True)))
    if not site or request.headers.get("origin") != site.origin:
        raise HTTPException(403, "Адрес сайта не совпадает")
    return Response(status_code=204, headers={"Access-Control-Allow-Origin": site.origin,
                                              "Access-Control-Allow-Methods": "POST, OPTIONS",
                                              "Access-Control-Allow-Headers": "Content-Type", "Vary": "Origin"})


@router.get("/analytics")
async def site_analytics(request: Request, project_id: int, start: date | None = None, end: date | None = None,
                         site_id: int | None = None, page: str | None = None, device: str | None = None,
                         utm_source: str | None = None, utm_campaign: str | None = None,
                         visitor: str | None = None, db: AsyncSession = Depends(get_db)):
    start, end = reporting_period(start, end)
    project, kind, user = await scoped_project(db, request, project_id)
    require_actor_permission(kind, user, "view_analytics")
    if site_id is not None and not await db.scalar(select(WebsiteSite.id).where(WebsiteSite.id == site_id, WebsiteSite.project_id == project.id)):
        raise HTTPException(404, "Сайт не найден")
    base = [WebsiteSession.project_id == project.id,
            WebsiteSession.started_at >= datetime.combine(start, time.min, tzinfo=timezone.utc),
            WebsiteSession.started_at < datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc)]
    if site_id: base.append(WebsiteSession.site_id == site_id)
    if page: base.append(WebsiteSession.landing_path == page)
    if device: base.append(WebsiteSession.device_type == device)
    if utm_source: base.append(WebsiteSession.utm_source == utm_source)
    if utm_campaign: base.append(WebsiteSession.utm_campaign == utm_campaign)
    if visitor in {"new", "returning"}: base.append(WebsiteSession.is_new_visitor.is_(visitor == "new"))
    session_ids = select(WebsiteSession.id).where(*base)
    sessions = (await db.execute(select(func.count(), func.count(func.distinct(WebsiteSession.visitor_key)),
                    func.sum(WebsiteSession.page_views))
                    .where(*base))).one()
    engaged = await db.scalar(select(func.count()).select_from(WebsiteSession).where(
        *base, or_(WebsiteSession.page_views > 1, WebsiteSession.engaged.is_(True)))) or 0
    funnel_counts = (await db.execute(select(func.sum(case((WebsiteSession.cta_clicked.is_(True), 1), else_=0)),
                                           func.sum(case((WebsiteSession.form_started.is_(True), 1), else_=0)),
                                           func.sum(case((WebsiteSession.form_succeeded.is_(True), 1), else_=0)))
        .where(*base))).one()
    rollup_base = [WebsiteEventDaily.project_id == project.id,
                   WebsiteEventDaily.day >= start, WebsiteEventDaily.day <= end]
    if site_id: rollup_base.append(WebsiteEventDaily.site_id == site_id)
    if page: rollup_base.append(WebsiteEventDaily.landing_path == page)
    if device: rollup_base.append(WebsiteEventDaily.device_type == device)
    if utm_source: rollup_base.append(WebsiteEventDaily.utm_source == utm_source)
    if utm_campaign: rollup_base.append(WebsiteEventDaily.utm_campaign == utm_campaign)
    if visitor in {"new", "returning"}: rollup_base.append(WebsiteEventDaily.is_new_visitor.is_(visitor == "new"))
    event_rows = (await db.execute(select(WebsiteEventDaily.event_name, func.sum(WebsiteEventDaily.events_count)).where(
        *rollup_base).group_by(WebsiteEventDaily.event_name))).all()
    events = {name: amount for name, amount in event_rows}
    pages = (await db.execute(select(WebsiteEventDaily.page_path, func.sum(WebsiteEventDaily.events_count)).where(
        *rollup_base, WebsiteEventDaily.event_name == "page_view", WebsiteEventDaily.page_path != "")
        .group_by(WebsiteEventDaily.page_path).order_by(func.sum(WebsiteEventDaily.events_count).desc()).limit(30))).all()
    elements = (await db.execute(select(WebsiteEventDaily.event_name, WebsiteEventDaily.element_name,
                                        func.sum(WebsiteEventDaily.events_count)).where(
        *rollup_base, WebsiteEventDaily.element_name != "")
        .group_by(WebsiteEventDaily.event_name, WebsiteEventDaily.element_name)
        .order_by(func.sum(WebsiteEventDaily.events_count).desc()).limit(30))).all()
    forms = (await db.execute(select(WebsiteEventDaily.element_name, WebsiteEventDaily.event_name,
                                    func.sum(WebsiteEventDaily.events_count), func.sum(WebsiteEventDaily.sessions_count)).where(
        *rollup_base, WebsiteEventDaily.element_name != "",
        WebsiteEventDaily.event_name.in_(["form_view", "form_start", "form_error", "form_submit", "form_success"]))
        .group_by(WebsiteEventDaily.element_name, WebsiteEventDaily.event_name))).all()
    form_map: dict[str, dict[str, int]] = {}
    for label, event_name, amount, unique_sessions in forms:
        form_map.setdefault(label, {})[event_name] = amount if event_name == "form_error" else unique_sessions
    depths = (await db.execute(select(WebsiteEventDaily.depth, func.sum(WebsiteEventDaily.sessions_count)).where(
        *rollup_base, WebsiteEventDaily.event_name == "scroll_depth")
        .group_by(WebsiteEventDaily.depth))).all()
    depth_map = {str(depth): amount for depth, amount in depths if depth is not None}
    linked = (await db.execute(select(CrmDeal.lead_id, ClientLead.qualified_at).join(CrmInbound, CrmInbound.deal_id == CrmDeal.id)
        .join(ClientLead, ClientLead.id == CrmDeal.lead_id)
        .where(CrmInbound.website_session_id.in_(session_ids), CrmDeal.lead_id.is_not(None)))).all()
    lead_ids = {row.lead_id for row in linked}
    qualified_ids = {row.lead_id for row in linked if row.qualified_at is not None}
    sale_rows = (await db.execute(select(ClientSale.id, ClientSale.amount).where(
        ClientSale.lead_id.in_(lead_ids), ClientSale.project_id == project.id))).all() if lead_ids else []
    revenue_values = [float(row.amount) for row in sale_rows if row.amount is not None]
    economy = await db.scalar(select(ProjectEconomics).where(ProjectEconomics.project_id == project.id))
    growth = await db.get(GrowthCalculation, economy.growth_calculation_id) if economy else None
    margin_value = (growth.inputs or {}).get("gross_margin") if growth else None
    margin = float(margin_value) if margin_value is not None else None
    linked_by_session = select(CrmInbound.website_session_id, CrmDeal.lead_id, ClientLead.qualified_at).join(
        CrmDeal, CrmDeal.id == CrmInbound.deal_id).join(ClientLead, ClientLead.id == CrmDeal.lead_id
    ).where(CrmInbound.project_id == project.id).subquery()
    action_events = {"cta_view", "cta_click", "phone_click", "messenger_click", "service_view",
                     "service_click", "case_view", "case_open", "pricing_view", "pricing_open"}
    action_counts: dict[str, dict[str, int]] = {}
    for event_name, label, amount in elements:
        if event_name in action_events:
            action_counts.setdefault(label, {})[event_name] = amount
    action_names = list(action_counts)
    action_breakdown = []
    if action_names:
        action_sessions = select(WebsiteEvent.element_name.label("name"), WebsiteEvent.session_id).where(
            WebsiteEvent.project_id == project.id, WebsiteEvent.session_id.in_(session_ids),
            WebsiteEvent.element_name.in_(action_names), WebsiteEvent.event_name.in_(action_events)
        ).distinct().subquery()
        action_breakdown = (await db.execute(select(action_sessions.c.name,
                func.count(func.distinct(linked_by_session.c.lead_id)),
                func.count(func.distinct(case((linked_by_session.c.qualified_at.is_not(None), linked_by_session.c.lead_id)))),
                func.count(func.distinct(ClientSale.id)), func.sum(ClientSale.amount))
            .outerjoin(linked_by_session, linked_by_session.c.website_session_id == action_sessions.c.session_id)
            .outerjoin(ClientSale, (ClientSale.lead_id == linked_by_session.c.lead_id) & (ClientSale.project_id == project.id))
            .group_by(action_sessions.c.name))).all()
    action_outcomes = {name: (lead_count, qualified_count, sales_count, float(revenue) if revenue is not None else None)
                       for name, lead_count, qualified_count, sales_count, revenue in action_breakdown}
    daily = (await db.execute(select(func.date(WebsiteSession.started_at),
                                    func.count(func.distinct(WebsiteSession.id)),
                                    func.count(func.distinct(linked_by_session.c.lead_id)),
                                    func.count(func.distinct(case((linked_by_session.c.qualified_at.is_not(None), linked_by_session.c.lead_id)))),
                                    func.count(func.distinct(ClientSale.id)))
        .outerjoin(linked_by_session, linked_by_session.c.website_session_id == WebsiteSession.id)
        .outerjoin(ClientSale, (ClientSale.lead_id == linked_by_session.c.lead_id) & (ClientSale.project_id == project.id))
        .where(*base).group_by(func.date(WebsiteSession.started_at)).order_by(func.date(WebsiteSession.started_at)))).all()
    breakdown_cols = (
        func.count(func.distinct(WebsiteSession.id)),
        func.count(func.distinct(linked_by_session.c.lead_id)),
        func.count(func.distinct(case((linked_by_session.c.qualified_at.is_not(None), linked_by_session.c.lead_id)))),
        func.count(func.distinct(ClientSale.id)), func.sum(ClientSale.amount),
    )
    page_breakdown = (await db.execute(select(WebsiteSession.landing_path, *breakdown_cols)
        .outerjoin(linked_by_session, linked_by_session.c.website_session_id == WebsiteSession.id)
        .outerjoin(ClientSale, (ClientSale.lead_id == linked_by_session.c.lead_id) & (ClientSale.project_id == project.id))
        .where(*base, WebsiteSession.landing_path.is_not(None))
        .group_by(WebsiteSession.landing_path).order_by(func.count(func.distinct(WebsiteSession.id)).desc()).limit(30))).all()
    device_breakdown = (await db.execute(select(WebsiteSession.device_type, *breakdown_cols)
        .outerjoin(linked_by_session, linked_by_session.c.website_session_id == WebsiteSession.id)
        .outerjoin(ClientSale, (ClientSale.lead_id == linked_by_session.c.lead_id) & (ClientSale.project_id == project.id))
        .where(*base, WebsiteSession.device_type.is_not(None)).group_by(WebsiteSession.device_type))).all()
    source_breakdown = (await db.execute(select(WebsiteSession.utm_source, *breakdown_cols)
        .outerjoin(linked_by_session, linked_by_session.c.website_session_id == WebsiteSession.id)
        .outerjoin(ClientSale, (ClientSale.lead_id == linked_by_session.c.lead_id) & (ClientSale.project_id == project.id))
        .where(*base).group_by(WebsiteSession.utm_source)
        .order_by(func.count(func.distinct(WebsiteSession.id)).desc()).limit(30))).all()
    campaign_breakdown = (await db.execute(select(WebsiteSession.utm_campaign, *breakdown_cols)
        .outerjoin(linked_by_session, linked_by_session.c.website_session_id == WebsiteSession.id)
        .outerjoin(ClientSale, (ClientSale.lead_id == linked_by_session.c.lead_id) & (ClientSale.project_id == project.id))
        .where(*base).group_by(WebsiteSession.utm_campaign)
        .order_by(func.count(func.distinct(WebsiteSession.id)).desc()).limit(30))).all()
    duration = (end - start).days + 1
    prior_start, prior_end = start - timedelta(days=duration), start - timedelta(days=1)
    prior_base = [WebsiteSession.project_id == project.id,
                  WebsiteSession.started_at >= datetime.combine(prior_start, time.min, tzinfo=timezone.utc),
                  WebsiteSession.started_at < datetime.combine(start, time.min, tzinfo=timezone.utc)]
    if site_id: prior_base.append(WebsiteSession.site_id == site_id)
    if page: prior_base.append(WebsiteSession.landing_path == page)
    if device: prior_base.append(WebsiteSession.device_type == device)
    if utm_source: prior_base.append(WebsiteSession.utm_source == utm_source)
    if utm_campaign: prior_base.append(WebsiteSession.utm_campaign == utm_campaign)
    if visitor in {"new", "returning"}: prior_base.append(WebsiteSession.is_new_visitor.is_(visitor == "new"))
    previous = (await db.execute(select(*breakdown_cols)
        .outerjoin(linked_by_session, linked_by_session.c.website_session_id == WebsiteSession.id)
        .outerjoin(ClientSale, (ClientSale.lead_id == linked_by_session.c.lead_id) & (ClientSale.project_id == project.id))
        .where(*prior_base))).one()
    previous_daily = (await db.execute(select(func.date(WebsiteSession.started_at),
                                    func.count(func.distinct(WebsiteSession.id)),
                                    func.count(func.distinct(linked_by_session.c.lead_id)),
                                    func.count(func.distinct(case((linked_by_session.c.qualified_at.is_not(None), linked_by_session.c.lead_id)))),
                                    func.count(func.distinct(ClientSale.id)))
        .outerjoin(linked_by_session, linked_by_session.c.website_session_id == WebsiteSession.id)
        .outerjoin(ClientSale, (ClientSale.lead_id == linked_by_session.c.lead_id) & (ClientSale.project_id == project.id))
        .where(*prior_base).group_by(func.date(WebsiteSession.started_at)).order_by(func.date(WebsiteSession.started_at)))).all()
    visits = sessions[0] or 0
    bounces = visits - engaged
    return {"period": {"start": start, "end": end, "previous_start": prior_start, "previous_end": prior_end}, "site_id": site_id,
            "totals": {"sessions": visits, "visitors": sessions[1] or 0, "page_views": sessions[2] or 0,
                       "engaged": engaged,
                       "bounce_rate": round(bounces / visits * 100, 1) if visits else None,
                       "leads": len(lead_ids), "qualified": len(qualified_ids), "sales": len(sale_rows),
                       "revenue": sum(revenue_values) if revenue_values else None,
                       "visit_to_lead": round(len(lead_ids) / visits * 100, 1) if visits else None,
                       "visit_to_qualified": round(len(qualified_ids) / visits * 100, 1) if visits else None,
                       "visit_to_sale": round(len(sale_rows) / visits * 100, 1) if visits else None,
                       "revenue_per_visit": round(sum(revenue_values) / visits, 2) if visits and revenue_values else None,
                       "gross_profit_per_visit": round(sum(revenue_values) * margin / 100 / visits, 2)
                           if visits and revenue_values and margin is not None else None},
            "funnel": {"cta_click": funnel_counts[0] or 0, "form_start": funnel_counts[1] or 0,
                       "form_success": funnel_counts[2] or 0},
            "events": events, "pages": [{"path": path, "views": amount} for path, amount in pages],
            "previous": {"sessions": previous[0] or 0, "leads": previous[1] or 0,
                         "qualified": previous[2] or 0, "sales": previous[3] or 0,
                         "revenue": float(previous[4]) if previous[4] is not None else None,
                         "visit_to_lead": round(previous[1] / previous[0] * 100, 1) if previous[0] else None,
                         "visit_to_qualified": round(previous[2] / previous[0] * 100, 1) if previous[0] else None,
                         "visit_to_sale": round(previous[3] / previous[0] * 100, 1) if previous[0] else None,
                         "revenue_per_visit": round(float(previous[4]) / previous[0], 2) if previous[0] and previous[4] is not None else None},
            "elements": [{"event": name, "name": label, "count": amount} for name, label, amount in elements],
            "actions": [{"name": label,
                         "views": sum(amount for event_name, amount in counts.items() if event_name.endswith("_view")),
                         "clicks": sum(amount for event_name, amount in counts.items() if event_name.endswith("_click") or event_name.endswith("_open")),
                         "leads": action_outcomes.get(label, (0, 0, 0, None))[0],
                         "qualified": action_outcomes.get(label, (0, 0, 0, None))[1],
                         "sales": action_outcomes.get(label, (0, 0, 0, None))[2],
                         "revenue": action_outcomes.get(label, (0, 0, 0, None))[3]}
                        for label, counts in action_counts.items()],
            "forms": [{"name": label, **values} for label, values in sorted(form_map.items())],
            "depths": depth_map,
            "daily": [{"date": str(day), "sessions": visit_count, "leads": lead_count,
                       "qualified": qualified_count, "sales": sales_count,
                       "conversion": round(lead_count / visit_count * 100, 1) if visit_count else None}
                      for day, visit_count, lead_count, qualified_count, sales_count in daily],
            "previous_daily": [{"date": str(day), "sessions": visit_count, "leads": lead_count,
                       "qualified": qualified_count, "sales": sales_count,
                       "conversion": round(lead_count / visit_count * 100, 1) if visit_count else None}
                      for day, visit_count, lead_count, qualified_count, sales_count in previous_daily],
            "landing_pages": [{"path": path, "sessions": visits_count, "leads": lead_count,
                               "qualified": qualified_count, "sales": sales_count,
                               "revenue": float(revenue) if revenue is not None else None}
                              for path, visits_count, lead_count, qualified_count, sales_count, revenue in page_breakdown],
            "devices": [{"device": kind, "sessions": visits_count, "leads": lead_count,
                         "qualified": qualified_count, "sales": sales_count,
                         "revenue": float(revenue) if revenue is not None else None}
                        for kind, visits_count, lead_count, qualified_count, sales_count, revenue in device_breakdown],
            "sources": [{"name": label or "Не определено", "sessions": visits_count, "leads": lead_count,
                         "qualified": qualified_count, "sales": sales_count,
                         "revenue": float(revenue) if revenue is not None else None}
                        for label, visits_count, lead_count, qualified_count, sales_count, revenue in source_breakdown],
            "campaigns": [{"name": label or "Не определено", "sessions": visits_count, "leads": lead_count,
                           "qualified": qualified_count, "sales": sales_count,
                           "revenue": float(revenue) if revenue is not None else None}
                          for label, visits_count, lead_count, qualified_count, sales_count, revenue in campaign_breakdown]}
