"""Раннер источников компаний: оценка, запуск, прогресс, запись в базу.

finder: fetch -> upsert_company + сигналы -> новый список (как поиск по Директу).
enricher: компании из target -> enrich (параллельно, с лимитом) -> контакты, поля, сигналы, lg_enrichments.
Источник в базу не пишет — только раннер. Сигналы источника при перепроверке заменяются (key = код источника).
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from copy import deepcopy
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.jobradar import leads
from app.domains.jobradar import timing as jr_timing
from app.domains.leadgen.core.normalize import normalize_domain
from app.domains.leadgen import service, source_settings
from app.domains.leadgen.core.scoring import REASON_LABELS
from app.domains.leadgen.models import (
    LgCompany, LgCompanyKey, LgContact, LgEnrichment, LgList, LgListItem, LgSignal, LgSource, LgSourceRun,
)
from app.domains.leadgen.sources import (
    REGISTRY, CompanySource, CompanyView, Enrichment, SourceSkipped, limited, short_error,
)
from app.services import progress

logger = logging.getLogger("uvicorn.error.leadgen")
MAX_COMPANIES = 500
SAVE_SECONDS = 0.05        # запись одной компании
_running: set[str] = set()


class RunError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def run_key(run_id: int) -> str:
    return f"leadgen:source-run:{run_id}"


# ---------------------------------------------------------------- вид для экрана

def source_state(row: LgSource) -> str:
    if not row.enabled:
        return "off"
    ok, err = service._aware(row.last_ok_at), service._aware(row.last_error_at)
    if row.last_error and (not ok or (err and err >= ok)):
        return "error"
    return "ok" if ok else "never"


def source_view(adapter: CompanySource, row: LgSource) -> dict:
    settings = source_settings.plain(adapter, row.settings)
    reason = adapter.ready(settings)
    return {
        "code": adapter.code, "kind": adapter.kind, "title": adapter.title, "icon": adapter.icon,
        "purpose": adapter.purpose, "enabled": row.enabled, "needs_key": adapter.needs_key,
        "ready": row.enabled and reason is None, "ready_reason": "источник выключен" if not row.enabled else reason,
        "settings_schema": adapter.settings_schema, "settings": source_settings.public(adapter, row.settings),
        "query_schema": adapter.query_schema if adapter.kind == "finder" else [],
        "state": source_state(row), "last_ok_at": _iso(row.last_ok_at),
        "last_error": row.last_error if source_state(row) == "error" else None,
        "signals": [{"kind": s["kind"], "label": REASON_LABELS.get(s["kind"], s.get("label") or s["kind"])}
                    for s in adapter.signals_info],
        "chainable": adapter.kind == "enricher" and adapter.chainable,
        "chain_default": adapter.kind == "enricher" and adapter.chain_default,
    }


def _iso(value) -> str | None:
    value = service._aware(value)
    return value.isoformat() if value else None


async def adapter_row(db: AsyncSession, code: str) -> tuple[CompanySource, LgSource]:
    adapter = REGISTRY.get(code)
    if adapter is None:
        raise RunError(404, "Источник не найден")
    rows = await source_settings.ensure(db)
    return adapter, rows[code]


# ---------------------------------------------------------------- цели и форма

async def resolve_target(db: AsyncSession, ws: int, target: dict | None) -> list[int]:
    target = target or {}
    keys = [k for k in ("company_ids", "list_id", "segment_id") if target.get(k) not in (None, [], "")]
    if len(keys) != 1:
        raise RunError(422, "Укажите компании: выбранные, список или сегмент")
    if keys[0] == "company_ids":
        raw = [int(i) for i in target["company_ids"]]
        ids = (await db.scalars(select(LgCompany.id).where(LgCompany.workspace_id == ws,
                                                           LgCompany.id.in_(raw)))).all()
    elif keys[0] == "list_id":
        lst = await db.get(LgList, int(target["list_id"]))
        if lst is None or lst.workspace_id != ws:
            raise RunError(404, "Список не найден")
        ids = (await db.scalars(select(LgListItem.company_id).where(LgListItem.list_id == lst.id))).all()
    else:
        from app.domains.leadgen.models import LgSegment
        from app.domains.leadgen.queries import FilterError, company_query
        seg = await db.get(LgSegment, int(target["segment_id"]))
        if seg is None or seg.workspace_id != ws:
            raise RunError(404, "Сегмент не найден")
        try:
            query = company_query(ws, dict(seg.filters or {}), service.utcnow())
        except FilterError as exc:
            raise RunError(422, str(exc))
        ids = (await db.scalars(query.with_only_columns(LgCompany.id).limit(MAX_COMPANIES + 1))).all()
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise RunError(422, "Нет компаний для проверки")
    if len(ids) > MAX_COMPANIES:
        raise RunError(422, f"Не больше {MAX_COMPANIES} компаний за запуск — сейчас {len(ids)}")
    return ids


def clean_query(adapter: CompanySource, query: dict | None) -> dict:
    """Значения формы по query_schema: типы, min/max, варианты multi/select, обязательные поля (adapter.missing)."""
    query = dict(query or {})
    specs = {s["key"]: s for s in adapter.query_schema}
    unknown = [k for k in query if k not in specs]
    if unknown:
        raise RunError(422, f"Неизвестное поле формы: {unknown[0]}")
    out = {}
    for key, spec in specs.items():
        value = query.get(key, spec.get("default"))
        kind = spec.get("type")
        empty = value in (None, "", [])
        label = spec["label"]
        if kind == "number" and not empty:
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise RunError(422, f"«{label}»: нужно число")
            if spec.get("min") is not None and value < spec["min"] or spec.get("max") is not None and value > spec["max"]:
                raise RunError(422, f"«{label}»: от {spec.get('min', 0)} до {spec.get('max', '…')}")
        elif kind == "list":
            items = value.splitlines() if isinstance(value, str) else list(value or [])
            value = list(dict.fromkeys(str(v).strip()[:200] for v in items if str(v).strip()))[:500]
        elif kind in ("multi", "select") and not empty:
            allowed = {o["value"] for o in spec.get("options") or []}
            values = value if isinstance(value, list) else [value]
            bad = [v for v in values if v not in allowed]
            if bad or (kind == "select" and isinstance(value, list)):
                raise RunError(422, f"«{label}»: нет варианта «{(bad or values)[0]}»")
            value = list(dict.fromkeys(values)) if kind == "multi" else value
        elif kind == "bool":
            value = bool(value)
        elif kind in ("text", "region") and not empty:
            value = str(value).strip()[:1000]
        out[key] = value
    label = adapter.missing(out)
    if label:
        raise RunError(422, f"Заполните поле «{label}»")
    problem = adapter.problem(out)
    if problem:
        raise RunError(422, problem)
    return out


# ---------------------------------------------------------------- оценка

def _unit_seconds(adapter: CompanySource, row: LgSource, settings: dict) -> float:
    learned = jr_timing._learned.get(adapter.code) or float((row.stats or {}).get("sec_per_unit") or 0)
    if learned:
        return learned
    cost = getattr(adapter, "unit_cost", None)
    return float(cost(settings)) if cost else adapter.unit_seconds


async def _with_site(db: AsyncSession, ids: list[int]) -> int:
    return len((await db.scalars(select(LgCompany.id).where(LgCompany.id.in_(ids),
                                                            LgCompany.domain.isnot(None)))).all())


async def clean_chain(db, adapter, then):
    if then is None:
        return []
    if adapter.kind != "finder":
        raise RunError(422, "Цепочка доступна только после поиска")
    out = []
    for code in then:
        child = REGISTRY.get(code)
        if child is None or child.kind != "enricher" or not child.chainable:
            raise RunError(422, "В цепочке допускаются только известные источники дообогащения")
        if code in out:
            raise RunError(422, "Шаги цепочки не должны повторяться")
        out.append(code)
    return out


def _sum_cost(current, extra):
    if not extra:
        return current
    if not current:
        return dict(extra)
    rub = None if current.get("rub") is None or extra.get("rub") is None else current["rub"] + extra["rub"]
    return {"requests": current["requests"] + extra["requests"], "rub": round(rub, 4) if rub is not None else None}


def pipeline_seconds(companies: int, steps: list[tuple[int, float]]) -> float:
    """Время конвейера: шаги идут одновременно по разным компаниям. Не меньше самого медленного шага и не меньше
    «компаний / PIPELINE_WORKERS × сумма шагов на одну компанию». steps: [(concurrency, секунд на компанию)]."""
    if not companies or not steps:
        return 0.0
    slowest = max(math.ceil(companies / max(1, conc)) * sec for conc, sec in steps)
    flow = math.ceil(companies / PIPELINE_WORKERS) * sum(sec for _, sec in steps)
    return max(slowest, flow)


async def estimate(db: AsyncSession, ws: int, code: str, *, query: dict | None, target: dict | None,
                   then: list[str] | None = None) -> dict:
    adapter, row = await adapter_row(db, code)
    settings = source_settings.plain(adapter, row.settings)
    chain = await clean_chain(db, adapter, then)
    companies = None
    if adapter.kind == "enricher":
        ids = await resolve_target(db, ws, target)
        companies = len(ids)
        units = await _with_site(db, ids) if adapter.requires_site else len(ids)
    else:
        query = clean_query(adapter, query)
        units = adapter.units(query, {}, settings)
    rounds = math.ceil(max(1, units) / max(1, adapter.concurrency)) if units else 0
    work = round(rounds * _unit_seconds(adapter, row, settings), 1)
    save = round(0.5 + SAVE_SECONDS * (companies if companies is not None else units * 10), 1)
    out = {"seconds": round(work + save), "units": units,
           "steps": [{"code": adapter.code, "name": adapter.title, "seconds": work},
                     {"code": "save", "name": "Запись в базу", "seconds": save}]}
    if companies is not None:
        out["companies"] = companies
    cost_query = query or {}
    if adapter.code == "find_site":
        missing_site = await db.scalar(select(func.count()).select_from(LgCompany).where(
            LgCompany.id.in_(ids), LgCompany.domain.is_(None)))
        cost_query = {"companies": missing_site}
    cost = adapter.cost(cost_query, settings)
    expected = min(MAX_COMPANIES, max(1, units * 10))
    flow = []
    for child_code in chain:
        child, child_row = await adapter_row(db, child_code)
        child_settings = source_settings.plain(child, child_row.settings)
        available = child_row.enabled and child.ready(child_settings) is None
        per = _unit_seconds(child, child_row, child_settings)
        duration = math.ceil(expected / max(1, child.concurrency)) * per if available else 0
        out["steps"].append({"code": child_code, "name": child.title, "seconds": round(duration, 1), "parallel": True})
        if available:
            flow.append((child.concurrency, per))
            cost = _sum_cost(cost, child.cost({"companies": expected}, child_settings))
    out["seconds"] += round(pipeline_seconds(expected, flow))
    if cost:
        out["cost"] = cost
    return out


# ---------------------------------------------------------------- запуск

async def create_run(db: AsyncSession, ws: int, code: str, *, query: dict | None, target: dict | None,
                     list_name: str | None, admin_id: int | None, then: list[str] | None = None) -> LgSourceRun:
    adapter, row = await adapter_row(db, code)
    settings = source_settings.plain(adapter, row.settings)
    chain = await clean_chain(db, adapter, then)
    if not row.enabled:
        raise RunError(400, "Источник не настроен: источник выключен")
    reason = adapter.ready(settings)
    if reason:
        raise RunError(400, f"Источник не настроен: {reason}")
    busy = code in _running or await db.scalar(select(LgSourceRun.id).where(
        LgSourceRun.workspace_id == ws, LgSourceRun.source == code, LgSourceRun.status.in_(("queued", "running")),
        LgSourceRun.created_at > service.utcnow() - timedelta(hours=3)))   # зависший после перезапуска — не держит
    if busy:
        raise RunError(409, "Этот источник уже запущен")
    params: dict = {"kind": adapter.kind}
    if adapter.kind == "enricher":
        params["company_ids"] = await resolve_target(db, ws, target)
        params["target"] = {k: v for k, v in (target or {}).items() if k != "company_ids"} or {"company_ids": True}
    else:
        params["query"] = clean_query(adapter, query)
        params["list_name"] = (list_name or "").strip()[:180] or None
        if then is not None:
            params["then"] = chain
    est = await estimate(db, ws, code, query=query, target=target, then=then)
    run = LgSourceRun(workspace_id=ws, source=code, params=params, status="queued", stats={},
                      created_by_id=admin_id)
    db.add(run)
    await db.flush()
    _running.add(code)
    progress.start(run_key(run.id), f"{adapter.title}",
                   [(s["code"], s["name"], s["seconds"], "pipe" if s.get("parallel") else None) for s in est["steps"]], queued=True)
    return run


async def execute(run_id: int, session_factory, lock=None) -> None:
    """Фоновая задача. lock — очередь на вид задач (обход сайтов не идёт параллельно с другими обходами)."""
    async with session_factory() as db:
        run = await db.get(LgSourceRun, run_id)
        code = run.source if run else None
    if run is None:
        return
    try:
        if lock is not None:
            async with lock:
                await _execute(run_id, session_factory)
        else:
            await _execute(run_id, session_factory)
    finally:
        _running.discard(code)


async def _execute(run_id: int, session_factory) -> None:
    async with session_factory() as db:
        run = await db.get(LgSourceRun, run_id)
        adapter, row = await adapter_row(db, run.source)
        settings = source_settings.plain(adapter, row.settings)
        tracker = progress.tracker(run_key(run_id)) or progress.start(run_key(run_id), adapter.title, [])
        tracker.run()
        run.status, run.started_at = "running", service.utcnow()
        await db.commit()
        result = {"found": 0, "new": 0, "updated": 0, "skipped": 0, "signals": {}, "errors": {}}
        started = time.monotonic()
        primary_duration = None
        per_company = None   # enricher: средняя работа на компанию (конвейер) — для оценки следующего запуска
        try:
            if adapter.kind == "enricher":
                ids = [int(i) for i in run.params.get("company_ids") or []]
                run.stats = {"company_ids": ids}
                timing = await _pipeline(db, run, [(adapter, settings, result)], ids, tracker, chained=False)
                tracker.begin("save")
                tracker.end("save")
                seconds, units = timing[adapter.code]
                per_company = seconds / units if units else None
            else:
                units = await _find(db, run, adapter, settings, tracker, result)
                primary_duration = time.monotonic() - started
                await db.commit()  # preserve the search/list even if a subsequent optional step fails
                if run.params.get("then"):
                    result["chain"] = {}
                    await _run_steps(db, run, list(run.params.get("then") or []),
                                     list((run.stats or {}).get("company_ids") or []), tracker, result)
                    run.stats = {**(run.stats or {}), "result": deepcopy(result)}
                    await db.commit()
        except Exception as exc:  # noqa: BLE001
            message = str(exc) if isinstance(exc, SourceSkipped) else short_error(exc)
            if not isinstance(exc, SourceSkipped):
                logger.exception("leadgen source %s run %s failed", run.source, run_id)
            await db.rollback()
            run = await db.get(LgSourceRun, run_id)
            row = await db.get(LgSource, run.source)
            result["errors"][adapter.code] = message
            run.status, run.error, run.finished_at = "failed", message, service.utcnow()
            run.stats = {**(run.stats or {}), "result": deepcopy(result)}
            source_settings.mark(row, service.utcnow(), message)
            await db.commit()
            tracker.finish("error", f"Ошибка: {message}")
            return
        if per_company:
            jr_timing.learn(adapter, row, per_company, 1, persist=True)
        elif units and primary_duration is not None:
            jr_timing.learn(adapter, row, primary_duration, units, persist=True)
        source_settings.mark(row, service.utcnow(), None)
        run.status, run.finished_at = "done", service.utcnow()
        run.stats = {**(run.stats or {}), "result": deepcopy(result)}
        await db.commit()
        tracker.finish("done", _summary(adapter, result))


def _summary(adapter: CompanySource, r: dict) -> str:
    if adapter.kind == "finder":
        text = f"Готово: найдено {r['found']}, новых {r['new']}"
    else:
        text = f"Готово: проверено {r['updated']}"
        if r["skipped"]:
            text += f", пропущено {r['skipped']}"
    if r["signals"]:
        text += ", сигналов " + str(sum(r["signals"].values()))
    return text


# ---------------------------------------------------------------- enricher

PIPELINE_WORKERS = 20      # компаний одновременно; у каждого шага ещё свой лимит (adapter.concurrency)
COMMIT_EVERY = 20          # компаний между коммитами (без ожидания «пачки»: следующая стартует сразу)


async def _run_steps(db, run, codes, ids, tracker, result, *, list_mode: bool = False) -> None:
    """Шаги цепочки или «функций по базе» — одним конвейером. Выключенные и ненастроенные — сразу «пропущено»."""
    steps, rows = [], {}
    for code in codes:
        adapter, row = await adapter_row(db, code)
        settings = source_settings.plain(adapter, row.settings)
        summary = {"found": 0, "updated": 0, "done": 0, "skipped": 0, "failed": 0,
                   "signals": {}, "errors": {}, "reasons": {}}
        result["chain"][code] = summary
        reason = "источник выключен" if not row.enabled else adapter.ready(settings)
        if reason:
            summary["skipped"] = len(ids)
            summary["reasons"][reason if list_mode else "источник выключен или не настроен"] = len(ids)
            if list_mode:
                result["errors"][code] = f"{adapter.title}: {reason}"
            tracker.end(code, "skipped", reason if list_mode else "источник выключен или не настроен")
            continue
        steps.append((adapter, settings, summary))
        rows[code] = row
    if not steps:
        return
    timing = await _pipeline(db, run, steps, ids, tracker)
    for adapter, _settings, summary in steps:
        seconds, units = timing[adapter.code]
        if units:   # средняя работа на компанию — по ней оценка следующего запуска
            jr_timing.learn(adapter, rows[adapter.code], seconds / units, 1, persist=True)
        source_settings.mark(rows[adapter.code], service.utcnow(), next(iter(summary["errors"].values()), None))
        if list_mode:
            result["updated"] += summary["done"]
            for kind, n in summary["signals"].items():
                result["signals"][kind] = result["signals"].get(kind, 0) + n
            if summary["errors"]:
                result["errors"][adapter.code] = next(iter(summary["errors"].values()))


async def _safe_enrich(adapter, view, settings, ctx) -> Enrichment:
    try:
        return await adapter.enrich(view, settings, ctx)
    except SourceSkipped as skip:
        return Enrichment(status="skipped", error=str(skip)[:120])
    except Exception as exc:  # noqa: BLE001 — один сайт не валит запуск
        return Enrichment(status="failed", error=short_error(exc))


async def _pipeline(db, run, steps, ids, tracker, *, chained: bool = True) -> dict[str, tuple[float, int]]:
    """Конвейер: каждая компания проходит свои шаги по порядку (сайт → контакты → проверка почт → …), и так
    одновременно идут до PIPELINE_WORKERS компаний. Сеть — параллельно (у шага свой лимит), запись в базу —
    строго по одной под замком: AsyncSession не терпит одновременных запросов. Общий Context запуска —
    общий кеш обходов: один сайт качается один раз на все функции.
    steps: [(adapter, settings, summary)]. -> {code: (секунд работы в сумме, компаний обработано)}."""
    ctx = source_settings.context()
    ctx.cache["leadgen_settings"] = await service.get_settings(db)
    if source_settings.sleep is not None:
        ctx.cache["crawl_delay"] = 0
    lock = asyncio.Lock()
    gates = {a.code: asyncio.Semaphore(max(1, a.concurrency)) for a, _, _ in steps}
    spent = {a.code: [0.0, 0] for a, _, _ in steps}
    failures: dict[str, dict[str, int]] = {a.code: {} for a, _, _ in steps}
    present = set((await db.scalars(select(LgCompany.id).where(
        LgCompany.id.in_(ids), LgCompany.workspace_id == run.workspace_id))).all())
    ids = [i for i in ids if i in present]
    for adapter, _settings, summary in steps:
        summary.setdefault("reasons", {})
        summary.setdefault("done", 0)
        summary.setdefault("failed", 0)
        summary["found"] = len(ids)
        tracker.begin(adapter.code, total=len(ids))
    finished = 0

    async def one_company(cid: int) -> None:
        nonlocal finished
        for adapter, settings, summary in steps:
            code = adapter.code
            async with lock:
                company = await db.get(LgCompany, cid)
                if adapter.requires_site and not company.domain:
                    summary["skipped"] += 1
                    summary["reasons"]["нет сайта"] = summary["reasons"].get("нет сайта", 0) + 1
                    tracker.tick(code)
                    continue
                contacts = (await db.scalars(select(LgContact).where(LgContact.company_id == cid))).all()
                view = CompanyView(id=company.id, name=company.display_name, domain=company.domain, city=company.city,
                                   inn=company.inn, region_code=company.region_code, legal_name=company.legal_name,
                                   provenance=company.provenance or {},
                                   contacts=[{"id": x.id, "kind": x.kind, "value": x.value, "is_personal": x.is_personal,
                                              "verified_at": x.verified_at} for x in contacts])
            started = time.monotonic()
            async with gates[code]:
                enr = await _safe_enrich(adapter, view, settings, ctx)
            async with lock:
                company = await db.get(LgCompany, cid)
                try:
                    async with db.begin_nested():
                        enr = await adapter.persist(db, company, enr, ctx)
                        await apply_enrichment(db, company, code, enr, run_id=run.id)
                except Exception as exc:  # noqa: BLE001
                    await db.refresh(company)
                    enr = Enrichment(status="failed", error=short_error(exc))
                    await apply_enrichment(db, company, code, enr, run_id=run.id)
                spent[code][0] += time.monotonic() - started
                spent[code][1] += 1
                for key, count in enr.counters.items():
                    summary[key] = summary.get(key, 0) + count
                if enr.status == "done":
                    summary["done"] += 1
                    summary["updated"] += 1
                    for sig in enr.signals:
                        summary["signals"][sig.kind] = summary["signals"].get(sig.kind, 0) + 1
                else:
                    if enr.status != "failed" or not chained:
                        summary["skipped"] += 1
                    reason = enr.error or "нет данных"
                    summary["reasons"][reason] = summary["reasons"].get(reason, 0) + 1
                    if enr.status == "failed":
                        summary["failed"] += 1
                        failures[code][reason] = failures[code].get(reason, 0) + 1
            tracker.tick(code)
        async with lock:
            finished += 1
            if finished % COMMIT_EVERY == 0:
                await db.commit()

    await limited(ctx, ids, one_company, PIPELINE_WORKERS)
    async with lock:
        await db.commit()
    for adapter, _settings, summary in steps:
        tracker.end(adapter.code, note=f"сделано {summary['updated']}")
        if failures[adapter.code]:
            top = max(failures[adapter.code], key=failures[adapter.code].get)
            summary["errors"][adapter.code] = f"не проверено {sum(failures[adapter.code].values())}: {top}"[:120]
    return {code: (sec, n) for code, (sec, n) in spent.items()}


async def apply_enrichment(db: AsyncSession, company: LgCompany, code: str, enr: Enrichment, *,
                           run_id: int | None = None) -> None:
    """Пишет результат enricher'а. Прежние сигналы этого источника снимаются — остаются только актуальные."""
    now = service.utcnow()
    if enr.review:
        company.needs_review = company.needs_review or enr.review
    if enr.status == "done":
        await db.execute(delete(LgSignal).where(LgSignal.company_id == company.id, LgSignal.key == code))
        for s in enr.signals:
            db.add(LgSignal(company_id=company.id, run_id=run_id, kind=s.kind, key=code, payload=s.payload or {},
                            observed_at=now,
                            expires_at=now + timedelta(days=s.ttl_days) if s.ttl_days else None))
        await _add_contacts(db, company, code, enr.contacts, now)
        if enr.fields:
            if code == "find_site":
                await _identity_fields(db, company, enr, now)
            else:
                service._apply_fields(company, enr.fields, REGISTRY[code].field_source or code, None, now)
        for update in enr.contact_updates:
            contact = await db.scalar(select(LgContact).where(LgContact.id == update["id"],
                LgContact.company_id == company.id, LgContact.is_personal.is_(False)))
            if contact is not None and update["verify_status"] in {"valid", "invalid", "risky"}:
                contact.verify_status, contact.verified_at = update["verify_status"], update["verified_at"]
    row = await db.scalar(select(LgEnrichment).where(LgEnrichment.company_id == company.id, LgEnrichment.step == code))
    if row is None:
        row = LgEnrichment(company_id=company.id, step=code)
        db.add(row)
    if enr.status == "done" or row.status != "done":   # неудачная перепроверка не затирает прежний профиль
        row.result = enr.profile or {}
        row.status = enr.status
    row.error, row.ran_at, row.cost_rub = enr.error, now, 0
    await db.flush()
    if enr.status == "done":
        await service.recalc_score(db, company, now=now)


