"""The only consumer of the installation notification bot. No customer-chat tokens.

CRM writes and processed update IDs commit together. Telegram side effects happen
after commit. A renewed Redis lease fences the long-polling process; without Redis
production fails closed. Tests invoke process_update directly without Redis.
"""
import asyncio
import hashlib
import html
import logging
import secrets
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select, update

from app.core.access import token_digest
from app.core.config import settings
from app.core.permissions import effective_permissions, require_permission
from app.db import SessionLocal
from app.models.access import AdminUser
from app.models.crm import CrmActivity, CrmDeal, CrmInbound, CrmTask
from app.models.marketing import ClientWorkspace, PortalProjectAccess, PortalUser, Project
from app.models.system import AppSetting
from app.services.notifications import TelegramError, discard_telegram, flush_telegram, telegram_api
from app.services.tg_preferences import bounded_html, lead_buttons, private_text, task_buttons

logger = logging.getLogger("uvicorn.error.tg_bot")
COMMANDS = [{"command": key, "description": label} for key, label in (
    ("menu", "Мои проекты и портал"), ("leads", "Новые заявки"), ("tasks", "Мои задачи"),
    ("report", "Отчёт проекта"), ("settings", "Личные уведомления"))]
DONE_RESULTS = ["Связался с клиентом", "Не ответил", "Отправил информацию"]


def state_key():
    return "tg_updates_" + hashlib.sha256(settings.telegram_bot_token.encode()).hexdigest()[:24]


def button(text, data=None, url=None):
    if data is not None and len(data.encode()) > 64:
        raise ValueError("Callback exceeds 64 bytes")
    return {"text": text, **({"callback_data": data} if data is not None else {"url": url})}


def screen(text, rows=None):
    return text, {"inline_keyboard": rows or []}


def portal_url(path="/crm"):
    return settings.frontend_origin.rstrip("/") + path


async def unlink_blocked(chat_id, sessions=None):
    async with (sessions or SessionLocal)() as db:
        for model in (PortalUser, AdminUser):
            await db.execute(update(model).where(model.telegram_chat_id == str(chat_id)).values(
                telegram_chat_id=None, telegram_username=None, telegram_link_code_hash=None,
                telegram_link_expires_at=None))
        await db.commit()


async def actor_for(db, chat_id, admin=False):
    if not admin:
        user = await db.scalar(select(PortalUser).where(PortalUser.telegram_chat_id == str(chat_id)).order_by(PortalUser.id))
        if user:
            company = await db.get(ClientWorkspace, user.workspace_id)
            if not user.active or not company or company.status == "deleted":
                raise HTTPException(403, "Доступ к кабинету отключён")
            return user
    user = await db.scalar(select(AdminUser).where(AdminUser.telegram_chat_id == str(chat_id)))
    if user and user.role == "admin":
        return user
    raise HTTPException(403, "Подключите Telegram в портале: Настройки → Уведомления → Мой Telegram")


async def link(db, chat, sender, code):
    expires_now = datetime.now(timezone.utc)
    target = None
    if code:
        for model in (PortalUser, AdminUser):
            target = await db.scalar(select(model).where(model.telegram_link_code_hash == token_digest(code)).with_for_update())
            if target:
                break
    expires = target.telegram_link_expires_at if target else None
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if not target or not expires or expires <= expires_now:
        return screen("Ссылка устарела или неверна. Нажмите «Подключить Telegram» в портале ещё раз.")
    if isinstance(target, PortalUser):
        company = await db.get(ClientWorkspace, target.workspace_id)
        if not target.active or not company or company.status == "deleted":
            raise HTTPException(403, "Доступ отключён")
    elif target.role != "admin":
        raise HTTPException(403, "Доступ отключён")
    target.telegram_chat_id = str(chat["id"])
    target.telegram_username = sender.get("username")
    target.telegram_link_code_hash = target.telegram_link_expires_at = None
    return screen("Telegram подключён. Уведомления будут приходить сюда.",
        [[button("Открыть меню", "menu" if isinstance(target, PortalUser) else "admin")]])


async def projects_for(db, user):
    from app.api.routes.crm import project_for
    query = select(Project).where(Project.workspace_id == user.workspace_id)
    if user.role != "client_owner":
        query = query.where(Project.id.in_(select(PortalProjectAccess.project_id).where(PortalProjectAccess.user_id == user.id)))
    rows = (await db.scalars(query.order_by(Project.id))).all()
    return [await project_for(db, user, p.id) for p in rows]


