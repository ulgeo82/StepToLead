"""«Запуск»: the first screen of a new client — setup checklist, the agency marketer, the agency work plan
with statuses and monthly goals with plan-vs-fact. Agency staff edit the card and the plan in the admin;
the client owner can adjust goals and hide the screen once everything is running."""
import secrets
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.crm import project_for
from app.core.access import check_origin, require_admin, require_portal_user
from app.core.permissions import effective_permissions
from app.db import get_db
from app.models.crm import CrmDeal, CrmPipeline
from app.models.marketing import (AdConnection, LeadInboundSource, PortalProjectAccess, PortalUser, Project,
                                  ProjectEconomics)
from app.models.tilda import TildaConnection
from app.models.website import WebsiteSite
from app.models.messaging import MessagingChannel
from app.models.telephony import TelephonyConnection

router = APIRouter(prefix="/crm", tags=["launch"], dependencies=[Depends(require_portal_user)])
admin_router = APIRouter(prefix="/portal/admin", tags=["launch-admin"], dependencies=[Depends(require_admin)])

PLAN_TEMPLATE = ["Аудит ниши и конкурентов", "Настройка CRM и воронки", "Подключение заявок с сайта, чатов и звонков",
                 "Запуск рекламы в Яндекс Директе", "Запуск Авито", "Первый отчёт и корректировка кампаний"]
STATUSES = {"todo": "Запланировано", "doing": "В работе", "done": "Готово"}


def launch_state(project: Project) -> dict:
    data = dict((project.portal_state or {}).get("launch") or {})
    data.setdefault("plan", [{"id": secrets.token_hex(4), "title": title, "status": "todo", "due": None}
                             for title in PLAN_TEMPLATE] if not data.get("plan_saved") else [])
    data.setdefault("marketer", None)
    data.setdefault("goals", {})
    data.setdefault("dismissed_by", [])
    data.setdefault("weekly_note", "")
    return data


def save_launch(project: Project, data: dict) -> None:
    project.portal_state = {**(project.portal_state or {}), "launch": data}


async def checklist(db: AsyncSession, project: Project, user: PortalUser | None) -> list[dict]:
    pid = project.id
    count = lambda query: db.scalar(select(func.count()).select_from(query.subquery()))  # noqa: E731
    pipeline = await count(select(CrmPipeline.id).where(CrmPipeline.project_id == pid))
    ads = await count(select(AdConnection.id).where(AdConnection.project_id == pid, AdConnection.status == "connected"))
    team = await count(select(PortalProjectAccess.id).join(PortalUser, PortalUser.id == PortalProjectAccess.user_id).where(
        PortalProjectAccess.project_id == pid, PortalUser.active.is_(True), PortalUser.role != "client_owner"))
    channels = (await count(select(MessagingChannel.id).where(MessagingChannel.project_id == pid, MessagingChannel.active.is_(True)))
                + await count(select(TelephonyConnection.id).where(TelephonyConnection.project_id == pid, TelephonyConnection.active.is_(True))))
    economics = await count(select(ProjectEconomics.id).where(ProjectEconomics.project_id == pid))
    deals = await count(select(CrmDeal.id).where(CrmDeal.project_id == pid))
    suffix = f"?project_id={pid}"
    items = [
        {"key": "pipeline", "label": "Воронка и этапы настроены под вашу нишу", "done": bool(pipeline), "agency": True,
         "href": f"/crm{suffix}&tab=pipeline"},
        {"key": "ads", "label": "Рекламные кабинеты подключены", "done": bool(ads), "agency": True, "href": f"/ads{suffix}"},
        {"key": "telegram", "label": "Подключите Telegram — заявки будут приходить на телефон за секунды",
         "done": bool(user and user.telegram_chat_id), "agency": False, "href": f"/settings{suffix}#telegram"},
        {"key": "team", "label": "Пригласите менеджеров, которые обрабатывают заявки", "done": bool(team), "agency": False,
         "href": "/team"},
        {"key": "channels", "label": "Подключите WhatsApp, чаты и телефонию (вместе с маркетологом)", "done": bool(channels),
         "agency": False, "href": f"/crm{suffix}&tab=chats"},
        {"key": "economics", "label": "Укажите средний чек и маржу — без них не посчитать окупаемость", "done": bool(economics),
         "agency": False, "href": f"/settings{suffix}#economics"},
        {"key": "first_lead", "label": "Примите первую заявку в CRM", "done": bool(deals), "agency": False,
         "href": f"/crm{suffix}"},
    ]
    if user is not None and user.role == "sales_manager":
        items = [i for i in items if i["key"] in {"telegram", "first_lead"}]
    return items


