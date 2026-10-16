"""Сохранённый отчёт клиенту: настройки (период, разделы, исключения, план) и снимок цифр на момент сборки.

Снимок не пересчитывается: клиент по ссылке видит ровно то, что отправили, даже если данные потом поправят.
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ClientReport(Base):
    __tablename__ = "client_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    token: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    # Клиент видит в портале только опубликованные отчёты; черновики видит только StepToLead.
    published: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("portal_users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
