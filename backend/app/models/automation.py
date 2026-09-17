"""Durable campaign execution state; existing campaign/lead tables stay intact."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CampaignRuntime(Base):
    __tablename__ = "campaign_runtimes"
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), default="paused")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_account_id: Mapped[int | None] = mapped_column()


class CampaignAccount(Base):
    __tablename__ = "campaign_accounts"
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("telegram_accounts.id", ondelete="CASCADE"), primary_key=True)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CampaignDialog(Base):
    __tablename__ = "campaign_dialogs"
    __table_args__ = (UniqueConstraint("account_id", "peer_id", name="uq_campaign_dialog_peer"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), unique=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("telegram_accounts.id", ondelete="CASCADE"), index=True)
    peer_id: Mapped[int] = mapped_column(BigInteger)
    access_hash: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="active")
    last_incoming_id: Mapped[int] = mapped_column(default=0)
    last_reply_id: Mapped[int] = mapped_column(default=0)
    followup_sent: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    next_reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CampaignMessage(Base):
    __tablename__ = "campaign_messages"
    __table_args__ = (UniqueConstraint("dialog_id", "dedupe_key", name="uq_campaign_message_dedupe"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    dialog_id: Mapped[int] = mapped_column(ForeignKey("campaign_dialogs.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("telegram_accounts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    body: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    telegram_message_id: Mapped[int | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CampaignEvent(Base):
    """Human-readable execution history without message bodies or credentials."""
    __tablename__ = "campaign_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    level: Mapped[str] = mapped_column(String(16), index=True)
    event: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_accounts.id", ondelete="SET NULL"), index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class GlobalBlock(Base):
    __tablename__ = "global_telegram_blocks"
    peer_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))
