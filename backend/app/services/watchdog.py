"""Client care loop: 15-minute response SLA, weekly owner report, CPL growth alert.

Runs every minute. Escalations respect the project's working hours (9:00–21:00 local time):
a request that arrives at night is escalated at 9:00 if it is still unanswered.
"""
import asyncio
import logging
import statistics
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crm import CrmDeal, CrmInbound
from app.models.marketing import ClientWorkspace, Project, ProjectNotificationRule
from app.models.messaging import Conversation
from app.models.telephony import Call
from app.services.messaging import project_people
from app.services.notifications import direct, flush_telegram, notify

logger = logging.getLogger("uvicorn.error.watchdog")
SLA_MINUTES = 15
WORK_START, WORK_END = 9, 21
CPL_DEFAULT_THRESHOLD = 30


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


async def project_zone(db: AsyncSession, project: Project) -> ZoneInfo:
    name = project.timezone
    if not name:
        workspace = await db.get(ClientWorkspace, project.workspace_id)
        name = workspace.timezone if workspace else None
    try:
        return ZoneInfo(name or "Europe/Moscow")
    except Exception:
        return ZoneInfo("Europe/Moscow")


async def working_hours(db: AsyncSession, project: Project, at: datetime | None = None) -> bool:
    local = (at or now()).astimezone(await project_zone(db, project))
    return WORK_START <= local.hour < WORK_END


def state(project: Project) -> dict:
    return dict(project.portal_state or {})


def mark_sent(project: Project, key: str, value: str) -> None:
    data = state(project)
    data["sent"] = {**(data.get("sent") or {}), key: value}
    project.portal_state = data


def was_sent(project: Project, key: str, value: str) -> bool:
    return (state(project).get("sent") or {}).get(key) == value


# --------------------------------------------------------------------------- SLA

