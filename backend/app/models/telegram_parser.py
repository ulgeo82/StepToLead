from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TelegramParseTask(Base):
    __tablename__ = "telegram_parse_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("telegram_accounts.id", ondelete="CASCADE"), index=True)
    sources: Mapped[list[str]] = mapped_column(JSON)
    collect_messages: Mapped[bool] = mapped_column(Boolean, default=True)
    collect_members: Mapped[bool] = mapped_column(Boolean, default=True)
    collect_comments: Mapped[bool] = mapped_column(Boolean, default=True)
    extended_profile: Mapped[bool] = mapped_column(Boolean, default=False)
    per_source_limit: Mapped[int] = mapped_column(default=100)
    total_limit: Mapped[int] = mapped_column(default=5000)
    activity_filter: Mapped[str] = mapped_column(String(16), default="all")
    max_offline_days: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    collected_count: Mapped[int] = mapped_column(default=0)
    processed_sources: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TelegramParsedContact(Base):
    __tablename__ = "telegram_parsed_contacts"
    __table_args__ = (UniqueConstraint("task_id", "telegram_user_id", name="uq_parser_task_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("telegram_parse_tasks.id", ondelete="CASCADE"), index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    first_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    activity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(Text)
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    found_via: Mapped[str] = mapped_column(String(24))
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TelegramParseLog(Base):
    __tablename__ = "telegram_parse_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("telegram_parse_tasks.id", ondelete="CASCADE"), index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
