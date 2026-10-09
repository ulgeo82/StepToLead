"""Фильтры базы компаний: один формат и для списка, и для сохранённых сегментов (lg_segments.filters)."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Select, exists, or_, select

from app.domains.leadgen.core.normalize import text_key
from app.domains.leadgen.models import STAGES, LgCompany, LgContact, LgSignal

SORTS = {
    "score": (LgCompany.score.desc(), LgCompany.id.desc()),
    "new": (LgCompany.first_seen_at.desc(), LgCompany.id.desc()),
    "seen": (LgCompany.last_seen_at.desc(), LgCompany.id.desc()),
    "name": (LgCompany.display_name.asc(), LgCompany.id.asc()),
}
CHANNELS = {"whatsapp": ("whatsapp", "phone"), "telegram": ("telegram",), "email": ("email",),
            "phone": ("phone",), "messenger": ("whatsapp", "telegram")}
FILTER_KEYS = {"q", "niche", "cities", "min_score", "max_score", "stages", "channels", "signals",
               "new_days", "needs_review", "fit"}


class FilterError(ValueError):
    pass


def clean_filters(raw: dict | None) -> dict:
    """Проверяет и нормализует фильтры. Неизвестные ключи — ошибка (чтобы сегмент не «молча» менялся)."""
    raw = dict(raw or {})
    unknown = set(raw) - FILTER_KEYS
    if unknown:
        raise FilterError(f"unknown filters: {', '.join(sorted(unknown))}")
    out: dict = {}
    if raw.get("q"):
        out["q"] = str(raw["q"]).strip()[:120]
    if raw.get("niche"):
        out["niche"] = str(raw["niche"]).strip()[:120]
    if raw.get("cities"):
        out["cities"] = [str(c).strip() for c in raw["cities"] if str(c).strip()][:50]
    for key in ("min_score", "max_score"):
        if raw.get(key) is not None:
            value = int(raw[key])
            if not 0 <= value <= 10:
                raise FilterError(f"{key} must be 0..10")
            out[key] = value
    if raw.get("stages"):
        bad = set(raw["stages"]) - set(STAGES)
        if bad:
            raise FilterError(f"unknown stages: {', '.join(sorted(bad))}")
        out["stages"] = list(raw["stages"])
    if raw.get("channels"):
        bad = set(raw["channels"]) - set(CHANNELS)
        if bad:
            raise FilterError(f"unknown channels: {', '.join(sorted(bad))}")
        out["channels"] = list(raw["channels"])
    if raw.get("signals"):
        out["signals"] = [str(s) for s in raw["signals"]][:20]
    if raw.get("new_days"):
        out["new_days"] = max(1, min(365, int(raw["new_days"])))
    if raw.get("needs_review") is not None:
        out["needs_review"] = bool(raw["needs_review"])
    if raw.get("fit"):
        if raw["fit"] not in ("fit", "maybe", "no"):
            raise FilterError("fit must be fit|maybe|no")
        out["fit"] = raw["fit"]
    return out


def company_query(workspace_id: int, filters: dict, now: datetime) -> Select:
    f = clean_filters(filters)
    q = select(LgCompany).where(LgCompany.workspace_id == workspace_id)
    if f.get("q"):
        raw = f["q"]
        variants = {raw, raw.lower(), raw[:1].upper() + raw[1:]}
        conds = [LgCompany.inn == raw.strip()]
        for v in variants:  # ILIKE по кириллице в SQLite не работает: ищем в типичных регистрах
            conds += [LgCompany.display_name.like(f"%{v}%"), LgCompany.legal_name.like(f"%{v}%"),
                      LgCompany.domain.like(f"%{v.lower()}%")]
        q = q.where(or_(*conds))
    if f.get("niche"):
        q = q.where(LgCompany.niche_key == text_key(f["niche"]))
    if f.get("cities"):
        q = q.where(LgCompany.city_key.in_([text_key(c) for c in f["cities"]]))
    if "min_score" in f:
        q = q.where(LgCompany.score >= f["min_score"])
    if "max_score" in f:
        q = q.where(LgCompany.score <= f["max_score"])
    if f.get("stages"):
        q = q.where(LgCompany.stage.in_(f["stages"]))
    else:
        q = q.where(LgCompany.stage != "hidden")  # скрытые видны только по фильтру «Скрытые»
    if f.get("channels"):
        kinds = sorted({k for ch in f["channels"] for k in CHANNELS[ch]})
        q = q.where(exists().where(LgContact.company_id == LgCompany.id, LgContact.kind.in_(kinds),
                                   LgContact.verify_status != "invalid", LgContact.bounced.is_(False)))
    for kind in f.get("signals", []):
        q = q.where(exists().where(LgSignal.company_id == LgCompany.id, LgSignal.kind == kind,
                                   or_(LgSignal.expires_at.is_(None), LgSignal.expires_at > now)))
    if f.get("new_days"):
        q = q.where(LgCompany.first_seen_at >= now - timedelta(days=f["new_days"]))
    if "needs_review" in f:
        q = q.where(LgCompany.needs_review.is_not(None) if f["needs_review"] else LgCompany.needs_review.is_(None))
    if f.get("fit"):
        q = q.where(LgCompany.fit_label == f["fit"])
    return q
