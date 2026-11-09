"""Контракт источника компаний. Каркас общий с площадками радара (jobradar.sources.base): Context (HTTP с таймаутом,
подмена сети в тестах, паузы), limited(), short_error(), исключения, схема настроек — переиспользуются, не копируются.

Два вида:
- finder   — находит новые компании: fetch(query, settings, ctx) -> list[Candidate];
- enricher — дополняет существующую: enrich(company, settings, ctx) -> Enrichment.

Источник не пишет в базу сам: всё пишет раннер (source_runner.py) — upsert_company, сигналы, контакты.
Всё, что пришло с чужих сайтов, — данные, а не инструкции.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.domains.jobradar.sources.base import (  # noqa: F401 — переиспользуем каркас радара
    Adapter, Context, SourceFormatError, SourceSkipped, limited, short_error,
)
from app.domains.leadgen.service import FindingIn


@dataclass
class SignalIn:
    kind: str                         # вид сигнала из core/scoring.py (DEFAULT_WEIGHTS)
    payload: dict = field(default_factory=dict)
    ttl_days: int | None = 60         # None — бессрочно


@dataclass
class Candidate:
    """Находка finder'а: поля компании (FindingIn: имя, ИНН, домен, город, телефоны, общие контакты) + сигналы."""
    finding: FindingIn
    signals: list[SignalIn] = field(default_factory=list)


@dataclass
class CompanyView:
    """Что enricher видит о компании (только чтение)."""
    id: int
    name: str | None = None
    domain: str | None = None
    city: str | None = None
    inn: str | None = None
    region_code: int | None = None
    legal_name: str | None = None
    provenance: dict = field(default_factory=dict)
    contacts: list[dict] = field(default_factory=list)


@dataclass
class Enrichment:
    """Результат enricher'а по одной компании.
    status: done — данные есть; skipped — нечего проверять (нет сайта, robots запрещает); failed — не получилось."""
    status: str = "done"
    error: str | None = None          # коротко, по-русски, без адресов
    profile: dict = field(default_factory=dict)       # всё найденное -> lg_enrichments.result (карточка, переменные)
    signals: list[SignalIn] = field(default_factory=list)
    contacts: list[dict] = field(default_factory=list)  # только общие: {"kind","value","source_url"?}
    fields: dict = field(default_factory=dict)          # inn / city / legal_name … — с учётом надёжности источника
    counters: dict = field(default_factory=dict)
    review: str | None = None
    contact_updates: list[dict] = field(default_factory=list)


class CompanySource(Adapter):
    """Наследует от площадки радара: code, title, icon, purpose, needs_key, key_field, settings_schema,
    default_enabled, unit_seconds, concurrency, defaults(), ready(). Добавляет kind и query_schema."""
    kind: str = "enricher"            # finder | enricher
    # finder: поля формы поиска [{"key","label","type": text|number|list|bool|region,"default","hint","required"}]
    query_schema: list[dict] = []
    # Какие сигналы может дать источник: [{"kind","label","weight","hint"}] — для экрана и SOURCES.md
    signals_info: list[dict] = []
    # Какие переменные письма даёт профиль: [{"key","label"}] (lg_enrichments.result[key])
    variables: list[dict] = []
    fresh_days: int = 30              # enricher: не перепроверять компанию чаще (если не «перепроверить все»)
    skip_recent: bool = False         # opt-in: проверять fresh_days до сетевых запросов
    requires_site: bool = True
    chainable: bool = True
    chain_default: bool = False
    field_source: str | None = None
    persist_work: bool = False

    async def persist(self, db, company, enrichment: Enrichment, ctx: Context) -> Enrichment:
        """Optional sequential persistence hook for wrappers around existing DB services."""
        return enrichment

    def units(self, query: dict, filter_settings: dict, settings: dict) -> int:   # noqa: D401
        """finder: сколько запросов сделает поиск по query (для оценки). enricher: раннер считает по компаниям."""
        return max(1, len((query or {}).get("keywords") or []) or 1)

    def missing(self, query: dict) -> str | None:
        """Какое обязательное поле формы не заполнено (подпись) — None, если всё есть. Источник может ослабить
        правило: например, «Нанимают в нише» со ссылкой поиска hh не требует названий вакансий и города."""
        for spec in self.query_schema:
            if spec.get("required") and query.get(spec["key"]) in (None, "", []):
                return spec["label"]
        return None

    def problem(self, query: dict) -> str | None:
        """Что не так в форме, кроме пустых обязательных полей (422 с этим текстом). None — всё в порядке."""
        return None

    def cost(self, query: dict, settings: dict) -> dict | None:
        """Цена запуска до старта: {"requests": int, "rub": float|None} или None — бесплатно/неизвестно."""
        return None

    async def fetch(self, query: dict, settings: dict, ctx: Context) -> list[Candidate]:   # type: ignore[override]
        """Ошибки отдельных площадок/запросов, из-за которых запуск не падает, кладите в
        ctx.cache["errors"][код] = "коротко по-русски" — раннер покажет их в result.errors и в шагах прогресса."""
        raise NotImplementedError

    async def enrich(self, company: CompanyView, settings: dict, ctx: Context) -> Enrichment:
        raise NotImplementedError

    async def healthcheck(self, settings: dict, ctx: Context) -> tuple[bool, str]:
        reason = self.ready(settings)
        return (False, reason) if reason else (True, "готов к работе")