async def _identity_fields(db, company, enr, now):
    keys = {k: v for k, v in enr.fields.items() if k in {"inn", "domain"}}
    for key, value in keys.items():
        other = await db.scalar(select(LgCompany.id).where(LgCompany.workspace_id == company.workspace_id,
            LgCompany.id != company.id, getattr(LgCompany, key) == value))
        keyed = await db.scalar(select(LgCompanyKey.id).where(LgCompanyKey.workspace_id == company.workspace_id,
            LgCompanyKey.company_id != company.id, LgCompanyKey.kind == key, LgCompanyKey.value_norm == value))
        if other or keyed:
            company.needs_review = "find_site:duplicate"
            enr.profile["conflict"] = "find_site:duplicate"
            enr.counters.clear()
            enr.status, enr.error = "skipped", "конфликт ИНН или домена, нужна ручная проверка"
            return
    try:
        async with db.begin_nested():
            service._apply_fields(company, keys, "site_search", enr.profile.get("url"), now)
            await service._add_keys(db, company, {k: [getattr(company, k)] for k in keys if getattr(company, k)})
            await db.flush()
    except IntegrityError:
        await db.refresh(company)
        company.needs_review = "find_site:duplicate"
        enr.profile["conflict"] = "find_site:duplicate"
        enr.counters.clear()
        enr.status, enr.error = "skipped", "конфликт ИНН или домена, нужна ручная проверка"


