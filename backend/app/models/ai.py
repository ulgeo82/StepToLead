"""Provider usage only: never store prompts, credentials or model answers here."""
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class AiUsage(Base):
    __tablename__ = "ai_usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("client_workspaces.id", ondelete="SET NULL"), index=True)
    feature: Mapped[str] = mapped_column(String(24), index=True)
    # client — лимит клиента; reserve — скрытый резерв на обязательные отчёты (бриф, ИИ-выводы в отчётах);
    # agency — служебные запросы агентства (диагностика, демо), клиенту не считаются.
    bucket: Mapped[str] = mapped_column(String(12), default="client", index=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(160), index=True)
    tokens_in: Mapped[int] = mapped_column(default=0)
    tokens_out: Mapped[int] = mapped_column(default=0)
    duration_ms: Mapped[int] = mapped_column(default=0)
    audio_seconds: Mapped[float] = mapped_column(Float, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(String(240))


class AiTokenAccount(Base):
    """Токены ИИ клиента: свои значения поверх тарифа и купленный остаток. Модель клиенту не показывается."""
    __tablename__ = "ai_token_accounts"
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), primary_key=True)
    monthly_override: Mapped[int | None] = mapped_column(BigInteger)   # None — по тарифу
    reserve_override: Mapped[int | None] = mapped_column(BigInteger)   # None — по тарифу
    bought_total: Mapped[int] = mapped_column(BigInteger, default=0)   # всего куплено/начислено
    bought_spent: Mapped[int] = mapped_column(BigInteger, default=0)   # списано из купленных за прошлые месяцы и текущий
    overflow_month: Mapped[str | None] = mapped_column(String(7))      # YYYY-MM, к которому относится overflow_recorded
    overflow_recorded: Mapped[int] = mapped_column(BigInteger, default=0)
    warned: Mapped[str | None] = mapped_column(String(16))             # YYYY-MM:25 / YYYY-MM:0 — уведомление отправлено


class AiTokenPurchase(Base):
    __tablename__ = "ai_token_purchases"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("portal_users.id", ondelete="SET NULL"))
    tokens: Mapped[int] = mapped_column(BigInteger)
    price_rub: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending | paid | canceled
    provider: Mapped[str] = mapped_column(String(16), default="invoice")           # yookassa | invoice | manual
    external_id: Mapped[str | None] = mapped_column(String(64), index=True)
    comment: Mapped[str | None] = mapped_column(String(240))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
