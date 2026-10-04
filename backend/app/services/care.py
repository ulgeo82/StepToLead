"""Client care after the first contact: meeting reminders, review requests after a sale, repeat-sale tasks.

Everything is off until the project turns it on (Settings → Воронка → «Забота о клиенте»).
Messages go through the deal's chat (WhatsApp / Telegram / Avito); WhatsApp can also start a new chat by phone.
When there is no way to write, the manager gets a task with the ready text instead. Each touch happens once (CareEvent).
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crm import CareEvent, CrmContact, CrmDeal, CrmTask
from app.models.marketing import ClientSale, ClientWorkspace, Project
from app.models.messaging import Conversation, MessagingChannel
from app.services import messaging

logger = logging.getLogger("uvicorn.error.care")
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
DEFAULTS = {
    "reminders": False, "reminder_hours": 24,
    "reminder_text": "Здравствуйте, {name}! Напоминаем: {date} в {time} — {what}. Если планы изменились, напишите нам, пожалуйста. {company}",
    "review": False, "review_days": 3, "review_url": "",
    "review_text": "{name}, спасибо, что выбрали {company}! Нам очень важно ваше мнение — оставьте, пожалуйста, отзыв: {review_url}",
    "repeat": False, "repeat_days": 180,
    "repeat_text": "Прошло {days} дн. с покупки. Свяжитесь с клиентом: всё ли в порядке, что можно предложить ещё (обслуживание, допродажа, рекомендация друзьям).",
}
WINDOW_DAYS = 30  # do not touch sales older than the trigger + this (no flood when a feature is switched on)


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def settings_for(project: Project) -> dict:
    data = dict((project.portal_state or {}).get("care") or {})
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


def first_name(name: str | None) -> str:
    parts = (name or "").split()
    return parts[0] if parts and parts[0].lower() not in {"клиент", "контакт"} else ""


def render(template: str, **values) -> str:
    text = template
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value or ""))
    return " ".join(text.replace(" ,", ",").split()).replace(", !", "!").strip()


async def company_name(db: AsyncSession, project: Project) -> str:
    req = ((project.portal_state or {}).get("requisites") or {}).get("company")
    if req:
        return req
    workspace = await db.get(ClientWorkspace, project.workspace_id)
    return (workspace.name if workspace else "") or project.name


async def chat_for(db: AsyncSession, deal: CrmDeal, contact: CrmContact | None) -> Conversation | None:
    """The deal's most recent live chat; for WhatsApp a new chat by phone if there is none."""
    rows = (await db.execute(select(Conversation, MessagingChannel).join(MessagingChannel, MessagingChannel.id == Conversation.channel_id)
                             .where(MessagingChannel.active.is_(True),
                                    (Conversation.deal_id == deal.id) | ((Conversation.contact_id == deal.contact_id) & (Conversation.contact_id.is_not(None))))
                             .order_by(Conversation.last_message_at.desc().nulls_last()))).all()
    if rows:
        return rows[0][0]
    digits = messaging.norm_phone((contact.phones or [None])[0]) if contact else ""
    if len(digits) < 10:
        return None
    channel = await db.scalar(select(MessagingChannel).where(MessagingChannel.project_id == deal.project_id, MessagingChannel.kind == "whatsapp",
                                                             MessagingChannel.active.is_(True)).limit(1))
    if not channel:
        return None
    chat_id = f"{digits}@c.us"
    row = await db.scalar(select(Conversation).where(Conversation.channel_id == channel.id, Conversation.external_chat_id == chat_id))
    if row is None:
        row = Conversation(workspace_id=deal.workspace_id, project_id=deal.project_id, channel_id=channel.id, external_chat_id=chat_id,
                           title=contact.name, phone=f"+{digits}", contact_id=contact.id, deal_id=deal.id,
                           assigned_user_id=deal.responsible_user_id, status="open", unread_count=0, meta={})
        db.add(row)
        await db.flush()
    return row


async def claim(db: AsyncSession, deal: CrmDeal, key: str, kind: str) -> CareEvent | None:
    """Reserve the touch (unique key) before doing anything external."""
    if await db.scalar(select(CareEvent.id).where(CareEvent.key == key)):
        return None
    event = CareEvent(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id, key=key, kind=kind, status="skipped")
    try:
        async with db.begin_nested():
            db.add(event)
    except IntegrityError:
        return None
    return event


async def deliver(db: AsyncSession, deal: CrmDeal, event: CareEvent, text: str, *, task_title: str, fallback_task: bool,
                  label: str) -> None:
    """Send `text` to the client's chat; otherwise (optionally) a task for the manager with the text. Commits."""
    from app.api.routes.crm import activity
    contact = await db.get(CrmContact, deal.contact_id)
    chat = await chat_for(db, deal, contact)
    if chat is not None:
        channel = await db.get(MessagingChannel, chat.channel_id)
        await db.commit()
        try:
            await messaging.send(db, chat, text, None)
            event.status, event.channel, event.detail = "sent", channel.kind, text[:500]
            activity(db, deal, None, "AUTOMATION", {"rule": "Забота о клиенте", "text": f"{label}: отправлено клиенту в {messaging.KINDS.get(channel.kind, channel.kind)}"}, touch=False)
            await db.commit()
            return
        except messaging.ChannelError as exc:
            event.detail = f"Не отправлено: {exc}"[:500]
            event.status = "failed"
    if fallback_task:
        db.add(CrmTask(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id, contact_id=deal.contact_id,
                       type_code="MESSAGE", title=task_title, description=text, responsible_user_id=deal.responsible_user_id,
                       due_at=now() + timedelta(minutes=30), priority="NORMAL"))
        event.status = "task"
        activity(db, deal, None, "AUTOMATION", {"rule": "Забота о клиенте", "text": f"{label}: задача менеджеру — написать клиенту"}, touch=False)
    await db.commit()