async def _add_contacts(db, company, code, contacts, now) -> None:
    """Только общие контакты компании: is_personal=True не сохраняем вовсе."""
    if not contacts:
        return
    existing = set((await db.execute(select(LgContact.kind, LgContact.value_norm)
                                     .where(LgContact.company_id == company.id))).all())
    for c in contacts:
        if c.get("is_personal") or not c.get("kind") or not c.get("value"):
            continue
        norm = service._contact_norm(c["kind"], str(c["value"]))
        if not norm or (c["kind"], norm) in existing:
            continue
        db.add(LgContact(company_id=company.id, kind=c["kind"], value=str(c["value"])[:500], value_norm=norm,
                         is_personal=False, source=code, source_url=c.get("source_url"), found_at=now))
        existing.add((c["kind"], norm))


# ---------------------------------------------------------------- finder

async def _find(db, run, adapter, settings, tracker, result) -> int:
    query = run.params.get("query") or {}
    units = adapter.units(query, {}, settings)
    tracker.begin(adapter.code, total=units)
    ctx = source_settings.context()
    ctx.on_request = lambda: tracker.tick(adapter.code)
    ctx.cache["errors"] = {}
    ctx.cache["leadgen_settings"] = await service.get_settings(db)
    candidates = await adapter.fetch(query, settings, ctx)
    errors = ctx.cache.get("errors") or {}
    result["errors"].update(errors)
    note = f"найдено {len(candidates)}" + ("; " + "; ".join(errors.values()) if errors else "")
    tracker.end(adapter.code, note=note[:200])
    tracker.begin("save", total=len(candidates))
    name = run.params.get("list_name") or f"{adapter.title} — {service.utcnow():%d.%m.%Y %H:%M}"
    lst = LgList(workspace_id=run.workspace_id, name=name[:180], kind="run", run_id=run.id,
                 created_by_id=run.created_by_id)
    db.add(lst)
    await db.flush()
    ids, new_ids, seen = [], [], set()
    for cand in candidates:
        tracker.tick("save")
        domain = normalize_domain(cand.finding.domain) if cand.finding.domain else None
        if await leads._domain_blocked(db, run.workspace_id, domain, service.utcnow()):
            result["skipped"] += 1    # стоп-лист по домену: компанию даже не заводим
            continue
        res = await service.upsert_company(db, run.workspace_id, cand.finding)
        company = res.company
        if company is None or await leads._company_blocked(db, company, service.utcnow()):
            result["skipped"] += 1
            continue
        if company.id in seen:
            continue
        seen.add(company.id)
        ids.append(company.id)
        if res.created:
            new_ids.append(company.id)
        now = service.utcnow()
        for s in cand.signals:
            db.add(LgSignal(company_id=company.id, run_id=run.id, kind=s.kind, key=adapter.code,
                            payload=s.payload or {}, observed_at=now,
                            expires_at=now + timedelta(days=s.ttl_days) if s.ttl_days else None))
            result["signals"][s.kind] = result["signals"].get(s.kind, 0) + 1
        db.add(LgListItem(list_id=lst.id, company_id=company.id, is_new=res.created))
        await db.flush()
        await service.recalc_score(db, company, now=now)
    result.update(found=len(ids), new=len(new_ids), updated=len(ids) - len(new_ids))
    run.stats = {"company_ids": ids, "new_company_ids": new_ids, "list_id": lst.id}
    tracker.end("save")
    return units


