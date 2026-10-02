"""Digital pipeline: rules that react to deals entering a stage, being created, or going idle.

Triggers
* DEAL_CREATED  – a new deal appears in the pipeline (manual, accepted request, import).
* STAGE_ENTER   – a deal enters the rule's stage.
* NO_ACTIVITY   – a deal sits in the rule's stage (or any open stage) without activity for delay_minutes.

Actions
* CREATE_TASK     – {type_code, title, due_minutes, priority, responsible: "owner" | user_id}
* SET_RESPONSIBLE – {mode: "user" | "round_robin", user_id, user_ids}
* ADD_TAG         – {tag}
* NOTIFY          – {text, to: "owner" | "heads" | "owner_and_heads"}

A rule fires at most once per stage entry (deal.automation_state[rule_id] = stage_entered_at),
so a deal that leaves and re-enters the stage is handled again — like amoCRM.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crm import CrmAutomation, CrmDeal, CrmStage, CrmTask
from app.models.marketing import PortalProjectAccess, PortalUser, Project
from app.services.notifications import direct as notify_direct, flush_telegram

logger = logging.getLogger("uvicorn.error.crm_automation")
TRIGGERS = {"DEAL_CREATED", "STAGE_ENTER", "NO_ACTIVITY"}
ACTIONS = {"CREATE_TASK", "SET_RESPONSIBLE", "ADD_TAG", "NOTIFY"}
TASK_TYPES = {"CALL", "MEETING", "MESSAGE", "SEND", "FOLLOW_UP", "OTHER"}
IDLE_POLL_SECONDS = 600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def entry_key(deal: CrmDeal) -> str:
    return (_aware(deal.stage_entered_at) or _aware(deal.created_at) or _now()).isoformat()


def normalize_tags(values) -> list[str]:
    result: list[str] = []
    for value in values or []:
        tag = " ".join(str(value).split())[:40]
        if tag and tag.casefold() not in {t.casefold() for t in result}:
            result.append(tag)
    return result[:20]


def matches(rule: CrmAutomation, deal: CrmDeal) -> bool:
    conditions = rule.conditions or {}
    if conditions.get("source_id") and deal.source_id != int(conditions["source_id"]):
        return False
    if conditions.get("origin") and deal.origin != conditions["origin"]:
        return False
    if conditions.get("min_amount") is not None and float(deal.amount or 0) < float(conditions["min_amount"]):
        return False
    if conditions.get("tag") and conditions["tag"].casefold() not in {t.casefold() for t in deal.tags or []}:
        return False
    return True


async def project_users(db: AsyncSession, project_id: int) -> dict[int, PortalUser]:
    project = await db.get(Project, project_id)
    rows = (await db.scalars(select(PortalUser).where(
        PortalUser.workspace_id == project.workspace_id, PortalUser.active.is_(True),
        or_(PortalUser.role == "client_owner", PortalUser.id.in_(
            select(PortalProjectAccess.user_id).where(PortalProjectAccess.project_id == project_id)))))).all()
    return {user.id: user for user in rows}


def describe(rule: CrmAutomation) -> str:
    params = rule.params or {}
    if rule.action == "CREATE_TASK":
        return f"Задача «{params.get('title') or 'Задача'}»"
    if rule.action == "SET_RESPONSIBLE":
        return "Назначен ответственный" if params.get("mode") != "round_robin" else "Ответственный по очереди"
    if rule.action == "ADD_TAG":
        return f"Тег «{params.get('tag')}»"
    return "Уведомление"


async def execute(db: AsyncSession, rule: CrmAutomation, deal: CrmDeal) -> str | None:
    """Apply one rule to one deal. Returns a human description, or None when skipped."""
    from app.api.routes.crm import activity  # routes own the activity helper
    params = rule.params or {}
    users = await project_users(db, deal.project_id)
    text = None
    if rule.action == "CREATE_TASK":
        title = str(params.get("title") or "Связаться с клиентом")[:220]
        exists = await db.scalar(select(CrmTask.id).where(CrmTask.deal_id == deal.id, CrmTask.status == "OPEN",
                                                         CrmTask.title == title).limit(1))
        if exists:
            return None
        owner = params.get("responsible")
        owner_id = deal.responsible_user_id if owner in (None, "owner") else int(owner)
        if owner_id not in users:
            owner_id = deal.responsible_user_id if deal.responsible_user_id in users else None
        due = _now() + timedelta(minutes=max(0, int(params.get("due_minutes") or 15)))
        db.add(CrmTask(workspace_id=deal.workspace_id, project_id=deal.project_id, deal_id=deal.id,
                       contact_id=deal.contact_id, type_code=params.get("type_code") if params.get("type_code") in TASK_TYPES else "CALL",
                       title=title, description=params.get("description"), responsible_user_id=owner_id,
                       due_at=due, priority=params.get("priority") if params.get("priority") in {"LOW", "NORMAL", "HIGH"} else "NORMAL"))
        text = f"Создана задача «{title}»"
    elif rule.action == "SET_RESPONSIBLE":
        candidates = [int(uid) for uid in (params.get("user_ids") or []) if int(uid) in users]
        if params.get("mode") == "round_robin" and candidates:
            index = int(params.get("rr_index") or 0) % len(candidates)
            target = candidates[index]
            rule.params = {**params, "rr_index": index + 1}
        else:
            target = int(params["user_id"]) if params.get("user_id") and int(params["user_id"]) in users else None
        if target is None or target == deal.responsible_user_id:
            return None
        deal.responsible_user_id = target
        from app.models.marketing import ClientLead
        lead = await db.get(ClientLead, deal.lead_id) if deal.lead_id else None
        if lead:
            lead.assigned_to_id = target
        text = f"Ответственный: {users[target].display_name}"
        notify_direct(db, deal.workspace_id, [target], "Вам назначена сделка", deal.name, users)
    elif rule.action == "ADD_TAG":
        tag = normalize_tags([params.get("tag")])
        if not tag or tag[0].casefold() in {t.casefold() for t in deal.tags or []}:
            return None
        deal.tags = normalize_tags([*(deal.tags or []), tag[0]])
        text = f"Добавлен тег «{tag[0]}»"
    elif rule.action == "NOTIFY":
        to = params.get("to") or "owner"
        recipients = set()
        if to in {"owner", "owner_and_heads"} and deal.responsible_user_id in users:
            recipients.add(deal.responsible_user_id)
        if to in {"heads", "owner_and_heads"} or not recipients:
            recipients |= {uid for uid, user in users.items() if user.role in {"client_owner", "sales_head"}}
        body = str(params.get("text") or rule.name)[:500]
        notify_direct(db, deal.workspace_id, sorted(recipients), body, deal.name, users)
        text = f"Уведомление: {body}"
    if text:
        activity(db, deal, None, "AUTOMATION", {"rule_id": rule.id, "rule": rule.name, "text": text}, touch=False)
        rule.fired_count = int(rule.fired_count or 0) + 1
        rule.last_fired_at = _now()
    return text


def _mark(deal: CrmDeal, rule: CrmAutomation) -> None:
    deal.automation_state = {**(deal.automation_state or {}), str(rule.id): entry_key(deal)}


def _already(deal: CrmDeal, rule: CrmAutomation) -> bool:
    return (deal.automation_state or {}).get(str(rule.id)) == entry_key(deal)


async def on_stage_enter(db: AsyncSession, deal: CrmDeal, stage: CrmStage, *, created: bool = False) -> list[str]:
    """Run immediate rules for a deal that was just created or moved. Does not commit."""
    triggers = ["STAGE_ENTER"] + (["DEAL_CREATED"] if created else [])
    rules = (await db.scalars(select(CrmAutomation).where(
        CrmAutomation.pipeline_id == deal.pipeline_id, CrmAutomation.active.is_(True),
        CrmAutomation.trigger.in_(triggers)).order_by(CrmAutomation.id))).all()
    done = []
    for rule in rules:
        if rule.trigger == "STAGE_ENTER" and rule.stage_id != stage.id:
            continue
        if rule.trigger == "DEAL_CREATED" and rule.stage_id and rule.stage_id != stage.id:
            continue
        if _already(deal, rule) or not matches(rule, deal):
            continue
        result = await execute(db, rule, deal)
        _mark(deal, rule)
        if result:
            done.append(result)
    return done


async def run_idle_rules(db: AsyncSession, project_id: int | None = None) -> int:
    """Fire NO_ACTIVITY rules. Commits. Returns the number of actions performed."""
    query = select(CrmAutomation).where(CrmAutomation.active.is_(True), CrmAutomation.trigger == "NO_ACTIVITY")
    if project_id:
        query = query.where(CrmAutomation.project_id == project_id)
    fired = 0
    for rule in (await db.scalars(query)).all():
        threshold = _now() - timedelta(minutes=max(15, int(rule.delay_minutes or 0)))
        stages = (await db.scalars(select(CrmStage.id).where(
            CrmStage.pipeline_id == rule.pipeline_id, CrmStage.archived_at.is_(None),
            CrmStage.analytics_type.in_(["LEAD", "QUALIFIED"])))).all()
        stage_ids = [rule.stage_id] if rule.stage_id else list(stages)
        deals = (await db.scalars(select(CrmDeal).where(
            CrmDeal.pipeline_id == rule.pipeline_id, CrmDeal.stage_id.in_(stage_ids),
            CrmDeal.archived_at.is_(None), CrmDeal.last_activity_at <= threshold).limit(200))).all()
        for deal in deals:
            if _already(deal, rule) or not matches(rule, deal):
                continue
            if await execute(db, rule, deal):
                fired += 1
            _mark(deal, rule)
        await db.commit()
        flush_telegram(db)
    return fired


async def run_worker() -> None:
    from app.db import SessionLocal
    await asyncio.sleep(30)
    while True:
        try:
            async with SessionLocal() as db:
                await run_idle_rules(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("crm idle automation failed")
        await asyncio.sleep(IDLE_POLL_SECONDS)


def recommended(pipeline_id: int, stages: list[CrmStage]) -> list[dict]:
    """A sales-ops starter kit: speed-to-lead, next step after qualification, no-idle control."""
    by_type: dict[str, CrmStage] = {}
    for stage in sorted(stages, key=lambda s: (s.position or 0, s.id)):
        by_type.setdefault(stage.analytics_type, stage)
    lead, qualified, won = by_type.get("LEAD"), by_type.get("QUALIFIED"), by_type.get("WON")
    rules = [{"name": "Скорость реакции: позвонить в течение 15 минут", "trigger": "DEAL_CREATED", "stage_id": None,
              "action": "CREATE_TASK", "params": {"type_code": "CALL", "title": "Связаться с новым клиентом",
                                                  "due_minutes": 15, "priority": "HIGH", "responsible": "owner"}}]
    if lead:
        rules.append({"name": "Лид без движения сутки — сигнал ответственному", "trigger": "NO_ACTIVITY",
                      "stage_id": lead.id, "delay_minutes": 24 * 60, "action": "NOTIFY",
                      "params": {"to": "owner_and_heads", "text": "Лид без движения больше суток"}})
    if qualified:
        rules.append({"name": "После квалификации — отправить КП", "trigger": "STAGE_ENTER", "stage_id": qualified.id,
                      "action": "CREATE_TASK", "params": {"type_code": "SEND", "title": "Подготовить и отправить КП",
                                                          "due_minutes": 24 * 60, "responsible": "owner"}})
        rules.append({"name": "Квалифицированный клиент без контакта 3 дня — дожать", "trigger": "NO_ACTIVITY",
                      "stage_id": qualified.id, "delay_minutes": 3 * 24 * 60, "action": "CREATE_TASK",
                      "params": {"type_code": "FOLLOW_UP", "title": "Вернуть клиента в работу",
                                 "due_minutes": 120, "priority": "HIGH", "responsible": "owner"}})
    if won:
        rules.append({"name": "Выигранная сделка — подтвердить продажу", "trigger": "STAGE_ENTER", "stage_id": won.id,
                      "action": "NOTIFY", "params": {"to": "owner_and_heads",
                                                     "text": "Сделка выиграна — подтвердите сумму продажи"}})
    return [{**rule, "pipeline_id": pipeline_id} for rule in rules]
