"""Сервисы лидогенерации поверх чистого ядра (core/): склейка находок, сигналы, скоринг, can_contact.

Внешних API здесь нет: источники (XMLStock, DaData, сайты) зовут upsert_company / add_signal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.leadgen.core.contact_policy import (
    CompanyState, ContactState, Decision, DncEntry, can_contact,
)
from app.domains.leadgen.core.matching import Candidate, Finding, MatchResult, normalized_keys, plan_match
from app.domains.leadgen.core.normalize import (
    DEFAULT_PLATFORM_DOMAINS, clean_company_name, normalize_domain, normalize_phone, text_key, validate_inn,
)
from app.domains.leadgen.core.scoring import DEFAULT_WEIGHTS, compute_score
from app.domains.leadgen.models import (
    LgAd, LgCompany, LgCompanyKey, LgContact, LgDnc, LgEnrichment, LgSignal, LgTouch,
)
from app.models.crm import CrmDeal
from app.models.system import AppSetting

SETTINGS_KEY = "leadgen:settings"
DEFAULT_SETTINGS = {
    "weights": DEFAULT_WEIGHTS,
    "platform_domains": list(DEFAULT_PLATFORM_DOMAINS),
    "recontact_days": 90,
    "outreach_pipeline_id": None,
}
# Чем выше, тем надёжнее источник поля (раздел «Дедупликация и склейка», правило 2).
SOURCE_RANK = {"dadata": 40, "fns": 35, "site": 30, "twogis": 20, "hh": 20,
               "yandex_direct": 10, "yandex_organic": 10, "import": 5, "manual": 50}
COMPANY_FIELDS = ("display_name", "legal_name", "inn", "domain", "city", "region_code", "niche")
CHANNEL_KINDS = {"email": {"email"}, "telegram": {"telegram"}, "whatsapp": {"whatsapp", "phone"},
                 "call": {"phone"}}
OUT_DONE = ("sent", "delivered", "replied")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def get_settings(db: AsyncSession) -> dict:
    row = await db.get(AppSetting, SETTINGS_KEY)
    value = dict(DEFAULT_SETTINGS)
    if row and isinstance(row.value, dict):
        value.update(row.value)
        value["weights"] = {**DEFAULT_WEIGHTS, **(row.value.get("weights") or {})}
    return value


async def ensure_settings(db: AsyncSession) -> None:
    """Сид настроек: создаёт запись с дефолтами, если её нет. Существующую не трогает."""
    if await db.get(AppSetting, SETTINGS_KEY) is None:
        db.add(AppSetting(key=SETTINGS_KEY, value=DEFAULT_SETTINGS))
        await db.flush()


# ---------------------------------------------------------------- находки

@dataclass
class FindingIn:
    source: str
    name: str | None = None
    legal_name: str | None = None
    inn: str | None = None
    domain: str | None = None
    city: str | None = None
    region_code: int | None = None
    niche: str | None = None
    phones: list[str] = field(default_factory=list)
    twogis_id: str | None = None
    hh_employer_id: str | None = None
    contacts: list[dict] = field(default_factory=list)  # {"kind","value","source_url"?, "is_personal"?, ...}
    source_url: str | None = None

    def as_core(self) -> Finding:
        phones = list(self.phones) + [c["value"] for c in self.contacts if c.get("kind") in ("phone", "whatsapp")]
        return Finding(inn=self.inn, domain=self.domain, phones=phones, city=self.city,
                       twogis_id=self.twogis_id, hh_employer_id=self.hh_employer_id,
                       name=self.name or self.legal_name)


@dataclass
class UpsertResult:
    company: LgCompany | None
    match: MatchResult
    created: bool


async def _lookup_index(db: AsyncSession, workspace_id: int, keys: dict[str, list[str]]):
    pairs = [(k, v) for k, vs in keys.items() for v in vs]
    if not pairs:
        return lambda kind, value: []
    rows = (await db.execute(
        select(LgCompanyKey.kind, LgCompanyKey.value_norm, LgCompany.id, LgCompany.city, LgCompany.inn)
        .join(LgCompany, LgCompany.id == LgCompanyKey.company_id)
        .where(LgCompanyKey.workspace_id == workspace_id,
               LgCompanyKey.value_norm.in_([v for _, v in pairs]))
        .order_by(LgCompany.id)
    )).all()
    index: dict[tuple[str, str], list[Candidate]] = {}
    for kind, value, cid, city, inn in rows:
        index.setdefault((kind, value), []).append(Candidate(str(cid), city=city, inn=inn))
    return lambda kind, value: index.get((kind, value), [])


def _contact_norm(kind: str, value: str) -> str | None:
    if kind in ("phone", "whatsapp"):
        return normalize_phone(value)
    if kind == "email":
        v = value.strip().lower()
        return v if "@" in v else None
    if kind == "telegram":
        v = value.strip().lower()
        for prefix in ("https://t.me/", "http://t.me/", "t.me/", "@"):
            if v.startswith(prefix):
                v = v[len(prefix):]
        return v.strip("/") or None
    return value.strip() or None


def _apply_fields(company: LgCompany, values: dict, source: str, url: str | None, now: datetime) -> None:
    rank = SOURCE_RANK.get(source, 0)
    prov = dict(company.provenance or {})
    for name, value in values.items():
        if value in (None, ""):
            continue
        current = getattr(company, name)
        old_rank = SOURCE_RANK.get((prov.get(name) or {}).get("source"), 0)
        if current in (None, "") or rank >= old_rank:
            setattr(company, name, value)
            prov[name] = {"source": source, "url": url, "at": now.isoformat()}
    company.provenance = prov
    company.city_key = text_key(company.city)
    company.niche_key = text_key(company.niche)


async def _add_keys(db: AsyncSession, company: LgCompany, keys: dict[str, list[str]]) -> None:
    existing = set((await db.execute(
        select(LgCompanyKey.kind, LgCompanyKey.value_norm).where(LgCompanyKey.company_id == company.id)
    )).all())
    for kind, values in keys.items():
        for value in values:
            if (kind, value) not in existing:
                db.add(LgCompanyKey(workspace_id=company.workspace_id, company_id=company.id,
                                    kind=kind, value_norm=value))
                existing.add((kind, value))


async def _add_contacts(db: AsyncSession, company: LgCompany, finding: FindingIn, now: datetime) -> None:
    existing = set((await db.execute(
        select(LgContact.kind, LgContact.value_norm).where(LgContact.company_id == company.id)
    )).all())
    items = [{"kind": "phone", "value": p} for p in finding.phones] + list(finding.contacts)
    for item in items:
        kind, value = item.get("kind"), (item.get("value") or "").strip()
        norm = _contact_norm(kind, value) if kind and value else None
        if not norm or (kind, norm) in existing:
            continue
        db.add(LgContact(company_id=company.id, kind=kind, value=value, value_norm=norm,
                         is_personal=bool(item.get("is_personal")), person_name=item.get("person_name"),
                         person_role=item.get("person_role"), source=finding.source,
                         source_url=item.get("source_url") or finding.source_url, found_at=now))
        existing.add((kind, norm))


async def upsert_company(db: AsyncSession, workspace_id: int, finding: FindingIn, *,
                         now: datetime | None = None) -> UpsertResult:
    """Находка -> компания. Не коммитит: вызывающий решает, когда сохранить."""
    now = now or utcnow()
    settings = await get_settings(db)
    core = finding.as_core()
    platforms = tuple(settings["platform_domains"])
    keys = normalized_keys(core)
    lookup = await _lookup_index(db, workspace_id, keys)
    match = plan_match(core, lookup, platforms=platforms)
    if match.action == "skip":
        return UpsertResult(None, match, False)

    created = False
    if match.action == "attach":
        company = await db.get(LgCompany, int(match.company_id))
    else:
        company = LgCompany(workspace_id=workspace_id, stage="new", score=0, score_reasons=[],
                            provenance={}, first_seen_at=now, last_seen_at=now)
        if match.action == "review":
            company.needs_review = f"{match.reason}:{match.company_id}"
        db.add(company)
        await db.flush()
        created = True

    values = {
        "display_name": clean_company_name(finding.name or finding.legal_name),
        "legal_name": finding.legal_name,
        "inn": validate_inn(finding.inn),
        "domain": normalize_domain(finding.domain),
        "city": finding.city,
        "region_code": finding.region_code,
        "niche": finding.niche,
    }
    if match.action == "review":
        # Конфликтный ключ остаётся у старой компании: новую не склеиваем по нему.
        keys = {k: v for k, v in keys.items() if k != match.matched_by}
    _apply_fields(company, values, finding.source, finding.source_url, now)
    company.last_seen_at = now
    await _add_keys(db, company, keys)
    await _add_contacts(db, company, finding, now)
    await db.flush()
    return UpsertResult(company, match, created)


# ---------------------------------------------------------------- сигналы и скоринг

async def add_signal(db: AsyncSession, company: LgCompany, kind: str, *, key: str | None = None,
                     payload: dict | None = None, run_id: int | None = None,
                     now: datetime | None = None, recalc: bool = True) -> LgSignal:
    signal = LgSignal(company_id=company.id, run_id=run_id, kind=kind, key=key,
                      payload=payload or {}, observed_at=now or utcnow())
    db.add(signal)
    await db.flush()
    if recalc:
        await recalc_score(db, company, now=now)
    return signal


async def _active_dnc(db: AsyncSession, workspace_id: int, now: datetime) -> list[DncEntry]:
    rows = (await db.execute(select(LgDnc).where(LgDnc.workspace_id == workspace_id))).scalars().all()
    entries = [DncEntry(r.kind, r.value_norm, _aware(r.until)) for r in rows]
    return [e for e in entries if e.active(now)]


async def recalc_score(db: AsyncSession, company: LgCompany, *, now: datetime | None = None) -> int:
    now = now or utcnow()
    settings = await get_settings(db)
    kinds = [k for (k,) in (await db.execute(
        select(LgSignal.kind).where(LgSignal.company_id == company.id,
                                    (LgSignal.expires_at.is_(None)) | (LgSignal.expires_at > now))
    )).all()]
    dnc = await _active_dnc(db, company.workspace_id, now)
    keys = {(e.kind, e.value_norm) for e in dnc}
    in_dnc = ("company", str(company.id)) in keys or bool(company.domain and ("domain", company.domain) in keys)
    result = compute_score(kinds, weights=settings["weights"], in_dnc=in_dnc,
                           legal_status=company.legal_status, fit_label=company.fit_label)
    company.score = result.score
    company.score_reasons = result.reasons
    await db.flush()
    return result.score


# ---------------------------------------------------------------- склейка вручную

async def merge_companies(db: AsyncSession, keep_id: int, drop_id: int, *, reason: str,
                          actor_id: int | None = None, now: datetime | None = None) -> LgCompany:
    """Переносит всё с drop на keep, пишет сигнал merged со снимком drop (для будущей отмены)."""
    if keep_id == drop_id:
        raise ValueError("cannot merge a company into itself")
    now = now or utcnow()
    keep = await db.get(LgCompany, keep_id)
    drop = await db.get(LgCompany, drop_id)
    if keep is None or drop is None or keep.workspace_id != drop.workspace_id:
        raise ValueError("companies not found in one workspace")
    if keep.inn and drop.inn and keep.inn != drop.inn:
        raise ValueError("different INN: merge refused")

    snapshot = {f: getattr(drop, f) for f in COMPANY_FIELDS}
    if drop.inn:
        drop.inn = None  # освобождаем уникальный ИНН до переноса на keep
        await db.flush()
    for name in COMPANY_FIELDS:
        drop_val = snapshot[name]
        if drop_val in (None, ""):
            continue
        keep_src = ((keep.provenance or {}).get(name) or {}).get("source")
        drop_src = ((drop.provenance or {}).get(name) or {}).get("source")
        if getattr(keep, name) in (None, "") or SOURCE_RANK.get(drop_src, 0) > SOURCE_RANK.get(keep_src, 0):
            setattr(keep, name, drop_val)
            prov = dict(keep.provenance or {})
            prov[name] = (drop.provenance or {}).get(name) or {"source": "merge", "at": now.isoformat()}
            keep.provenance = prov

    async def move_unique(model, cols):
        kept = set((await db.execute(select(*[getattr(model, c) for c in cols])
                                     .where(model.company_id == keep_id))).all())
        rows = (await db.execute(select(model).where(model.company_id == drop_id))).scalars().all()
        moved = 0
        for row in rows:
            if tuple(getattr(row, c) for c in cols) in kept:
                await db.delete(row)
            else:
                row.company_id = keep_id
                moved += 1
        return moved

    moved = {
        "keys": await move_unique(LgCompanyKey, ("kind", "value_norm")),
        "contacts": await move_unique(LgContact, ("kind", "value_norm")),
        "ads": await move_unique(LgAd, ("content_hash",)),
        "enrichments": await move_unique(LgEnrichment, ("step",)),
    }
    for model in (LgSignal, LgTouch):
        res = await db.execute(update(model).where(model.company_id == drop_id).values(company_id=keep_id))
        moved[model.__tablename__] = res.rowcount or 0

    keep.first_seen_at = min(_aware(keep.first_seen_at), _aware(drop.first_seen_at))
    keep.last_seen_at = max(_aware(keep.last_seen_at), _aware(drop.last_seen_at))
    if keep.needs_review and keep.needs_review.endswith(f":{drop_id}"):
        keep.needs_review = None
    await db.flush()
    db.add(LgSignal(company_id=keep_id, kind="merged", key=str(drop_id), observed_at=now,
                    payload={"drop_id": drop_id, "reason": reason, "actor_id": actor_id,
                             "moved": moved, "drop_snapshot": snapshot}))
    await db.delete(drop)
    await db.flush()
    await recalc_score(db, keep, now=now)
    return keep


# ---------------------------------------------------------------- можно ли писать

async def check_can_contact(db: AsyncSession, company_id: int, contact_id: int | None, channel: str, *,
                            sequence_id: str | None = None, now: datetime | None = None) -> Decision:
    now = now or utcnow()
    settings = await get_settings(db)
    company = await db.get(LgCompany, company_id)
    if company is None:
        return Decision(False, "not_found")
    contact = await db.get(LgContact, contact_id) if contact_id else None
    if contact is not None and contact.company_id != company.id:
        return Decision(False, "contact:foreign")

    has_open_sales_deal = False
    if company.crm_contact_id:
        query = select(func.count(CrmDeal.id)).where(
            CrmDeal.contact_id == company.crm_contact_id, CrmDeal.closed_at.is_(None),
            CrmDeal.archived_at.is_(None))
        if settings.get("outreach_pipeline_id"):
            query = query.where(CrmDeal.pipeline_id != settings["outreach_pipeline_id"])
        has_open_sales_deal = bool(await db.scalar(query))

    last_out = (await db.execute(
        select(LgTouch.happened_at, LgTouch.sequence_id)
        .where(LgTouch.company_id == company.id, LgTouch.direction == "out", LgTouch.status.in_(OUT_DONE))
        .order_by(LgTouch.happened_at.desc()).limit(1)
    )).first()
    last_touch_at = _aware(last_out[0]) if last_out else None
    active_sequence = last_out[1] if last_out and company.stage == "in_outreach" else None

    state = CompanyState(id=str(company.id), stage=company.stage, domain=company.domain,
                         has_open_sales_deal=has_open_sales_deal, is_client=company.stage == "converted",
                         last_touch_at=last_touch_at, active_sequence_id=active_sequence)
    contact_state = None
    has_shared = True
    if contact is not None:
        contact_state = ContactState(kind=contact.kind, value_norm=contact.value_norm,
                                     verify_status=contact.verify_status, bounced=contact.bounced,
                                     is_personal=contact.is_personal)
        kinds = CHANNEL_KINDS.get(channel, {contact.kind})
        has_shared = bool(await db.scalar(select(func.count(LgContact.id)).where(
            LgContact.company_id == company.id, LgContact.kind.in_(kinds),
            LgContact.is_personal.is_(False), LgContact.verify_status != "invalid",
            LgContact.bounced.is_(False))))
    dnc = await _active_dnc(db, company.workspace_id, now)
    return can_contact(state, contact_state, channel, dnc=dnc, now=now,
                       recontact_days=int(settings["recontact_days"]),
                       has_shared_channel=has_shared, sequence_id=sequence_id)