# ---------------------------------------------------------------- тех-профиль в карточке

PLATFORMS = ("tilda", "wordpress", "bitrix")


async def tech_for(db: AsyncSession, company_id: int) -> dict | None:
    row = await db.scalar(select(LgEnrichment).where(LgEnrichment.company_id == company_id,
                                                     LgEnrichment.step == "site_tech"))
    if row is None or row.status != "done" or not row.result:
        return None
    p = row.result
    platform = p.get("platform")
    return {
        "checked_at": _iso(row.ran_at),
        "platform": platform if platform in PLATFORMS else ("other" if platform else None),
        "analytics": (["yandex_metrika"] if p.get("has_metrika") else []) + (["google_analytics"] if p.get("has_ga") else []),
        "calltracking": list(p.get("calltracking") or []),
        "chat": list(p.get("chats") or []),
        "crm_forms": list(p.get("crm_forms") or []),
        "quiz": bool(p.get("has_quiz")),
        "privacy_policy": bool(p.get("has_privacy_policy")),
        "footer_year": p.get("footer_year"),
        "pagespeed_mobile": p.get("mobile_speed"),
        "tag_manager": bool(p.get("has_gtm")),   # счётчики могут стоять через GTM и не видны в html
    }


# ---------------------------------------------------------------- запуск функций по базе (WORKSPACE.md)

