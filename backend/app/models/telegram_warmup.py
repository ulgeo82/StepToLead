"""Прогрев Telegram-аккаунтов (docs/TELEGRAM_WARMUP.md): журнал событий и переписки «свои ↔ свои»."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TelegramWarmupEvent(Base):
    __tablename__ = "telegram_warmup_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("telegram_accounts.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)


class TelegramWarmupChat(Base):
    """Короткий диалог двух своих аккаунтов: кто пишет следующим, когда и сколько реплик осталось."""
    __tablename__ = "telegram_warmup_chats"
    id: Mapped[int] = mapped_column(primary_key=True)
    a_id: Mapped[int] = mapped_column(ForeignKey("telegram_accounts.id", ondelete="CASCADE"), index=True)
    b_id: Mapped[int] = mapped_column(ForeignKey("telegram_accounts.id", ondelete="CASCADE"), index=True)
    topic: Mapped[str] = mapped_column(String(32))
    turn: Mapped[int] = mapped_column(Integer, default=0)
    turns: Mapped[int] = mapped_column(Integer, default=4)
    next_from_id: Mapped[int] = mapped_column(Integer, index=True)
    next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
