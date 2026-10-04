from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, JSON, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ClientWorkspace(Base):
    __tablename__ = "client_workspaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="active")
    legal_name: Mapped[str | None] = mapped_column(String(240))
    contact_email: Mapped[str | None] = mapped_column(String(254))
    contact_phone: Mapped[str | None] = mapped_column(String(64))
    website: Mapped[str | None] = mapped_column(String(500))
    timezone: Mapped[str] = mapped_column(String(80), default="Europe/Moscow")
    currency: Mapped[str] = mapped_column(String(12), default="RUB")
    logo_data: Mapped[str | None] = mapped_column(Text)
    # Agency tariff: start | growth | system; empty = individual terms, no limits (clients from before tariffs).
    plan: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    website: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="active")
    timezone: Mapped[str | None] = mapped_column(String(80))
    meeting_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Client launch screen (marketer card, agency work plan, monthly goals) and worker bookkeeping
    # (weekly report / alert send marks): {"launch": {...}, "sent": {...}}
    portal_state: Mapped[dict | None] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdConnection(Base):
    __tablename__ = "ad_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(180))
    external_account_id: Mapped[str] = mapped_column(String(180))
    access_token_encrypted: Mapped[str] = mapped_column(Text)
    vk_client_id_encrypted: Mapped[str | None] = mapped_column(Text)
    vk_client_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    vk_refresh_token_encrypted: Mapped[str | None] = mapped_column(Text)
    vk_access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    currency: Mapped[str | None] = mapped_column(String(12))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Platform-specific state (balances, Avito lead import cursors, campaign catalogue). Never secrets.
    config: Mapped[dict | None] = mapped_column(JSON)
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
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), index=True)
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
    manage_sources: Mapped[bool] = mapped_column(Boolean, default=False)
    manage_integrations: Mapped[bool] = mapped_column(Boolean, default=False)
    phone: Mapped[str | None] = mapped_column(String(64))
    permissions: Mapped[list | None] = mapped_column(JSON)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_chat_id: Mapped[str | None] = mapped_column(String(32))
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    telegram_link_code_hash: Mapped[str | None] = mapped_column(String(64))
    telegram_link_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_state: Mapped[dict | None] = mapped_column(JSON)
    # Self-service password reset: one-time code sent by the Telegram bot.
    reset_code_hash: Mapped[str | None] = mapped_column(String(64))
    reset_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reset_attempts: Mapped[int] = mapped_column(default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PortalProjectAccess(Base):
    __tablename__ = "portal_project_access"
    __table_args__ = (UniqueConstraint("user_id", "project_id", name="uq_portal_user_project"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("portal_users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    manage_sources: Mapped[bool] = mapped_column(Boolean, default=False)
    manage_integrations: Mapped[bool] = mapped_column(Boolean, default=False)


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
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("project_sources.id"), index=True)
    qualified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    meeting_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    assigned_to_id: Mapped[int | None] = mapped_column(ForeignKey("portal_users.id", ondelete="SET NULL"), index=True)
    full_name: Mapped[str] = mapped_column(String(180))
    phone: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(254))
    telegram: Mapped[str | None] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(120), default="Вручную")
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    value: Mapped[float | None] = mapped_column(Numeric(16, 2))
    notes: Mapped[str | None] = mapped_column(Text)
    lost_reason: Mapped[str | None] = mapped_column(Text)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # Lead quality marked by sales: target | non_target (None = not marked yet). Drives "доля целевых".
    quality: Mapped[str | None] = mapped_column(String(16), index=True)
    quality_reason: Mapped[str | None] = mapped_column(String(120))
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
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(12))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_assign: Mapped[bool] = mapped_column(Boolean, default=True)
    # Trusted source (site form, calls): requests become deals right away, skipping «Неразобранное».
    auto_accept: Mapped[bool] = mapped_column(Boolean, default=False)
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


class ClientSale(Base):
    __tablename__ = "client_sales"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("client_leads.id", ondelete="CASCADE"), index=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("crm_deals.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    amount: Mapped[float | None] = mapped_column(Numeric(16, 2))
    comment: Mapped[str | None] = mapped_column(Text)
    confirmed_by_id: Mapped[int | None] = mapped_column(ForeignKey("portal_users.id", ondelete="SET NULL"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectEconomics(Base):
    __tablename__ = "project_economics"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), unique=True, index=True)
    growth_calculation_id: Mapped[int] = mapped_column(ForeignKey("growth_calculations.id"))
    allowable_cac: Mapped[float | None] = mapped_column(Numeric(16, 2))
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectSource(Base):
    __tablename__ = "project_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    kind: Mapped[str] = mapped_column(String(20))
    method: Mapped[str] = mapped_column(String(20))
    category: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), default="active")
    data_mode: Mapped[str | None] = mapped_column(String(20))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("portal_users.id", ondelete="SET NULL"))
    source_metadata: Mapped[dict | None] = mapped_column("metadata", JSON)
    connection_id: Mapped[int | None] = mapped_column(ForeignKey("ad_connections.id", ondelete="SET NULL"), unique=True)
    inbound_source_id: Mapped[int | None] = mapped_column(ForeignKey("lead_inbound_sources.id", ondelete="SET NULL"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceMetricDaily(Base):
    __tablename__ = "source_metrics_daily"
    __table_args__ = (UniqueConstraint("source_id", "date", name="uq_source_metric_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("project_sources.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date)
    spend: Mapped[float | None] = mapped_column(Numeric(16, 2))
    impressions: Mapped[int | None] = mapped_column()
    clicks: Mapped[int | None] = mapped_column()
    aggregated_leads: Mapped[int | None] = mapped_column()
    aggregated_qualified: Mapped[int | None] = mapped_column()
    aggregated_sales: Mapped[int | None] = mapped_column()
    aggregated_revenue: Mapped[float | None] = mapped_column(Numeric(16, 2))


class ProjectLostReason(Base):
    __tablename__ = "project_lost_reasons"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(160))
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="active")
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectNotificationRule(Base):
    __tablename__ = "project_notification_rules"
    __table_args__ = (UniqueConstraint("project_id", "event_key", name="uq_project_notification_rule"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    event_key: Mapped[str] = mapped_column(String(48))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    threshold: Mapped[float | None] = mapped_column(Numeric(12, 2))
    in_app: Mapped[bool] = mapped_column(Boolean, default=True)
    email: Mapped[bool] = mapped_column(Boolean, default=False)
    telegram: Mapped[bool] = mapped_column(Boolean, default=False)
    # None = default audience (owner and sales heads); a list = exactly these project members.
    recipient_user_ids: Mapped[list | None] = mapped_column(JSON)
    notify_assignee: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
