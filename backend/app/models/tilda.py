"""Tilda connection metadata and delivery idempotency, not a second lead store."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TildaConnection(Base):
    __tablename__ = "tilda_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    secret_hash: Mapped[str] = mapped_column(String(64))
    organization_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    website_id: Mapped[int] = mapped_column(ForeignKey("website_sites.id", ondelete="CASCADE"), index=True)
    inbound_source_id: Mapped[int] = mapped_column(ForeignKey("lead_inbound_sources.id"), unique=True)
    allowed_form_id: Mapped[str] = mapped_column(String(100), default="form3645799701")
    form_name: Mapped[str] = mapped_column(String(180), default="Рассчитаем потенциал продвижения")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TildaReceipt(Base):
    __tablename__ = "tilda_receipts"
    __table_args__ = (UniqueConstraint("tilda_connection_id", "tranid", name="uq_tilda_connection_tranid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tilda_connection_id: Mapped[int] = mapped_column(ForeignKey("tilda_connections.id", ondelete="CASCADE"), index=True)
    tranid: Mapped[str] = mapped_column(String(180))
    inbound_id: Mapped[int] = mapped_column(ForeignKey("crm_inbound.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
