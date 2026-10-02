"""Project-scoped CRM entities layered on the existing Lead/Sale facts."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CrmContact(Base):
    __tablename__ = "crm_contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    phones: Mapped[list] = mapped_column(JSON, default=list)
    emails: Mapped[list] = mapped_column(JSON, default=list)
    phone_normalized: Mapped[str | None] = mapped_column(String(32), index=True)
    email_normalized: Mapped[str | None] = mapped_column(String(254), index=True)
    telegram: Mapped[str | None] = mapped_column(String(120))
    company: Mapped[str | None] = mapped_column(String(180))
    position: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list | None] = mapped_column(JSON, default=list)
    custom_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CrmInbound(Base):
    __tablename__ = "crm_inbound"
    __table_args__ = (UniqueConstraint("inbound_source_id", "external_id", name="uq_crm_inbound_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    website_session_id: Mapped[int | None] = mapped_column(ForeignKey("website_sessions.id", ondelete="SET NULL"), index=True)
    inbound_source_id: Mapped[int | None] = mapped_column(ForeignKey("lead_inbound_sources.id"))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("project_sources.id"))
    external_id: Mapped[str | None] = mapped_column(String(180))
    name: Mapped[str | None] = mapped_column(String(180))
    phone: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(254))
    raw_payload: Mapped[dict] = mapped_column(JSON)
    attribution: Mapped[dict] = mapped_column(JSON, default=dict)
    origin: Mapped[str] = mapped_column(String(32), default="INTEGRATION")
    status: Mapped[str] = mapped_column(String(24), default="NEW", index=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(24))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("crm_contacts.id"))
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("crm_deals.id"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CrmPipeline(Base):
    __tablename__ = "crm_pipelines"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CrmStage(Base):
    __tablename__ = "crm_stages"

    id: Mapped[int] = mapped_column(primary_key=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("crm_pipelines.id"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    analytics_type: Mapped[str] = mapped_column(String(16))
    color: Mapped[str] = mapped_column(String(16), default="#006BFD")
    position: Mapped[int] = mapped_column(default=0)
    required_fields: Mapped[list] = mapped_column(JSON, default=list)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CrmDeal(Base):
    __tablename__ = "crm_deals"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("crm_contacts.id"), index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("client_leads.id"), unique=True)
    inbound_id: Mapped[int | None] = mapped_column(ForeignKey("crm_inbound.id"))
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("crm_pipelines.id"), index=True)
    stage_id: Mapped[int] = mapped_column(ForeignKey("crm_stages.id"), index=True)
    responsible_user_id: Mapped[int | None] = mapped_column(ForeignKey("portal_users.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(220))
    amount: Mapped[float | None] = mapped_column(Numeric(16, 2))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("project_sources.id"))
    origin: Mapped[str] = mapped_column(String(24), default="MANUAL")
    custom_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    attribution_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    lost_reason_id: Mapped[int | None] = mapped_column(ForeignKey("project_lost_reasons.id"))
    lost_comment: Mapped[str | None] = mapped_column(Text)
    lost_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tags: Mapped[list | None] = mapped_column(JSON, default=list)
    # When the deal entered its current stage / was last touched / got the first human response.
    stage_entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Which automation rules already fired for the current stage entry: {rule_id: stage_entered_at iso}.
    automation_state: Mapped[dict | None] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CrmAutomation(Base):
    """Digital-pipeline rule: trigger (stage entry / deal created / no activity) -> action."""
    __tablename__ = "crm_automations"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("crm_pipelines.id"), index=True)
    stage_id: Mapped[int | None] = mapped_column(ForeignKey("crm_stages.id"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    trigger: Mapped[str] = mapped_column(String(24))       # STAGE_ENTER | DEAL_CREATED | NO_ACTIVITY
    delay_minutes: Mapped[int] = mapped_column(default=0)  # NO_ACTIVITY: idle time before firing
    conditions: Mapped[dict | None] = mapped_column(JSON, default=dict)  # {source_id, origin, min_amount}
    action: Mapped[str] = mapped_column(String(24))        # CREATE_TASK | SET_RESPONSIBLE | ADD_TAG | NOTIFY | MOVE_STAGE
    params: Mapped[dict | None] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    fired_count: Mapped[int] = mapped_column(default=0)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CrmStageHistory(Base):
    __tablename__ = "crm_stage_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("crm_deals.id"), index=True)
    from_stage_id: Mapped[int | None] = mapped_column(ForeignKey("crm_stages.id"))
    to_stage_id: Mapped[int] = mapped_column(ForeignKey("crm_stages.id"))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("portal_users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CrmActivity(Base):
    __tablename__ = "crm_activities"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("crm_deals.id"), index=True)
    inbound_id: Mapped[int | None] = mapped_column(ForeignKey("crm_inbound.id"))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("portal_users.id", ondelete="SET NULL"))
    actor_name: Mapped[str | None] = mapped_column(String(160))
    event_type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CrmTaskType(Base):
    __tablename__ = "crm_task_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(100))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CrmTask(Base):
    __tablename__ = "crm_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("crm_deals.id"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("crm_contacts.id"))
    type_id: Mapped[int | None] = mapped_column(ForeignKey("crm_task_types.id"))
    type_code: Mapped[str] = mapped_column(String(32), default="OTHER")
    title: Mapped[str] = mapped_column(String(220))
    description: Mapped[str | None] = mapped_column(Text)
    responsible_user_id: Mapped[int | None] = mapped_column(ForeignKey("portal_users.id", ondelete="SET NULL"))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    duration_minutes: Mapped[int | None] = mapped_column()
    priority: Mapped[str] = mapped_column(String(12), default="NORMAL")
    status: Mapped[str] = mapped_column(String(16), default="OPEN", index=True)
    result: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("portal_users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CrmCustomFieldDefinition(Base):
    __tablename__ = "crm_custom_field_definitions"
    __table_args__ = (UniqueConstraint("project_id", "key", name="uq_crm_custom_field_project_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    key: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(140))
    field_type: Mapped[str] = mapped_column(String(20))
    options: Mapped[list | None] = mapped_column(JSON)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
