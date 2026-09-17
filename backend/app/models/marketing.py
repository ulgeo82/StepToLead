from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, JSON, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ClientWorkspace(Base):
    __tablename__ = "client_workspaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdConnection(Base):
    __tablename__ = "ad_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(180))
    external_account_id: Mapped[str] = mapped_column(String(180))
    access_token_encrypted: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    currency: Mapped[str | None] = mapped_column(String(12))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdMetricDaily(Base):
    __tablename__ = "ad_metrics_daily"
    __table_args__ = (UniqueConstraint("connection_id", "date", name="uq_ad_metric_connection_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("ad_connections.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date)
    spend: Mapped[float] = mapped_column(Numeric(16, 2), default=0)
    impressions: Mapped[int] = mapped_column(default=0)
    clicks: Mapped[int] = mapped_column(default=0)
    leads: Mapped[int] = mapped_column(default=0)
    raw: Mapped[dict | None] = mapped_column(JSON)


class AdCampaignMetricDaily(Base):
    __tablename__ = "ad_campaign_metrics_daily"
    __table_args__ = (UniqueConstraint("connection_id", "date", "external_campaign_id", name="uq_ad_campaign_metric_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("ad_connections.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    external_campaign_id: Mapped[str] = mapped_column(String(180), index=True)
    campaign_name: Mapped[str] = mapped_column(String(300))
    spend: Mapped[float] = mapped_column(Numeric(16, 2), default=0)
    impressions: Mapped[int] = mapped_column(default=0)
    clicks: Mapped[int] = mapped_column(default=0)
    raw: Mapped[dict | None] = mapped_column(JSON)


class AdHypothesis(Base):
    __tablename__ = "ad_hypotheses"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(220))
    audience: Mapped[str | None] = mapped_column(Text)
    offer: Mapped[str | None] = mapped_column(Text)
    landing_url: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(32), default="testing", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdHypothesisCampaign(Base):
    __tablename__ = "ad_hypothesis_campaigns"
    __table_args__ = (UniqueConstraint("connection_id", "external_campaign_id", name="uq_ad_campaign_hypothesis"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    hypothesis_id: Mapped[int] = mapped_column(ForeignKey("ad_hypotheses.id", ondelete="CASCADE"), index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("ad_connections.id", ondelete="CASCADE"), index=True)
    external_campaign_id: Mapped[str] = mapped_column(String(180), index=True)


class PortalUser(Base):
    __tablename__ = "portal_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    username: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(String(160))
    role: Mapped[str] = mapped_column(String(32), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PortalSession(Base):
    __tablename__ = "portal_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("portal_users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PortalNotification(Base):
    __tablename__ = "portal_notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("portal_users.id", ondelete="CASCADE"), index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    title: Mapped[str] = mapped_column(String(180))
    body: Mapped[str | None] = mapped_column(Text)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClientLead(Base):
    __tablename__ = "client_leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    assigned_to_id: Mapped[int | None] = mapped_column(ForeignKey("portal_users.id", ondelete="SET NULL"), index=True)
    full_name: Mapped[str] = mapped_column(String(180))
    phone: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(254))
    source: Mapped[str] = mapped_column(String(120), default="Вручную")
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    value: Mapped[float] = mapped_column(Numeric(16, 2), default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ClientLeadEvent(Base):
    __tablename__ = "client_lead_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("client_leads.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("portal_users.id", ondelete="SET NULL"), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LeadInboundSource(Base):
    __tablename__ = "lead_inbound_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(12))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_assign: Mapped[bool] = mapped_column(Boolean, default=True)
    last_assigned_to_id: Mapped[int | None] = mapped_column(ForeignKey("portal_users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LeadInboundReceipt(Base):
    __tablename__ = "lead_inbound_receipts"
    __table_args__ = (UniqueConstraint("source_id", "external_id", name="uq_lead_inbound_source_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("lead_inbound_sources.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("client_leads.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(180))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClientLeadAttribution(Base):
    __tablename__ = "client_lead_attributions"
    __table_args__ = (UniqueConstraint("lead_id", name="uq_client_lead_attribution"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("client_leads.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("lead_inbound_sources.id", ondelete="SET NULL"), index=True)
    connection_id: Mapped[int | None] = mapped_column(ForeignKey("ad_connections.id", ondelete="SET NULL"), index=True)
    hypothesis_id: Mapped[int | None] = mapped_column(ForeignKey("ad_hypotheses.id", ondelete="SET NULL"), index=True)
    external_campaign_id: Mapped[str | None] = mapped_column(String(180), index=True)
    external_ad_id: Mapped[str | None] = mapped_column(String(180))
    utm_source: Mapped[str | None] = mapped_column(String(255))
    utm_medium: Mapped[str | None] = mapped_column(String(255))
    utm_campaign: Mapped[str | None] = mapped_column(String(500))
    utm_content: Mapped[str | None] = mapped_column(String(500))
    utm_term: Mapped[str | None] = mapped_column(String(500))
    landing_url: Mapped[str | None] = mapped_column(String(1500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
