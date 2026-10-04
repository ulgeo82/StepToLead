"""Inbox API: channels, conversations, messages, quick-reply templates."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.crm import deal_for, project_for
from app.core.access import check_origin, require_portal_user
from app.core.crypto import encrypt_secret
from app.core.permissions import effective_permissions, require_permission
from app.db import get_db
from app.models.crm import CrmContact, CrmDeal
from app.models.marketing import AdConnection, PortalUser
from app.models.messaging import Conversation, Message, MessagingChannel, ReplyTemplate
from app.services import plans, ai, avito, messaging

router = APIRouter(prefix="/crm", tags=["messaging"], dependencies=[Depends(require_portal_user)])


def can_manage_channels(user: PortalUser) -> bool:
    permissions = effective_permissions(user)
    return bool({"manage_pipeline", "manage_integrations", "manage_settings"} & permissions)


def channel_json(row: MessagingChannel, unread: int = 0) -> dict:
    data = row.config or {}
    return {"id": row.id, "kind": row.kind, "kind_name": messaging.KINDS.get(row.kind, row.kind), "name": row.name,
            "active": row.active, "status": row.status, "last_error": row.last_error, "ai_mode": row.ai_mode,
            "bot_username": data.get("bot_username"), "instance_id": data.get("instance_id"),
            "api_url": data.get("api_url"), "connection_id": data.get("connection_id"),
            "last_polled_at": row.last_polled_at, "unread": unread}


def conversation_json(row: Conversation, channel: MessagingChannel | None = None, contact_name: str | None = None,
                      deal_name: str | None = None, assigned_name: str | None = None) -> dict:
    return {"id": row.id, "channel_id": row.channel_id, "channel_kind": channel.kind if channel else None,
            "channel_name": channel.name if channel else None, "title": row.title, "phone": row.phone,
            "contact_id": row.contact_id, "contact_name": contact_name, "deal_id": row.deal_id, "deal_name": deal_name,
            "inbound_id": row.inbound_id, "assigned_user_id": row.assigned_user_id, "assigned_name": assigned_name,
            "status": row.status, "unread_count": row.unread_count, "last_message_at": row.last_message_at,
            "last_message_preview": row.last_message_preview, "last_direction": row.last_direction,
            "waiting_since": row.waiting_since, "meta": row.meta or {}}


def message_json(row: Message) -> dict:
    return {"id": row.id, "direction": row.direction, "text": row.text, "author_name": row.author_name,
            "status": row.status, "error": row.error, "is_ai": row.is_ai, "sent_at": row.sent_at}


async def conversation_for(db: AsyncSession, user: PortalUser, conversation_id: int) -> Conversation:
    row = await db.get(Conversation, conversation_id)
    if not row or row.workspace_id != user.workspace_id:
        raise HTTPException(404, "Диалог не найден")
    await project_for(db, user, row.project_id)
    if "view_all_deals" not in effective_permissions(user):
        deal_owner = (await db.get(CrmDeal, row.deal_id)).responsible_user_id if row.deal_id else None
        if row.assigned_user_id not in (None, user.id) and deal_owner != user.id:
            raise HTTPException(403, "Диалог назначен другому сотруднику")
    return row


def visible(query, user: PortalUser):
    if "view_all_deals" in effective_permissions(user):
        return query
    own_deals = select(CrmDeal.id).where(CrmDeal.responsible_user_id == user.id)
    return query.where(or_(Conversation.assigned_user_id == user.id, Conversation.assigned_user_id.is_(None),
                           Conversation.deal_id.in_(own_deals)))


# --------------------------------------------------------------------------- channels

class ChannelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    name: str = Field(min_length=2, max_length=180)
    token: str | None = Field(default=None, max_length=500)
    instance_id: str | None = Field(default=None, max_length=40)
    api_url: str | None = Field(default=None, max_length=200)
    connection_id: int | None = None


class ChannelPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=180)
    active: bool | None = None
    token: str | None = Field(default=None, max_length=500)


def reject_notification_token(kind: str, token: str | None):
    import secrets
    from app.core.config import settings
    if kind == "telegram_bot" and settings.telegram_bot_token and secrets.compare_digest(
            (token or "").strip().encode(), settings.telegram_bot_token.encode()):
        raise HTTPException(422, "Служебный бот StepToLead нельзя подключить как канал переписки")


@router.get("/projects/{project_id}/channels")
async def list_channels(project_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm"); await project_for(db, user, project_id)
    rows = (await db.scalars(select(MessagingChannel).where(MessagingChannel.project_id == project_id)
                             .order_by(MessagingChannel.id))).all()
    unread = dict((await db.execute(visible(select(Conversation.channel_id, func.sum(Conversation.unread_count)).where(
        Conversation.project_id == project_id), user).group_by(Conversation.channel_id))).all())
    avito_cabinets = (await db.scalars(select(AdConnection).where(AdConnection.project_id == project_id,
                                                                  AdConnection.platform == avito.ITEMS))).all()
    return {"channels": [channel_json(r, int(unread.get(r.id) or 0)) for r in rows], "can_manage": can_manage_channels(user),
            "avito_cabinets": [{"id": c.id, "name": c.name, "status": c.status} for c in avito_cabinets]}


@router.post("/projects/{project_id}/channels", status_code=201)
async def create_channel(project_id: int, payload: ChannelIn, request: Request, db: AsyncSession = Depends(get_db),
                         user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    if not can_manage_channels(user):
        raise HTTPException(403, "Подключать каналы может руководитель или владелец")
    project = await project_for(db, user, project_id)
    if payload.kind not in messaging.KINDS:
        raise HTTPException(422, "Неизвестный тип канала")
    if payload.kind == "telegram_bot":
        reject_notification_token(payload.kind, payload.token)
        await plans.require(db, project.workspace_id, "all_chats")
    config: dict = {}
    if payload.kind == "avito":
        cabinet = await db.get(AdConnection, payload.connection_id or 0)
        if not cabinet or cabinet.project_id != project.id or cabinet.platform != avito.ITEMS:
            raise HTTPException(422, "Выберите подключённый кабинет «Авито · Объявления»")
        if await db.scalar(select(MessagingChannel.id).where(MessagingChannel.project_id == project.id,
                                                            MessagingChannel.kind == "avito")):
            existing = (await db.scalars(select(MessagingChannel).where(MessagingChannel.project_id == project.id,
                                                                       MessagingChannel.kind == "avito"))).all()
            if any((c.config or {}).get("connection_id") == cabinet.id for c in existing):
                raise HTTPException(409, "Чаты этого кабинета Авито уже подключены")
        config = {"connection_id": cabinet.id, "since": int(datetime.now(timezone.utc).timestamp()) - 3600}
    elif not payload.token or len(payload.token.strip()) < 10:
        raise HTTPException(422, "Укажите токен")
    if payload.kind == "whatsapp":
        if not (payload.instance_id or "").strip().isdigit():
            raise HTTPException(422, "Укажите idInstance GREEN-API (число)")
        api_url = (payload.api_url or "https://api.green-api.com").strip().rstrip("/")
        if not api_url.startswith("https://"):
            raise HTTPException(422, "apiUrl должен начинаться с https://")
        config = {"instance_id": payload.instance_id.strip(), "api_url": api_url}
    channel = MessagingChannel(workspace_id=project.workspace_id, project_id=project.id, kind=payload.kind,
                               name=payload.name.strip(), config=config, status="pending", active=True,
                               secret_encrypted=encrypt_secret(payload.token.strip()) if payload.token else None)
    db.add(channel); await db.flush()
    try:
        details = await messaging.adapter(channel, db).test()
        channel.status = "connected"
    except (messaging.ChannelError, avito.AvitoError) as exc:
        channel.status, channel.last_error, details = "error", str(exc)[:500], None
    await db.commit()
    return {**channel_json(channel), "details": details}


@router.patch("/channels/{channel_id}")
async def edit_channel(channel_id: int, payload: ChannelPatch, request: Request, db: AsyncSession = Depends(get_db),
                       user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    channel = await db.get(MessagingChannel, channel_id)
    if not channel or channel.workspace_id != user.workspace_id:
        raise HTTPException(404, "Канал не найден")
    await project_for(db, user, channel.project_id)
    if not can_manage_channels(user):
        raise HTTPException(403, "Настраивать каналы может руководитель или владелец")
    reject_notification_token(channel.kind, payload.token)
    if payload.name: channel.name = payload.name.strip()
    if payload.active is not None: channel.active = payload.active
    if payload.token: channel.secret_encrypted = encrypt_secret(payload.token.strip())
    if payload.token or payload.active:
        try:
            await messaging.adapter(channel, db).test()
            channel.status, channel.last_error = "connected", None
        except (messaging.ChannelError, avito.AvitoError) as exc:
            channel.status, channel.last_error = "error", str(exc)[:500]
    await db.commit()
    return channel_json(channel)


@router.post("/channels/{channel_id}/sync")
async def sync_channel(channel_id: int, request: Request, db: AsyncSession = Depends(get_db),
                       user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    channel = await db.get(MessagingChannel, channel_id)
    if not channel or channel.workspace_id != user.workspace_id:
        raise HTTPException(404, "Канал не найден")
    await project_for(db, user, channel.project_id)
    count = await messaging.poll_channel(db, channel)
    channel = await db.get(MessagingChannel, channel_id)
    if channel.status == "error":
        raise HTTPException(422, channel.last_error or "Канал недоступен")
    return {"new_messages": count}


# --------------------------------------------------------------------------- conversations

@router.get("/projects/{project_id}/conversations")
async def list_conversations(project_id: int, filter: str = "all", channel_id: int | None = None,
                             search: str | None = None, status: str = "open", limit: int = Query(60, ge=1, le=200),
                             db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm"); await project_for(db, user, project_id)
    query = visible(select(Conversation).where(Conversation.project_id == project_id), user)
    if status in {"open", "closed"}: query = query.where(Conversation.status == status)
    if channel_id: query = query.where(Conversation.channel_id == channel_id)
    if filter == "mine": query = query.where(Conversation.assigned_user_id == user.id)
    if filter == "unread": query = query.where(Conversation.unread_count > 0)
    if filter == "waiting": query = query.where(Conversation.waiting_since.is_not(None))
    if filter == "unassigned": query = query.where(Conversation.assigned_user_id.is_(None))
    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.where(or_(Conversation.title.ilike(term), Conversation.phone.ilike(term),
                                Conversation.last_message_preview.ilike(term)))
    rows = (await db.scalars(query.order_by(Conversation.last_message_at.desc().nulls_last(), Conversation.id.desc())
                             .limit(limit))).all()
    channels = {c.id: c for c in (await db.scalars(select(MessagingChannel).where(MessagingChannel.project_id == project_id))).all()}
    contacts = {c.id: c.name for c in (await db.scalars(select(CrmContact).where(CrmContact.id.in_([r.contact_id for r in rows if r.contact_id])))).all()} if rows else {}
    deals = {d.id: d.name for d in (await db.scalars(select(CrmDeal).where(CrmDeal.id.in_([r.deal_id for r in rows if r.deal_id])))).all()} if rows else {}
    people = {u.id: u.display_name for u in (await db.scalars(select(PortalUser).where(PortalUser.workspace_id == user.workspace_id))).all()}
    counts = {}
    for key, cond in {"unread": Conversation.unread_count > 0, "waiting": Conversation.waiting_since.is_not(None),
                      "unassigned": Conversation.assigned_user_id.is_(None)}.items():
        counts[key] = await db.scalar(select(func.count()).select_from(visible(select(Conversation.id).where(
            Conversation.project_id == project_id, Conversation.status == "open", cond), user).subquery())) or 0
    return {"items": [conversation_json(r, channels.get(r.channel_id), contacts.get(r.contact_id), deals.get(r.deal_id),
                                        people.get(r.assigned_user_id)) for r in rows], "counts": counts}


@router.get("/conversations/{conversation_id}")
async def conversation_detail(conversation_id: int, before_id: int | None = None, db: AsyncSession = Depends(get_db),
                              user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm")
    row = await conversation_for(db, user, conversation_id)
    query = select(Message).where(Message.conversation_id == row.id)
    if before_id: query = query.where(Message.id < before_id)
    messages = list(reversed((await db.scalars(query.order_by(Message.sent_at.desc(), Message.id.desc()).limit(100))).all()))
    channel = await db.get(MessagingChannel, row.channel_id)
    if row.unread_count:
        row.unread_count = 0
        if channel and channel.kind == "avito":
            try:
                await messaging.AvitoMessenger(channel, db).mark_read(row)
            except Exception:
                pass  # reading state on Avito is a courtesy, never block the inbox
        await db.commit()
    contact = await db.get(CrmContact, row.contact_id) if row.contact_id else None
    deal = await db.get(CrmDeal, row.deal_id) if row.deal_id else None
    owner = await db.get(PortalUser, row.assigned_user_id) if row.assigned_user_id else None
    return {**conversation_json(row, channel, contact.name if contact else None, deal.name if deal else None,
                                owner.display_name if owner else None),
            "messages": [message_json(m) for m in messages], "has_more": len(messages) == 100,
            "ai_available": ai.configured() and await plans.has(db, row.workspace_id, "ai_chat")}


class SendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4096)


@router.post("/conversations/{conversation_id}/messages", status_code=201)
async def send_message(conversation_id: int, payload: SendIn, request: Request, db: AsyncSession = Depends(get_db),
                       user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "edit_deal")
    row = await conversation_for(db, user, conversation_id)
    try:
        message = await messaging.send(db, row, payload.text, user)
    except messaging.ChannelError as exc:
        raise HTTPException(422, str(exc)) from None
    return message_json(message)


class ConversationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assigned_user_id: int | None = None
    status: str | None = None
    deal_id: int | None = None


@router.patch("/conversations/{conversation_id}")
async def edit_conversation(conversation_id: int, payload: ConversationPatch, request: Request,
                            db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "edit_deal")
    row = await conversation_for(db, user, conversation_id)
    changes = payload.model_dump(exclude_unset=True)
    if "assigned_user_id" in changes:
        from app.api.routes.crm import validate_owner
        await validate_owner(db, row.project_id, row.workspace_id, changes["assigned_user_id"])
        row.assigned_user_id = changes["assigned_user_id"]
    if "status" in changes:
        if changes["status"] not in {"open", "closed"}:
            raise HTTPException(422, "Неверный статус")
        row.status = changes["status"]
        if row.status == "closed":
            row.waiting_since = None
            row.unread_count = 0
    if "deal_id" in changes:
        deal = await deal_for(db, user, changes["deal_id"]) if changes["deal_id"] else None
        if deal and deal.project_id != row.project_id:
            raise HTTPException(422, "Сделка из другого проекта")
        row.deal_id = deal.id if deal else None
        if deal:
            row.contact_id = deal.contact_id
    await db.commit()
    return {"ok": True}


@router.get("/deals/{deal_id}/conversations")
async def deal_conversations(deal_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm")
    deal = await deal_for(db, user, deal_id)
    rows = (await db.scalars(select(Conversation).where(Conversation.project_id == deal.project_id, or_(
        Conversation.deal_id == deal.id, Conversation.contact_id == deal.contact_id)).order_by(
        Conversation.last_message_at.desc().nulls_last()))).all()
    channels = {c.id: c for c in (await db.scalars(select(MessagingChannel).where(MessagingChannel.project_id == deal.project_id))).all()}
    return [conversation_json(r, channels.get(r.channel_id)) for r in rows]


# --------------------------------------------------------------------------- quick replies

class TemplateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=4000)


@router.get("/projects/{project_id}/reply-templates")
async def list_templates(project_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm"); await project_for(db, user, project_id)
    rows = (await db.scalars(select(ReplyTemplate).where(ReplyTemplate.project_id == project_id).order_by(ReplyTemplate.title))).all()
    return [{"id": r.id, "title": r.title, "text": r.text} for r in rows]


@router.post("/projects/{project_id}/reply-templates", status_code=201)
async def create_template(project_id: int, payload: TemplateIn, request: Request, db: AsyncSession = Depends(get_db),
                          user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "edit_deal")
    project = await project_for(db, user, project_id)
    row = ReplyTemplate(workspace_id=project.workspace_id, project_id=project.id, title=payload.title.strip(), text=payload.text.strip())
    db.add(row); await db.commit()
    return {"id": row.id, "title": row.title, "text": row.text}


@router.delete("/reply-templates/{template_id}", status_code=204)
async def delete_template(template_id: int, request: Request, db: AsyncSession = Depends(get_db),
                          user: PortalUser = Depends(require_portal_user)):
    check_origin(request); require_permission(user, "edit_deal")
    row = await db.get(ReplyTemplate, template_id)
    if not row or row.workspace_id != user.workspace_id:
        raise HTTPException(404, "Шаблон не найден")
    await project_for(db, user, row.project_id)
    await db.delete(row); await db.commit()


class StartChat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel_id: int


@router.post("/deals/{deal_id}/conversations", status_code=201)
async def start_conversation(deal_id: int, payload: StartChat, request: Request, db: AsyncSession = Depends(get_db),
                             user: PortalUser = Depends(require_portal_user)):
    """Start a WhatsApp chat with the deal's contact (WhatsApp allows writing first; Telegram bots and Avito do not)."""
    check_origin(request); require_permission(user, "edit_deal")
    deal = await deal_for(db, user, deal_id)
    channel = await db.get(MessagingChannel, payload.channel_id)
    if not channel or channel.project_id != deal.project_id or not channel.active:
        raise HTTPException(404, "Канал не найден")
    if channel.kind != "whatsapp":
        raise HTTPException(422, "Первым можно написать только в WhatsApp")
    contact = await db.get(CrmContact, deal.contact_id)
    digits = messaging.norm_phone((contact.phones or [None])[0])
    if len(digits) < 10:
        raise HTTPException(422, "У клиента нет телефона")
    chat_id = f"{digits}@c.us"
    row = await db.scalar(select(Conversation).where(Conversation.channel_id == channel.id, Conversation.external_chat_id == chat_id))
    if row is None:
        row = Conversation(workspace_id=deal.workspace_id, project_id=deal.project_id, channel_id=channel.id, external_chat_id=chat_id,
                           title=contact.name, phone=f"+{digits}", contact_id=contact.id, deal_id=deal.id,
                           assigned_user_id=deal.responsible_user_id or user.id, status="open", unread_count=0, meta={})
        db.add(row)
    else:
        row.deal_id, row.contact_id = deal.id, contact.id
    await db.commit()
    return conversation_json(row, channel)


