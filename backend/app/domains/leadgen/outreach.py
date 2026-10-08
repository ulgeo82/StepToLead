"""«В аутрич»: компания из базы лидогенерации -> контакт и сделка в воронке «Аутрич» CRM агентства.

Сделки живут в отдельном проекте «Аутрич» аккаунта агентства: каждая сделка CRM считается лидом в аналитике,
и холодные компании не должны раздувать заявки и цену лида в основном проекте.
Обратная связь: этап сделки -> стадия компании (Ответил -> replied, Клиент -> converted, Отказ -> rejected + стоп-лист).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.crm import activity, create_deal_fact, norm_phone
from app.domains.leadgen import service
from app.domains.leadgen.models import LgAd, LgCompany, LgContact, LgDnc
from app.models.crm import CrmContact, CrmDeal, CrmPipeline, CrmStage
from app.models.marketing import Project
from app.models.system import AppSetting

PROJECT_NAME = "Аутрич"
PIPELINE_NAME = "Аутрич"
STAGES = (
    ("Новый", "LEAD", "#006BFD"),
    ("Написали", "LEAD", "#3B82F6"),
    ("Ответил", "QUALIFIED", "#8555E8"),
    ("Созвон назначен", "QUALIFIED", "#A855F7"),
    ("Тест портала", "QUALIFIED", "#F59E0B"),
    ("Клиент", "WON", "#15A86B"),
    ("Отказ", "LOST", "#EF4A59"),
)
REFUSAL_DNC_DAYS = 365
TAG = "лидогенерация"


async def ensure_pipeline(db: AsyncSession, workspace_id: int) -> tuple[Project, CrmPipeline]:
    """Проект и воронка «Аутрич» в аккаунте агентства. Создаются один раз, id запоминаются в настройках."""
    settings = await service.get_settings(db)
    project = await db.get(Project, settings.get("outreach_project_id") or 0)
    if project is None or project.workspace_id != workspace_id:
        project = await db.scalar(select(Project).where(Project.workspace_id == workspace_id,
                                                        Project.name == PROJECT_NAME).limit(1))
    if project is None:
        project = Project(workspace_id=workspace_id, name=PROJECT_NAME, is_default=False, status="active",
                          description="Холодный аутрич: компании из лидогенерации", portal_state={})
        db.add(project)
        await db.flush()
    pipeline = await db.get(CrmPipeline, settings.get("outreach_pipeline_id") or 0)
    if pipeline is None or pipeline.project_id != project.id or pipeline.archived_at is not None:
        pipeline = await db.scalar(select(CrmPipeline).where(CrmPipeline.project_id == project.id,
                                                             CrmPipeline.archived_at.is_(None))
                                   .order_by(CrmPipeline.is_default.desc(), CrmPipeline.id).limit(1))
    if pipeline is None:
        pipeline = CrmPipeline(workspace_id=workspace_id, project_id=project.id, name=PIPELINE_NAME, is_default=True)
        db.add(pipeline)
        await db.flush()
        for position, (name, kind, color) in enumerate(STAGES):
            db.add(CrmStage(pipeline_id=pipeline.id, name=name, analytics_type=kind, color=color,
                            position=position, required_fields=[]))
        await db.flush()
    if (settings.get("outreach_project_id"), settings.get("outreach_pipeline_id")) != (project.id, pipeline.id):
        row = await db.get(AppSetting, service.SETTINGS_KEY)
        value = {**settings, "outreach_project_id": project.id, "outreach_pipeline_id": pipeline.id}
        if row is None:
            db.add(AppSetting(key=service.SETTINGS_KEY, value=value))
        else:
            row.value = value
        await db.flush()
    return project, pipeline


@dataclass
class Handoff:
    company_id: int
    ok: bool
    reason: str | None = None
    deal_id: int | None = None


def _context_note(company: LgCompany, ad: LgAd | None, contacts: list[LgContact]) -> str:
    lines = [f"Из лидогенерации · скоринг {company.score}/10"]
    if company.domain:
        lines.append(f"Сайт: https://{company.domain}")
    if company.legal_name or company.inn:
        lines.append(f"Юрлицо: {company.legal_name or '—'}{f', ИНН {company.inn}' if company.inn else ''}")
    if company.director_name:
        lines.append(f"Руководитель: {company.director_name}")
    reasons = [r["label"] for r in company.score_reasons or [] if r.get("weight")]
    if reasons:
        lines.append("Почему горячая: " + "; ".join(reasons))
    if ad:
        lines.append(f"Что крутят: «{ad.title or ''}» — {ad.text or ''}".strip())
        if ad.keywords:
            lines.append("По ключам: " + ", ".join(ad.keywords[:5]))
    channels = [f"{c.kind}: {c.value} (источник: {c.source or '—'})" for c in contacts if c.kind in ("whatsapp", "telegram")]
    if channels:
        lines.append("Мессенджеры: " + "; ".join(channels))
    return "\n".join(lines)


async def hand_off(db: AsyncSession, company: LgCompany, *, now: datetime | None = None) -> Handoff:
    """Одна компания -> сделка. Не коммитит. Проверка стоп-листа и повторного контакта — через can_contact."""
    now = now or service.utcnow()
    if company.crm_deal_id:
        return Handoff(company.id, False, "already_in_crm", company.crm_deal_id)
    decision = await service.check_can_contact(db, company.id, None, "call", now=now)
    if not decision.ok:
        return Handoff(company.id, False, decision.reason)

    contacts = (await db.execute(select(LgContact).where(
        LgContact.company_id == company.id, LgContact.is_personal.is_(False), LgContact.bounced.is_(False),
        LgContact.verify_status != "invalid").order_by(LgContact.is_primary.desc(), LgContact.id))).scalars().all()
    phones = list(dict.fromkeys(c.value_norm for c in contacts if c.kind in ("phone", "whatsapp")))
    emails = list(dict.fromkeys(c.value_norm for c in contacts if c.kind == "email"))
    telegram = next((c.value_norm for c in contacts if c.kind == "telegram"), None)
    if not (phones or emails or telegram):
        return Handoff(company.id, False, "no_contacts")

    project, pipeline = await ensure_pipeline(db, company.workspace_id)
    first_stage = await db.scalar(select(CrmStage).where(CrmStage.pipeline_id == pipeline.id,
                                                         CrmStage.analytics_type == "LEAD",
                                                         CrmStage.archived_at.is_(None)).order_by(CrmStage.position).limit(1))
    name = company.display_name or company.legal_name or company.domain or f"Компания #{company.id}"

    contact = None
    digits = [norm_phone(p) for p in phones if norm_phone(p)]
    if digits or emails:
        conditions = []
        if digits:
            conditions.append(CrmContact.phone_normalized.in_(digits))
        if emails:
            conditions.append(CrmContact.email_normalized.in_(emails))
        contact = await db.scalar(select(CrmContact).where(CrmContact.project_id == project.id, or_(*conditions)).limit(1))
    if contact is None:
        contact = CrmContact(workspace_id=project.workspace_id, project_id=project.id, name=name,
                             phones=phones, emails=emails, telegram=telegram,
                             phone_normalized=digits[0] if digits else None,
                             email_normalized=emails[0] if emails else None,
                             company=company.legal_name or company.display_name,
                             position=None, notes=f"https://{company.domain}" if company.domain else None,
                             tags=[TAG], custom_fields={})
        db.add(contact)
        await db.flush()

    deal = await create_deal_fact(db, project, contact, pipeline, first_stage, f"{name} — аутрич", None, None,
                                  None, "API", {}, actor=None)
    deal.tags = [TAG]
    ad = await db.scalar(select(LgAd).where(LgAd.company_id == company.id).order_by(LgAd.last_seen_at.desc()).limit(1))
    activity(db, deal, None, "COMMENT_ADDED", {"text": _context_note(company, ad, list(contacts))}, touch=False)
    company.crm_contact_id, company.crm_deal_id, company.stage = contact.id, deal.id, "in_outreach"
    await db.flush()
    return Handoff(company.id, True, None, deal.id)


async def sync_from_crm(db: AsyncSession, workspace_id: int, *, now: datetime | None = None) -> int:
    """Подтягивает этап сделки обратно в стадию компании. Возвращает число изменённых компаний."""
    now = now or service.utcnow()
    rows = (await db.execute(
        select(LgCompany, CrmStage.analytics_type, CrmDeal.archived_at)
        .join(CrmDeal, CrmDeal.id == LgCompany.crm_deal_id)
        .join(CrmStage, CrmStage.id == CrmDeal.stage_id)
        .where(LgCompany.workspace_id == workspace_id, LgCompany.stage.in_(("in_outreach", "replied")))
    )).all()
    changed, blocked = 0, []
    for company, kind, archived in rows:
        new_stage, dnc_reason, until = None, None, None
        if kind == "WON":
            new_stage, dnc_reason = "converted", "client"
        elif kind == "LOST" or archived is not None:
            new_stage, dnc_reason, until = "rejected", "refused", now + timedelta(days=REFUSAL_DNC_DAYS)
        elif kind == "QUALIFIED" and company.stage != "replied":
            new_stage = "replied"
        if not new_stage:
            continue
        company.stage = new_stage
        if dnc_reason:
            exists = await db.scalar(select(LgDnc).where(LgDnc.workspace_id == workspace_id, LgDnc.kind == "company",
                                                         LgDnc.value_norm == str(company.id)))
            if exists is None:
                db.add(LgDnc(workspace_id=workspace_id, kind="company", value_norm=str(company.id),
                             reason=dnc_reason, until=until, note="Автоматически по итогу сделки «Аутрич»"))
            blocked.append(company)
        changed += 1
    if changed:
        await db.flush()
        for company in blocked:
            await service.recalc_score(db, company, now=now)
    return changed
