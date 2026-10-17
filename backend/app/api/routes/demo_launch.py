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
from datetime import datetime, timedelta, timezone  # noqa: F401
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import check_origin, require_admin, require_portal_user
from app.core.config import settings
from app.db import get_db
from app.models.brief import ClientBrief, ExpressAssessment
from app.models.marketing import ClientWorkspace, PortalUser, Project
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
    """Любой демо-кабинет: из экспресс-оценки (есть ответы) или созданный агентством в админке."""
    from app.api.routes.crm import project_for
    project = await project_for(db, user, project_id)
    if project.status not in {"demo", "trial"}:
        raise HTTPException(404, "Это не демо-кабинет")
    demo = (project.portal_state or {}).get("brief_demo") or {}
    row = await db.get(ExpressAssessment, int(demo["assessment_id"])) if demo.get("assessment_id") else None
    if row and row.demo_workspace_id != project.workspace_id:
        row = None
    return project, row


async def _lead_for(db: AsyncSession, project: Project, row: ExpressAssessment | None):
    """Заявка агентства: из экспресс-оценки или созданная этим демо раньше."""
    from app.models.crm import CrmInbound
    if row:
        return await db.get(CrmInbound, row.lead_id)
    lead_id = (project.portal_state or {}).get("demo_launch_lead_id")
    return await db.get(CrmInbound, int(lead_id)) if lead_id else None