def when_text(due: datetime, zone) -> tuple[str, str]:
    local = due.astimezone(zone)
    return f"{local.day} {MONTHS[local.month - 1]}", local.strftime("%H:%M")


async def reminders(db: AsyncSession, project: Project, conf: dict, zone) -> int:
    hours = max(1, min(72, int(conf["reminder_hours"] or 24)))
    current = now()
    tasks = (await db.scalars(select(CrmTask).where(
        CrmTask.project_id == project.id, CrmTask.status == "OPEN", CrmTask.type_code == "MEETING", CrmTask.deal_id.is_not(None),
        CrmTask.due_at > current + timedelta(hours=1), CrmTask.due_at <= current + timedelta(hours=hours)).limit(30))).all()
    done = 0
    for task in tasks:
        deal = await db.get(CrmDeal, task.deal_id)
        if not deal or deal.archived_at:
            continue
        event = await claim(db, deal, f"reminder:{task.id}", "reminder")
        if not event:
            continue
        contact = await db.get(CrmContact, deal.contact_id)
        date, time = when_text(aware(task.due_at), zone)
        text = render(conf["reminder_text"], name=first_name(contact.name if contact else ""), date=date, time=time,
                      what=task.title, company=await company_name(db, project))
        await deliver(db, deal, event, text, task_title="Напомнить клиенту о встрече", fallback_task=False, label="Напоминание о встрече")
        done += 1
    return done


async def after_sale(db: AsyncSession, project: Project, conf: dict, kind: str) -> int:
    days = max(1, int(conf["review_days"] if kind == "review" else conf["repeat_days"]) or 1)
    current = now()
    sales = (await db.scalars(select(ClientSale).where(
        ClientSale.project_id == project.id, ClientSale.deal_id.is_not(None),
        ClientSale.occurred_at <= current - timedelta(days=days),
        ClientSale.occurred_at > current - timedelta(days=days + WINDOW_DAYS)).order_by(ClientSale.occurred_at).limit(30))).all()
    done = 0
    for sale in sales:
        deal = await db.get(CrmDeal, sale.deal_id)
        if not deal:
            continue
        event = await claim(db, deal, f"{kind}:{deal.id}", kind)
        if not event:
            continue
        contact = await db.get(CrmContact, deal.contact_id)
        if kind == "review":
            text = render(conf["review_text"], name=first_name(contact.name if contact else ""), company=await company_name(db, project),
                          review_url=conf["review_url"])
            await deliver(db, deal, event, text, task_title="Попросить отзыв", fallback_task=True, label="Просьба об отзыве")
        else:
            from app.api.routes.crm import activity
            text = render(conf["repeat_text"], name=first_name(contact.name if contact else ""), days=days)
            db.add(CrmTask(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id, contact_id=deal.contact_id,
                           type_code="CALL", title="Повторная продажа", description=text, responsible_user_id=deal.responsible_user_id,
                           due_at=current + timedelta(hours=2), priority="NORMAL"))
            event.status = "task"
            activity(db, deal, None, "AUTOMATION", {"rule": "Забота о клиенте", "text": f"Повторная продажа: задача менеджеру через {days} дн. после покупки"}, touch=False)
            await db.commit()
        done += 1
    return done


def working(zone) -> bool:
    return 9 <= now().astimezone(zone).hour < 21


async def run(db: AsyncSession) -> int:
    """One pass over projects with care on. Messages only in working hours (9–21 local)."""
    from app.services.watchdog import project_zone
    projects = (await db.scalars(select(Project).where(Project.status == "active"))).all()
    total = 0
    for project in projects:
        conf = settings_for(project)
        if not (conf["reminders"] or conf["review"] or conf["repeat"]):
            continue
        zone = await project_zone(db, project)
        if not working(zone):
            continue
        try:
            if conf["reminders"]:
                total += await reminders(db, project, conf, zone)
            if conf["review"] and conf["review_url"]:
                total += await after_sale(db, project, conf, "review")
            if conf["repeat"]:
                total += await after_sale(db, project, conf, "repeat")
        except Exception:
            await db.rollback()
            logger.exception("care pass failed project=%s", project.id)
    return total


async def stats(db: AsyncSession, project_id: int, days: int = 30) -> dict:
    rows = (await db.scalars(select(CareEvent).where(CareEvent.project_id == project_id,
                                                     CareEvent.created_at >= now() - timedelta(days=days)))).all()
    out = {k: {"sent": 0, "task": 0, "failed": 0, "skipped": 0} for k in ("reminder", "review", "repeat")}
    for row in rows:
        out.setdefault(row.kind, {"sent": 0, "task": 0, "failed": 0, "skipped": 0})[row.status] += 1
    return out
