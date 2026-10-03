"""Telephony: a cloud PBX connected to a project (Mango Office first) and the call log."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TelephonyConnection(Base):
    __tablename__ = "telephony_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    provider: Mapped[str] = mapped_column(String(24))  # mango
    name: Mapped[str] = mapped_column(String(180))
    # Secret part of the webhook address given to the PBX: /api/telephony/<provider>/<public_id>
    public_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)   # Mango: "уникальный код АТС" (vpbx_api_key)
    api_salt_encrypted: Mapped[str | None] = mapped_column(Text)  # Mango: "ключ для создания подписи"
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")  # pending | connected | error
    last_error: Mapped[str | None] = mapped_column(Text)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # {pbx_users: [{extension, name, numbers}], user_map: {"101": portal_user_id}, create_leads, missed_task_minutes}
    config: Mapped[dict | None] = mapped_column(JSON, default=dict)
    inbound_source_id: Mapped[int | None] = mapped_column(ForeignKey("lead_inbound_sources.id"))
    retention_days: Mapped[int] = mapped_column(default=90)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Call(Base):
    __tablename__ = "calls"
    __table_args__ = (UniqueConstraint("connection_id", "entry_id", name="uq_call_entry"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("telephony_connections.id", ondelete="CASCADE"), index=True)
    entry_id: Mapped[str] = mapped_column(String(180))           # provider call group id
    direction: Mapped[str] = mapped_column(String(8), default="in")  # in | out
    # ringing | talking → answered | missed (final, after the PBX summary)
    status: Mapped[str] = mapped_column(String(12), default="ringing", index=True)
    client_phone: Mapped[str | None] = mapped_column(String(32), index=True)
    line_number: Mapped[str | None] = mapped_column(String(32))
    extension: Mapped[str | None] = mapped_column(String(32))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("portal_users.id", ondelete="SET NULL"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_sec: Mapped[int] = mapped_column(default=0)   # talk time
    wait_sec: Mapped[int] = mapped_column(default=0)       # until answer (or hang-up when missed)
    disconnect_reason: Mapped[str | None] = mapped_column(String(32))
    recording_id: Mapped[str | None] = mapped_column(String(255))
    recording_path: Mapped[str | None] = mapped_column(String(500))
    recording_error: Mapped[str | None] = mapped_column(String(300))
    recording_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("crm_contacts.id", ondelete="SET NULL"), index=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("crm_deals.id", ondelete="SET NULL"), index=True)
    inbound_id: Mapped[int | None] = mapped_column(ForeignKey("crm_inbound.id", ondelete="SET NULL"), index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("crm_tasks.id", ondelete="SET NULL"))
    processed: Mapped[bool] = mapped_column(Boolean, default=False)  # summary handled: CRM links, task, timeline
    # Future AI: transcript, summary, script score.
    meta: Mapped[dict | None] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
