"""Installation-wide settings and per-device push subscriptions."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("portal_users.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(String(1000), unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkspaceBot(Base):
    """Свой Telegram-бот клиента (токен от @BotFather): уведомления, отчёты и меню CRM под его брендом."""
    __tablename__ = "workspace_bots"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), unique=True, index=True)
    token_encrypted: Mapped[str] = mapped_column(String(512))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    bot_id: Mapped[str] = mapped_column(String(32))
    username: Mapped[str] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[str | None] = mapped_column(String(240))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