# Порядок выполнения — всегда этот, как бы ни прислали: сначала сайт и ИНН, потом почты, потом проверки.
WORKSPACE_ORDER = ("find_site", "site_contacts", "email_by_inn", "email_check", "name_clean", "site_tech", "dadata", "fns_revenue",
                   "fit_ai", "personalize")


def list_run_source(list_id: int) -> str:
    return f"list:{list_id}"


def ordered_steps(steps) -> list[str]:
    steps = list(dict.fromkeys(steps or []))
    if not steps:
        raise RunError(422, "Выберите хотя бы одну функцию")
    unknown = [s for s in steps if s not in WORKSPACE_ORDER or s not in REGISTRY]
    if unknown:
        raise RunError(422, f"Неизвестная функция: {unknown[0]}")
    return [s for s in WORKSPACE_ORDER if s in steps]


async def list_targets(db: AsyncSession, ws: int, list_id: int, company_ids: list[int] | None) -> list[int]:
    lst = await db.get(LgList, list_id)
    if lst is None or lst.workspace_id != ws:
        raise RunError(404, "Список не найден")
    query = select(LgCompany.id).join(LgListItem, LgListItem.company_id == LgCompany.id).where(
        LgListItem.list_id == list_id, LgCompany.workspace_id == ws, LgCompany.stage != "hidden")
    if company_ids is not None:
        query = query.where(LgCompany.id.in_([int(i) for i in company_ids]))
    ids = list(dict.fromkeys((await db.scalars(query.order_by(LgCompany.id))).all()))
    if not ids:
        raise RunError(422, "Нет компаний для запуска")
    if len(ids) > MAX_COMPANIES:
        raise RunError(422, f"Не больше {MAX_COMPANIES} компаний за запуск — сейчас {len(ids)}")
    return ids


