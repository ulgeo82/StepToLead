"""Project-scoped site visits and deliberately limited business events."""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WebsiteSite(Base):
    __tablename__ = "website_sites"
    __table_args__ = (UniqueConstraint("project_id", "origin", name="uq_website_site_project_origin"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    origin: Mapped[str] = mapped_column(String(500))
    public_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Callback / messengers widget (stl-widget.js): display settings + the inbound source its leads go to.
    widget: Mapped[dict | None] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WebsiteSession(Base):
    __tablename__ = "website_sessions"
    __table_args__ = (
        UniqueConstraint("site_id", "session_key", name="uq_website_site_session"),
        Index("ix_website_session_project_started", "project_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("website_sites.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    visitor_key: Mapped[str] = mapped_column(String(64), index=True)
    session_key: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    landing_url: Mapped[str | None] = mapped_column(String(1200))
    landing_path: Mapped[str | None] = mapped_column(String(700), index=True)
    referrer: Mapped[str | None] = mapped_column(String(1200))
    device_type: Mapped[str | None] = mapped_column(String(20), index=True)
    browser: Mapped[str | None] = mapped_column(String(40))
    os: Mapped[str | None] = mapped_column(String(40))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("project_sources.id", ondelete="SET NULL"), index=True)
    connection_id: Mapped[int | None] = mapped_column(ForeignKey("ad_connections.id", ondelete="SET NULL"), index=True)
    external_campaign_id: Mapped[str | None] = mapped_column(String(180), index=True)
    utm_source: Mapped[str | None] = mapped_column(String(255))
    utm_medium: Mapped[str | None] = mapped_column(String(255))
    utm_campaign: Mapped[str | None] = mapped_column(String(500))
    utm_content: Mapped[str | None] = mapped_column(String(500))
    utm_term: Mapped[str | None] = mapped_column(String(500))
    click_id: Mapped[str | None] = mapped_column(String(255))
    click_type: Mapped[str | None] = mapped_column(String(12))      # yclid | gclid | vkclid
    ym_client_id: Mapped[str | None] = mapped_column(String(32))    # Yandex Metrica ClientId for offline conversions
    is_new_visitor: Mapped[bool] = mapped_column(Boolean, default=False)
    page_views: Mapped[int] = mapped_column(default=0)
    engaged: Mapped[bool] = mapped_column(Boolean, default=False)
    cta_clicked: Mapped[bool] = mapped_column(Boolean, default=False)
    form_started: Mapped[bool] = mapped_column(Boolean, default=False)
    form_succeeded: Mapped[bool] = mapped_column(Boolean, default=False)


class WebsiteEvent(Base):
    __tablename__ = "website_events"
    __table_args__ = (
        UniqueConstraint("site_id", "event_key", name="uq_website_event_site_key"),
        Index("ix_website_event_project_name_time", "project_id", "event_name", "occurred_at"),
        Index("ix_website_event_session_time", "session_id", "occurred_at"),
        Index("ix_website_event_project_path_time", "project_id", "page_path", "occurred_at"),
        Index("ix_website_event_project_element_session", "project_id", "element_name", "session_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("website_sites.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("website_sessions.id", ondelete="CASCADE"), index=True)
    event_key: Mapped[str] = mapped_column(String(64))
    event_name: Mapped[str] = mapped_column(String(40), index=True)
    event_category: Mapped[str] = mapped_column(String(24))
    page_url: Mapped[str | None] = mapped_column(String(1200))
    page_path: Mapped[str | None] = mapped_column(String(700))
    element_id: Mapped[str | None] = mapped_column(String(100))
    element_name: Mapped[str | None] = mapped_column(String(180))
    properties: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WebsiteEventDaily(Base):
    """Small queryable rollup of accepted events, partitioned by session dimensions."""
    __tablename__ = "website_event_daily"
    __table_args__ = (
        UniqueConstraint("site_id", "day", "group_key", name="uq_website_event_daily_group"),
        Index("ix_website_event_daily_project_day", "project_id", "day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("website_sites.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date)
    group_key: Mapped[str] = mapped_column(String(64))
    event_name: Mapped[str] = mapped_column(String(40))
    page_path: Mapped[str] = mapped_column(String(700), default="")
    landing_path: Mapped[str] = mapped_column(String(700), default="")
    element_name: Mapped[str] = mapped_column(String(180), default="")
    device_type: Mapped[str] = mapped_column(String(20), default="")
    utm_source: Mapped[str] = mapped_column(String(255), default="")
    utm_campaign: Mapped[str] = mapped_column(String(500), default="")
    is_new_visitor: Mapped[bool] = mapped_column(Boolean, default=False)
    depth: Mapped[int] = mapped_column(default=0)
    events_count: Mapped[int] = mapped_column(default=0)
    sessions_count: Mapped[int] = mapped_column(default=0)
