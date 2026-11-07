"""Действие «Лид в CRM клиента»: компания, которая нанимает, -> сделка в воронке «Радар» проекта клиента.

Как передача лидов из аутрича: воронка и источник создаются сами, задача «Связаться» на ответственного,
уведомление — как о новой заявке. Новые вакансии той же компании дописываются в её открытую сделку.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.jobradar.models import JrFilter, JrMatch
from app.domains.jobradar.sources import Vacancy, norm
from app.models.crm import CrmActivity, CrmContact, CrmDeal, CrmPipeline, CrmStage, CrmTask
from app.models.marketing import Project, ProjectSource

logger = logging.getLogger("uvicorn.error.jobradar")
PIPELINE = "Радар"
STAGES = (("Новый сигнал", "LEAD", "#8555E8"), ("Связались", "QUALIFIED", "#E8590C"),
          ("Интерес", "QUALIFIED", "#A855F7"), ("Клиент", "WON", "#15A86B"), ("Отказ", "LOST", "#EF4A59"))
SOURCE = "Радар вакансий"
ACTOR = "Радар StepToLead"


def company_key(v: Vacancy) -> str:
    inn = (v.company_inn or (v.employer or {}).get("inn") or "").strip()
    if inn:
        return f"inn:{inn}"
    name = re.sub(r"[^a-zа-я0-9]", "", re.sub(r"\b(ооо|ао|пао|зао|ип|оао)\b", " ", norm(v.company)))
    return f"name:{name}"[:200] if name else ""


async def ensure_pipeline(db: AsyncSession, project: Project) -> tuple[CrmPipeline, ProjectSource]:
    pipeline = await db.scalar(select(CrmPipeline).where(CrmPipeline.project_id == project.id,
                                                         CrmPipeline.name == PIPELINE,
                                                         CrmPipeline.archived_at.is_(None)).limit(1))
    if pipeline is None:
        pipeline = CrmPipeline(workspace_id=project.workspace_id, project_id=project.id, name=PIPELINE, is_default=False)
        db.add(pipeline)
        await db.flush()
        for position, (name, kind, color) in enumerate(STAGES):
            db.add(CrmStage(pipeline_id=pipeline.id, name=name, analytics_type=kind, color=color, position=position,
                            required_fields=[]))
        await db.flush()
    source = await db.scalar(select(ProjectSource).where(ProjectSource.project_id == project.id,
                                                         ProjectSource.name == SOURCE).limit(1))
    if source is None:
        source = ProjectSource(project_id=project.id, name=SOURCE, kind="INTEGRATION", method="API", category="Радар",
                               data_mode="API", status="active")
        db.add(source)
        await db.flush()
    return pipeline, source


async def find_contacts(db: AsyncSession, cards: list[Vacancy]) -> dict:
    """Телефон и почта компании: сначала из самих вакансий, потом из базы компаний агентства (по ИНН).
    Только общие контакты компании — личные (ФИО сотрудника) не передаём."""
    phones: list[str] = []
    emails: list[str] = []
    for v in cards:
        e = v.employer or {}
        phones += [x for x in e.get("phones") or [] if x]
        emails += [x for x in e.get("emails") or [] if x]
    inn = next((v.company_inn or (v.employer or {}).get("inn") for v in cards
                if v.company_inn or (v.employer or {}).get("inn")), None)
    if inn and not (phones or emails):
        from app.core.config import settings
        from app.domains.leadgen.models import LgCompany, LgContact
        company_id = await db.scalar(select(LgCompany.id).where(LgCompany.workspace_id == settings.agency_workspace_id,
                                                                LgCompany.inn == inn).limit(1))
        if company_id:
            rows = (await db.execute(select(LgContact.kind, LgContact.value).where(
                LgContact.company_id == company_id, LgContact.is_personal.is_(False), LgContact.bounced.is_(False),
                LgContact.verify_status != "invalid", LgContact.kind.in_(("phone", "email")))
                .order_by(LgContact.is_primary.desc(), LgContact.id))).all()
            phones += [v for k, v in rows if k == "phone"]
            emails += [v for k, v in rows if k == "email"]
    return {"phones": list(dict.fromkeys(phones))[:3], "emails": list(dict.fromkeys(emails))[:3]}


def _vacancy_text(v: Vacancy, query_name: str | None, score: int | None, why: str) -> str:
    salary = ""
    if v.salary_from or v.salary_to:
        salary = f"{v.salary_from or ''}–{v.salary_to or ''} ₽".strip("–")
    lines = [f"Вакансия: {v.title}", f"Компания: {v.company or '—'}"]
    lines += [x for x in (f"Город: {v.city}" if v.city else "", "Удалённо" if v.remote else "",
                          f"Зарплата: {salary}" if salary else "", f"Ссылка: {v.url}" if v.url else "",
                          f"Запрос сканера: {query_name}" if query_name else "",
                          f"ИИ: {score}/100 — {why}" if score is not None else "") if x]
    return "\n".join(lines)


async def push(db: AsyncSession, scanner: JrFilter, cards: list[Vacancy], *, responsible_user_id: int | None,
               query_name: str | None, score: int | None, why: str, now: datetime) -> tuple[int | None, str]:
    """(id сделки, added | appended | skipped | no_contact). Сделку не дублируем: та же компания — в её открытую сделку.
    Новую сделку CRM создаёт только с телефоном или почтой: нет их ни в вакансии, ни в базе — no_contact."""
    main = cards[0]
    if not scanner.project_id or not (main.company or "").strip():
        return None, "skipped"
    project = await db.get(Project, scanner.project_id)
    if project is None:
        return None, "skipped"
    key = company_key(main)
    text = _vacancy_text(main, query_name, score, why)
    if key:
        previous = await db.scalar(select(JrMatch.deal_id).where(
            JrMatch.filter_id == scanner.id, JrMatch.company_key == key, JrMatch.deal_id.is_not(None))
            .order_by(JrMatch.id.desc()).limit(1))
        deal = await db.get(CrmDeal, previous) if previous else None
        if deal is not None and deal.closed_at is None and deal.archived_at is None:
            db.add(CrmActivity(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id,
                               actor_name=ACTOR, event_type="COMMENT_ADDED",
                               payload={"text": "Ещё одна вакансия этой компании:\n" + text}))
            deal.last_activity_at = now
            await db.flush()
            return deal.id, "appended"

    found = await find_contacts(db, cards)
    if not (found["phones"] or found["emails"]):
        return None, "no_contact"
    from app.api.routes.crm import create_deal_fact, norm_phone
    from app.domains.leadgen import client_outreach
    pipeline, source = await ensure_pipeline(db, project)
    stage = await db.scalar(select(CrmStage).where(CrmStage.pipeline_id == pipeline.id, CrmStage.position == 0))
    owner = responsible_user_id
    if owner:
        from app.models.marketing import PortalUser
        user = await db.get(PortalUser, owner)
        owner = user.id if user and user.workspace_id == project.workspace_id else None
    if not owner:
        from app.models.marketing import PortalUser
        owner = await db.scalar(select(PortalUser.id).where(
            PortalUser.workspace_id == project.workspace_id, PortalUser.role == "client_owner",
            PortalUser.is_agency.is_(False)).order_by(PortalUser.id).limit(1))
    inn = main.company_inn or (main.employer or {}).get("inn")
    contact = CrmContact(workspace_id=project.workspace_id, project_id=project.id, name=main.company[:200],
                         phones=list(dict.fromkeys(norm_phone(x) or x for x in found["phones"])),
                         emails=[x.lower() for x in found["emails"]], company=main.company[:200], tags=["радар"], custom_fields={},
                         notes="\n".join(x for x in (f"ИНН {inn}" if inn else "",
                                                     (main.employer or {}).get("url") or "") if x) or None)
    db.add(contact)
    await db.flush()
    deal = await create_deal_fact(db, project, contact, pipeline, stage, f"{main.company} — нанимает: {main.title}"[:200],
                                  owner, None, source.id, "RADAR", {}, actor=None)
    deal.tags = ["радар"]
    db.add(CrmActivity(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id, actor_name=ACTOR,
                       event_type="COMMENT_ADDED",
                       payload={"text": f"Сигнал радара «{scanner.name}»: компания ищет сотрудника.\n" + text}))
    due = client_outreach.due_at(now, (project.portal_state or {}).get("work_schedule"), False)
    db.add(CrmTask(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id, contact_id=contact.id,
                   type_code="CALL", title=f"Связаться: {main.company} — нанимает «{main.title}»"[:220],
                   description=text[:2000], responsible_user_id=owner, due_at=due, priority="NORMAL", status="OPEN"))
    await db.flush()
    try:
        from app.services import notifications
        await notifications.notify(db, project.id, "new_lead", f"Радар: {main.company} нанимает",
                                   f"{main.title}" + (f" · {main.city}" if main.city else ""), assignee_id=owner)
    except Exception:  # noqa: BLE001 — уведомление не должно ломать передачу
        logger.info("jobradar crm notify failed")
    return deal.id, "added"
