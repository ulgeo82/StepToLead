"""Из демо — в клиенты.

1) Посетитель демо жмёт «Хочу такой портал»: 5–7 вопросов (то, что уже было в экспресс-оценке, подставлено),
   заявка агентства помечается горячей, агентству — уведомление в Telegram и задача «Созвониться».
2) Агентство в админке одной кнопкой создаёт новый чистый кабинет: компания, проект, владелец с временным паролем,
   бриф с перенесёнными ответами, воронки по направлениям, база знаний ИИ, исходные цифры. Демо удаляется.
"""
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import check_origin, hash_password, require_admin, require_portal_user
from app.core.config import settings
from app.db import get_db
from app.models.brief import ClientBrief, ExpressAssessment
from app.models.marketing import ClientWorkspace, PortalProjectAccess, PortalUser, Project
from app.services import brief as brief_service
from app.services import brief_funnels
from app.services.project_scope import default_project

router = APIRouter(tags=["demo-launch"])
admin = APIRouter(prefix="/admin/launch-requests", dependencies=[Depends(require_admin)])
logger = logging.getLogger("uvicorn.error.demo_launch")

QUESTIONS = [
    {"k": "directions", "t": "Какие направления продаёте?", "type": "area", "required": True,
     "hint": "Каждое с новой строки — под каждое сделаем свою воронку", "example": "Кухни на заказ\nШкафы-купе"},
    {"k": "crm", "t": "Где сейчас ведёте заявки?", "type": "one", "required": True,
     "o": ["amoCRM", "Битрикс24", "Таблица", "Мессенджеры", "Нигде"]},
    {"k": "team", "t": "Сколько человек обрабатывает заявки?", "type": "one", "required": True,
     "o": ["Только я", "2–3 человека", "4–10 человек", "Больше 10"]},
    {"k": "site", "t": "Сайт (если есть)", "type": "text", "required": False, "hint": "Посмотрим до созвона"},
    {"k": "budget", "t": "Бюджет на рекламу в месяц", "type": "one", "required": True,
     "o": ["До 50 тыс. ₽", "50–150 тыс. ₽", "150–300 тыс. ₽", "Больше 300 тыс. ₽", "Пока не знаю"]},
    {"k": "start", "t": "Когда хотите запуститься?", "type": "one", "required": True,
     "o": ["В этом месяце", "В ближайшие 3 месяца", "Пока изучаю варианты"]},
    {"k": "call", "t": "Когда удобно созвониться на 20 минут?", "type": "one", "required": True,
     "o": ["Сегодня", "Завтра", "На этой неделе", "Напишите мне в мессенджер"]},
    {"k": "contact", "t": "Телефон или Telegram для связи", "type": "text", "required": True},
]
KEYS = {q["k"] for q in QUESTIONS}
TEMPLATE_HINTS = [("kitchens", r"кухн|мебел|шкаф"), ("houses", r"дом|бан[яиь]|строит|каркас"), ("renovation", r"ремонт|отделк"),
                  ("windows", r"окн|потол|остекл"), ("dental", r"стомат|клиник|зуб|медиц"), ("fences", r"забор|ворот|огражд")]


async def _demo_context(db: AsyncSession, user: PortalUser, project_id: int):
    from app.api.routes.crm import project_for
    project = await project_for(db, user, project_id)
    demo = (project.portal_state or {}).get("brief_demo") or {}
    row = await db.get(ExpressAssessment, int(demo["assessment_id"])) if demo.get("assessment_id") else None
    if not row or row.demo_workspace_id != project.workspace_id:
        raise HTTPException(404, "Это не демо-кабинет")
    return project, row


