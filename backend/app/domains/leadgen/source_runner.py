"""Раннер источников компаний: оценка, запуск, прогресс, запись в базу.

finder: fetch -> upsert_company + сигналы -> новый список (как поиск по Директу).
enricher: компании из target -> enrich (параллельно, с лимитом) -> контакты, поля, сигналы, lg_enrichments.
Источник в базу не пишет — только раннер. Сигналы источника при перепроверке заменяются (key = код источника).
"""
from __future__ import annotations

import logging
import math
import time
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.jobradar import timing as jr_timing
from app.domains.leadgen import service, source_settings
from app.domains.leadgen.core.scoring import REASON_LABELS
from app.domains.leadgen.models import (
    LgCompany, LgContact, LgEnrichment, LgList, LgListItem, LgSignal, LgSource, LgSourceRun,
)
from app.domains.leadgen.sources import (
    REGISTRY, CompanySource, CompanyView, Enrichment, SourceSkipped, limited, short_error,
)
from app.services import progress

logger = logging.getLogger("uvicorn.error.leadgen")
MAX_COMPANIES = 500
CHUNK = 20                 # компаний между коммитами
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
    query = dict(query or {})
    specs = {s["key"]: s for s in adapter.query_schema}
    unknown = [k for k in query if k not in specs]
    if unknown:
        raise RunError(422, f"Неизвестное поле формы: {unknown[0]}")
    out = {}
    for key, spec in specs.items():
        value = query.get(key, spec.get("default"))
        empty = value in (None, "", [])
        if spec.get("required") and empty:
            raise RunError(422, f"Заполните поле «{spec['label']}»")
        if spec.get("type") == "number" and not empty:
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise RunError(422, f"«{spec['label']}»: нужно число")
            if spec.get("min") is not None and value < spec["min"] or spec.get("max") is not None and value > spec["max"]:
                raise RunError(422, f"«{spec['label']}»: от {spec.get('min', 0)} до {spec.get('max', '…')}")
        if spec.get("type") == "list" and isinstance(value, str):
            value = [v.strip() for v in value.splitlines() if v.strip()]
        out[key] = value
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


async def estimate(db: AsyncSession, ws: int, code: str, *, query: dict | None, target: dict | None) -> dict:
    adapter, row = await adapter_row(db, code)
    settings = source_settings.plain(adapter, row.settings)
    companies = None
    if adapter.kind == "enricher":
        ids = await resolve_target(db, ws, target)
        companies = len(ids)
        units = await _with_site(db, ids)
    else:
        units = adapter.units(clean_query(adapter, query), {}, settings)
    rounds = math.ceil(max(1, units) / max(1, adapter.concurrency)) if units else 0
    work = round(rounds * _unit_seconds(adapter, row, settings), 1)
    save = round(0.5 + SAVE_SECONDS * (companies if companies is not None else units * 10), 1)
    out = {"seconds": round(work + save), "units": units,
           "steps": [{"code": adapter.code, "name": adapter.title, "seconds": work},
                     {"code": "save", "name": "Запись в базу", "seconds": save}]}
    if companies is not None:
        out["companies"] = companies
    return out


# ---------------------------------------------------------------- запуск

async def create_run(db: AsyncSession, ws: int, code: str, *, query: dict | None, target: dict | None,
                     list_name: str | None, admin_id: int | None) -> LgSourceRun:
    adapter, row = await adapter_row(db, code)
    settings = source_settings.plain(adapter, row.settings)
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
    est = await estimate(db, ws, code, query=query, target=target)
    run = LgSourceRun(workspace_id=ws, source=code, params=params, status="queued", stats={},
                      created_by_id=admin_id)
    db.add(run)
    await db.flush()
    _running.add(code)
    progress.start(run_key(run.id), f"{adapter.title}",
                   [(s["code"], s["name"], s["seconds"]) for s in est["steps"]], queued=True)
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
        try:
            if adapter.kind == "enricher":
                units = await _enrich_all(db, run, adapter, settings, tracker, result)
            else:
                units = await _find(db, run, adapter, settings, tracker, result)
        except Exception as exc:  # noqa: BLE001
            message = str(exc) if isinstance(exc, SourceSkipped) else short_error(exc)
            if not isinstance(exc, SourceSkipped):
                logger.exception("leadgen source %s run %s failed", run.source, run_id)
            await db.rollback()
            run = await db.get(LgSourceRun, run_id)
            row = await db.get(LgSource, run.source)
            result["errors"][adapter.code] = message
            run.status, run.error, run.finished_at = "failed", message, service.utcnow()
            run.stats = {**(run.stats or {}), "result": result}
            source_settings.mark(row, service.utcnow(), message)
            await db.commit()
            tracker.finish("error", f"Ошибка: {message}")
            return
        if units:
            jr_timing.learn(adapter, row, time.monotonic() - started, units, persist=True)
        source_settings.mark(row, service.utcnow(), None)
        run.status, run.finished_at = "done", service.utcnow()
        run.stats = {**(run.stats or {}), "result": result}
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

