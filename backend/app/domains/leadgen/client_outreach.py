"""Аутрич как услуга: ответивший лид кампании клиента -> CRM клиента (воронка «Аутрич»), задача, уведомление.

Решения (29.10.2026): «Интересно» и горячие уходят клиенту сами, остальное — кнопкой «Передать клиенту»;
отказы, отписки и автоответы не передаём. В сделке — компания, контакт и вся переписка; задача «Связаться»
на ответственного из кампании (иначе — собственник клиента), горячим — 15 минут рабочего времени клиента.
Дальше лида ведёт менеджер клиента: агентство отвечает лиду с копией на его почту (вся ветка в цитате).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.leadgen import service
from app.domains.leadgen.models import LgAd, LgCampaign, LgCompany, LgContact, LgEnrollment, LgTouch
from app.models.crm import CrmActivity, CrmContact, CrmDeal, CrmPipeline, CrmStage, CrmTask
from app.models.marketing import PortalUser, Project, ProjectSource

PIPELINE_NAME = "Аутрич"
STAGES = (
    ("Ответил", "LEAD", "#8555E8"),
    ("Интерес", "QUALIFIED", "#E8590C"),
    ("Созвон назначен", "QUALIFIED", "#A855F7"),
    ("КП / встреча", "QUALIFIED", "#F59E0B"),
    ("Клиент", "WON", "#15A86B"),
    ("Отказ", "LOST", "#EF4A59"),
)
SOURCE_NAME = "Аутрич (email)"
HOT_MINUTES = 15
NEVER = ("not_interested", "unsubscribe", "auto")
TAG = "аутрич"


@dataclass
class Handed:
    ok: bool
    reason: str | None = None
    deal_id: int | None = None
    created: bool = False


# ---------------------------------------------------------------- воронка и источник у клиента

async def ensure_pipeline(db: AsyncSession, project: Project) -> tuple[CrmPipeline, ProjectSource]:
    pipeline = await db.scalar(select(CrmPipeline).where(CrmPipeline.project_id == project.id,
                                                         CrmPipeline.name == PIPELINE_NAME,
                                                         CrmPipeline.archived_at.is_(None)).limit(1))
    if pipeline is None:
        pipeline = CrmPipeline(workspace_id=project.workspace_id, project_id=project.id, name=PIPELINE_NAME,
                               is_default=False)
        db.add(pipeline)
        await db.flush()
        for position, (name, kind, color) in enumerate(STAGES):
            db.add(CrmStage(pipeline_id=pipeline.id, name=name, analytics_type=kind, color=color,
                            position=position, required_fields=[]))
        await db.flush()
    source = await db.scalar(select(ProjectSource).where(ProjectSource.project_id == project.id,
                                                         ProjectSource.name == SOURCE_NAME).limit(1))
    if source is None:
        source = ProjectSource(project_id=project.id, name=SOURCE_NAME, kind="INTEGRATION", method="API",
                               category="Аутрич", data_mode="API", status="active")
        db.add(source)
        await db.flush()
    return pipeline, source


async def _stage(db: AsyncSession, pipeline: CrmPipeline, name: str) -> CrmStage | None:
    return await db.scalar(select(CrmStage).where(CrmStage.pipeline_id == pipeline.id, CrmStage.name == name,
                                                  CrmStage.archived_at.is_(None)))


# ---------------------------------------------------------------- кто и когда

async def campaign_of(db: AsyncSession, touch: LgTouch) -> LgCampaign | None:
    if touch.campaign_kind not in ("sequence", "manual_reply") or not touch.campaign_id:
        return None
    return await db.scalar(select(LgCampaign).where(LgCampaign.sequence_id == touch.campaign_id))


async def responsible(db: AsyncSession, campaign: LgCampaign, project: Project) -> int | None:
    if campaign.responsible_user_id:
        user = await db.get(PortalUser, campaign.responsible_user_id)
        if user and user.workspace_id == project.workspace_id:
            return user.id
    owner = await db.scalar(select(PortalUser.id).where(
        PortalUser.workspace_id == project.workspace_id, PortalUser.role == "client_owner",
        PortalUser.is_agency.is_(False)).order_by(PortalUser.id).limit(1))
    return owner


def due_at(now: datetime, schedule: dict | None, hot: bool) -> datetime:
    """Горячий — через 15 минут рабочего времени клиента, остальные — до конца его рабочего дня."""
    sched = schedule or {"timezone": "Europe/Moscow", "days": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"}
    try:
        zone = ZoneInfo(sched.get("timezone") or "Europe/Moscow")
        opening, closing = time.fromisoformat(sched["start"]), time.fromisoformat(sched["end"])
        days = set(sched.get("days") or [1, 2, 3, 4, 5])
    except (KeyError, ValueError):
        return now + (timedelta(minutes=HOT_MINUTES) if hot else timedelta(hours=8))
    local = service._aware(now).astimezone(zone)
    for _ in range(14):   # ближайший рабочий момент
        start = datetime.combine(local.date(), opening, zone)
        end = datetime.combine(local.date(), closing, zone)
        if local.isoweekday() in days and local < end:
            moment = max(local, start)
            if not hot:
                return end
            due = moment + timedelta(minutes=HOT_MINUTES)
            return due if due <= end else datetime.combine(local.date(), closing, zone)
        local = datetime.combine(local.date() + timedelta(days=1), opening, zone)
    return now + timedelta(minutes=HOT_MINUTES)


# ---------------------------------------------------------------- передача

async def _thread_text(db: AsyncSession, enr: LgEnrollment, company: LgCompany) -> str:
    from app.domains.leadgen.inbox import clean_body
    from app.domains.leadgen.sequences import seq_key
    rows = (await db.scalars(select(LgTouch).where(
        LgTouch.company_id == company.id, LgTouch.sequence_id == seq_key(enr), LgTouch.channel == "email")
        .order_by(LgTouch.happened_at, LgTouch.id).limit(30))).all()
    parts = []
    for t in rows:
        if t.direction == "out" and t.status not in ("sent", "delivered", "replied"):
            continue
        when = service._aware(t.happened_at).astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m %H:%M") if t.happened_at else ""
        who = "Лид" if t.direction == "in" else "Мы"
        parts.append(f"{who} · {when}{f' · {t.subject}' if t.subject else ''}\n{clean_body(t.body)[:1500]}")
    return "\n\n".join(parts)


async def hand_to_client(db: AsyncSession, touch: LgTouch, company: LgCompany, *, manual: bool = False,
                         now: datetime | None = None) -> Handed:
    """Сделка + задача в CRM клиента. Повторный вызов не дублирует (вернёт уже созданную сделку)."""
    now = now or service.utcnow()
    campaign = await campaign_of(db, touch)
    if campaign is None or not campaign.client_project_id:
        return Handed(False, "not_client_campaign")
    if not manual and touch.label in NEVER:
        return Handed(False, "not_interested")
    enr = await enrollment_of(db, touch, company, campaign)
    if enr is None:
        return Handed(False, "no_enrollment")
    project = await db.get(Project, campaign.client_project_id)
    if project is None:
        return Handed(False, "no_project")
    if enr.client_deal_id:
        deal = await db.get(CrmDeal, enr.client_deal_id)
        if deal is not None:
            return Handed(True, "already", deal.id, False)

    pipeline, source = await ensure_pipeline(db, project)
    hot = bool(touch.hot_at) or touch.label == "interested"
    stage = await _stage(db, pipeline, "Интерес" if hot else "Ответил")
    owner = await responsible(db, campaign, project)
    name = (enr.vars or {}).get("company") or company.display_name or company.legal_name or company.domain or "Компания"

    lead_email = (touch.address or "").lower() or None
    contacts = (await db.scalars(select(LgContact).where(
        LgContact.company_id == company.id, LgContact.bounced.is_(False), LgContact.verify_status != "invalid"))).all()
    emails = list(dict.fromkeys([e for e in [lead_email] if e] + [c.value_norm for c in contacts if c.kind == "email"]))[:5]
    from app.api.routes.crm import create_deal_fact, norm_phone
    phones = list(dict.fromkeys(c.value_norm for c in contacts if c.kind in ("phone", "whatsapp")))[:5]
    digits = [d for d in (norm_phone(p) for p in phones) if d]
    conditions = ([CrmContact.email_normalized.in_(emails)] if emails else []) + \
                 ([CrmContact.phone_normalized.in_(digits)] if digits else [])
    contact = await db.scalar(select(CrmContact).where(CrmContact.project_id == project.id, or_(*conditions)).limit(1)) \
        if conditions else None
    person = (enr.vars or {}).get("director_first_name") or company.director_name
    if contact is None:
        contact = CrmContact(workspace_id=project.workspace_id, project_id=project.id,
                             name=(f"{person} · {name}" if person else name)[:200], phones=phones, emails=emails,
                             phone_normalized=digits[0] if digits else None,
                             email_normalized=emails[0] if emails else None,
                             company=company.legal_name or name, position=None,
                             notes=f"https://{company.domain}" if company.domain else None, tags=[TAG], custom_fields={})
        db.add(contact)
        await db.flush()
    deal = await create_deal_fact(db, project, contact, pipeline, stage, f"{name} — аутрич", owner, None, source.id,
                                  "OUTREACH", {}, actor=None)
    deal.tags = [TAG]
    ad = await db.scalar(select(LgAd).where(LgAd.company_id == company.id).order_by(LgAd.last_seen_at.desc()).limit(1))
    info = [f"Лид из аутрича · кампания «{campaign.name}»"]
    if company.domain:
        info.append(f"Сайт: https://{company.domain}")
    if touch.summary:
        info.append(f"Суть ответа: {touch.summary}")
    if touch.intent_score is not None:
        info.append(f"Интерес: {touch.intent_score}/100")
    if ad and ad.title:
        info.append(f"Их реклама: «{ad.title}»")
    if campaign.handoff_cc:
        info.append(f"Переписку передаём на {campaign.handoff_cc} — продолжайте с этого адреса.")
    db.add(CrmActivity(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id,
                       actor_name="Аутрич StepToLead", event_type="COMMENT_ADDED", payload={"text": "\n".join(info)}))
    db.add(CrmActivity(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id,
                       actor_name="Аутрич StepToLead", event_type="COMMENT_ADDED",
                       payload={"text": "Переписка:\n\n" + await _thread_text(db, enr, company)}))
    due = due_at(now, (project.portal_state or {}).get("work_schedule"), bool(touch.hot_at))
    db.add(CrmTask(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id, contact_id=contact.id,
                   type_code="CALL", title=(f"🔥 Связаться: {name} — ответ на рассылку" if touch.hot_at
                                            else f"Связаться: {name} — ответ на рассылку")[:220],
                   description=(touch.summary or "") + f"\n\nОтвет {lead_email or ''}:\n{(touch.body or '')[:1500]}",
                   responsible_user_id=owner, due_at=due, priority="HIGH" if touch.hot_at else "NORMAL", status="OPEN"))
    enr.client_deal_id, enr.client_handed_at = deal.id, now
    await db.flush()
    from app.services import notifications
    try:
        await notifications.notify(db, project.id, "new_lead", f"Новый лид из рассылки: {name}",
                                   touch.summary or (touch.body or "")[:300], assignee_id=owner)
    except Exception:  # noqa: BLE001 — уведомление не должно ломать передачу
        pass
    return Handed(True, None, deal.id, True)


async def append_reply(db: AsyncSession, enr: LgEnrollment, touch: LgTouch) -> None:
    """Лид написал ещё раз после передачи — письмо в ленту сделки клиента."""
    if not enr.client_deal_id:
        return
    deal = await db.get(CrmDeal, enr.client_deal_id)
    if deal is None:
        return
    from app.domains.leadgen.inbox import clean_body
    db.add(CrmActivity(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id,
                       actor_name="Аутрич StepToLead", event_type="COMMENT_ADDED",
                       payload={"text": f"Новое письмо от лида ({touch.address}):\n{clean_body(touch.body)[:2000]}"}))
    deal.last_activity_at = service.utcnow()


async def enrollment_of(db: AsyncSession, touch: LgTouch, company: LgCompany,
                        campaign: LgCampaign | None = None) -> LgEnrollment | None:
    if touch.sequence_id and touch.sequence_id.startswith("enr") and touch.sequence_id[3:].isdigit():
        enr = await db.get(LgEnrollment, int(touch.sequence_id[3:]))
        if enr is not None:
            return enr
    campaign = campaign or await campaign_of(db, touch)
    if campaign is None:
        return None
    return await db.scalar(select(LgEnrollment).where(LgEnrollment.campaign_id == campaign.id,
                                                      LgEnrollment.company_id == company.id)
                           .order_by(LgEnrollment.id.desc()).limit(1))


async def close_deal(db: AsyncSession, enr: LgEnrollment, note: str, now: datetime | None = None) -> None:
    """Лид отказался/отписался после передачи — сделка клиента на «Отказ» (если её ещё не закрыли)."""
    if not enr.client_deal_id:
        return
    deal = await db.get(CrmDeal, enr.client_deal_id)
    if deal is None:
        return
    current = await db.get(CrmStage, deal.stage_id)
    lost = await db.scalar(select(CrmStage).where(CrmStage.pipeline_id == deal.pipeline_id,
                                                  CrmStage.analytics_type == "LOST", CrmStage.archived_at.is_(None))
                           .order_by(CrmStage.position).limit(1))
    if current is not None and current.analytics_type not in ("WON", "LOST") and lost is not None:
        deal.stage_id, deal.stage_entered_at = lost.id, now or service.utcnow()
    db.add(CrmActivity(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id,
                       actor_name="Аутрич StepToLead", event_type="COMMENT_ADDED", payload={"text": note}))
    deal.last_activity_at = now or service.utcnow()


async def after_reply(db: AsyncSession, touch: LgTouch, company: LgCompany, *, now: datetime | None = None,
                      append: bool = True) -> Handed | None:
    """Автопередача: «Интересно» и горячие — сразу клиенту. Уже переданным — новое письмо в ленту сделки
    (append=False — повторная проверка после ИИ, письмо уже дописано)."""
    campaign = await campaign_of(db, touch)
    if campaign is None or not campaign.client_project_id:
        return None
    enr = await enrollment_of(db, touch, company, campaign)
    if enr is not None and enr.client_deal_id:
        if append:
            await append_reply(db, enr, touch)
        return Handed(True, "already", enr.client_deal_id, False)
    if campaign.auto_handoff and touch.label not in NEVER and (touch.hot_at or touch.label == "interested"):
        return await hand_to_client(db, touch, company, now=now)
    return None


# ---------------------------------------------------------------- отчёт «Аутрич» для клиента и сводка агентства

async def report(db: AsyncSession, project: Project, start: datetime, end: datetime) -> dict:
    """Цифры рассылки для кабинета клиента: написали, ответили, передали, созвоны. Без настроек и адресов."""
    from sqlalchemy import func
    camps = (await db.scalars(select(LgCampaign).where(LgCampaign.client_project_id == project.id)
                              .order_by(LgCampaign.id))).all()
    if not camps:
        return {"enabled": False}
    pipeline = await db.scalar(select(CrmPipeline).where(CrmPipeline.project_id == project.id,
                                                         CrmPipeline.name == PIPELINE_NAME,
                                                         CrmPipeline.archived_at.is_(None)).limit(1))
    stages = (await db.scalars(select(CrmStage).where(CrmStage.pipeline_id == pipeline.id)
                               .order_by(CrmStage.position))).all() if pipeline else []
    call_from = next((s.position for s in stages if s.name == "Созвон назначен"), None)
    rows, totals = [], {"written": 0, "replied": 0, "handed": 0, "calls": 0, "won": 0}
    handed_all: list[int] = []
    for camp in camps:
        out = await db.scalar(select(func.count(func.distinct(LgTouch.company_id))).where(
            LgTouch.campaign_kind == "sequence", LgTouch.campaign_id == camp.sequence_id, LgTouch.direction == "out",
            LgTouch.channel == "email", LgTouch.status.in_(("sent", "delivered", "replied")),
            LgTouch.happened_at >= start, LgTouch.happened_at < end)) or 0
        replied = await db.scalar(select(func.count(func.distinct(LgTouch.company_id))).where(
            LgTouch.campaign_kind == "sequence", LgTouch.campaign_id == camp.sequence_id, LgTouch.direction == "in",
            LgTouch.status == "replied", LgTouch.happened_at >= start, LgTouch.happened_at < end)) or 0
        handed_rows = (await db.execute(select(LgEnrollment.client_deal_id).where(
            LgEnrollment.campaign_id == camp.id, LgEnrollment.client_deal_id.is_not(None),
            LgEnrollment.client_handed_at >= start, LgEnrollment.client_handed_at < end))).scalars().all()
        handed_all += list(handed_rows)
        calls = won = 0
        if handed_rows:
            for stage in (await db.execute(select(CrmStage).join(CrmDeal, CrmDeal.stage_id == CrmStage.id)
                                           .where(CrmDeal.id.in_(handed_rows)))).scalars().all():
                if stage.analytics_type == "WON":
                    won += 1
                    calls += 1
                elif call_from is not None and stage.pipeline_id == (pipeline.id if pipeline else None) \
                        and stage.position >= call_from and stage.analytics_type != "LOST":
                    calls += 1
        row = {"id": camp.id, "name": camp.name, "status": camp.status, "written": int(out), "replied": int(replied),
               "handed": len(handed_rows), "calls": calls, "won": won,
               "reply_rate": round(replied * 100 / out, 1) if out else None}
        rows.append(row)
        for key in totals:
            totals[key] += row[key]
    funnel = []
    if pipeline:   # где сейчас лиды, переданные за период
        counts = dict((await db.execute(select(CrmDeal.stage_id, func.count(CrmDeal.id)).where(
            CrmDeal.pipeline_id == pipeline.id, CrmDeal.id.in_(handed_all or [-1]))
            .group_by(CrmDeal.stage_id))).all())
        funnel = [{"stage_id": s.id, "name": s.name, "color": s.color, "count": int(counts.get(s.id, 0))} for s in stages]
    totals["reply_rate"] = round(totals["replied"] * 100 / totals["written"], 1) if totals["written"] else None
    return {"enabled": True, "pipeline_id": pipeline.id if pipeline else None, "totals": totals,
            "campaigns": rows, "funnel": funnel}
