"""Сколько займёт проход сканера — до запуска, и прогресс во время него.

Оценка = по каждой площадке: «кругов» запросов × секунд на круг. Круг — concurrency запросов разом
(Adapter.units / Adapter.concurrency). Секунды на круг радар учит по факту проходов (скользящее среднее
в jr_sources.stats), пока опыта нет — берёт Adapter.unit_seconds. Площадки опрашиваются одновременно,
поэтому этап «площадки» длится столько, сколько самая медленная из них.
"""
from __future__ import annotations

import math

from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.jobradar import platforms, scanners
from app.domains.jobradar.models import JrFilter, JrSource
from app.domains.jobradar.sources import REGISTRY, Adapter
from app.services import progress

MATCH_SECONDS = 1.5               # склейка дублей и отбор по запросам
AI_SECONDS_PER_ITEM = 1.5         # ИИ-оценка находки (пачкой)
ACTION_SECONDS = {"companies": 2.5, "client_crm": 0.8, "telegram": 3.5}   # на одну находку; Telegram — с паузой
EXPECTED_NEW = 3                  # сколько новых находок ждать, если прошлых проходов нет
_learned: dict[str, float] = {}   # то же, что в базе, плюс опыт предпросмотров (они в базу не пишут)


def run_key(scanner_id: int) -> str:
    return f"jobradar:run:{scanner_id}"


def preview_key(scanner_id: int) -> str:
    return f"jobradar:preview:{scanner_id}"


def seconds_per_unit(adapter: Adapter, row: JrSource | None) -> float:
    return _learned.get(adapter.code) or float(((row.stats if row else None) or {}).get("sec_per_unit") or 0) \
        or adapter.unit_seconds


def rounds(adapter: Adapter, units: int) -> int:
    return max(1, math.ceil(max(1, units) / max(1, adapter.concurrency)))


def learn(adapter: Adapter, row: JrSource | None, seconds: float, units: int, *, persist: bool) -> None:
    """Запомнить, сколько реально заняла площадка. Среднее сглажено: один медленный проход не ломает оценку."""
    measured = max(0.2, seconds / rounds(adapter, units))
    old = _learned.get(adapter.code) or ((row.stats if row else None) or {}).get("sec_per_unit")
    value = measured if not old else 0.6 * float(old) + 0.4 * measured
    _learned[adapter.code] = round(value, 2)
    if persist and row is not None:
        stats = dict(row.stats or {})
        row.stats = {**stats, "sec_per_unit": round(value, 2), "samples": int(stats.get("samples") or 0) + 1}


async def source_steps(db: AsyncSession, f: JrFilter, query: dict) -> list[dict]:
    """По площадкам сканера: {code, name, units, seconds, skip} — skip: почему площадку не опросим."""
    rows = await platforms.ensure(db)
    out = []
    for code in dict.fromkeys(f.sources or []):
        adapter, row = REGISTRY.get(code), rows.get(code)
        if adapter is None or row is None:
            continue
        settings = platforms.plain(adapter, row.settings)
        skip = "площадка выключена" if not row.enabled else adapter.ready(settings)
        fs = dict((f.source_settings or {}).get(code) or {})
        units = adapter.units(query, fs, settings)
        out.append({"code": code, "name": adapter.title, "units": units, "skip": skip or None,
                    "seconds": 0.5 if skip else round(rounds(adapter, units) * seconds_per_unit(adapter, row), 1)})
    return out


def _expected_new(f: JrFilter) -> int:
    last = f.last_result or {}
    return int(last.get("matched") or 0) if last.get("at") else EXPECTED_NEW


def action_seconds(f: JrFilter, items: int) -> float:
    acts = scanners.enabled_actions(f)
    return max(0.5, items * sum(ACTION_SECONDS.get(a, 0.5) for a in acts))


async def plan(db: AsyncSession, f: JrFilter, query: dict, *, kind: str = "run") -> list[tuple[str, str, float]]:
    """Шаги для progress.start: площадки, отбор, (ИИ), действия. kind=preview — без ИИ и действий."""
    steps = [(s["code"], s["name"], s["seconds"], "sources") for s in await source_steps(db, f, query)]
    steps.append(("match", "Отбор по запросам", MATCH_SECONDS, None))
    if kind == "run":
        expected = _expected_new(f)
        if f.ai_min_score:
            steps.append(("ai", "ИИ-оценка", 3 + expected * AI_SECONDS_PER_ITEM, None))
        steps.append(("actions", "Действия с находками", action_seconds(f, expected), None))
    return steps


async def estimate(db: AsyncSession, f: JrFilter, query: dict, *, kind: str = "run") -> dict:
    sources = await source_steps(db, f, query)
    steps = await plan(db, f, query, kind=kind)
    parallel = max([s for _, _, s, g in steps if g == "sources"] or [0])   # площадки — одновременно
    serial = sum(s for _, _, s, g in steps if g != "sources")
    return {"seconds": round(parallel + serial), "kind": kind,
            "steps": [{"code": c, "name": n, "seconds": round(s, 1), "parallel": g == "sources"} for c, n, s, g in steps],
            "sources": sources}


async def start(db: AsyncSession, f: JrFilter, query: dict, *, kind: str = "run", queued: bool = False):
    key = run_key(f.id) if kind == "run" else preview_key(f.id)
    title = f"Проверка «{f.name}»" if kind == "run" else f"Предпросмотр «{f.name}»"
    return progress.start(key, title, await plan(db, f, query, kind=kind), queued=queued)
