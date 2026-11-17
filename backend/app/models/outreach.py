"""Единые входящие аутрича (docs/OUTREACH_INBOX.md, этап C): кто ведёт переписку и шаблоны ответов.

Состояние — по «ветке», а не по последнему сообщению: email — по компании (thread = "email-co:<company_id>"),
Telegram — по диалогу ("tg:<dialog_id>"). Новый ответ лида не сбрасывает назначенного оператора.
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class OutreachThreadState(Base):
    __tablename__ = "outreach_thread_states"
    id: Mapped[int] = mapped_column(primary_key=True)
    thread: Mapped[str] = mapped_column(String(48), unique=True)
    workspace_id: Mapped[int | None] = mapped_column(Integer, index=True)
    assignee_id: Mapped[int | None] = mapped_column(Integer, index=True)
    locked_by_id: Mapped[int | None] = mapped_column(Integer)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Только для Telegram: у email метка, «разобрано» и «горячий» живут в самом письме (lg_touches).
    label: Mapped[str | None] = mapped_column(String(24))
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handled_message_id: Mapped[int | None] = mapped_column(Integer)
    crm_deal_id: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutreachTemplate(Base):
    __tablename__ = "outreach_reply_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(120))
    text: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(12), default="any")
    created_by_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