@router.get("/brief/demo/launch")
async def launch_form(project_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    project, row = await _demo_context(db, user, project_id)
    lead = await _lead_for(db, project, row)
    sent = (lead.raw_payload or {}).get("launch_request") if lead else None
    a = (row.answers if row else {}) or {}
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


async def _new_agency_lead(db: AsyncSession, project: Project, answers: dict):
    """Демо, созданное агентством: заявки ещё нет — заводим её в «Неразобранном» агентства."""
    from app.api.routes.portal import InboundLead
    from app.models.marketing import LeadInboundSource
    from app.services.inbound_lead import create_inbound
    agency = await db.get(ClientWorkspace, settings.agency_workspace_id) if settings.agency_workspace_id else None
    if not agency or agency.status != "active":
        return None
    agency_project = await default_project(db, agency.id)
    source = await db.scalar(select(LeadInboundSource).where(LeadInboundSource.project_id == agency_project.id,
                                                              LeadInboundSource.name == "Демо-портал"))
    if not source:
        source = LeadInboundSource(workspace_id=agency.id, project_id=agency_project.id, name="Демо-портал",
                                   token_hash=secrets.token_hex(32), token_prefix="demo", active=False, auto_assign=True, auto_accept=False)
        db.add(source); await db.flush()
    contact = answers["contact"]
    phone = contact if len(re.sub(r"\D", "", contact)) >= 10 and not contact.startswith("@") else None
    result = await create_inbound(db, source, InboundLead(full_name=(project.name or "Клиент из демо")[:180], contact=contact,
        contact_method="Telegram" if contact.startswith("@") else "Телефон", phone=phone, source="Демо-портал",
        contact_consent=True, notes="Из демо-портала: хочет свой портал"), allow_raw_contact=True, commit=False)
    from app.models.crm import CrmInbound
    lead = await db.get(CrmInbound, result["inbound_id"])
    lead.raw_payload = {**(lead.raw_payload or {}), "demo_workspace_id": project.workspace_id}
    project.portal_state = {**(project.portal_state or {}), "demo_launch_lead_id": lead.id}
    return lead


@router.post("/brief/demo/launch")
async def launch_submit(project_id: int, payload: LaunchIn, request: Request, db: AsyncSession = Depends(get_db),
                        user: PortalUser = Depends(require_portal_user)):
    from app.api.routes.crm import activity
    from app.models.crm import CrmDeal, CrmInbound, CrmTask
    check_origin(request)
    project, row = await _demo_context(db, user, project_id)
    answers = _clean(payload.answers)
    lead = await _lead_for(db, project, row)
    if not lead:
        lead = await _new_agency_lead(db, project, answers)
        if not lead:
            raise HTTPException(503, "Приём заявок агентства пока не настроен")
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
        who = (row.answers if row else {}) or {}
        await monitor.send(db, f"🔥 Из демо хотят свой портал\n{escape(who.get('name') or project.name)} · {escape(who.get('what') or answers['directions'][:80])}\n"
                               f"Бюджет: {escape(answers['budget'])} · старт: {escape(answers['start'])}\n"
                               f"Созвон: {escape(answers['call'])} · {escape(answers['contact'])}\n"
                               f'<a href="{escape(url, quote=True)}">Открыть заявку</a>')
    except Exception:
        logger.exception("launch alert failed")
    return {"ok": True, "booking_url": settings.brief_booking_url}


# ------------------------------------------------------------------ живое демо

LIVE_DAYS = 7


@router.post("/brief/demo/go-live")
async def go_live(project_id: int, request: Request, db: AsyncSession = Depends(get_db),
                  user: PortalUser = Depends(require_portal_user)):
    """«Очистить демо и подключить свои кабинеты»: новый пустой кабинет-демо на 7 дней, человек переносится туда,
    кабинет с вымышленными данными удаляется. Рекламные кабинеты подключаются только на чтение (тариф «Демо»)."""
    from app.services import workspace_archive
    check_origin(request)
    project, row = await _demo_context(db, user, project_id)
    if project.status != "demo":
        raise HTTPException(409, "Демо уже очищено — подключайте свои рекламные кабинеты в разделе «Реклама»")
    if user.role != "client_owner":
        raise HTTPException(403, "Очистить демо может владелец демо-кабинета")
    old = await db.get(ClientWorkspace, project.workspace_id)
    now = datetime.now(timezone.utc)
    base = re.sub(r"^Демо · ", "", old.name).split(" · ")[0][:120]
    workspace = ClientWorkspace(name=f"Живое демо · {base} · {secrets.token_hex(2)}", plan="trial")
    db.add(workspace); await db.flush()
    live = await default_project(db, workspace.id)
    live.name, live.status = project.name, "trial"
    state = {k: v for k, v in (project.portal_state or {}).items() if k in {"ai", "brief_demo", "demo_launch_lead_id"}}
    state["demo_live"] = {"started_at": now.isoformat(), "expires_at": (now + timedelta(days=LIVE_DAYS)).isoformat()}
    live.portal_state = state
    user.workspace_id = workspace.id
    if row:
        row.demo_workspace_id = workspace.id
        row.expires_at = now + timedelta(days=LIVE_DAYS)
    lead = await _lead_for(db, project, row)
    if lead:
        lead.raw_payload = {**(lead.raw_payload or {}), "demo_workspace_id": workspace.id}
    await db.flush()
    await workspace_archive.delete_workspace(db, old)  # пользователь уже перенесён — его сессия не трогается
    await db.commit()
    return {"project_id": live.id, "expires_at": state["demo_live"]["expires_at"]}


async def expire_live_demos(db: AsyncSession, now: datetime | None = None) -> int:
    """Через 7 дней живое демо удаляется вместе с ключами доступа к рекламным кабинетам."""
    from app.models.marketing import AdConnection
    from app.services import workspace_archive
    now = now or datetime.now(timezone.utc)
    count = 0
    for project in (await db.scalars(select(Project).where(Project.status == "trial"))).all():
        until = ((project.portal_state or {}).get("demo_live") or {}).get("expires_at")
        if not until or datetime.fromisoformat(until) > now:
            continue
        workspace = await db.get(ClientWorkspace, project.workspace_id)
        if not workspace or workspace.status == "deleted":
            continue
        for connection in (await db.scalars(select(AdConnection).where(AdConnection.workspace_id == workspace.id))).all():
            connection.access_token_encrypted = ""
            connection.vk_refresh_token_encrypted = connection.vk_client_secret_encrypted = None
        await workspace_archive.delete_workspace(db, workspace)
        count += 1
    await db.commit()
    return count


# ------------------------------------------------------------------ ИИ-разбор аудита: один на демо, дёшево

AUDIT_PROMPT = ("Ты — маркетолог агентства StepToLead. По данным экспресс-аудита рекламы за 30 дней напиши владельцу бизнеса "
                "коротко и по делу: 3 главные потери и что сделать с каждой (1–2 предложения). Используй только цифры из данных, "
                "ничего не придумывай и не обещай результат. Если находок нет — скажи, каких данных не хватает для вывода. "
                "Без приветствий, без markdown-таблиц, до 120 слов.")


@router.post("/result/audit/ai")
async def audit_ai(project_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Демо: один разбор на кабинет (≈600 токенов ответа, дешевле 1 ₽). Клиенты: по тарифу с ИИ, раз в сутки."""
    from app.api.routes.result import advertising_audit, scoped_project
    from app.services import ai, plans
    check_origin(request)
    project, _, _ = await scoped_project(db, request, project_id)
    demo = project.status in {"demo", "trial"}
    saved = (project.portal_state or {}).get("audit_ai") or {}
    today = datetime.now(timezone.utc).date().isoformat()
    if saved.get("text") and (demo or saved.get("date") == today):
        return {**saved, "repeat": False}
    if not demo:
        await plans.require(db, project.workspace_id, "ai_chat")
    if not ai.configured("summary"):
        raise HTTPException(409, "ИИ-разбор временно недоступен — смотрите находки аудита выше")
    data = await advertising_audit(request, project_id, db)
    compact = {"spend_rub": data.get("spend"), "leads": data.get("leads"), "loss_rub": data.get("total_loss_rub"),
               "niche": project.name, "findings": [{"title": f["title"], "loss_rub": f["loss_rub"],
                                                    "campaigns": [c["campaign_name"] for c in f["campaigns"]][:5]}
                                                   for f in data.get("findings", [])[:8]]}
    try:
        text_ = await ai.complete(AUDIT_PROMPT, [{"role": "user", "content": json.dumps(compact, ensure_ascii=False)}],
                                  max_tokens=500, temperature=0.2, feature="summary", workspace_id=project.workspace_id, bucket="agency")
    except ai.AIError as exc:
        raise HTTPException(409, str(exc)) from None
    result = {"text": text_.strip()[:2000], "date": today}
    project = await db.scalar(select(Project).where(Project.id == project.id).with_for_update().execution_options(populate_existing=True))
    project.portal_state = {**(project.portal_state or {}), "audit_ai": result}
    await db.commit()
    return {**result, "repeat": False}


# ------------------------------------------------------------------ тестовая заявка по QR: «заявка за секунду»

@router.get("/brief/demo/test-lead")
async def test_lead_link(project_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    import io
    import segno
    project, _ = await _demo_context(db, user, project_id)
    state = dict(project.portal_state or {})
    token = state.get("demo_test_token")
    if not token:
        token = secrets.token_urlsafe(18)
        project.portal_state = {**state, "demo_test_token": token}
        await db.commit()
    url = settings.frontend_origin.rstrip("/") + f"/try/{token}"
    buffer = io.BytesIO()
    segno.make(url, error="m").save(buffer, kind="svg", scale=5, border=2, dark="#101c31", xmldecl=False)
    return {"url": url, "svg": buffer.getvalue().decode()}


class TestLeadIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=5, max_length=40)
    comment: str = Field(default="", max_length=500)


@router.post("/public/try/{token}")
async def test_lead_submit(token: str, payload: TestLeadIn, request: Request, db: AsyncSession = Depends(get_db)):
    from app.api.routes.access import rate_limit
    from app.api.routes.portal import InboundLead
    from app.models.marketing import LeadInboundSource
    from app.services.inbound_lead import create_inbound
    from app.services.notifications import flush_telegram
    await rate_limit(request, "demo_test_lead", 10, 3600)
    if len(token) < 16:
        raise HTTPException(404, "Ссылка не найдена")
    projects = (await db.scalars(select(Project).where(Project.status.in_(("demo", "trial"))))).all()
    project = next((p for p in projects if secrets.compare_digest(str((p.portal_state or {}).get("demo_test_token") or ""), token)), None)
    if not project:
        raise HTTPException(404, "Ссылка устарела")
    source = await db.scalar(select(LeadInboundSource).where(LeadInboundSource.project_id == project.id,
                                                              LeadInboundSource.name == "Тестовая заявка"))
    if not source:
        source = LeadInboundSource(workspace_id=project.workspace_id, project_id=project.id, name="Тестовая заявка",
                                   token_hash=secrets.token_hex(32), token_prefix="try", active=False, auto_assign=True, auto_accept=False)
        db.add(source); await db.flush()
    await create_inbound(db, source, InboundLead(full_name=payload.name.strip(), phone=payload.phone.strip(), contact=payload.phone.strip(),
                                                 contact_method="Телефон", source="Тестовая заявка", contact_consent=True,
                                                 notes=payload.comment.strip() or "Тестовая заявка с телефона"),
                         allow_raw_contact=True, commit=False)
    await db.commit()
    flush_telegram(db)
    return {"ok": True}


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


async def _launch_leads(db: AsyncSession):
    from app.models.crm import CrmInbound
    rows = (await db.scalars(select(CrmInbound).where(CrmInbound.workspace_id == settings.agency_workspace_id)
                             .order_by(CrmInbound.id.desc()).limit(1000))).all() if settings.agency_workspace_id else []
    return [lead for lead in rows if (lead.raw_payload or {}).get("launch_request")]


async def _express_for(db: AsyncSession, lead):
    aid = (lead.raw_payload or {}).get("assessment_id")
    return await db.get(ExpressAssessment, int(aid)) if aid else None


@admin.get("")
async def launch_requests(db: AsyncSession = Depends(get_db)):
    out = []
    for lead in await _launch_leads(db):
        row = await _express_for(db, lead)
        a = (row.answers if row else {}) or {}
        launch = lead.raw_payload["launch_request"]
        out.append({"lead_id": lead.id, "name": a.get("name") or lead.name, "company": a.get("company") or a.get("what") or lead.name,
                    "what": a.get("what"), "city": a.get("city"), "contact": a.get("contact") or launch["answers"].get("contact"),
                    "launch": launch, "converted_workspace_id": (lead.raw_payload or {}).get("converted_workspace_id"),
                    "demo_workspace_id": (row.demo_workspace_id if row else None) or (lead.raw_payload or {}).get("demo_workspace_id")})
    return out


class ConvertIn(BaseModel):
    company_name: str = Field(min_length=2, max_length=180)
    bot_token: str | None = Field(default=None, max_length=128)
    delete_demo: bool = True


@admin.post("/{lead_id}/convert")
async def convert(lead_id: int, payload: ConvertIn, request: Request, db: AsyncSession = Depends(get_db)):
    """Новый чистый кабинет без пользователей: доступы агентство выдаёт само в карточке клиента."""
    from app.api.routes.crm import activity
    from app.models.crm import CrmDeal, CrmInbound
    from app.services import workspace_archive
    check_origin(request)
    lead = await db.get(CrmInbound, lead_id)
    if not lead or lead.workspace_id != settings.agency_workspace_id or not (lead.raw_payload or {}).get("launch_request"):
        raise HTTPException(404, "Заявка не найдена")
    if (lead.raw_payload or {}).get("converted_workspace_id"):
        raise HTTPException(409, "Кабинет по этой заявке уже создан")
    name = payload.company_name.strip()
    if await db.scalar(select(ClientWorkspace.id).where(func.lower(ClientWorkspace.name) == name.lower())):
        raise HTTPException(409, "Клиент с таким названием уже существует")
    row = await _express_for(db, lead)
    express, launch = (row.answers if row else {}) or {}, lead.raw_payload["launch_request"].get("answers") or {}
    workspace = ClientWorkspace(name=name)
    db.add(workspace); await db.flush()
    project = await default_project(db, workspace.id)
    project.name = (express.get("what") or (launch.get("directions") or "").split("\n")[0] or name)[:180]
    if launch.get("site"):
        project.website = launch["site"][:500]
    answers = brief_answers(express, launch)
    db.add(ClientBrief(workspace_id=workspace.id, project_id=project.id, lead_id=lead.id, answers=answers, source="demo", status="draft"))
    knowledge = brief_service.fallback_knowledge(answers)
    state = dict(project.portal_state or {})
    state["ai"] = {**dict(state.get("ai") or {}), **{k: knowledge[k] for k in ("knowledge", "tone", "goal")}}
    if express:
        state["baseline"] = brief_service.baseline_from_answers(express, express=True)
    project.portal_state = state
    await db.flush()
    funnels = brief_funnels.parse(answers.get(brief_funnels.KEY))
    if funnels:
        from app.api.routes.crm import pipeline_for
        await pipeline_for(db, project.id, commit=False)
        await brief_funnels.plan(db, project, funnels, apply=True)
    demo_id = (row.demo_workspace_id if row else None) or (lead.raw_payload or {}).get("demo_workspace_id")
    demo = await db.get(ClientWorkspace, int(demo_id)) if demo_id else None
    moved = 0
    if demo and demo.plan == "trial" and demo.status != "deleted":
        # Кабинеты, подключённые в живом демо, переезжают в настоящий кабинет — переподключать не нужно.
        from app.models.marketing import AdConnection, ProjectSource
        for connection in (await db.scalars(select(AdConnection).where(AdConnection.workspace_id == demo.id))).all():
            connection.workspace_id, connection.project_id = workspace.id, project.id
            moved += 1
        demo_projects = [p.id for p in (await db.scalars(select(Project).where(Project.workspace_id == demo.id))).all()]
        for source in (await db.scalars(select(ProjectSource).where(ProjectSource.project_id.in_(demo_projects),
                                                                   ProjectSource.connection_id.is_not(None)))).all() if demo_projects else []:
            source.project_id = project.id
    if payload.delete_demo and demo and demo.status != "deleted":
        await workspace_archive.delete_workspace(db, demo)
    lead.raw_payload = {**(lead.raw_payload or {}), "converted_workspace_id": workspace.id}
    deal = await db.get(CrmDeal, lead.deal_id) if lead.deal_id else None
    if deal:
        activity(db, deal, None, "COMMENT_ADDED", {"text": f"Создан кабинет клиента «{name}». Доступы выдаются в карточке клиента."})
    await db.commit()
    bot = None
    if payload.bot_token and payload.bot_token.strip():
        from app.services import client_bots
        try:
            bot = (await client_bots.connect(db, workspace.id, payload.bot_token, name)).username
        except client_bots.BotTokenError as exc:
            bot = f"не подключён: {exc}"
    return {"workspace_id": workspace.id, "project_id": project.id, "pipelines": len(funnels) or 1, "ad_connections": moved,
            "brief_answers": len(answers), "demo_deleted": bool(payload.delete_demo and demo), "bot": bot}


router.include_router(admin)