@router.get("/brief/demo/launch")
async def launch_form(project_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    from app.models.crm import CrmInbound
    _, row = await _demo_context(db, user, project_id)
    lead = await db.get(CrmInbound, row.lead_id)
    sent = (lead.raw_payload or {}).get("launch_request") if lead else None
    a = row.answers or {}
    prefill = {"budget": a.get("budget", ""), "start": a.get("start", ""), "contact": a.get("contact", ""),
               "directions": a.get("what", "")}
    return {"questions": QUESTIONS, "prefill": {**prefill, **((sent or {}).get("answers") or {})},
            "sent_at": (sent or {}).get("at"), "booking_url": settings.brief_booking_url}


class LaunchIn(BaseModel):
    answers: dict[str, str] = Field(default_factory=dict)


def _clean(answers: dict) -> dict:
    result = {}
    for q in QUESTIONS:
        value = str(answers.get(q["k"]) or "").strip()[:2000]
        if q["type"] == "one" and value and value not in q["o"]:
            raise HTTPException(422, f"Выберите вариант: {q['t']}")
        if q["required"] and not value:
            raise HTTPException(422, f"Заполните: {q['t']}")
        result[q["k"]] = value
    return result


@router.post("/brief/demo/launch")
async def launch_submit(project_id: int, payload: LaunchIn, request: Request, db: AsyncSession = Depends(get_db),
                        user: PortalUser = Depends(require_portal_user)):
    from app.api.routes.crm import activity
    from app.models.crm import CrmDeal, CrmInbound, CrmTask
    check_origin(request)
    _, row = await _demo_context(db, user, project_id)
    answers = _clean(payload.answers)
    lead = await db.get(CrmInbound, row.lead_id)
    if not lead:
        raise HTTPException(404, "Заявка не найдена")
    previous = (lead.raw_payload or {}).get("launch_request") or {}
    if previous.get("at") and datetime.fromisoformat(previous["at"]) > datetime.now(timezone.utc) - timedelta(minutes=2):
        return {"ok": True, "booking_url": settings.brief_booking_url}
    now = datetime.now(timezone.utc)
    lead.raw_payload = {**(lead.raw_payload or {}), "launch_request": {"answers": answers, "at": now.isoformat()},
                        "priority": "hot", "notes": f"🔥 Хочет свой портал. Созвон: {answers['call']}. Направления: {answers['directions'][:300]}"}
    deal = await db.get(CrmDeal, lead.deal_id) if lead.deal_id else None
    if deal:
        activity(db, deal, None, "COMMENT_ADDED", {"text": "🔥 Из демо: хочет свой портал. " + "; ".join(
            f"{q['t']} — {answers[q['k']]}" for q in QUESTIONS if answers.get(q["k"]))[:3000]})
        db.add(CrmTask(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id,
                       responsible_user_id=deal.responsible_user_id, title=f"Созвон по запуску портала ({answers['call']})",
                       type_code="CALL", due_at=now + timedelta(hours=1)))
    await db.commit()
    try:
        from app.services import monitor
        url = settings.frontend_origin.rstrip("/") + f"/crm?project_id={lead.project_id}" + (f"&deal={deal.id}" if deal else "")
        await monitor.send(db, f"🔥 Из демо хотят свой портал\n{escape(row.answers.get('name', ''))} · {escape(row.answers.get('what', ''))}\n"
                               f"Бюджет: {escape(answers['budget'])} · старт: {escape(answers['start'])}\n"
                               f"Созвон: {escape(answers['call'])} · {escape(answers['contact'])}\n"
                               f'<a href="{escape(url, quote=True)}">Открыть заявку</a>')
    except Exception:
        logger.exception("launch alert failed")
    return {"ok": True, "booking_url": settings.brief_booking_url}


# ------------------------------------------------------------------ админка: перевод в клиента

def _template_for(name: str) -> str:
    low = name.lower()
    return next((key for key, pattern in TEMPLATE_HINTS if re.search(pattern, low)), "other")


def _directions(text: str) -> list[dict]:
    names = [re.sub(r"^[\s•\-–\d.)]+", "", line).strip() for line in re.split(r"[\n;]", text or "")]
    result, seen = [], set()
    for index, name in enumerate(n for n in names if len(n) >= 2):
        if name.casefold() in seen:
            continue
        seen.add(name.casefold())
        template = brief_funnels.TEMPLATES[_template_for(name)]
        result.append({"id": f"d{index + 1}", "name": name[:60], "stages": list(template["stages"]),
                       "qualified_from": template["q"], "check": "", "cycle_days": ""})
    return result[:brief_funnels.MAX_DIRECTIONS]


def brief_answers(express: dict, launch: dict) -> dict:
    """Ответы экспресс-оценки и заявки из демо → вопросы полного брифа (остальное клиент дозаполнит)."""
    conv = express.get("conv")
    mapped = {
        "company.name": express.get("company") or express.get("what"), "company.what": express.get("what"),
        "company.geo": express.get("city"), "company.staff": launch.get("team"),
        "econ.check": express.get("check"), "mkt.channels": express.get("channels"),
        "mkt.leads": express.get("leads"), "mkt.cpl": express.get("cpl"), "mkt.budget": launch.get("budget") or express.get("budget"),
        "mkt.site": launch.get("site"), "goals.g3": express.get("goal"), "goals.launch": launch.get("start") or express.get("start"),
        "sales.response": express.get("speed"), "sales.crm": launch.get("crm"),
        "sales.conv": str(int(conv) * 10) if str(conv or "").isdigit() else None,
    }
    directions = _directions(launch.get("directions") or express.get("what") or "")
    if directions:
        mapped[brief_funnels.KEY] = json.dumps({"directions": directions}, ensure_ascii=False)
    return {k: str(v)[:10000] for k, v in mapped.items() if v and k in brief_service.ALL_QUESTIONS}


@admin.get("")
async def launch_requests(db: AsyncSession = Depends(get_db)):
    from app.models.crm import CrmInbound
    rows = (await db.execute(select(ExpressAssessment, CrmInbound).join(CrmInbound, CrmInbound.id == ExpressAssessment.lead_id)
                             .order_by(ExpressAssessment.id.desc()).limit(300))).all()
    out = []
    for row, lead in rows:
        launch = (lead.raw_payload or {}).get("launch_request")
        if not launch:
            continue
        a = row.answers or {}
        out.append({"assessment_id": row.id, "lead_id": lead.id, "name": a.get("name"), "company": a.get("company") or a.get("what"),
                    "what": a.get("what"), "city": a.get("city"), "contact": a.get("contact"), "launch": launch,
                    "converted_workspace_id": (lead.raw_payload or {}).get("converted_workspace_id"),
                    "demo_workspace_id": row.demo_workspace_id})
    return out


class ConvertIn(BaseModel):
    company_name: str = Field(min_length=2, max_length=180)
    owner_name: str = Field(min_length=2, max_length=180)
    owner_username: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9._@-]+$")
    bot_token: str | None = Field(default=None, max_length=128)
    delete_demo: bool = True


