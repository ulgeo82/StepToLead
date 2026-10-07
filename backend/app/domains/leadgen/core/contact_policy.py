"""Единая проверка перед любой отправкой (спецификация: «Стоп-лист, касания и персональные данные»).

Чистая логика без БД: сервис портала собирает состояние из lg_* таблиц и CRM и передаёт сюда.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .normalize import normalize_domain, normalize_phone

DEFAULT_RECONTACT_DAYS = 90
BLOCKED_STAGES = {"rejected", "dnc"}


@dataclass(frozen=True)
class DncEntry:
    kind: str           # company / domain / email / phone / telegram
    value_norm: str
    until: datetime | None = None  # None = навсегда

    def active(self, now: datetime) -> bool:
        return self.until is None or self.until > now


@dataclass
class CompanyState:
    id: str
    stage: str
    domain: str | None = None
    has_open_sales_deal: bool = False       # открытая сделка в любой воронке, КРОМЕ «Аутрич»
    is_client: bool = False
    last_touch_at: datetime | None = None   # последнее исходящее касание в любом канале
    active_sequence_id: str | None = None   # цепочка касаний, которая сейчас идёт по компании


@dataclass
class ContactState:
    kind: str           # phone / whatsapp / telegram / email / vk / form_url
    value_norm: str
    verify_status: str = "unknown"  # unknown / valid / invalid / risky
    bounced: bool = False
    is_personal: bool = False


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str | None = None


def _dnc_values(contact: ContactState | None) -> set[tuple[str, str]]:
    if contact is None:
        return set()
    v = contact.value_norm
    if contact.kind in ("phone", "whatsapp"):
        return {("phone", normalize_phone(v) or v)}
    if contact.kind == "email":
        email = v.strip().lower()
        out = {("email", email)}
        if "@" in email:
            dom = normalize_domain(email.split("@", 1)[1])
            if dom:
                out.add(("domain", dom))
        return out
    if contact.kind == "telegram":
        return {("telegram", v.strip().lower().lstrip("@"))}
    return set()


def can_contact(
    company: CompanyState,
    contact: ContactState | None,
    channel: str,
    *,
    dnc: list[DncEntry],
    now: datetime,
    recontact_days: int = DEFAULT_RECONTACT_DAYS,
    has_shared_channel: bool = True,
    sequence_id: str | None = None,
) -> Decision:
    """Возвращает Decision(ok, reason). Порядок проверок = порядок причин, которые увидит менеджер."""
    if company.stage in BLOCKED_STAGES:
        return Decision(False, f"stage:{company.stage}")

    active = {(e.kind, e.value_norm) for e in dnc if e.active(now)}
    if ("company", company.id) in active:
        return Decision(False, "dnc:company")
    if company.domain and ("domain", company.domain) in active:
        return Decision(False, "dnc:domain")
    for key in _dnc_values(contact):
        if key in active:
            return Decision(False, f"dnc:{key[0]}")

    if company.is_client:
        return Decision(False, "crm:client")
    if company.has_open_sales_deal:
        return Decision(False, "crm:open_deal")

    is_followup = sequence_id is not None and sequence_id == company.active_sequence_id
    if company.active_sequence_id and not is_followup:
        return Decision(False, "sequence:another_active")
    if (
        not is_followup
        and company.last_touch_at
        and now - company.last_touch_at < timedelta(days=recontact_days)
    ):
        return Decision(False, "recently_contacted")

    if contact is not None:
        if contact.verify_status == "invalid" or contact.bounced:
            return Decision(False, "contact:invalid")
        if contact.is_personal and has_shared_channel:
            return Decision(False, "contact:personal_while_shared_exists")

    return Decision(True)
