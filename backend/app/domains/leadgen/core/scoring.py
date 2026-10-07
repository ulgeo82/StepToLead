"""Скоринг 0–10 из сигналов (спецификация: «Сигналы и скоринг»). Веса приходят из настроек."""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_WEIGHTS: dict[str, int] = {
    "ad_direct": 3,       # показы в Директе по 3+ ключам
    "ad_premium": 2,      # спецразмещение
    "new_advertiser": 2,  # впервые в рекламе за 14 дней
    "ads_stopped": 1,     # пропал из рекламы
    "has_messenger": 2,   # WhatsApp или Telegram
    "site_quiz": 1,
    "no_crm": 1,
    "legal_active": 1,
    "organic_only": 0,
    "hh_marketing": 2,
    "twogis_card": 1,
    "slow_response": 1,
    "chain": -3,
    "merged": 0,
}

REASON_LABELS: dict[str, str] = {
    "ad_direct": "В Директе по 3+ ключам",
    "ad_premium": "Спецразмещение",
    "new_advertiser": "Новый рекламодатель",
    "ads_stopped": "Пропал из рекламы",
    "has_messenger": "Есть WhatsApp или Telegram",
    "site_quiz": "Квиз на сайте",
    "no_crm": "Нет CRM на сайте",
    "legal_active": "Действующее юрлицо",
    "hh_marketing": "Ищут маркетолога",
    "twogis_card": "Есть в 2ГИС",
    "slow_response": "Медленно отвечают на заявки",
    "chain": "Федеральная сеть",
}

MIN_SCORE, MAX_SCORE, FIT_NO_CAP = 0, 10, 2


@dataclass
class ScoreResult:
    score: int
    reasons: list[dict] = field(default_factory=list)  # [{"kind","label","weight"}] + служебные правила


def compute_score(
    signal_kinds,
    *,
    weights: dict[str, int] | None = None,
    in_dnc: bool = False,
    legal_status: str | None = None,
    fit_label: str | None = None,
) -> ScoreResult:
    """Каждый вид сигнала учитывается один раз; неизвестные виды игнорируются."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    reasons: list[dict] = []
    total = 0
    for kind in sorted(set(signal_kinds)):
        weight = w.get(kind)
        if not weight:
            continue
        total += weight
        reasons.append({"kind": kind, "label": REASON_LABELS.get(kind, kind), "weight": weight})
    reasons.sort(key=lambda r: -r["weight"])

    score = max(MIN_SCORE, min(MAX_SCORE, total))
    if in_dnc:
        return ScoreResult(0, reasons + [{"kind": "rule", "label": "В стоп-листе", "weight": None}])
    if legal_status == "liquidated":
        return ScoreResult(0, reasons + [{"kind": "rule", "label": "Компания ликвидирована", "weight": None}])
    if fit_label == "no" and score > FIT_NO_CAP:
        score = FIT_NO_CAP
        reasons.append({"kind": "rule", "label": "Не подходит под ЦА: не выше 2", "weight": None})
    return ScoreResult(score, reasons)
