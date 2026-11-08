"""API сканеров радара: /api/admin/jobradar/scanners, /queries, /templates. Только админы агентства.
Контракт — SCANNERS.md."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import require_admin
from app.db import get_db
from app.domains.jobradar import core, platforms, posting, runner, scanners, timing
from app.domains.jobradar.models import JrFilter, JrMatch, JrQuery, JrTemplate, JrTgChannel, JrVacancy
from app.domains.jobradar.routes import (EXPERIENCE, STATUS_NAMES, _channel_name, _source_settings, _tasks, _words, iso,
                                         workspace_id)
from app.domains.jobradar.sources import REGISTRY, Vacancy
from app.models.access import AdminUser
from app.models.marketing import ClientWorkspace, PortalUser, Project
from app.services import progress

router = APIRouter(prefix="/admin/jobradar", tags=["admin-jobradar"])
CHAT_RE = r"^(-?\d{3,20}|@[A-Za-z0-9_]{4,32})$"


# ---------------------------------------------------------------- модели запросов

class QueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=120)
    enabled: bool | None = None
    keywords: list[str] | None = Field(default=None, max_length=60)
    exclude: list[str] | None = Field(default=None, max_length=60)
    regions: list[str] | None = Field(default=None, max_length=60)
    remote_only: bool | None = None
    salary_from: int | None = Field(default=None, ge=0, le=10_000_000)
    allow_no_salary: bool | None = None
    experience: list[str] | None = Field(default=None, max_length=4)
    position: int | None = Field(default=None, ge=0, le=1000)


class ScannerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=160)
    project_id: int | None = None
    template: str | None = Field(default=None, max_length=40)
    enabled: bool | None = None
    profile: str | None = Field(default=None, max_length=2000)
    ai_min_score: int | None = Field(default=None, ge=0, le=100)
    sources: list[str] | None = Field(default=None, max_length=12)
    source_settings: dict | None = None
    actions: dict | None = None


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days: int = Field(default=3, ge=1, le=14)
    queries: list[QueryIn] | None = Field(default=None, max_length=20)


class EstimateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(default="run", pattern="^(run|preview)$")
    queries: list[QueryIn] | None = Field(default=None, max_length=20)


# ---------------------------------------------------------------- помощники

def _query_values(data: dict) -> dict:
    out = {}
    for key, value in data.items():
        if value is None:
            continue
        if key in ("keywords", "exclude", "regions"):
            value = _words(value, 60)
        elif key == "experience":
            value = [e for e in dict.fromkeys(value) if e in EXPERIENCE]
        elif key == "name":
            value = value.strip()[:120] or "Запрос"
        out[key] = value
    return out


def _query_out(q: JrQuery) -> dict:
    return {"id": q.id, "name": q.name, "enabled": q.enabled, "keywords": q.keywords or [], "exclude": q.exclude or [],
            "regions": q.regions or [], "remote_only": q.remote_only, "salary_from": q.salary_from,
            "allow_no_salary": q.allow_no_salary, "experience": q.experience or [], "position": q.position}


async def _owner(db: AsyncSession, project_id: int | None) -> dict | None:
    if not project_id:
        return None
    project = await db.get(Project, project_id)
    if project is None:
        return None
    ws = await db.get(ClientWorkspace, project.workspace_id)
    return {"project_id": project.id, "name": f"{ws.name} · {project.name}" if ws and ws.name != project.name else project.name}


async def _client_project(db: AsyncSession, project_id: int) -> Project:
    project = await db.get(Project, project_id)
    if project is None or project.workspace_id == workspace_id():
        raise HTTPException(422, "Клиент не найден")
    return project


async def _short(db: AsyncSession, f: JrFilter) -> dict:
    since = runner.now() - timedelta(days=7)
    sent, leads, deals, liked, rated = (await db.execute(select(
        func.count(JrMatch.id).filter(JrMatch.sent_at >= since),
        func.count(JrMatch.id).filter(JrMatch.status == "lead", JrMatch.created_at >= since),
        func.count(JrMatch.id).filter(JrMatch.deal_id.is_not(None), JrMatch.created_at >= since),
        func.count(JrMatch.id).filter(JrMatch.feedback == 1),
        func.count(JrMatch.id).filter(JrMatch.feedback.is_not(None)),
    ).where(JrMatch.filter_id == f.id))).one()
    found = await db.scalar(select(func.count(JrMatch.id)).where(JrMatch.filter_id == f.id, JrMatch.created_at >= since))
    queries = await db.scalar(select(func.count(JrQuery.id)).where(JrQuery.filter_id == f.id, JrQuery.enabled.is_(True)))
    return {"id": f.id, "name": f.name, "enabled": f.enabled, "kind": f.kind or "jobs", "template": f.template,
            "owner": await _owner(db, f.project_id), "sources": f.sources or [], "actions": scanners.enabled_actions(f),
            "queries": int(queries or 0), "last_run_at": iso(f.last_run_at), "last_result": f.last_result or {},
            "stats": {"found_7d": int(found or 0), "sent_7d": int(sent or 0), "leads_7d": int(leads or 0),
                      "deals_7d": int(deals or 0), "rated": int(rated or 0),
                      "liked_share": round(liked * 100 / rated) if rated else None}}


async def _full(db: AsyncSession, f: JrFilter) -> dict:
    return {**await _short(db, f), "profile": f.profile, "ai_min_score": f.ai_min_score,
            "source_settings": f.source_settings or {}, "actions": scanners.actions_of(f),
            "query_items": [_query_out(q) for q in await scanners.queries_of(db, f, only_enabled=False)]}


async def scanner_or_404(db: AsyncSession, scanner_id: int) -> JrFilter:
    f = await db.get(JrFilter, scanner_id)
    if f is None or f.workspace_id != workspace_id():
        raise HTTPException(404, "Сканер не найден")
    await scanners.ensure_queries(db, f)
    return f


def _clean_actions(raw: dict, current: dict) -> dict:
    out = {code: dict(current.get(code) or {}) for code in scanners.ACTIONS}
    for code, values in (raw or {}).items():
        if code not in scanners.ACTIONS or not isinstance(values, dict):
            raise HTTPException(422, f"Неизвестное действие «{code}»")
        target = out[code]
        if "enabled" in values:
            target["enabled"] = bool(values["enabled"])
        if code == "telegram" and "chat_id" in values:
            import re
            chat = str(values["chat_id"] or "").strip()
            if chat and not re.fullmatch(CHAT_RE, chat):
                raise HTTPException(422, "Чат: числовой id (для канала вида -100…) или @имя_канала")
            target["chat_id"] = chat or None
        if code == "companies":
            if "list_name" in values:
                target["list_name"] = (str(values["list_name"] or "").strip() or "Нанимает маркетолога")[:180]
            if "signal" in values:
                if values["signal"] not in scanners.SIGNALS:
                    raise HTTPException(422, "Сигнал: hh_marketing или hiring")
                target["signal"] = values["signal"]
        if code == "client_crm" and "responsible_user_id" in values:
            target["responsible_user_id"] = int(values["responsible_user_id"]) if values["responsible_user_id"] else None
    return out


async def _validate(db: AsyncSession, f: JrFilter) -> None:
    """Включать можно только рабочий сканер — тексты ошибок показываются на экране как есть."""
    acts = scanners.actions_of(f)
    if acts["client_crm"]["enabled"] and not f.project_id:
        raise HTTPException(422, "«Лид в CRM клиента» — только для сканера клиента: выберите клиента")
    if acts["companies"]["enabled"] and f.project_id:
        raise HTTPException(422, "«Компания в базу компаний» — только для сканеров агентства")
    if acts["telegram"]["enabled"] and not acts["telegram"]["chat_id"]:
        raise HTTPException(422, "Укажите Telegram-чат для постов")
    rid = acts["client_crm"].get("responsible_user_id")
    if rid and f.project_id:
        project = await db.get(Project, f.project_id)
        user = await db.get(PortalUser, rid)
        if user is None or project is None or user.workspace_id != project.workspace_id:
            raise HTTPException(422, "Ответственный должен быть из команды клиента")
    if not f.enabled:
        return
    if not await scanners.queries_of(db, f):
        raise HTTPException(422, "Добавьте и включите хотя бы один запрос")
    if not any(q.keywords for q in await scanners.queries_of(db, f)):
        raise HTTPException(422, "В запросах нет ни одного ключевого слова")
    if not f.sources:
        raise HTTPException(422, "Выберите хотя бы одну площадку")
    if not scanners.enabled_actions(f):
        raise HTTPException(422, "Включите хотя бы одно действие: пост в Telegram, база компаний или CRM клиента")


# ---------------------------------------------------------------- шаблоны и сканеры

@router.get("/templates")
async def list_templates(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    await scanners.ensure_templates(db)
    await db.commit()
    rows = (await db.scalars(select(JrTemplate).order_by(JrTemplate.position, JrTemplate.code))).all()
    return {"items": [{"code": t.code, "name": t.name, "description": t.description, "icon": t.icon, "kind": t.kind,
                       "preview": {"queries": len((t.payload or {}).get("queries") or []),
                                   "sources": [s for s in (t.payload or {}).get("sources") or [] if s in REGISTRY],
                                   "actions": [c for c, a in ((t.payload or {}).get("actions") or {}).items()
                                               if (a or {}).get("enabled")]}} for t in rows]}


@router.get("/scanners")
async def list_scanners(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    ws = workspace_id()
    await runner.ensure_starter(db, ws)
    rows = (await db.scalars(select(JrFilter).where(JrFilter.workspace_id == ws).order_by(JrFilter.id))).all()
    for f in rows:
        await scanners.ensure_queries(db, f)
    await db.commit()
    return {"items": [await _short(db, f) for f in rows]}


@router.post("/scanners", status_code=201)
async def create_scanner(payload: ScannerIn, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    if not payload.name:
        raise HTTPException(422, "Назовите сканер")
    await scanners.ensure_templates(db)
    template = await db.get(JrTemplate, payload.template or "blank")
    if payload.template and template is None:
        raise HTTPException(422, "Шаблон не найден")
    project_id = (await _client_project(db, payload.project_id)).id if payload.project_id else None
    f = await scanners.create_from_template(db, workspace_id(), name=payload.name.strip(), template=template,
                                            project_id=project_id)
    await db.commit()
    return await _full(db, f)


@router.get("/scanners/{scanner_id}")
async def get_scanner(scanner_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    f = await scanner_or_404(db, scanner_id)
    await db.commit()
    return await _full(db, f)


@router.patch("/scanners/{scanner_id}")
async def patch_scanner(scanner_id: int, payload: ScannerIn, db: AsyncSession = Depends(get_db),
                        admin: AdminUser = Depends(require_admin)):
    f = await scanner_or_404(db, scanner_id)
    data = payload.model_dump(exclude_unset=True)
    data.pop("template", None)
    if "project_id" in data:
        f.project_id = (await _client_project(db, data.pop("project_id"))).id if data.get("project_id") else None
        data.pop("project_id", None)
    if "name" in data and data["name"]:
        f.name = data.pop("name").strip()
    if data.get("sources") is not None:
        f.sources = [s for s in dict.fromkeys(data.pop("sources")) if s in REGISTRY]
    if data.get("source_settings") is not None:
        f.source_settings = {**(f.source_settings or {}), **_source_settings(data.pop("source_settings"))}
    if data.get("actions") is not None:
        f.actions = _clean_actions(data.pop("actions"), scanners.actions_of(f))
    for key in ("profile", "ai_min_score", "enabled"):
        if key in data and (data[key] is not None or key == "profile"):
            setattr(f, key, (data[key] or None) if key == "profile" else data[key])
    await _validate(db, f)
    await scanners.sync_legacy(db, f)
    await db.commit()
    return await _full(db, f)


@router.delete("/scanners/{scanner_id}", status_code=204)
async def delete_scanner(scanner_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    f = await scanner_or_404(db, scanner_id)
    for model in (JrMatch, JrQuery, JrTgChannel):
        await db.execute(delete(model).where(model.filter_id == f.id))
    await db.delete(f)
    await db.commit()


# ---------------------------------------------------------------- запросы

async def query_or_404(db: AsyncSession, query_id: int) -> tuple[JrQuery, JrFilter]:
    q = await db.get(JrQuery, query_id)
    f = await db.get(JrFilter, q.filter_id) if q else None
    if q is None or f is None or f.workspace_id != workspace_id():
        raise HTTPException(404, "Запрос не найден")
    return q, f


@router.post("/scanners/{scanner_id}/queries", status_code=201)
async def add_query(scanner_id: int, payload: QueryIn, db: AsyncSession = Depends(get_db),
                    admin: AdminUser = Depends(require_admin)):
    f = await scanner_or_404(db, scanner_id)
    existing = await scanners.queries_of(db, f, only_enabled=False)
    if len(existing) >= 30:
        raise HTTPException(422, "Не больше 30 запросов в сканере")
    values = _query_values(payload.model_dump(exclude_unset=True))
    q = JrQuery(filter_id=f.id, name=values.pop("name", f"Запрос {len(existing) + 1}"),
                position=values.pop("position", len(existing)), enabled=values.pop("enabled", True),
                keywords=[], exclude=[], regions=[], experience=[], remote_only=False, salary_from=0, allow_no_salary=True)
    for key, value in values.items():
        setattr(q, key, value)
    db.add(q)
    await db.flush()
    await scanners.sync_legacy(db, f)
    await db.commit()
    return _query_out(q)


@router.patch("/queries/{query_id}")
async def patch_query(query_id: int, payload: QueryIn, db: AsyncSession = Depends(get_db),
                      admin: AdminUser = Depends(require_admin)):
    q, f = await query_or_404(db, query_id)
    changed = False
    for key, value in _query_values(payload.model_dump(exclude_unset=True)).items():
        if key in scanners.QUERY_FIELDS and getattr(q, key) != value:
            changed = True
        setattr(q, key, value)
    if changed:   # условия поменялись — отсеянное правилами пересмотрим при следующем проходе
        await db.execute(delete(JrMatch).where(JrMatch.filter_id == f.id, JrMatch.status == "skipped",
                                               JrMatch.company_id.is_(None)))
    await db.flush()
    await _validate(db, f)
    await scanners.sync_legacy(db, f)
    await db.commit()
    return _query_out(q)


@router.delete("/queries/{query_id}", status_code=204)
async def delete_query(query_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    q, f = await query_or_404(db, query_id)
    await db.delete(q)
    await db.flush()
    if f.enabled and not await scanners.queries_of(db, f):
        f.enabled = False   # без запросов сканер работать не может — выключаем, а не падаем
    await scanners.sync_legacy(db, f)
    await db.commit()


# ---------------------------------------------------------------- запуск, тест, предпросмотр, лента

@router.post("/scanners/{scanner_id}/run", status_code=202)
async def run_scanner(scanner_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    f = await scanner_or_404(db, scanner_id)
    if not await scanners.queries_of(db, f) or not f.sources:
        raise HTTPException(422, "В сканере нет включённых запросов или площадок")
    if progress.active(timing.run_key(f.id)):   # уже идёт или ждёт очереди — второй раз не ставим
        return {"queued": True, "already": True}
    _rows, _checks, query = await runner.plan(db, f)
    track = await timing.start(db, f, query, queued=True)   # в очереди, пока идёт фоновая проверка
    await db.commit()
    task = asyncio.create_task(runner.run_one(f.id, track=track))   # фоном: веб-запрос не ждёт сайтов и Telegram
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"queued": True}


async def _named_queries(db: AsyncSession, f: JrFilter, drafts_in) -> list[tuple]:
    """(id|None, имя, условия) — черновики из формы или включённые запросы сканера."""
    strict = scanners.strict_city(f)
    if drafts_in is not None:
        drafts = [{"name": d.get("name") or f"Черновик {i + 1}", **_query_values(d)}
                  for i, d in enumerate(q.model_dump(exclude_unset=True) for q in drafts_in)]
        return [(None, d["name"], scanners.query_dict(d, strict_city=strict)) for d in drafts if d.get("enabled", True)]
    return [(q.id, q.name, scanners.query_dict(q, strict_city=strict)) for q in await scanners.queries_of(db, f)]


@router.post("/scanners/{scanner_id}/estimate")
async def estimate_scanner(scanner_id: int, payload: EstimateIn, db: AsyncSession = Depends(get_db),
                           admin: AdminUser = Depends(require_admin)):
    """Сколько займёт проверка или предпросмотр — до запуска. По площадкам: запросов × секунд на запрос."""
    f = await scanner_or_404(db, scanner_id)
    checks = [c for _, _, c in await _named_queries(db, f, payload.queries)]
    out = await timing.estimate(db, f, scanners.merged(checks), kind=payload.kind)
    await db.rollback()
    return out


@router.get("/scanners/{scanner_id}/progress")
async def scanner_progress(scanner_id: int, kind: str = Query("run", pattern="^(run|preview)$"),
                           db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    await scanner_or_404(db, scanner_id)
    key = timing.run_key(scanner_id) if kind == "run" else timing.preview_key(scanner_id)
    return progress.get(key) or {"state": "idle"}


@router.post("/scanners/{scanner_id}/test")
async def test_scanner(scanner_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    f = await scanner_or_404(db, scanner_id)
    chat = scanners.actions_of(f)["telegram"]["chat_id"]
    if not chat:
        raise HTTPException(422, "Укажите Telegram-чат в действии «Пост в Telegram»")
    sample = Vacancy(source="hh", ext_id="test", title="Тест: сканер радара подключён ✅", company="StepToLead",
                     remote=True, url="https://hh.ru", snippet=f"Сканер «{f.name}». Так будут выглядеть новые находки.")
    try:
        await posting.send(chat, posting.render(sample, sources=["hh"]))
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", None)
        hint = {400: "Telegram не нашёл чат — проверьте id",
                403: "Бот не может писать в чат — добавьте его в канал администратором"}
        raise HTTPException(409, hint.get(code, "Telegram не принял сообщение — проверьте, что бот портала подключён"))
    return {"ok": True}


@router.post("/scanners/{scanner_id}/preview")
async def preview_scanner(scanner_id: int, payload: PreviewIn, db: AsyncSession = Depends(get_db),
                          admin: AdminUser = Depends(require_admin)):
    """«Посмотреть, что найдёт»: по включённым запросам сканера или по черновикам из формы. Ничего не сохраняет."""
    f = await scanner_or_404(db, scanner_id)
    named = await _named_queries(db, f, payload.queries)
    checks = [c for _, _, c in named]
    if not any(c["keywords"] for c in checks):
        raise HTTPException(422, "Добавьте ключевые слова хотя бы в один запрос")
    if not f.sources:
        raise HTTPException(422, "Выберите хотя бы одну площадку")
    if progress.active(timing.preview_key(f.id)):
        raise HTTPException(409, "Предпросмотр этого сканера уже идёт — дождитесь результата")
    track = await timing.start(db, f, scanners.merged(checks), kind="preview")
    ctx = await platforms.context(db, first_run_days=payload.days, ai_extract=runner.ai_extractor(workspace_id()))
    try:
        found, by_source, _ = await asyncio.wait_for(runner.collect(
            db, f, runner.now() - timedelta(days=payload.days), ctx, runner.now(), record=False,
            query=scanners.merged(checks), checks=checks, track=track), 150)
    except asyncio.TimeoutError:
        track.finish("error", "Площадки отвечают слишком долго")
        raise HTTPException(504, "Площадки отвечают слишком долго — попробуйте меньше слов или период")
    except Exception:
        track.finish("error", "Предпросмотр прервался из-за ошибки")
        raise
    await db.rollback()   # предпросмотр ничего не пишет (состояние каналов тоже)
    track.begin("match", note=f"{len(found)} вакансий")
    by_query = [{"id": qid, "name": name, "found": 0, "passed": 0} for qid, name, _ in named]
    items = []
    for cards in core.group(found).values():
        hit, reason = None, ""
        for card in cards:
            hit, why = runner.match_any(card, checks)
            reason = reason or why
            if hit is not None:
                break
        for i, (_, _, c) in enumerate(named):
            if any(core.has_keyword(core.text(cards[0].title), k) for k in c["keywords"]):
                by_query[i]["found"] += 1
        if hit is not None:
            by_query[hit]["passed"] += 1
        main = cards[0]
        items.append({"title": main.title, "company": main.company, "city": main.city, "remote": main.remote,
                      "salary_from": main.salary_from, "salary_to": main.salary_to, "salary_known": main.salary_known,
                      "kind": main.kind, "url": main.url, "employer_url": (main.employer or {}).get("url"),
                      "sources": [posting.source_name(c.source) for c in cards], "ok": hit is not None,
                      "reason": None if hit is not None else reason,
                      "query": named[hit][1] if hit is not None else None})
    items.sort(key=lambda i: (not i["ok"], (i["company"] or "").lower()))
    matched = [i for i in items if i["ok"]]
    track.finish("done", f"Готово: найдено {len(items)}, подходит {len(matched)}")
    sources = [{"code": code, "name": posting.source_name(code),
                "found": b.get("found", 0) if b.get("state") == "ok" else None,
                "skipped": b.get("note") if b.get("state") == "skipped" else None,
                "error": b.get("error") if b.get("state") == "error" else None} for code, b in by_source.items()]
    return {"days": payload.days, "sources": sources, "total": len(items), "matched": len(matched),
            "companies": len({(i["company"] or "").lower() for i in matched}), "by_query": by_query,
            "items": items[:300]}


@router.get("/scanners/{scanner_id}/feed")
async def scanner_feed(scanner_id: int, status: str | None = Query(None, pattern="^(sent|lead|deal|found|skipped|ai_low)$"),
                       query_id: int | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                       db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    f = await scanner_or_404(db, scanner_id)
    names = {q.id: q.name for q in await scanners.queries_of(db, f, only_enabled=False)}
    query = select(JrMatch, JrVacancy).join(JrVacancy, JrVacancy.id == JrMatch.vacancy_id).where(JrMatch.filter_id == f.id)
    if status:
        query = query.where(JrMatch.status == status)
    if query_id:
        query = query.where(JrMatch.query_id == query_id)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (await db.execute(query.order_by(JrMatch.created_at.desc(), JrMatch.id.desc()).limit(limit).offset(offset))).all()
    await db.commit()
    return {"total": int(total or 0), "items": [{
        "id": m.id, "status": m.status, "status_name": STATUS_NAMES.get(m.status, m.status), "reason": m.reason,
        "score": m.score, "why": m.why, "sources": [posting.source_name(s) for s in m.sources or []],
        "query": names.get(m.query_id), "feedback": m.feedback, "company_id": m.company_id, "deal_id": m.deal_id,
        "sent_at": iso(m.sent_at), "created_at": iso(m.created_at),
        "vacancy": {"title": v.title, "company": v.company, "city": v.city, "remote": v.remote,
                    "salary_from": v.salary_from, "salary_to": v.salary_to, "salary_known": v.salary_known,
                    "kind": v.kind, "url": v.url, "published_at": iso(v.published_at)},
    } for m, v in rows]}