async def month_fact(db: AsyncSession, project: Project) -> dict:
    from app.services.result_analytics import result_facts
    today = datetime.now(timezone.utc).date()
    facts = await result_facts(db, project, today.replace(day=1), today)
    t = facts["current"]["totals"]
    return {"leads": t.get("leads"), "cpl": t.get("cpl"), "meetings": t.get("meetings"), "sales": t.get("sales"),
            "spend": t.get("spend"), "target_share": t.get("target_share"), "month": today.replace(day=1)}


async def launch_payload(db: AsyncSession, project: Project, user: PortalUser | None) -> dict:
    data = launch_state(project)
    items = await checklist(db, project, user)
    done = sum(i["done"] for i in items)
    plan = data["plan"]
    return {"project_id": project.id, "checklist": items, "done": done, "total": len(items),
            "complete": done == len(items) and all(p["status"] == "done" for p in plan),
            "dismissed": bool(user and user.id in data["dismissed_by"]),
            "marketer": data["marketer"], "plan": plan, "statuses": STATUSES, "goals": data["goals"],
            "fact": await month_fact(db, project), "weekly_note": data["weekly_note"],
            "can_edit_goals": bool(user and (user.role == "client_owner" or "manage_settings" in effective_permissions(user)))}


@router.get("/projects/{project_id}/launch")
async def get_launch(project_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    project = await project_for(db, user, project_id)
    return await launch_payload(db, project, user)


class Goals(BaseModel):
    model_config = ConfigDict(extra="forbid")
    leads: int | None = Field(default=None, ge=0, le=100000)
    cpl: float | None = Field(default=None, ge=0, le=10_000_000)
    meetings: int | None = Field(default=None, ge=0, le=100000)
    sales: int | None = Field(default=None, ge=0, le=100000)


@router.put("/projects/{project_id}/launch/goals")
async def put_goals(project_id: int, payload: Goals, request: Request, db: AsyncSession = Depends(get_db),
                    user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    project = await project_for(db, user, project_id)
    if not (user.role == "client_owner" or "manage_settings" in effective_permissions(user)):
        raise HTTPException(403, "Цели меняет владелец или руководитель")
    data = launch_state(project)
    data["goals"] = {k: v for k, v in payload.model_dump().items() if v is not None}
    save_launch(project, data)
    await db.commit()
    return await launch_payload(db, project, user)


@router.post("/projects/{project_id}/launch/dismiss")
async def dismiss(project_id: int, request: Request, db: AsyncSession = Depends(get_db),
                  user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    project = await project_for(db, user, project_id)
    data = launch_state(project)
    data["dismissed_by"] = sorted(set(data["dismissed_by"]) | {user.id})
    save_launch(project, data)
    await db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- agency side

class Marketer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=120)
    role: str | None = Field(default="Ваш маркетолог", max_length=80)
    telegram: str | None = Field(default=None, max_length=64)
    phone: str | None = Field(default=None, max_length=40)
    hours: str | None = Field(default=None, max_length=80)
    photo_url: str | None = Field(default=None, max_length=500, pattern=r"^https://")


class PlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, max_length=16)
    title: str = Field(min_length=2, max_length=160)
    status: str = Field(default="todo", pattern="^(todo|doing|done)$")
    due: date | None = None
    note: str | None = Field(default=None, max_length=300)


class LaunchAdmin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    marketer: Marketer | None = None
    plan: list[PlanItem] | None = Field(default=None, max_length=30)
    goals: Goals | None = None
    weekly_note: str | None = Field(default=None, max_length=1000)


@admin_router.get("/projects")
async def admin_projects(workspace_id: int, db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(Project).where(Project.workspace_id == workspace_id).order_by(Project.id))).all()
    return [{"id": p.id, "name": p.name, "launch": await launch_payload(db, p, None)} for p in rows]


