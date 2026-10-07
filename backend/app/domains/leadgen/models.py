"""Лидогенерация агентства: база компаний-кандидатов для аутрича (таблицы lg_*).

Спецификация: документ «Лидогенерация StepToLead — модель данных». Существующие таблицы портала не меняются;
в CRM компания попадает только при «В аутрич» (crm_contact_id / crm_deal_id).
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# Стадии компании: двигают только сервисы.
STAGES = ("new", "enriched", "ready", "in_outreach", "replied", "converted", "rejected", "dnc")


class LgCompany(Base):
    __tablename__ = "lg_companies"
    __table_args__ = (UniqueConstraint("workspace_id", "inn", name="uq_lg_company_inn"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    display_name: Mapped[str | None] = mapped_column(String(240))
    legal_name: Mapped[str | None] = mapped_column(String(400))
    inn: Mapped[str | None] = mapped_column(String(12))
    ogrn: Mapped[str | None] = mapped_column(String(15))
    domain: Mapped[str | None] = mapped_column(String(253), index=True)
    city: Mapped[str | None] = mapped_column(String(120))
    city_key: Mapped[str | None] = mapped_column(String(120), index=True)    # text_key(city): для фильтров
    region_code: Mapped[int | None] = mapped_column(Integer)
    niche: Mapped[str | None] = mapped_column(String(120))
    niche_key: Mapped[str | None] = mapped_column(String(120), index=True)   # text_key(niche)
    okved: Mapped[str | None] = mapped_column(String(16))
    director_name: Mapped[str | None] = mapped_column(String(240))
    director_post: Mapped[str | None] = mapped_column(String(120))
    revenue_rub: Mapped[int | None] = mapped_column(BigInteger)
    revenue_year: Mapped[int | None] = mapped_column(Integer)
    legal_status: Mapped[str] = mapped_column(String(16), default="unknown")   # active / liquidated / unknown
    fit_label: Mapped[str | None] = mapped_column(String(8))                   # fit / maybe / no
    fit_reason: Mapped[str | None] = mapped_column(Text)
    score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    score_reasons: Mapped[list] = mapped_column(JSON, default=list)
    stage: Mapped[str] = mapped_column(String(16), default="new", index=True)
    needs_review: Mapped[str | None] = mapped_column(String(40))              # причина ручной проверки
    crm_contact_id: Mapped[int | None] = mapped_column(ForeignKey("crm_contacts.id", ondelete="SET NULL"))
    crm_deal_id: Mapped[int | None] = mapped_column(ForeignKey("crm_deals.id", ondelete="SET NULL"))
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)               # поле -> {source, url, at}
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LgCompanyKey(Base):
    """Ключи склейки. Уникальность (workspace, kind, value) для всех видов, кроме name_city."""
    __tablename__ = "lg_company_keys"
    __table_args__ = (
        Index("ix_lg_company_keys_lookup", "workspace_id", "kind", "value_norm"),
        UniqueConstraint("workspace_id", "kind", "value_norm", "company_id", name="uq_lg_company_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"))
    company_id: Mapped[int] = mapped_column(ForeignKey("lg_companies.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))   # inn / domain / phone / twogis_id / hh_employer_id / name_city
    value_norm: Mapped[str] = mapped_column(String(300))


class LgSourceRun(Base):
    __tablename__ = "lg_source_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(24))  # yandex_direct / yandex_organic / twogis / hh / import
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued / running / done / failed
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LgAd(Base):
    """Объявление компании («что крутят»). content_hash = заголовок + текст."""
    __tablename__ = "lg_ads"
    __table_args__ = (UniqueConstraint("company_id", "content_hash", name="uq_lg_ad_content"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("lg_companies.id", ondelete="CASCADE"), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(300))
    text: Mapped[str | None] = mapped_column(Text)
    display_url: Mapped[str | None] = mapped_column(String(300))
    landing_url: Mapped[str | None] = mapped_column(String(1000))
    sitelinks: Mapped[list] = mapped_column(JSON, default=list)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    region_code: Mapped[int | None] = mapped_column(Integer)
    placement: Mapped[str | None] = mapped_column(String(16))  # premium / other
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LgSignal(Base):
    __tablename__ = "lg_signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("lg_companies.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("lg_source_runs.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(32), index=True)
    key: Mapped[str | None] = mapped_column(String(500))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LgContact(Base):
    __tablename__ = "lg_contacts"
    __table_args__ = (UniqueConstraint("company_id", "kind", "value_norm", name="uq_lg_contact"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("lg_companies.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # phone / whatsapp / telegram / email / vk / form_url
    value: Mapped[str] = mapped_column(String(500))
    value_norm: Mapped[str] = mapped_column(String(500), index=True)
    is_personal: Mapped[bool] = mapped_column(Boolean, default=False)
    person_name: Mapped[str | None] = mapped_column(String(240))
    person_role: Mapped[str | None] = mapped_column(String(120))
    source: Mapped[str | None] = mapped_column(String(32))
    source_url: Mapped[str | None] = mapped_column(String(1000))
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    verify_status: Mapped[str] = mapped_column(String(12), default="unknown")  # unknown / valid / invalid / risky
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bounced: Mapped[bool] = mapped_column(Boolean, default=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)


class LgEnrichment(Base):
    __tablename__ = "lg_enrichments"
    __table_args__ = (UniqueConstraint("company_id", "step", name="uq_lg_enrichment_step"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("lg_companies.id", ondelete="CASCADE"), index=True)
    step: Mapped[str] = mapped_column(String(24))  # clean_name / find_inn / dadata / fns_revenue / site_check / fit_ai / personalize
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending / done / failed / skipped
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_rub: Mapped[float | None] = mapped_column()
    error: Mapped[str | None] = mapped_column(Text)
    ran_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LgSegment(Base):
    __tablename__ = "lg_segments"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LgTouch(Base):
    """Касание компании в любом канале, со снимком текста."""
    __tablename__ = "lg_touches"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("lg_companies.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("lg_contacts.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(String(16))     # email / telegram / whatsapp / call
    direction: Mapped[str] = mapped_column(String(4))    # out / in
    sequence_id: Mapped[str | None] = mapped_column(String(64), index=True)
    campaign_kind: Mapped[str | None] = mapped_column(String(24))
    campaign_id: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="planned")  # planned / sent / delivered / bounced / replied / failed
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(String(300))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    happened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class LgDnc(Base):
    """Стоп-лист. until = None — навсегда."""
    __tablename__ = "lg_dnc"
    __table_args__ = (UniqueConstraint("workspace_id", "kind", "value_norm", name="uq_lg_dnc"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # company / domain / email / phone / telegram
    value_norm: Mapped[str] = mapped_column(String(300))
    reason: Mapped[str] = mapped_column(String(16))  # unsubscribed / refused / client / competitor / bounced / legal
    note: Mapped[str | None] = mapped_column(Text)
    until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