async def list_estimate(db: AsyncSession, ws: int, list_id: int, *, steps, company_ids=None) -> dict:
    order = ordered_steps(steps)
    ids = await list_targets(db, ws, list_id, company_ids)
    with_site = await _with_site(db, ids)
    missing_site = await db.scalar(select(func.count()).select_from(LgCompany).where(
        LgCompany.id.in_(ids), LgCompany.domain.is_(None)))
    out = {"seconds": 0, "steps": [], "companies": len(ids)}
    cost, tokens, flow = None, 0, []
    for code in order:
        adapter, row = await adapter_row(db, code)
        settings = source_settings.plain(adapter, row.settings)
        available = row.enabled and adapter.ready(settings) is None
        units = (with_site if adapter.requires_site else len(ids)) if available else 0
        rounds = math.ceil(units / max(1, adapter.concurrency)) if units else 0
        per = _unit_seconds(adapter, row, settings)
        seconds = round(rounds * per + SAVE_SECONDS * units, 1)
        step = {"code": code, "name": adapter.title, "seconds": seconds, "parallel": True}
        if units:
            flow.append((adapter.concurrency, per))
        if not available:
            step["skip"] = "источник выключен" if not row.enabled else adapter.ready(settings)
        out["steps"].append(step)
        if available:
            cost = _sum_cost(cost, adapter.cost({"companies": missing_site if code == "find_site" else units},
                                                settings))
            tokens += int(getattr(adapter, "ai_tokens_per_company", 0) or 0) * units
    out["seconds"] = round(pipeline_seconds(len(ids), flow) + SAVE_SECONDS * len(ids))
    if cost:
        out["cost"] = cost
    if tokens:
        out["ai_tokens"] = tokens
    return out


