"""Project notifications: who receives an event and how (in the cabinet and/or Telegram).

Rules live in ProjectNotificationRule per project and event. Recipients are always
limited to active members of the project (owner or users with project access).
Telegram messages are queued on the DB session and sent only after commit via
``flush_telegram`` so a rolled-back request never sends anything.
"""
import asyncio
import html
import logging

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.marketing import PortalNotification, PortalProjectAccess, PortalUser, Project, ProjectNotificationRule

logger = logging.getLogger("uvicorn.error.notifications")

EVENTS = {
    "new_lead": {"label": "Новый лид", "active": True, "default": True, "recipients": True},
    "new_sale": {"label": "Новая продажа", "active": True, "default": True, "recipients": True},
    "lead_sla": {"label": "Заявка без ответа 15 минут", "active": True, "default": True, "recipients": True,
                 "telegram_default": True},
    "weekly_report": {"label": "Еженедельный отчёт (понедельник, 9:00)", "active": True, "default": True,
                      "recipients": True, "telegram_default": True},
    "lead_idle": {"label": "Лид без обработки", "active": False, "unit": "часов"},
    "cac_limit": {"label": "CAC превысил допустимый", "active": False},
    "cpl_growth": {"label": "Цена заявки выросла за неделю", "active": True, "default": True, "unit": "%",
                   "default_threshold": 30, "recipients": True, "telegram_default": True},
    "ad_budget": {"label": "Рекламный бюджет заканчивается", "active": False, "unit": "%"},
    "ad_sync_error": {"label": "Ошибка синхронизации рекламы", "active": False},
}
DEFAULT_ROLES = ("client_owner", "sales_head")
PENDING_KEY = "pending_telegram"
_background: set[asyncio.Task] = set()


def telegram_configured() -> bool:
    return bool(settings.telegram_bot_token)


async def project_members(db: AsyncSession, project: Project) -> list[PortalUser]:
    """Active users who can see the project: the owner role plus explicit project access."""
    return list((await db.scalars(select(PortalUser).where(
        PortalUser.workspace_id == project.workspace_id, PortalUser.active.is_(True),
        or_(PortalUser.role == "client_owner", PortalUser.id.in_(
            select(PortalProjectAccess.user_id).where(PortalProjectAccess.project_id == project.id)))
    ).order_by(PortalUser.display_name, PortalUser.id))).all())


async def rule_for(db: AsyncSession, project_id: int, event_key: str) -> ProjectNotificationRule | None:
    return await db.scalar(select(ProjectNotificationRule).where(
        ProjectNotificationRule.project_id == project_id, ProjectNotificationRule.event_key == event_key))


def rule_state(event_key: str, row: ProjectNotificationRule | None) -> dict:
    config = EVENTS[event_key]
    return {"enabled": row.enabled if row else config.get("default", False),
            "in_app": row.in_app if row else True,
            "telegram": bool(row.telegram) if row else bool(config.get("telegram_default")),
            "recipient_user_ids": list(row.recipient_user_ids) if row and row.recipient_user_ids is not None else None,
            "notify_assignee": row.notify_assignee if row and row.notify_assignee is not None else True}


async def notify(db: AsyncSession, project_id: int | None, event_key: str, title: str, body: str, *,
                 assignee_id: int | None = None, actor_id: int | None = None, details: list[str] | None = None) -> int:
    """Create notifications for an event. Returns the number of recipients. Call before commit."""
    if not project_id or event_key not in EVENTS or not EVENTS[event_key]["active"]:
        return 0
    project = await db.get(Project, project_id)
    if project is None:
        return 0
    state = rule_state(event_key, await rule_for(db, project_id, event_key))
    if not state["enabled"] or not (state["in_app"] or state["telegram"]):
        return 0
    members = {user.id: user for user in await project_members(db, project)}
    selected = state["recipient_user_ids"]
    if selected is None:
        # Default audience: the responsible person if known, otherwise owners and sales heads.
        if assignee_id in members and state["notify_assignee"]:
            ids = {assignee_id}
        else:
            ids = {uid for uid, user in members.items() if user.role in DEFAULT_ROLES}
    else:
        ids = {int(uid) for uid in selected if int(uid) in members}
        if state["notify_assignee"] and assignee_id in members:
            ids.add(assignee_id)
    ids.discard(actor_id)  # Nobody needs a notification about their own action.
    recipients = [members[uid] for uid in sorted(ids)]
    from app.services import push
    for user in recipients:
        if state["in_app"]:
            db.add(PortalNotification(workspace_id=project.workspace_id, user_id=user.id, level="success",
                                      title=title[:180], body=body))
            push.queue(db, user.id, title, body, f"/crm?project_id={project.id}")
        if state["telegram"] and telegram_configured() and user.telegram_chat_id:
            lines = [f"<b>{html.escape(title)}</b> · {html.escape(project.name)}", html.escape(body)]
            lines += [html.escape(line) for line in details or [] if line]
            db.sync_session.info.setdefault(PENDING_KEY, []).append((user.telegram_chat_id, "\n".join(lines)))
    return len(recipients)


async def telegram_api(method: str, payload: dict | None = None, timeout: float = 10) -> dict:
    if not telegram_configured():
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}",
                                     json=payload or {})
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or f"Telegram API error {response.status_code}")
    return data["result"]


async def _send_all(messages: list[tuple[str, str]]) -> None:
    for chat_id, text in messages:
        try:
            await telegram_api("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                               "disable_web_page_preview": True})
        except Exception as exc:  # Delivery is best-effort; the cabinet notification already exists.
            logger.warning("telegram notification failed chat_id=%s error=%s", chat_id, exc)


def flush_telegram(db: AsyncSession) -> None:
    """Send queued Telegram messages (and web pushes) in the background. Call right after a successful commit."""
    from app.services import push
    push.flush(db)
    messages = db.sync_session.info.pop(PENDING_KEY, None)
    if not messages:
        return
    task = asyncio.get_running_loop().create_task(_send_all(messages))
    _background.add(task)
    task.add_done_callback(_background.discard)


def discard_telegram(db: AsyncSession) -> None:
    from app.services import push
    push.discard(db)
    db.sync_session.info.pop(PENDING_KEY, None)


def direct(db: AsyncSession, workspace_id: int, user_ids: list[int], title: str, body: str,
           users: dict[int, PortalUser]) -> None:
    """Notify specific users (automation rules): cabinet notification + Telegram if linked. Call before commit."""
    for user_id in user_ids:
        user = users.get(user_id)
        if not user:
            continue
        db.add(PortalNotification(workspace_id=workspace_id, user_id=user.id, level="info",
                                  title=title[:180], body=body))
        from app.services import push
        push.queue(db, user.id, title, body)
        if telegram_configured() and user.telegram_chat_id:
            text = f"<b>{html.escape(title)}</b>\n{html.escape(body)}"
            db.sync_session.info.setdefault(PENDING_KEY, []).append((user.telegram_chat_id, text))