async def chosen_project(db, user):
    from app.api.routes.crm import project_for
    project_id = (user.telegram_state or {}).get("project_id")
    if project_id:
        return await project_for(db, user, int(project_id))
    rows = await projects_for(db, user)
    if not rows:
        raise HTTPException(403, "Нет доступных проектов")
    project = rows[0]
    user.telegram_state = {**(user.telegram_state or {}), "project_id": project.id,
        "project_timezone": project.timezone or "Europe/Moscow"}
    return project


async def menu(db, user):
    require_permission(user, "view_crm")
    rows = [[button(p.name[:60], f"project:{p.id}")] for p in await projects_for(db, user)]
    rows += [[button("Новые заявки", "leads:0"), button("Мои задачи", "tasks")],
             [button("Отчёт за 7 дней", "report:7"), button("За 30 дней", "report:30")],
             [button("Уведомления", "settings"), button("Открыть портал", url=portal_url())]]
    return screen("<b>StepToLead</b>\nВыберите проект или действие.", rows)


async def leads(db, user, page):
    from app.api.routes.crm import own_filter
    require_permission(user, "view_crm")
    project = await chosen_project(db, user)
    if page < 0 or page > 1000:
        raise HTTPException(422, "Неверная страница")
    # Bound each branch before merging; only the top (page + 1) * 5 can be shown.
    limit = (page + 1) * 5 + 1
    # Unassigned incoming requests are only visible to project-wide supervisors.
    inbound = []
    if "view_all_deals" in effective_permissions(user):
        inbound = list((await db.scalars(select(CrmInbound).where(CrmInbound.project_id == project.id,
            CrmInbound.status == "NEW").order_by(CrmInbound.received_at.desc(), CrmInbound.id.desc()).limit(limit))).all())
    deals_query = own_filter(select(CrmDeal).where(CrmDeal.project_id == project.id,
        CrmDeal.archived_at.is_(None), CrmDeal.closed_at.is_(None), CrmDeal.first_response_at.is_(None)), user)
    deals = list((await db.scalars(deals_query.order_by(CrmDeal.created_at.desc(), CrmDeal.id.desc()).limit(limit))).all())
    items = [(i.received_at, "i", i) for i in inbound] + [(d.created_at, "d", d) for d in deals]
    items.sort(key=lambda i: i[0].replace(tzinfo=timezone.utc) if i[0].tzinfo is None else i[0], reverse=True)
    lines = [f"<b>Новые заявки · {html.escape(project.name)}</b>"]
    rows = []
    for _, kind, obj in items[page*5:page*5+5]:
        name = obj.name or "Без имени"
        lines.append(f"{'Заявка' if kind == 'i' else 'Сделка'} #{obj.id}: {html.escape(private_text(user, name))}")
        rows.append([button(f"Открыть #{obj.id}", f"show:{kind}:{obj.id}")])
    if len(lines) == 1:
        lines.append("Новых заявок нет.")
    nav = []
    if page:
        nav.append(button("← Назад", f"leads:{page-1}"))
    if (page+1)*5 < len(items):
        nav.append(button("Далее →", f"leads:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([button("Меню", "menu")])
    return screen("\n".join(lines), rows)


async def task_menu(db, user):
    require_permission(user, "view_crm")
    project = await chosen_project(db, user)
    zone = ZoneInfo(project.timezone or "Europe/Moscow")
    today = datetime.now(zone).date()
    rows = (await db.scalars(select(CrmTask).where(CrmTask.project_id == project.id,
        CrmTask.responsible_user_id == user.id, CrmTask.status == "OPEN").order_by(CrmTask.due_at).limit(100))).all()
    lines, keys = ["<b>Мои задачи</b>"], []
    for label, predicate in (("Просроченные", lambda d: d < today), ("Сегодня", lambda d: d == today),
                              ("Завтра", lambda d: d == today + timedelta(days=1))):
        lines.append(f"\n<b>{label}</b>")
        found = 0
        for task in rows:
            due = task.due_at if task.due_at.tzinfo else task.due_at.replace(tzinfo=timezone.utc)
            if predicate(due.astimezone(zone).date()):
                lines.append(f"#{task.id} · {html.escape(private_text(user, task.title)[:120])}")
                keys.append([button(f"Задача #{task.id}", f"task:{task.id}")]); found += 1
                if len(keys) == 10:
                    break
        if not found:
            lines.append("Нет задач")
        if len(keys) == 10:
            lines.append("Остальные задачи — в портале."); break
    keys.append([button("Меню", "menu")])
    return screen("\n".join(lines), keys)


async def report(db, user, days):
    from app.services.result_analytics import result_facts
    require_permission(user, "view_result")
    project = await chosen_project(db, user)
    if days not in (7, 30):
        raise HTTPException(422, "Выберите 7 или 30 дней")
    end = datetime.now(ZoneInfo(project.timezone or "Europe/Moscow")).date()
    data = await result_facts(db, project, end-timedelta(days=days-1), end)
    totals = data["current"]["totals"]
    def value(key):
        v = totals.get(key)
        return "нет данных" if v is None else f"{v:,.2f}".replace(",", " ")
    return screen(f"<b>{html.escape(project.name)} · {days} дней</b>\nЗаявки: {value('leads')}\n"
        f"Цена заявки: {value('cpl')} ₽\nПродажи: {value('sales')}\nВыручка: {value('revenue')} ₽\nROMI: {value('romi')} %",
        [[button("7 дней", "report:7"), button("30 дней", "report:30")], [button("Меню", "menu")]])


def preference_menu(user):
    from app.services.notifications import EVENTS
    state = user.telegram_state or {}
    rows = [[button(("✅ " if state.get("notifications", {}).get(event, True) else "❌ ") + config["label"], f"pref:{event}")]
            for event, config in EVENTS.items() if config["active"]]
    rows += [[button(("✅ " if state.get("notifications", {}).get("direct", True) else "❌ ") + "Задачи и личные уведомления", "pref:direct")],
        [button("Телефон клиента: " + ("показан" if state.get("show_phone") else "скрыт"), "phone")],
        [button("Тихие часы: " + ("включены" if (state.get("quiet_hours") or {}).get("enabled") else "выключены"), "quiet")],
        [button("22:00–09:00", "hours:22:9"), button("23:00–08:00", "hours:23:8")], [button("Меню", "menu")]]
    quiet = state.get("quiet_hours") or {}
    return screen(f"<b>Личные уведомления</b>\nПравила проекта остаются главными.\nТихие часы: "
        f"{quiet.get('start', 22):02d}:00–{quiet.get('end', 9):02d}:00 по часовому поясу выбранного проекта.", rows)


async def admin_menu(db, data):
    from app.services import monitor
    if data == "admin:problems":
        items = await monitor.problems(db)
        text = "<b>Сбои сейчас</b>\n" + ("\n".join(html.escape(i["title"]) for i in items) or "Всё работает")
    elif data == "admin:workers":
        text = "<b>Фоновые задачи</b>\n" + "\n".join(html.escape(name) + (" · остановлена" if task.done() else " · работает")
                                                for name, task in monitor._tasks.items())
    else:
        text = "<b>StepToLead · администратор</b>"
    return screen(bounded_html(text), [[button("Сбои сейчас", "admin:problems"), button("Фоновые задачи", "admin:workers")],
        [button("Открыть мониторинг", url=portal_url("/admin/monitor"))]])


async def target_deal(db, user, kind, ident, *, accept=False):
    from app.api.routes import crm
    require_permission(user, "view_crm")
    if kind == "d":
        deal = await crm.deal_for(db, user, ident)
    elif kind == "i":
        inbound = await db.get(CrmInbound, ident, with_for_update=True)
        if not inbound or inbound.workspace_id != user.workspace_id:
            raise HTTPException(404, "Заявка не найдена")
        await crm.project_for(db, user, inbound.project_id)
        if inbound.deal_id:
            deal = await crm.deal_for(db, user, inbound.deal_id)
        elif not accept:
            if "view_all_deals" not in effective_permissions(user):
                raise HTTPException(403, "Нет доступа к неразобранной заявке")
            return None, inbound
        else:
            if "view_all_deals" not in effective_permissions(user):
                raise HTTPException(403, "Нет доступа к неразобранной заявке")
            result = await crm.accept_inbound_core(ident, crm.InboundAction(responsible_user_id=user.id), db, user, commit=False)
            deal = await crm.deal_for(db, user, result["deal_id"])
    else:
        raise HTTPException(422, "Неверное действие")
    if deal.archived_at:
        raise HTTPException(409, "Сделка в архиве")
    return deal, None


async def task_for(db, user, ident):
    from app.api.routes.crm import deal_for, project_for
    require_permission(user, "view_crm")
    task = await db.get(CrmTask, ident, with_for_update=True)
    if not task or task.workspace_id != user.workspace_id:
        raise HTTPException(404, "Задача не найдена")
    await project_for(db, user, task.project_id)
    if task.responsible_user_id != user.id and "view_all_deals" not in effective_permissions(user):
        raise HTTPException(403, "Чужая задача")
    if task.deal_id:
        await deal_for(db, user, task.deal_id)
    return task


async def dispatch(db, user, data):
    from app.api.routes import crm
    if isinstance(user, AdminUser):
        return await admin_menu(db, data)
    bits = data.split(":")
    action = bits[0]
    if action in {"menu", "start"}:
        return await menu(db, user)
    if action == "project":
        project = await crm.project_for(db, user, int(bits[1]))
        user.telegram_state = {**(user.telegram_state or {}), "project_id": project.id,
            "project_timezone": project.timezone or "Europe/Moscow"}
        text, keyboard = await menu(db, user)
        return screen(f"Выбран проект: <b>{html.escape(project.name)}</b>\n" + text, keyboard["inline_keyboard"])
    if action == "leads":
        return await leads(db, user, int(bits[1]) if len(bits) > 1 else 0)
    if action == "tasks":
        return await task_menu(db, user)
    if action == "report":
        return await report(db, user, int(bits[1]) if len(bits) > 1 else 7)
    if action in {"settings", "pref", "phone", "quiet", "hours"}:
        await chosen_project(db, user)
        state = dict(user.telegram_state or {})
        if action == "pref":
            from app.services.notifications import EVENTS
            if bits[1] not in {*EVENTS, "direct"}:
                raise HTTPException(422, "Неизвестное уведомление")
            choices = dict(state.get("notifications", {}))
            choices[bits[1]] = not choices.get(bits[1], True); state["notifications"] = choices
        elif action == "phone":
            state["show_phone"] = not state.get("show_phone", False)
        elif action == "quiet":
            q = dict(state.get("quiet_hours") or {}); q["enabled"] = not q.get("enabled", False); state["quiet_hours"] = q
        elif action == "hours":
            start, end = int(bits[1]), int(bits[2])
            if not (0 <= start < 24 and 0 <= end < 24):
                raise HTTPException(422, "Неверное время")
            state["quiet_hours"] = {"enabled": True, "start": start, "end": end}
        user.telegram_state = state
        return preference_menu(user)
    if action in {"task", "done", "finish", "tomorrow"}:
        task = await task_for(db, user, int(bits[1]))
        if action == "task":
            return html.escape(private_text(user, task.title)), task_buttons(task.id, task.deal_id)
        if action == "done":
            return screen("Выберите результат:", [[button(result, f"finish:{task.id}:{index}")] for index, result in enumerate(DONE_RESULTS)])
        if action == "finish":
            index = int(bits[2])
            if not 0 <= index < len(DONE_RESULTS):
                raise HTTPException(422, "Неверный результат")
            await crm.complete_task_core(task.id, crm.TaskComplete(result=DONE_RESULTS[index]), db, user, commit=False)
            text = "Задача выполнена"
        else:
            if task.status != "OPEN":
                raise HTTPException(409, "Задача закрыта")
            project = await crm.project_for(db, user, task.project_id)
            due = tomorrow_at(project)
            await crm.update_task_core(task.id, crm.TaskUpdate(due_at=due), db, user, commit=False)
            text = "Задача перенесена на завтра"
        if task.deal_id:
            crm.activity(db, await crm.deal_for(db, user, task.deal_id), user, "TELEGRAM_ACTION",
                {"text": text + " — через Telegram", "task_id": task.id})
        else:
            db.add(CrmActivity(workspace_id=task.workspace_id, project_id=task.project_id,
                actor_id=user.id, actor_name=user.display_name, event_type="TELEGRAM_ACTION",
                payload={"text": text + " — через Telegram", "task_id": task.id}))
        return screen(text, [[button("Мои задачи", "tasks")]])
    if action in {"show", "take", "quality", "reason", "call1", "calltom", "dial"}:
        kind, ident = bits[1], int(bits[2])
        # Authorize the requested write BEFORE accepting an inbound request.
        if action != "show":
            require_permission(user, {"take": "edit_deal", "quality": "edit_deal", "reason": "edit_deal",
                "call1": "manage_tasks", "calltom": "manage_tasks", "dial": "view_crm"}[action])
        deal, inbound = await target_deal(db, user, kind, ident, accept=action in {"take", "reason", "call1", "calltom"})
        if action == "show":
            name = deal.name if deal else inbound.name or "Без имени"
            detail = ""
            if inbound:
                payload = inbound.raw_payload or {}
                detail = "\n" + "\n".join(f"{label}: {html.escape(private_text(user, str(payload[key])))}" for key, label in
                    (("contact", "Контакт"), ("comment", "Комментарий"), ("notes", "Комментарий")) if payload.get(key))
            keyboard = lead_buttons(inbound.id if inbound else None, deal.id if deal else None)
            if deal:
                from app.api.routes.telephony import project_connection
                from app.services.telephony import extension_for_user
                conn = await project_connection(db, deal.project_id)
                if conn and conn.active and extension_for_user(conn, user.id):
                    keyboard["inline_keyboard"].insert(-1, [button("Позвонить через Mango", f"dial:d:{deal.id}")])
            return bounded_html(html.escape(private_text(user, name)) + detail), keyboard
        if action == "quality":
            return screen("Почему заявка нецелевая?", [[button(reason, f"reason:{kind}:{ident}:{index}")]
                for index, reason in enumerate(crm.QUALITY_REASONS)])
        if not deal:
            raise HTTPException(422, "Не удалось создать сделку")
        project = await crm.project_for(db, user, deal.project_id)
        if action == "take":
            await crm.update_deal_core(deal.id, crm.DealUpdate(responsible_user_id=user.id), db, user, commit=False)
            text = "Сделка взята в работу"
        elif action == "reason":
            index = int(bits[3])
            if not 0 <= index < len(crm.QUALITY_REASONS):
                raise HTTPException(422, "Неизвестная причина")
            await crm.set_quality_core(deal.id, crm.QualityIn(quality="non_target", reason=crm.QUALITY_REASONS[index]), db, user, commit=False)
            text = "Заявка отмечена как нецелевая"
        elif action in {"call1", "calltom"}:
            due = datetime.now(timezone.utc) + timedelta(hours=1) if action == "call1" else tomorrow_at(project)
            await crm.create_task_core(crm.TaskCreate(project_id=project.id, deal_id=deal.id,
                title="Перезвонить клиенту", type_code="CALL", due_at=due, responsible_user_id=user.id), db, user, commit=False)
            text = "Задача на перезвон создана"
        else:
            from app.api.routes.telephony import DialIn, dial_core
            from app.models.crm import CrmContact
            contact = await db.get(CrmContact, deal.contact_id)
            phone = (contact.phones or [None])[0] if contact else None
            if not phone:
                raise HTTPException(422, "У контакта нет телефона")
            await dial_core(project.id, DialIn(phone=phone, deal_id=deal.id), db, user)
            text = "Звонок запрошен. Снимите трубку на своём телефоне"
        crm.activity(db, deal, user, "TELEGRAM_ACTION", {"text": text + " — через Telegram"})
        return screen(text, [[button("Открыть сделку", url=portal_url(f"/crm?deal={deal.id}"))], [button("Меню", "menu")]])
    raise HTTPException(422, "Неизвестная команда. Откройте /menu")


def tomorrow_at(project):
    zone = ZoneInfo(project.timezone or "Europe/Moscow")
    local = datetime.now(zone)
    return (local + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


async def safe_api(method, payload, sessions=None):
    try:
        return await telegram_api(method, payload)
    except TelegramError as exc:
        if exc.code == 403 and payload.get("chat_id"):
            await unlink_blocked(payload["chat_id"], sessions)
        logger.warning("telegram delivery failed method=%s code=%s", method, exc.code)
    except Exception:
        logger.warning("telegram delivery failed method=%s", method)


async def process_update(update, sessions=None):
    sessions = sessions or SessionLocal
    query = update.get("callback_query")
    message = (query or {}).get("message") if query else update.get("message")
    message = message or {}
    chat = message.get("chat") or {}
    private = chat.get("type") == "private"
    callback_id = (query or {}).get("id")
    # Each callback, even a duplicate or group callback, gets an acknowledgement.
    if callback_id:
        await safe_api("answerCallbackQuery", {"callback_query_id": callback_id}, sessions)
    sender = (query or {}).get("from") if query else message.get("from")
    private = private and str((sender or {}).get("id")) == str(chat.get("id"))
    update_id = int(update["update_id"])
    response = None
    async with sessions() as db:
        state = await db.get(AppSetting, state_key(), with_for_update=True)
        if state is None:
            state = AppSetting(key=state_key(), value={"offset": 0, "processed": []}); db.add(state); await db.flush()
        saved = state.value or {}
        if update_id in saved.get("processed", []) or update_id < saved.get("offset", 0):
            return
        try:
            # A savepoint prevents an invalid action from committing partial acceptance/writes.
            async with db.begin_nested():
                text = message.get("text") or ""
                if not private:
                    pass
                elif not query and text.startswith("/start "):
                    response = await link(db, chat, sender or {}, text.split(maxsplit=1)[1].strip())
                else:
                    data = str((query or {}).get("data", "")) if query else text.split()[0].split("@")[0].lstrip("/") if text else "menu"
                    if not query:
                        data = {"leads": "leads:0", "report": "report:7", "start": "menu"}.get(data, data)
                    if len(data.encode()) > 64:
                        raise HTTPException(422, "Неверная команда")
                    user = await actor_for(db, chat["id"], admin=data.startswith("admin"))
                    response = await dispatch(db, user, data)
        except HTTPException as exc:
            discard_telegram(db)
            response = screen(html.escape(str(exc.detail)))
        except (ValueError, IndexError, TypeError):
            discard_telegram(db)
            response = screen("Неверная кнопка. Откройте /menu ещё раз.")
        state.value = {"offset": max(saved.get("offset", 0), update_id + 1),
                       "processed": (saved.get("processed", []) + [update_id])[-200:]}
        await db.commit()
        flush_telegram(db)
    if response:
        text, markup = response
        payload = {"chat_id": chat["id"], "text": bounded_html(text), "parse_mode": "HTML", "reply_markup": markup,
                   "disable_web_page_preview": True}
        if query:
            payload["message_id"] = message["message_id"]
        await safe_api("editMessageText" if query else "sendMessage", payload, sessions)


RENEW = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


async def poll():
    # Telegram polling is installation-wide. Multiple processes contend for a lease.
    lock_key = state_key() + ":lease"
    while True:
        try:
            async with Redis.from_url(settings.redis_url) as redis:
                owner = secrets.token_hex(16)
                if not await redis.set(lock_key, owner, nx=True, ex=60):
                    await asyncio.sleep(5); continue
                lost = asyncio.Event()
                async def heartbeat():
                    try:
                        while True:
                            await asyncio.sleep(10)
                            if not await redis.eval(RENEW, 1, lock_key, owner, 60):
                                lost.set(); return
                    except Exception:
                        lost.set()
                lease = asyncio.create_task(heartbeat())
                try:
                    await telegram_api("setMyCommands", {"commands": COMMANDS})
                    while not lost.is_set():
                        async with SessionLocal() as db:
                            row = await db.get(AppSetting, state_key())
                            offset = (row.value or {}).get("offset", 0) if row else 0
                        updates = await telegram_api("getUpdates", {"offset": offset, "timeout": 25,
                            "allowed_updates": ["message", "callback_query"]}, timeout=35)
                        for item in updates or []:
                            if lost.is_set() or await redis.get(lock_key) not in (owner, owner.encode()):
                                raise RuntimeError("Telegram polling lease lost")
                            await process_update(item)
                finally:
                    lease.cancel()
                    with suppress(asyncio.CancelledError):
                        await lease
                    with suppress(Exception):
                        await redis.eval(RELEASE, 1, lock_key, owner)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("telegram polling retry type=%s", type(exc).__name__)
            await asyncio.sleep(5)


async def run_worker():
    if settings.telegram_bot_token:
        await poll()