async def create_list_run(db: AsyncSession, ws: int, list_id: int, *, steps, company_ids=None,
                          admin_id: int | None = None) -> LgSourceRun:
    order = ordered_steps(steps)
    source = list_run_source(list_id)
    busy = source in _running or await db.scalar(select(LgSourceRun.id).where(
        LgSourceRun.workspace_id == ws, LgSourceRun.source == source, LgSourceRun.status.in_(("queued", "running")),
        LgSourceRun.created_at > service.utcnow() - timedelta(hours=3)))
    if busy:
        raise RunError(409, "По этой базе уже идёт запуск")
    ids = await list_targets(db, ws, list_id, company_ids)
    est = await list_estimate(db, ws, list_id, steps=order, company_ids=company_ids)
    run = LgSourceRun(workspace_id=ws, source=source, status="queued", created_by_id=admin_id,
                      params={"kind": "list", "list_id": list_id, "steps": order, "company_ids": ids,
                              "scope": "selected" if company_ids is not None else "all"},
                      stats={"company_ids": ids, "list_id": list_id})
    db.add(run)
    await db.flush()
    _running.add(source)
    progress.start(run_key(run.id), "Функции по базе", [(s["code"], s["name"], s["seconds"], "pipe" if s.get("parallel") else None) for s in est["steps"]],
                   queued=True)
    return run