async def _enrich_all(db, run, adapter, settings, tracker, result) -> int:
    ids = [int(i) for i in run.params.get("company_ids") or []]
    companies = (await db.scalars(select(LgCompany).where(LgCompany.id.in_(ids)))).all()
    with_site = [c for c in companies if c.domain]
    result["skipped"] += len(companies) - len(with_site)
    result["found"] = len(companies)
    run.stats = {"company_ids": ids}
    tracker.begin(adapter.code, total=len(with_site))
    ctx = source_settings.context()
    failures: dict[str, int] = {}

    async def one(view: CompanyView) -> Enrichment:
        try:
            return await adapter.enrich(view, settings, ctx)
        except SourceSkipped as skip:
            return Enrichment(status="skipped", error=str(skip)[:120])
        except Exception as exc:  # noqa: BLE001 — один сайт не валит запуск
            return Enrichment(status="failed", error=short_error(exc))
        finally:   # отметка по готовности: «проверено 3 из 5» — честно, а не «начато»
            tracker.tick(adapter.code)

    saved = 0
    for start in range(0, len(with_site), CHUNK):
        chunk = with_site[start:start + CHUNK]
        views = [CompanyView(id=c.id, name=c.display_name, domain=c.domain, city=c.city, inn=c.inn,
                             region_code=c.region_code) for c in chunk]
        found = await limited(ctx, views, one, adapter.concurrency)
        for company, enr in zip(chunk, found):
            await apply_enrichment(db, company, adapter.code, enr, run_id=run.id)
            if enr.status == "done":
                result["updated"] += 1
                for s in enr.signals:
                    result["signals"][s.kind] = result["signals"].get(s.kind, 0) + 1
            else:
                result["skipped"] += 1
                if enr.status == "failed":
                    failures[enr.error or "ошибка"] = failures.get(enr.error or "ошибка", 0) + 1
            saved += 1
        await db.commit()
    tracker.end(adapter.code, note=f"проверено {result['updated']}")
    tracker.begin("save")
    tracker.end("save")
    if failures:
        top = max(failures, key=failures.get)
        result["errors"][adapter.code] = f"не проверено {sum(failures.values())}: {top}"[:120]
    return len(with_site)


async def apply_enrichment(db: AsyncSession, company: LgCompany, code: str, enr: Enrichment, *,
                           run_id: int | None = None) -> None:
    """Пишет результат enricher'а. Прежние сигналы этого источника снимаются — остаются только актуальные."""
    now = service.utcnow()
    if enr.status == "done":
        await db.execute(delete(LgSignal).where(LgSignal.company_id == company.id, LgSignal.key == code))
        for s in enr.signals:
            db.add(LgSignal(company_id=company.id, run_id=run_id, kind=s.kind, key=code, payload=s.payload or {},
                            observed_at=now,
                            expires_at=now + timedelta(days=s.ttl_days) if s.ttl_days else None))
        await _add_contacts(db, company, code, enr.contacts, now)
        if enr.fields:
            service._apply_fields(company, enr.fields, code, None, now)
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
    candidates = await adapter.fetch(query, settings, ctx)
    tracker.end(adapter.code, note=f"найдено {len(candidates)}")
    tracker.begin("save", total=len(candidates))
    name = run.params.get("list_name") or f"{adapter.title} — {service.utcnow():%d.%m.%Y %H:%M}"
    lst = LgList(workspace_id=run.workspace_id, name=name[:180], kind="run", run_id=run.id,
                 created_by_id=run.created_by_id)
    db.add(lst)
    await db.flush()
    ids, new_ids, seen = [], [], set()
    for cand in candidates:
        tracker.tick("save")
        res = await service.upsert_company(db, run.workspace_id, cand.finding)
        company = res.company
        if company is None:
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