# --------------------------------------------------------------------------- AI assistant

def ai_settings(project) -> dict:
    data = dict((project.portal_state or {}).get("ai") or {})
    return {"knowledge": data.get("knowledge") or "", "tone": data.get("tone") or "", "goal": data.get("goal") or "",
            "calls": data.get("calls", True) is not False, "call_checklist": data.get("call_checklist") or ""}


async def ai_context(db: AsyncSession, row: Conversation):
    from app.models.crm import CrmStage
    from app.models.marketing import Project
    project = await db.get(Project, row.project_id)
    channel = await db.get(MessagingChannel, row.channel_id)
    messages = list(reversed((await db.scalars(select(Message).where(Message.conversation_id == row.id)
                                               .order_by(Message.sent_at.desc(), Message.id.desc()).limit(30))).all()))
    deal_line = None
    if row.deal_id:
        deal = await db.get(CrmDeal, row.deal_id)
        stage = await db.get(CrmStage, deal.stage_id) if deal else None
        if deal:
            deal_line = f"«{deal.name}», этап «{stage.name if stage else '—'}»" + (f", бюджет {float(deal.amount):,.0f} ₽".replace(",", " ") if deal.amount else "")
    return project, channel, messages, deal_line


@router.post("/conversations/{conversation_id}/ai/suggest")
async def ai_suggest(conversation_id: int, request: Request, db: AsyncSession = Depends(get_db),
                     user: PortalUser = Depends(require_portal_user)):
    """Draft of the next reply. The manager edits and sends it; nothing goes to the client automatically."""
    check_origin(request); require_permission(user, "view_crm")
    row = await conversation_for(db, user, conversation_id)
    await plans.require(db, row.workspace_id, "ai_chat")
    project, channel, messages, deal_line = await ai_context(db, row)
    if not messages:
        raise HTTPException(422, "В диалоге ещё нет сообщений")
    limit = 1000 if channel and channel.kind == "avito" else 1500
    try:
        text = await ai.complete(ai.suggest_prompt(project.name, messaging.KINDS.get(channel.kind, "чате") if channel else "чате",
                                                   ai_settings(project), deal_line, limit),
                                 ai.transcript(messages, row.title), max_tokens=400)
    except ai.AIError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"text": text[:limit]}


class SummaryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    save: bool = False


@router.post("/conversations/{conversation_id}/ai/summary")
async def ai_summary(conversation_id: int, payload: SummaryIn, request: Request, db: AsyncSession = Depends(get_db),
                     user: PortalUser = Depends(require_portal_user)):
    from app.api.routes.crm import activity
    check_origin(request); require_permission(user, "view_crm")
    row = await conversation_for(db, user, conversation_id)
    await plans.require(db, row.workspace_id, "ai_chat")
    _, _, messages, _ = await ai_context(db, row)
    if not messages:
        raise HTTPException(422, "В диалоге ещё нет сообщений")
    lines = "\n".join(f"{'Клиент' if m.direction == 'in' else 'Менеджер'}: {m.text}" for m in messages if m.text)
    try:
        text = await ai.complete(ai.SUMMARY_PROMPT, [{"role": "user", "content": lines[-12000:]}], max_tokens=500, temperature=0.1)
    except ai.AIError as exc:
        raise HTTPException(422, str(exc)) from None
    saved = False
    if payload.save and row.deal_id:
        deal = await db.get(CrmDeal, row.deal_id)
        activity(db, deal, user, "COMMENT_ADDED", {"text": f"Резюме переписки (ИИ):\n{text}"}, touch=False)
        await db.commit(); saved = True
    return {"text": text, "saved": saved}