async def execute_list(run_id: int, session_factory, lock=None) -> None:
    async with session_factory() as db:
        run = await db.get(LgSourceRun, run_id)
        source = run.source if run else None
    if run is None:
        return
    try:
        if lock is not None:
            async with lock:
                await _execute_list(run_id, session_factory)
        else:
            await _execute_list(run_id, session_factory)
    finally:
        _running.discard(source)


async def _execute_list(run_id: int, session_factory) -> None:
    async with session_factory() as db:
        run = await db.get(LgSourceRun, run_id)
        tracker = progress.tracker(run_key(run_id)) or progress.start(run_key(run_id), "Функции по базе", [])
        tracker.run()
        run.status, run.started_at = "running", service.utcnow()
        await db.commit()
        ids = list(run.params.get("company_ids") or [])
        result = {"found": len(ids), "new": 0, "updated": 0, "skipped": 0, "signals": {}, "errors": {}, "chain": {}}
        try:
            await _run_steps(db, run, list(run.params.get("steps") or []), ids, tracker, result, list_mode=True)
            run.stats = {**(run.stats or {}), "result": deepcopy(result)}
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("leadgen list run %s failed", run_id)
            await db.rollback()
            run = await db.get(LgSourceRun, run_id)
            message = short_error(exc)
            run.status, run.error, run.finished_at = "failed", message, service.utcnow()
            run.stats = {**(run.stats or {}), "result": deepcopy(result)}
            await db.commit()
            tracker.finish("error", f"Ошибка: {message}")
            return
        run.status, run.finished_at = "done", service.utcnow()
        run.stats = {**(run.stats or {}), "result": deepcopy(result)}
        await db.commit()
        parts = [f"{REGISTRY[c].title}: {s['done']}" for c, s in result["chain"].items()]
        tracker.finish("done", "Готово — " + "; ".join(parts))
