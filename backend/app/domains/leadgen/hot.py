"""Триггер «Явный интерес» во Входящих аутрича (только почта).

Горячий ответ — когда ИИ дал силу интереса не ниже порога (по умолчанию 70) или в тексте явные признаки:
телефон, просьба созвониться, вопрос о цене/тесте. Тогда сразу, без клика человека:
  • сообщение в Telegram администраторам агентства;
  • задача в CRM «Связаться за 15 минут» (срок считается в рабочих часах цепочки);
  • сделка → этап «Интерес» воронки аутрича;
  • ответ помечается 🔥 и встаёт первым во вкладке «Горячие».
Если за срок ответ не разобран — одно повторное напоминание. Если человек поставил другую метку — задача
закрывается, сделка возвращается на «Ответил».
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings as app_settings
from app.domains.leadgen import service
from app.domains.leadgen.core import sequence as core
from app.domains.leadgen.models import LgCompany, LgSequence, LgTouch
from app.models.crm import CrmActivity, CrmDeal, CrmStage, CrmTask

logger = logging.getLogger("uvicorn.error.leadgen.hot")
STAGE_INTEREST, STAGE_REPLIED = "Интерес", "Ответил"
DEFAULTS = {"threshold": 70, "sla_minutes": 15, "notify": True, "remind": True}
SIGNAL_NAMES = {"phone": "оставил телефон", "call": "предлагает созвон", "price": "спрашивает цену",
                "trial": "интересуется тестом", "meeting": "называет время", "details": "просит подробности"}
_PHONE = re.compile(r"(?:\+7|\b8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}\b")
_RULES = {
    "call": re.compile(r"созвон|позвоните|перезвоните|наберите|звоните|давайте\s+(обсудим|встретимся|созвонимся)", re.I),
    "price": re.compile(r"сколько\s+стоит|какая\s+(цена|стоимость)|стоимост|прайс|ценник", re.I),
    "trial": re.compile(r"тест\w*\s+(портал|период|доступ)|попробовать|пробн|демо", re.I),
    "meeting": re.compile(r"\b(завтра|сегодня|в\s+понедельник|во\s+вторник|в\s+среду|в\s+четверг|в\s+пятницу)\b.*\d{1,2}[:.]?\d{0,2}", re.I),
}
_NEGATIVE = re.compile(r"не\s+(интересн|актуальн|нужн|звоните|пишите)|неинтересн|неактуальн|отпиш", re.I)


async def config(db: AsyncSession) -> dict:
    value = (await service.get_settings(db)).get("hot") or {}
    out = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in value and value[key] is not None:
            out[key] = value[key]
    out["threshold"] = max(1, min(100, int(out["threshold"])))
    out["sla_minutes"] = max(5, min(24 * 60, int(out["sla_minutes"])))
    out["notify"], out["remind"] = bool(out["notify"]), bool(out["remind"])
    return out


def rule_signals(text: str) -> list[str]:
    """Явные признаки интереса без ИИ. Отказ («не звоните») признаками не считается."""
    t = text or ""
    if _NEGATIVE.search(t):
        return []
    found = [name for name, rx in _RULES.items() if rx.search(t)]
    if _PHONE.search(t):
        found.insert(0, "phone")
    return found


def is_hot(score: int | None, signals: list[str], threshold: int) -> bool:
    strong = {"phone", "call", "meeting"}
    return (score is not None and score >= threshold) or bool(strong & set(signals)) or len(signals) >= 2


async def _stage(db: AsyncSession, pipeline_id: int, name: str) -> CrmStage | None:
    return await db.scalar(select(CrmStage).where(CrmStage.pipeline_id == pipeline_id, CrmStage.name == name,
                                                  CrmStage.archived_at.is_(None)))


async def ensure_interest_stage(db: AsyncSession, pipeline_id: int) -> CrmStage | None:
    """Этап «Интерес» сразу после «Ответил» — для воронок аутрича, созданных до триггера."""
    found = await _stage(db, pipeline_id, STAGE_INTEREST)
    if found:
        return found
    replied = await _stage(db, pipeline_id, STAGE_REPLIED)
    if replied is None:
        return None
    later = (await db.scalars(select(CrmStage).where(CrmStage.pipeline_id == pipeline_id,
                                                     CrmStage.position > replied.position))).all()
    for stage in later:
        stage.position += 1
    row = CrmStage(pipeline_id=pipeline_id, name=STAGE_INTEREST, analytics_type="QUALIFIED", color="#E8590C",
                   position=replied.position + 1, required_fields=[])
    db.add(row)
    await db.flush()
    return row


async def _deal(db: AsyncSession, company: LgCompany) -> CrmDeal | None:
    return await db.get(CrmDeal, company.crm_deal_id) if company.crm_deal_id else None


async def _window(db: AsyncSession, touch: LgTouch) -> core.Window:
    seq = await db.get(LgSequence, touch.campaign_id) if touch.campaign_kind == "sequence" and touch.campaign_id else None
    try:
        return core.parse_window(seq.window if seq else None)
    except core.SequenceError:
        return core.parse_window(None)


async def due_at(db: AsyncSession, touch: LgTouch, now: datetime, minutes: int) -> datetime:
    """Срок «связаться» в рабочих часах: ночью ответ ждёт утра, а не горит."""
    start = core.in_window(now, await _window(db, touch))
    return start + timedelta(minutes=minutes)


def _link(touch: LgTouch) -> str:
    return f"{app_settings.frontend_origin.rstrip('/')}/admin/outreach/inbox?touch={touch.id}"


async def _telegram(db: AsyncSession, text: str) -> None:
    from app.services import monitor
    try:
        await monitor.send(db, text)
    except Exception:  # noqa: BLE001 — уведомление не должно ломать разбор почты
        logger.exception("hot lead telegram failed")


async def mark(db: AsyncSession, touch: LgTouch, company: LgCompany, *, score: int | None, signals: list[str],
               now: datetime | None = None) -> bool:
    """Записывает силу интереса; если ответ горячий и ещё не был — запускает автодействия. True — стал горячим."""
    now = now or service.utcnow()
    cfg = await config(db)
    touch.intent_score = score if score is not None else touch.intent_score
    touch.intent_signals = sorted(set((touch.intent_signals or []) + signals))
    if touch.hot_at or touch.label_source == "user" or touch.label in ("not_interested", "unsubscribe", "auto"):
        await db.flush()
        return False
    if not is_hot(touch.intent_score, touch.intent_signals, cfg["threshold"]):
        await db.flush()
        return False
    touch.hot_at = now
    name = company.display_name or company.domain or "компания"
    due = await due_at(db, touch, now, cfg["sla_minutes"])
    from app.domains.leadgen import client_outreach
    client_campaign = await client_outreach.campaign_of(db, touch)
    # Рассылка клиента: задачу и сделку создаёт передача клиенту, сделку агентства не трогаем.
    deal = None if client_campaign is not None and client_campaign.client_project_id else await _deal(db, company)
    if deal is not None:
        task = CrmTask(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id,
                       contact_id=deal.contact_id, type_code="CALL", title=f"🔥 Связаться: {name} — горячий ответ"[:220],
                       description=f"{touch.summary or ''}\n\nОтвет от {touch.address or 'компании'}:\n{(touch.body or '')[:1500]}".strip(),
                       due_at=due, priority="HIGH", status="OPEN")
        db.add(task)
        await db.flush()
        touch.hot_task_id = task.id
        stage = await ensure_interest_stage(db, deal.pipeline_id)
        current = await db.get(CrmStage, deal.stage_id)
        if stage and current and current.analytics_type not in ("WON", "LOST") and current.position < stage.position:
            deal.stage_id, deal.stage_entered_at = stage.id, now
        db.add(CrmActivity(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id,
                           actor_name="Аутрич", event_type="COMMENT_ADDED",
                           payload={"text": f"🔥 Горячий ответ ({_signals_text(touch)}). Связаться до {_local(due, touch)}."}))
        deal.last_activity_at = now
    await db.flush()
    if cfg["notify"]:
        await _telegram(db, "\n".join(filter(None, [
            f"🔥 <b>Горячий ответ на аутрич</b> · {html.escape(name)}",
            html.escape(touch.summary or (touch.body or "")[:200]),
            f"Признаки: {html.escape(_signals_text(touch))}" if touch.intent_signals or touch.intent_score else None,
            f"Связаться до {_local(due, touch)}", _link(touch)])))
    return True


def _signals_text(touch: LgTouch) -> str:
    parts = [SIGNAL_NAMES.get(s, s) for s in touch.intent_signals or []]
    if touch.intent_score is not None:
        parts.append(f"интерес {touch.intent_score}/100")
    return ", ".join(parts) or "явный интерес"


def _local(moment: datetime, touch: LgTouch) -> str:
    from zoneinfo import ZoneInfo
    return service._aware(moment).astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m %H:%M МСК")


async def cool(db: AsyncSession, touch: LgTouch, company: LgCompany, now: datetime | None = None) -> None:
    """Человек решил, что это не горячий ответ: закрываем задачу и возвращаем сделку на «Ответил»."""
    now = now or service.utcnow()
    if touch.hot_task_id:
        task = await db.get(CrmTask, touch.hot_task_id)
        if task and task.status == "OPEN":
            task.status, task.completed_at, task.result = "CANCELLED", now, "Не горячий ответ (решение во «Входящих»)"
    deal = await _deal(db, company)
    if deal is not None:
        current = await db.get(CrmStage, deal.stage_id)
        replied = await _stage(db, deal.pipeline_id, STAGE_REPLIED)
        if current and replied and current.name == STAGE_INTEREST:
            deal.stage_id, deal.stage_entered_at = replied.id, now
    touch.hot_at = None
    await db.flush()


async def remind_due(db: AsyncSession, now: datetime | None = None) -> int:
    """Горячие, которые не разобрали к сроку, — одно повторное сообщение в Telegram."""
    now = now or service.utcnow()
    cfg = await config(db)
    if not cfg["remind"]:
        return 0
    rows = (await db.execute(select(LgTouch, LgCompany).join(LgCompany, LgCompany.id == LgTouch.company_id).where(
        LgTouch.hot_at.is_not(None), LgTouch.handled_at.is_(None), LgTouch.hot_reminded_at.is_(None),
        LgTouch.direction == "in").limit(50))).all()
    sent = 0
    for touch, company in rows:
        task = await db.get(CrmTask, touch.hot_task_id) if touch.hot_task_id else None
        due = task.due_at if task else await due_at(db, touch, service._aware(touch.hot_at), cfg["sla_minutes"])
        if service._aware(due) > now or (task and task.status != "OPEN"):
            continue
        touch.hot_reminded_at = now
        await _telegram(db, f"⏰ <b>Горячий ответ ждёт</b> · {html.escape(company.display_name or company.domain or '')}\n"
                            f"Срок «связаться» прошёл. {_link(touch)}")
        sent += 1
    await db.flush()
    return sent
