"""ИИ-оценка «подходит ли под ЦА»: по сайту, объявлению и данным юрлица -> fit / maybe / no + короткая причина.

Портрет клиента (ICP) — в настройках лидогенерации (ключ "icp"), его можно править из админки.
Модель — та, что настроена в портале (feature="summary"); расход виден в «ИИ: качество и расход».
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.leadgen import service
from app.domains.leadgen.models import LgAd, LgCompany, LgEnrichment, LgSignal
from app.services import ai

STEP = "fit_ai"
FEATURE = "summary"
DEFAULT_ICP = (
    "Подходят: малый и средний бизнес, который сам продаёт свои товары или услуги частным клиентам (B2C) "
    "и получает заявки через рекламу — кухни и мебель на заказ, окна, потолки, ремонт, бани и дома, "
    "медицина и красота, автосервисы. Не подходят: федеральные сети и крупные фабрики, маркетплейсы и агрегаторы, "
    "оптовики и чистый B2B, франшизы с центральным маркетингом, государственные организации, "
    "рекламные и маркетинговые агентства (это конкуренты)."
)
SYSTEM = (
    "Ты помогаешь маркетинговому агентству отобрать компании для холодного предложения. "
    "Оцени компанию по портрету клиента. Отвечай ТОЛЬКО JSON без пояснений вокруг:\n"
    '{"label": "fit" | "maybe" | "no", "reason": "до 120 символов, по-русски, по существу", '
    '"chain": true если это федеральная сеть или крупная фабрика, иначе false}\n'
    "Если данных мало — label \"maybe\". Не выдумывай фактов, которых нет в данных."
)
_JSON = re.compile(r"\{.*\}", re.S)


class FitParseError(ValueError):
    pass


def parse_answer(text: str) -> dict:
    m = _JSON.search(text or "")
    if not m:
        raise FitParseError("нет JSON в ответе")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise FitParseError("битый JSON") from exc
    label = str(data.get("label", "")).strip().lower()
    if label not in ("fit", "maybe", "no"):
        raise FitParseError(f"неизвестная оценка {label!r}")
    reason = re.sub(r"\s+", " ", str(data.get("reason") or "")).strip()[:200] or None
    return {"label": label, "reason": reason, "chain": bool(data.get("chain"))}


def _validator(text: str) -> None:
    try:
        parse_answer(text)
    except FitParseError as exc:
        raise ai.AIError(f"ИИ вернул ответ не по формату: {exc}") from exc


async def build_prompt(db: AsyncSession, company: LgCompany, icp: str) -> str:
    ads = (await db.execute(select(LgAd).where(LgAd.company_id == company.id)
                            .order_by(LgAd.last_seen_at.desc()).limit(3))).scalars().all()
    site = await db.scalar(select(LgEnrichment.result).where(LgEnrichment.company_id == company.id,
                                                             LgEnrichment.step == "site_check"))
    excerpt = ((site or {}).get("text_excerpt") or "")[:2500]
    lines = [f"Портрет клиента агентства:\n{icp}", "", "Компания:"]
    for label, value in (("Название", company.display_name), ("Юрлицо", company.legal_name), ("Сайт", company.domain),
                         ("Город", company.city), ("Ниша (метка поиска)", company.niche), ("ОКВЭД", company.okved),
                         ("Выручка, ₽", f"{company.revenue_rub:,}".replace(",", " ") if company.revenue_rub else None)):
        if value:
            lines.append(f"- {label}: {value}")
    for ad in ads:
        lines.append(f"- Объявление: «{ad.title or ''}» — {ad.text or ''}".rstrip(" —"))
    if excerpt:
        lines += ["", "Текст главной страницы сайта (фрагмент):", excerpt]
    return "\n".join(lines)


async def assess(db: AsyncSession, company: LgCompany, *, now: datetime | None = None,
                 complete=None) -> dict:
    """Оценивает одну компанию. complete — подмена ai.complete в тестах."""
    now = now or service.utcnow()
    settings = await service.get_settings(db)
    icp = (settings.get("icp") or DEFAULT_ICP).strip()
    row = await db.scalar(select(LgEnrichment).where(LgEnrichment.company_id == company.id, LgEnrichment.step == STEP))
    if row is None:
        row = LgEnrichment(company_id=company.id, step=STEP)
        db.add(row)
    row.ran_at = now
    prompt = await build_prompt(db, company, icp)
    call = complete or ai.complete
    try:
        text = await call(SYSTEM, [{"role": "user", "content": prompt}], max_tokens=200, temperature=0.1,
                          feature=FEATURE, workspace_id=company.workspace_id, validator=_validator)
        result = parse_answer(text)
    except (ai.AIError, FitParseError) as exc:
        row.status, row.error, row.result = "failed", str(exc)[:300], {}
        await db.flush()
        return {"status": "failed", "error": str(exc)}
    company.fit_label, company.fit_reason = result["label"], result["reason"]
    if result["chain"]:
        db.add(LgSignal(company_id=company.id, kind="chain", key=STEP, payload={"reason": result["reason"]},
                        observed_at=now, expires_at=now + timedelta(days=365)))
    row.status, row.error, row.result = "done", None, result
    await db.flush()
    await service.recalc_score(db, company, now=now)
    return {"status": "done", **result}


def available() -> bool:
    return ai.configured(FEATURE)