async def check_sla(db: AsyncSession) -> int:
    """Escalate requests, new deals and chats that wait for a first answer longer than 15 minutes."""
    current = now()
    cutoff, floor = current - timedelta(minutes=SLA_MINUTES), current - timedelta(hours=24)
    projects: dict[int, Project] = {}
    open_hours: dict[int, bool] = {}
    sent = 0

    async def ready(project_id: int) -> Project | None:
        if project_id not in projects:
            projects[project_id] = await db.get(Project, project_id)
            open_hours[project_id] = bool(projects[project_id]) and await working_hours(db, projects[project_id], current)
        return projects[project_id] if open_hours[project_id] else None

    for inbound in (await db.scalars(select(CrmInbound).where(
            CrmInbound.status == "NEW", CrmInbound.escalated_at.is_(None),
            CrmInbound.received_at < cutoff, CrmInbound.received_at > floor).limit(200))).all():
        project = await ready(inbound.project_id)
        if not project:
            continue
        minutes = int((current - aware(inbound.received_at)).total_seconds() // 60)
        await notify(db, project.id, "lead_sla", "Заявка ждёт ответа", f"{inbound.name or 'Без имени'} · {minutes} мин в «Неразобранном»",
                     details=[f"Телефон: {inbound.phone}" if inbound.phone else None])
        inbound.escalated_at = current; sent += 1

    for deal in (await db.scalars(select(CrmDeal).where(
            CrmDeal.first_response_at.is_(None), CrmDeal.inbound_id.is_not(None), CrmDeal.archived_at.is_(None),
            CrmDeal.closed_at.is_(None), CrmDeal.created_at < cutoff, CrmDeal.created_at > floor).limit(200))).all():
        if (deal.automation_state or {}).get("sla_escalated"):
            continue
        project = await ready(deal.project_id)
        if not project:
            continue
        minutes = int((current - aware(deal.created_at)).total_seconds() // 60)
        people = await project_people(db, project.id)
        if deal.responsible_user_id in people:
            direct(db, project.workspace_id, [deal.responsible_user_id], "Клиент ждёт ответа",
                   f"{deal.name}: прошло {minutes} мин, а с клиентом ещё не связались", people)
        await notify(db, project.id, "lead_sla", "Заявка без ответа", f"{deal.name} · {minutes} мин без ответа",
                     actor_id=deal.responsible_user_id)
        deal.automation_state = {**(deal.automation_state or {}), "sla_escalated": current.isoformat()}
        sent += 1

    for conversation in (await db.scalars(select(Conversation).where(
            Conversation.status == "open", Conversation.waiting_since.is_not(None),
            Conversation.waiting_since < cutoff, Conversation.waiting_since > floor).limit(200))).all():
        stamp = aware(conversation.waiting_since).isoformat()
        if (conversation.meta or {}).get("sla_for") == stamp:
            continue
        project = await ready(conversation.project_id)
        if not project:
            continue
        minutes = int((current - aware(conversation.waiting_since)).total_seconds() // 60)
        people = await project_people(db, project.id)
        if conversation.assigned_user_id in people:
            direct(db, project.workspace_id, [conversation.assigned_user_id], "Клиент ждёт ответа в чате",
                   f"{conversation.title}: {minutes} мин без ответа", people)
        await notify(db, project.id, "lead_sla", "Чат без ответа", f"{conversation.title} · {minutes} мин без ответа",
                     actor_id=conversation.assigned_user_id,
                     details=[f"Последнее сообщение: {conversation.last_message_preview}" if conversation.last_message_preview else None])
        conversation.meta = {**(conversation.meta or {}), "sla_for": stamp}
        sent += 1
    if sent:
        await db.commit()
        flush_telegram(db)
    return sent


# --------------------------------------------------------------------------- weekly report and CPL alert

def money(value) -> str:
    return "—" if value is None else f"{value:,.0f} ₽".replace(",", " ")


def change(current, previous) -> str:
    if current is None or not previous:
        return ""
    delta = (current - previous) / abs(previous) * 100
    return f" ({'+' if delta >= 0 else ''}{delta:.0f}%)"


async def weekly_report(db: AsyncSession, project: Project, end: date) -> tuple[str, list[str]] | None:
    """Last 7 days vs the 7 before. None when the project has no data at all."""
    from app.services.result_analytics import result_facts
    start = end - timedelta(days=6)
    facts = await result_facts(db, project, start, end)
    t, p = facts["current"]["totals"], facts["previous"]["totals"]
    if not t.get("leads") and not t.get("spend"):
        return None
    lines = [f"Расход: {money(t.get('spend'))}{change(t.get('spend'), p.get('spend'))}",
             f"Заявки: {t.get('leads') or 0}{change(t.get('leads'), p.get('leads'))}"
             + (f" · целевых {t['target_share']:.0f}%" if t.get("target_share") is not None else ""),
             f"Цена заявки: {money(t.get('cpl'))}{change(t.get('cpl'), p.get('cpl'))}",
             f"Продажи: {t.get('sales') or 0} на {money(t.get('revenue'))}"
             + (f" · ROMI {t['romi']:.0f}%" if t.get("romi") is not None else "")]
    begin = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    finish = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    deals = (await db.scalars(select(CrmDeal).where(CrmDeal.project_id == project.id, CrmDeal.created_at >= begin,
                                                    CrmDeal.created_at < finish))).all()
    waits = [(aware(d.first_response_at) - aware(d.created_at)).total_seconds() / 60 for d in deals if d.first_response_at]
    if waits:
        lines.append(f"Скорость ответа: {statistics.median(waits):.0f} мин (медиана)")
    unanswered = sum(1 for d in deals if d.first_response_at is None and d.closed_at is None)
    if unanswered:
        lines.append(f"Без ответа до сих пор: {unanswered}")
    missed = len((await db.scalars(select(Call.id).where(Call.project_id == project.id, Call.direction == "in",
                                                         Call.status == "missed", Call.started_at >= begin,
                                                         Call.started_at < finish))).all())
    if missed:
        lines.append(f"Пропущенных звонков: {missed}")
    channels = [s for s in facts["current"]["sources"] if s.get("leads")]
    if channels:
        best = max(channels, key=lambda s: (s.get("sales") or 0, s.get("leads") or 0))
        lines.append(f"Лучший канал: {best['name']} — {best['leads']} заявок"
                     + (f" по {money(best.get('cpl'))}" if best.get("cpl") is not None else ""))
    note = ((state(project).get("launch") or {}).get("weekly_note") or "").strip()
    if note:
        lines.append(f"Комментарий маркетолога: {note[:500]}")
    return f"Неделя {start:%d.%m}–{end:%d.%m}", lines


async def threshold_for(db: AsyncSession, project_id: int, key: str, default: float) -> float:
    row = await db.scalar(select(ProjectNotificationRule).where(ProjectNotificationRule.project_id == project_id,
                                                                ProjectNotificationRule.event_key == key))
    return float(row.threshold) if row and row.threshold is not None else default


async def cpl_alert(db: AsyncSession, project: Project, end: date) -> bool:
    from app.services.result_analytics import result_facts
    facts = await result_facts(db, project, end - timedelta(days=6), end)
    t, p = facts["current"]["totals"], facts["previous"]["totals"]
    if (t.get("leads") or 0) < 5 or (p.get("leads") or 0) < 5 or not t.get("cpl") or not p.get("cpl"):
        return False
    growth = (t["cpl"] - p["cpl"]) / p["cpl"] * 100
    if growth < await threshold_for(db, project.id, "cpl_growth", CPL_DEFAULT_THRESHOLD):
        return False
    await notify(db, project.id, "cpl_growth", f"Цена заявки выросла на {growth:.0f}%",
                 f"{money(p['cpl'])} → {money(t['cpl'])} за последние 7 дней",
                 details=[f"Заявок: {p['leads']} → {t['leads']}", "Разбор причин — на странице «Аналитика»"])
    return True


async def scheduled(db: AsyncSession) -> None:
    """Monday 9:00 weekly report, daily 10:00 CPL check — in each project's local time, once."""
    for project in (await db.scalars(select(Project).where(Project.status == "active"))).all():
        local = now().astimezone(await project_zone(db, project))
        yesterday = local.date() - timedelta(days=1)
        try:
            if local.weekday() == 0 and local.hour == 9:
                week = local.strftime("%G-W%V")
                if not was_sent(project, "weekly", week):
                    report = await weekly_report(db, project, yesterday)
                    if report:
                        title, lines = report
                        await notify(db, project.id, "weekly_report", f"Итоги недели · {title}", lines[0], details=lines[1:])
                    mark_sent(project, "weekly", week)
                    await db.commit(); flush_telegram(db)
            if local.hour == 10 and not was_sent(project, "cpl", local.date().isoformat()):
                await cpl_alert(db, project, yesterday)
                mark_sent(project, "cpl", local.date().isoformat())
                await db.commit(); flush_telegram(db)
        except Exception:
            await db.rollback()
            logger.exception("scheduled report failed project=%s", project.id)


async def run_worker() -> None:
    from app.db import SessionLocal
    await asyncio.sleep(30)
    last_hourly = None
    while True:
        try:
            async with SessionLocal() as db:
                await check_sla(db)
                hour = now().replace(minute=0, second=0, microsecond=0)
                if last_hourly != hour or now().minute in (1, 31):
                    await scheduled(db)
                    last_hourly = hour
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("watchdog iteration failed")
        await asyncio.sleep(60)