@admin.post("/{assessment_id}/convert")
async def convert(assessment_id: int, payload: ConvertIn, request: Request, db: AsyncSession = Depends(get_db)):
    from app.api.routes.crm import activity
    from app.models.crm import CrmDeal, CrmInbound
    from app.services import workspace_archive
    check_origin(request)
    row = await db.get(ExpressAssessment, assessment_id)
    lead = await db.get(CrmInbound, row.lead_id) if row else None
    if not row or not lead:
        raise HTTPException(404, "Заявка не найдена")
    if (lead.raw_payload or {}).get("converted_workspace_id"):
        raise HTTPException(409, "Кабинет по этой заявке уже создан")
    name = payload.company_name.strip()
    if await db.scalar(select(ClientWorkspace.id).where(func.lower(ClientWorkspace.name) == name.lower())):
        raise HTTPException(409, "Клиент с таким названием уже существует")
    username = payload.owner_username.strip().lower()
    if await db.scalar(select(PortalUser.id).where(PortalUser.username == username)):
        raise HTTPException(409, "Логин уже занят")
    express, launch = row.answers or {}, ((lead.raw_payload or {}).get("launch_request") or {}).get("answers") or {}
    workspace = ClientWorkspace(name=name)
    db.add(workspace); await db.flush()
    project = await default_project(db, workspace.id)
    project.name = (express.get("what") or name)[:180]
    if launch.get("site") and hasattr(project, "website"):
        project.website = launch["site"][:500]
    password = secrets.token_urlsafe(12)
    owner = PortalUser(workspace_id=workspace.id, username=username, display_name=payload.owner_name.strip(), role="client_owner",
                       password_hash=await run_in_threadpool(hash_password, password))
    if hasattr(owner, "must_change_password"):
        owner.must_change_password = True
    db.add(owner); await db.flush()
    db.add(PortalProjectAccess(user_id=owner.id, project_id=project.id))
    answers = brief_answers(express, launch)
    db.add(ClientBrief(workspace_id=workspace.id, project_id=project.id, lead_id=lead.id, answers=answers, source="demo", status="draft"))
    knowledge = brief_service.fallback_knowledge(answers)
    state = dict(project.portal_state or {})
    state["ai"] = {**dict(state.get("ai") or {}), **{k: knowledge[k] for k in ("knowledge", "tone", "goal")}}
    state["baseline"] = brief_service.baseline_from_answers(express, express=True)
    project.portal_state = state
    await db.flush()
    funnels = brief_funnels.parse(answers.get(brief_funnels.KEY))
    if funnels:
        from app.api.routes.crm import pipeline_for
        await pipeline_for(db, project.id, commit=False)
        await brief_funnels.plan(db, project, funnels, apply=True)
    demo = await db.get(ClientWorkspace, row.demo_workspace_id) if row.demo_workspace_id else None
    if payload.delete_demo and demo and demo.status != "deleted":
        await workspace_archive.delete_workspace(db, demo)
    lead.raw_payload = {**(lead.raw_payload or {}), "converted_workspace_id": workspace.id}
    deal = await db.get(CrmDeal, lead.deal_id) if lead.deal_id else None
    if deal:
        activity(db, deal, None, "COMMENT_ADDED", {"text": f"Создан кабинет клиента «{name}», владелец {username}"})
    await db.commit()
    bot = None
    if payload.bot_token and payload.bot_token.strip():
        from app.services import client_bots
        try:
            bot = (await client_bots.connect(db, workspace.id, payload.bot_token, name)).username
        except client_bots.BotTokenError as exc:
            bot = f"не подключён: {exc}"
    return {"workspace_id": workspace.id, "project_id": project.id, "username": username, "password": password,
            "login_url": settings.frontend_origin.rstrip("/") + "/login", "pipelines": len(funnels) or 1,
            "brief_answers": len(answers), "demo_deleted": bool(payload.delete_demo and demo), "bot": bot}


router.include_router(admin)
