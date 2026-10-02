"""Omnichannel inbox: channels (Avito, Telegram bot, WhatsApp), conversations and messages."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MessagingChannel(Base):
    __tablename__ = "messaging_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(24))          # avito | telegram_bot | whatsapp
    name: Mapped[str] = mapped_column(String(180))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")  # pending | connected | error
    last_error: Mapped[str | None] = mapped_column(Text)
    # Non-secret settings and cursors: {connection_id, api_url, instance_id, bot_username, offset, since}
    config: Mapped[dict | None] = mapped_column(JSON, default=dict)
    secret_encrypted: Mapped[str | None] = mapped_column(Text)  # bot token / API token, encrypted
    inbound_source_id: Mapped[int | None] = mapped_column(ForeignKey("lead_inbound_sources.id"))
    # Future AI autoresponder: off | suggest | auto (see docs); kept here so channels can opt in per channel.
    ai_mode: Mapped[str] = mapped_column(String(12), default="off")
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("channel_id", "external_chat_id", name="uq_conversation_channel_chat"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("messaging_channels.id", ondelete="CASCADE"), index=True)
    external_chat_id: Mapped[str] = mapped_column(String(180))
    title: Mapped[str] = mapped_column(String(180))
    phone: Mapped[str | None] = mapped_column(String(32))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("crm_contacts.id", ondelete="SET NULL"), index=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("crm_deals.id", ondelete="SET NULL"), index=True)
    inbound_id: Mapped[int | None] = mapped_column(ForeignKey("crm_inbound.id", ondelete="SET NULL"), index=True)
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("portal_users.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)  # open | closed
    unread_count: Mapped[int] = mapped_column(default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_message_preview: Mapped[str | None] = mapped_column(String(300))
    last_direction: Mapped[str | None] = mapped_column(String(8))
    # Set on the first client message that has not been answered yet: drives "ждёт ответа" and SLA.
    waiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    ai_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict | None] = mapped_column(JSON, default=dict)  # item, profile url, username...
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("conversation_id", "external_id", name="uq_message_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    direction: Mapped[str] = mapped_column(String(8))  # in | out | system
    text: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[list | None] = mapped_column(JSON, default=list)
    external_id: Mapped[str | None] = mapped_column(String(180))
    author_user_id: Mapped[int | None] = mapped_column(ForeignKey("portal_users.id", ondelete="SET NULL"))
    author_name: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(12), default="received")  # received | sent | failed
    error: Mapped[str | None] = mapped_column(String(500))
    is_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReplyTemplate(Base):
    __tablename__ = "reply_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(120))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
