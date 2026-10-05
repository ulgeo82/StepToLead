from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, Float, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class ClientBrief(Base):
    __tablename__ = "client_briefs"
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("client_workspaces.id"), index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("crm_inbound.id"), index=True)
    demo_workspace_id: Mapped[int | None] = mapped_column(ForeignKey("client_workspaces.id"), index=True)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    preview: Mapped[dict | None] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(16), default="manual")
    transcript: Mapped[str | None] = mapped_column(Text)
    ai_fields: Mapped[list | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    ip_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ExpressAssessment(Base):
    __tablename__ = "express_assessments"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("crm_inbound.id"), index=True)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    limit_cpl: Mapped[float | None] = mapped_column(Float)
    current_cpl: Mapped[float | None] = mapped_column(Float)
    utm: Mapped[dict] = mapped_column(JSON, default=dict)
    ip_hash: Mapped[str] = mapped_column(String(64), index=True)
    demo_workspace_id: Mapped[int | None] = mapped_column(ForeignKey("client_workspaces.id"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