@admin_router.put("/projects/{project_id}/launch")
async def admin_put_launch(project_id: int, payload: LaunchAdmin, db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    data = launch_state(project)
    changes = payload.model_dump(exclude_unset=True, mode="json")
    if "marketer" in changes:
        data["marketer"] = changes["marketer"]
    if "plan" in changes:
        data["plan"] = [{**item, "id": item.get("id") or secrets.token_hex(4)} for item in changes["plan"] or []]
        data["plan_saved"] = True
    if "goals" in changes:
        data["goals"] = {k: v for k, v in (changes["goals"] or {}).items() if v is not None}
    if "weekly_note" in changes:
        data["weekly_note"] = (changes["weekly_note"] or "").strip()
    save_launch(project, data)
    await db.commit()
    return await launch_payload(db, project, None)


# --------------------------------------------------------------------------- integrations overview

PLATFORMS = {"yandex_direct": "Яндекс Директ", "vk_ads": "VK Реклама", "avito_items": "Авито · Объявления",
             "avito_ads": "Авито Реклама", "telegram_ads": "Telegram Ads"}


def can_manage(user: PortalUser) -> bool:
    return bool({"manage_pipeline", "manage_integrations", "manage_settings", "manage_sources"} & effective_permissions(user))


@router.get("/projects/{project_id}/integrations")
async def integrations(project_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    """Everything connected to the project on one screen, with where to configure each part."""
    project = await project_for(db, user, project_id)
    pid = project.id
    ads = (await db.scalars(select(AdConnection).where(AdConnection.project_id == pid).order_by(AdConnection.id))).all()
    channels = (await db.scalars(select(MessagingChannel).where(MessagingChannel.project_id == pid))).all()
    phone = (await db.scalars(select(TelephonyConnection).where(TelephonyConnection.project_id == pid))).all()
    sites = (await db.scalars(select(WebsiteSite).where(WebsiteSite.project_id == pid))).all()
    tilda = (await db.scalars(select(TildaConnection).where(TildaConnection.project_id == pid))).all()
    sources = (await db.scalars(select(LeadInboundSource).where(LeadInboundSource.project_id == pid)
                                .order_by(LeadInboundSource.id))).all()
    suffix = f"?project_id={pid}"
    return {
        "can_manage": can_manage(user),
        "groups": [
            {"key": "ads", "title": "Реклама", "href": f"/ads{suffix}", "items": [
                {"name": a.name, "kind": PLATFORMS.get(a.platform, a.platform), "status": a.status,
                 "detail": f"данные за {a.last_synced_at:%d.%m %H:%M}" if a.last_synced_at else None} for a in ads]},
            {"key": "site", "title": "Сайт и формы", "href": f"/analytics{suffix}&tab=website", "items": [
                *[{"name": s.name, "kind": "Трекинг сайта", "status": "connected" if s.active else "off",
                   "detail": f"последний визит {s.last_event_at:%d.%m %H:%M}" if s.last_event_at else "визитов ещё не было"} for s in sites],
                *[{"name": t.form_name, "kind": "Форма Tilda", "status": "connected" if t.is_active else "off",
                   "detail": f"последняя заявка {t.last_received_at:%d.%m %H:%M}" if t.last_received_at else "заявок ещё не было"} for t in tilda]]},
            {"key": "chats", "title": "Чаты", "href": f"/crm{suffix}&tab=chats", "items": [
                {"name": c.name, "kind": {"avito": "Авито", "telegram_bot": "Telegram-бот", "whatsapp": "WhatsApp"}.get(c.kind, c.kind),
                 "status": c.status if c.active else "off", "detail": c.last_error} for c in channels]},
            {"key": "phone", "title": "Телефония", "href": f"/crm{suffix}&tab=calls", "items": [
                {"name": t.name, "kind": "Mango Office", "status": t.status if t.active else "off",
                 "detail": f"последний звонок {t.last_event_at:%d.%m %H:%M}" if t.last_event_at else "событий ещё не было"} for t in phone]},
        ],
        "sources": [{"id": s.id, "name": s.name, "active": s.active, "auto_accept": s.auto_accept,
                     "auto_assign": s.auto_assign} for s in sources],
    }


class SourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto_accept: bool | None = None
    auto_assign: bool | None = None


@router.patch("/inbound-sources/{source_id}")
async def patch_source(source_id: int, payload: SourcePatch, request: Request, db: AsyncSession = Depends(get_db),
                       user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    source = await db.get(LeadInboundSource, source_id)
    if not source or source.workspace_id != user.workspace_id or not source.project_id:
        raise HTTPException(404, "Источник не найден")
    await project_for(db, user, source.project_id)
    if not can_manage(user):
        raise HTTPException(403, "Настраивать источники может руководитель или владелец")
    for key, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(source, key, value)
    await db.commit()
    return {"id": source.id, "auto_accept": source.auto_accept, "auto_assign": source.auto_assign}


# --------------------------------------------------------------------------- offline conversions

@router.get("/projects/{project_id}/conversions")
async def conversions_overview(project_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    from app.models.crm import OfflineConversion
    from app.services import conversions
    project = await project_for(db, user, project_id)
    data = conversions.metrika_cfg(project)
    rows = (await db.execute(select(OfflineConversion.kind, OfflineConversion.status, func.count()).where(
        OfflineConversion.project_id == project.id).group_by(OfflineConversion.kind, OfflineConversion.status))).all()
    stats = {}
    for kind, status, amount in rows:
        stats.setdefault(kind, {"pending": 0, "sent": 0, "no_id": 0, "error": 0})[status] = amount
    return {"can_manage": can_manage(user), "stats": stats, "kinds": conversions.KIND_NAMES,
            "metrika": {"connected": bool(data.get("counter_id")), "counter_id": data.get("counter_id"),
                        "counter_name": data.get("counter_name"), "last_error": data.get("last_error"),
                        "last_upload_at": data.get("last_upload_at"), "last_upload_rows": data.get("last_upload_rows")}}


class MetrikaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    counter_id: int = Field(gt=0)
    token: str = Field(min_length=20, max_length=200)


@router.put("/projects/{project_id}/metrika")
async def connect_metrika(project_id: int, payload: MetrikaIn, request: Request, db: AsyncSession = Depends(get_db),
                          user: PortalUser = Depends(require_portal_user)):
    from app.services import conversions, plans
    check_origin(request)
    project = await project_for(db, user, project_id)
    await plans.require(db, project.workspace_id, "conversions")
    if not can_manage(user):
        raise HTTPException(403, "Подключать Метрику может руководитель или владелец")
    try:
        await conversions.connect(project, payload.counter_id, payload.token.strip())
    except conversions.MetrikaError as exc:
        raise HTTPException(422, str(exc)) from None
    await db.commit()
    return await conversions_overview(project_id, db, user)


@router.delete("/projects/{project_id}/metrika")
async def disconnect_metrika(project_id: int, request: Request, db: AsyncSession = Depends(get_db),
                             user: PortalUser = Depends(require_portal_user)):
    from app.services import conversions
    check_origin(request)
    project = await project_for(db, user, project_id)
    if not can_manage(user):
        raise HTTPException(403, "Отключать Метрику может руководитель или владелец")
    conversions.save_metrika(project, None)
    await db.commit()
    return {"ok": True}


@router.post("/projects/{project_id}/conversions/upload")
async def upload_now(project_id: int, request: Request, db: AsyncSession = Depends(get_db),
                     user: PortalUser = Depends(require_portal_user)):
    from app.services import conversions
    check_origin(request)
    project = await project_for(db, user, project_id)
    if not can_manage(user):
        raise HTTPException(403, "Недостаточно прав")
    sent = await conversions.upload(db, project)
    error = conversions.metrika_cfg(project).get("last_error")
    if error:
        raise HTTPException(422, error)
    return {"sent": sent}


@router.get("/projects/{project_id}/conversions.csv")
async def conversions_csv(project_id: int, kind: str | None = None, db: AsyncSession = Depends(get_db),
                          user: PortalUser = Depends(require_portal_user)):
    """For VK Ads «Офлайн-конверсии» and audiences. Contains phones, so only for managers of the project."""
    from app.models.crm import OfflineConversion
    from app.services import conversions
    project = await project_for(db, user, project_id)
    if not can_manage(user):
        raise HTTPException(403, "Выгрузку с телефонами клиентов может скачать руководитель или владелец")
    query = select(OfflineConversion).where(OfflineConversion.project_id == project.id)
    if kind in conversions.KIND_NAMES:
        query = query.where(OfflineConversion.kind == kind)
    rows = (await db.scalars(query.order_by(OfflineConversion.occurred_at))).all()
    return Response(conversions.export_csv(list(rows)), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="conversions-{project.id}.csv"'})


# --------------------------------------------------------------------------- client health (agency admin)

async def client_health(db: AsyncSession, workspace_id: int) -> dict:
    """A client whose leads are not processed leaves saying «реклама не работает» — see it weeks earlier."""
    import statistics
    from datetime import timedelta
    from app.models.crm import CrmTask
    from app.models.marketing import ClientSale
    current = datetime.now(timezone.utc)
    week, month = current - timedelta(days=7), current - timedelta(days=30)

    def aware(value):
        return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value
    users = (await db.scalars(select(PortalUser).where(PortalUser.workspace_id == workspace_id, PortalUser.active.is_(True)))).all()
    last_seen = max((aware(u.last_activity_at) for u in users if u.last_activity_at), default=None)
    # Only the timestamps are needed; full deal rows (JSON fields included) made this page slow on large clients.
    deals = (await db.execute(select(CrmDeal.id, CrmDeal.created_at, CrmDeal.first_response_at, CrmDeal.closed_at, CrmDeal.archived_at)
                              .where(CrmDeal.workspace_id == workspace_id, CrmDeal.created_at >= month))).all()
    week_deals = [d for d in deals if aware(d.created_at) >= week]
    waits = [(aware(d.first_response_at) - aware(d.created_at)).total_seconds() / 60 for d in week_deals if d.first_response_at]
    unanswered = sum(1 for d in week_deals if d.first_response_at is None and d.closed_at is None and d.archived_at is None)
    open_ids = [d.id for d in deals if d.closed_at is None and d.archived_at is None]
    with_task = set((await db.scalars(select(CrmTask.deal_id).where(CrmTask.deal_id.in_(open_ids), CrmTask.status == "OPEN"))).all()) if open_ids else set()
    no_task = len([i for i in open_ids if i not in with_task])
    sales = await db.scalar(select(func.count(ClientSale.id)).join(Project, Project.id == ClientSale.project_id).where(
        Project.workspace_id == workspace_id, ClientSale.occurred_at >= month)) or 0
    median = statistics.median(waits) if waits else None
    score, reasons = 100, []
    if last_seen is None or last_seen < week:
        score -= 30; reasons.append("Клиент не заходил в портал больше недели" if last_seen else "Клиент ещё ни разу не заходил в портал")
    if median is not None and median > 60:
        score -= 20; reasons.append(f"Медленный ответ на заявки: {median:.0f} мин")
    elif median is not None and median > 15:
        score -= 10; reasons.append(f"Ответ на заявки дольше 15 минут: {median:.0f} мин")
    if unanswered:
        score -= min(20, unanswered * 5); reasons.append(f"Без ответа: {unanswered} заявок за неделю")
    if open_ids and no_task / len(open_ids) > 0.3:
        score -= 15; reasons.append(f"Без следующего шага: {no_task} из {len(open_ids)} открытых сделок")
    if len(deals) >= 10 and not sales:
        score -= 15; reasons.append("За месяц не подтверждено ни одной продажи — ROMI не посчитать")
    status = "good" if score >= 75 else "warning" if score >= 50 else "risk"
    return {"workspace_id": workspace_id, "score": max(0, score), "status": status, "reasons": reasons,
            "last_seen_at": last_seen, "leads_7d": len(week_deals), "median_response_min": round(median) if median is not None else None,
            "unanswered_7d": unanswered, "open_without_task": no_task, "sales_30d": sales}


@admin_router.get("/health")
async def admin_health(db: AsyncSession = Depends(get_db)):
    from app.models.marketing import ClientWorkspace
    workspaces = (await db.scalars(select(ClientWorkspace).where(ClientWorkspace.status != "deleted").order_by(ClientWorkspace.id))).all()
    return [await client_health(db, w.id) for w in workspaces]


# --------------------------------------------------------------------------- tariffs

@router.get("/plan")
async def my_plan(db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    """The client's tariff and what it includes (Settings → Компания)."""
    from app.services import plans
    return await plans.overview(db, user.workspace_id)


class PlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: str | None = Field(default=None, pattern=r"^(start|growth|system)$")


@admin_router.get("/plans")
async def admin_plans(db: AsyncSession = Depends(get_db)):
    from app.models.marketing import ClientWorkspace
    from app.services import plans
    rows = (await db.scalars(select(ClientWorkspace).order_by(ClientWorkspace.id))).all()
    return {"plans": [plans.plan_info(c) for c in plans.ORDER], "features": plans.FEATURES,
            "workspaces": [{"workspace_id": w.id, "plan": w.plan if w.plan in plans.PLANS else None} for w in rows]}


@admin_router.put("/workspaces/{workspace_id}/plan")
async def admin_set_plan(workspace_id: int, payload: PlanIn, request: Request, db: AsyncSession = Depends(get_db)):
    from app.models.marketing import ClientWorkspace
    from app.services import plans
    check_origin(request)
    workspace = await db.get(ClientWorkspace, workspace_id)
    if not workspace:
        raise HTTPException(404, "Клиент не найден")
    workspace.plan = payload.plan
    await db.commit()
    return await plans.overview(db, workspace.id)