@router.get("/projects/{project_id}/ai-settings")
async def get_ai_settings(project_id: int, db: AsyncSession = Depends(get_db), user: PortalUser = Depends(require_portal_user)):
    require_permission(user, "view_crm")
    project = await project_for(db, user, project_id)
    from app.services import call_ai
    return {**ai_settings(project), "configured": ai.configured(), "provider": ai.provider_name(),
            "calls_available": call_ai.available() and await plans.has(db, project.workspace_id, "ai_calls"),
            "chat_allowed": await plans.has(db, project.workspace_id, "ai_chat"), "default_checklist": "\n".join(call_ai.DEFAULT_CHECKLIST),
            "can_manage": can_manage_channels(user)}


class AISettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    knowledge: str = Field(default="", max_length=8000)
    tone: str = Field(default="", max_length=300)
    goal: str = Field(default="", max_length=300)
    calls: bool = True
    call_checklist: str = Field(default="", max_length=2000)


@router.put("/projects/{project_id}/ai-settings")
async def put_ai_settings(project_id: int, payload: AISettingsIn, request: Request, db: AsyncSession = Depends(get_db),
                          user: PortalUser = Depends(require_portal_user)):
    check_origin(request)
    project = await project_for(db, user, project_id)
    if not can_manage_channels(user):
        raise HTTPException(403, "Базу знаний меняет руководитель или владелец")
    project.portal_state = {**(project.portal_state or {}), "ai": {
        k: v.strip() if isinstance(v, str) else v for k, v in payload.model_dump().items()}}
    await db.commit()
    return await get_ai_settings(project_id, db, user)
