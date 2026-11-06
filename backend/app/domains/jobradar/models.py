"""Радар вакансий: фильтры, найденные вакансии, что и куда отправлено (и оценки 👍/👎)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class JrFilter(Base):
    __tablename__ = "jr_filters"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("client_workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    mode: Mapped[str] = mapped_column(String(8), default="work")          # work — вакансии себе, leads — компании в базу
    keywords: Mapped[list] = mapped_column(JSON, default=list)            # хотя бы одно — в названии вакансии
    exclude: Mapped[list] = mapped_column(JSON, default=list)             # любое — в названии, и вакансия отсеивается
    remote_only: Mapped[bool] = mapped_column(Boolean, default=False)
    regions: Mapped[list] = mapped_column(JSON, default=list)             # пусто — вся Россия
    salary_from: Mapped[int] = mapped_column(Integer, default=0)
    allow_no_salary: Mapped[bool] = mapped_column(Boolean, default=True)
    experience: Mapped[list] = mapped_column(JSON, default=list)          # hh: noExperience / between1And3 / ...
    sources: Mapped[list] = mapped_column(JSON, default=list)             # hh / trudvsem / habr
    chat_id: Mapped[str | None] = mapped_column(String(32))
    ai_min_score: Mapped[int] = mapped_column(Integer, default=0)         # 0 — без ИИ-отбора
    profile: Mapped[str | None] = mapped_column(Text)                     # кого ищем — для ИИ
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class JrVacancy(Base):
    __tablename__ = "jr_vacancies"

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(200), unique=True)            # source:ext_id
    fingerprint: Mapped[str] = mapped_column(String(400), index=True)
    source: Mapped[str] = mapped_column(String(16))
    ext_id: Mapped[str] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(400))
    company: Mapped[str | None] = mapped_column(String(400))
    city: Mapped[str | None] = mapped_column(String(160))
    salary_from: Mapped[int | None] = mapped_column(Integer)
    salary_to: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(String(8))
    remote: Mapped[bool] = mapped_column(Boolean, default=False)
    experience: Mapped[str | None] = mapped_column(String(32))
    url: Mapped[str | None] = mapped_column(String(1000))
    snippet: Mapped[str | None] = mapped_column(Text)
    employer: Mapped[dict] = mapped_column(JSON, default=dict)            # id, url, inn, site — для режима «лиды»
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JrMatch(Base):
    """Решение по вакансии в рамках фильтра. Одна строка на (фильтр, fingerprint): дважды не отправляем."""
    __tablename__ = "jr_matches"
    __table_args__ = (UniqueConstraint("filter_id", "fingerprint", name="uq_jr_match"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    filter_id: Mapped[int] = mapped_column(ForeignKey("jr_filters.id", ondelete="CASCADE"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(400))
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("jr_vacancies.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(12), index=True)          # sent / skipped / ai_low / failed / lead
    reason: Mapped[str | None] = mapped_column(String(240))              # почему отсеяна
    score: Mapped[int | None] = mapped_column(Integer)
    why: Mapped[str | None] = mapped_column(String(240))                 # объяснение ИИ
    sources: Mapped[list] = mapped_column(JSON, default=list)            # все сайты, где нашлась вакансия
    company_id: Mapped[int | None] = mapped_column(Integer)              # режим «лиды»: компания в базе
    chat_id: Mapped[str | None] = mapped_column(String(32))
    message_id: Mapped[int | None] = mapped_column(Integer)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    feedback: Mapped[int | None] = mapped_column(Integer)                # 1 — 👍, -1 — 👎
    feedback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
