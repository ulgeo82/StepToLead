import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class LeadStatus(str, enum.Enum):
    NEW = "new"
    QUEUED = "queued"
    SENT = "sent"
    REPLIED = "replied"
    INTERESTED = "interested"
    NOT_INTERESTED = "not_interested"
    HANDOFF = "handoff"
    FAILED = "failed"


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (UniqueConstraint("campaign_id", "username", name="uq_campaign_username"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    first_message: Mapped[str] = mapped_column(Text)
    status: Mapped[LeadStatus] = mapped_column(Enum(LeadStatus), default=LeadStatus.NEW, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    campaign: Mapped["Campaign"] = relationship(back_populates="leads")


