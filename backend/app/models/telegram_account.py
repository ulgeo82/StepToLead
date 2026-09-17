from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    bio: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_version: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(32), default="disconnected", index=True)
    session_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_session_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_code_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requires_2fa: Mapped[bool] = mapped_column(Boolean, default=False)
    proxy_id: Mapped[int | None] = mapped_column(ForeignKey("proxies.id", ondelete="SET NULL"), nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    flood_wait_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    proxy: Mapped["Proxy | None"] = relationship(back_populates="accounts")

