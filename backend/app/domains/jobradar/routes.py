"""API «Радара вакансий» для админки агентства: /api/admin/jobradar/*. Только админы агентства (require_admin)."""
from __future__ import annotations

import asyncio
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings as app_settings
from app.core.access import require_admin
from app.core.crypto import encrypt_secret
from app.db import get_db
from app.domains.jobradar import posting, runner, scoring
from app.domains.jobradar.models import JrFilter, JrMatch, JrVacancy
from app.domains.jobradar.sources import ALL, NAMES, Vacancy
from app.models.access import AdminUser
from app.models.system import AppSetting

router = APIRouter(prefix="/admin/jobradar", tags=["admin-jobradar"])
EXPERIENCE = ("noExperience", "between1And3", "between3And6", "moreThan6")
STATUS_NAMES = {"sent": "Отправлено", "lead": "В базе компаний", "skipped": "Отсеяно", "ai_low": "Отсеял ИИ",
                "failed": "Не отправилось"}
_tasks: set[asyncio.Task] = set()


def workspace_id() -> int:
    if not app_settings.agency_workspace_id:
        raise HTTPException(503, "Не задан AGENCY_WORKSPACE_ID — радар работает в аккаунте агентства")
    return app_settings.agency_workspace_id


def iso(value):
    return value.isoformat() if value else None


def _words(items: list[str], limit: int) -> list[str]:
    out = []
    for item in items:
        word = re.sub(r"\s+", " ", (item or "").strip())[:120]
        if word and word.lower() not in {w.lower() for w in out}:
            out.append(word)
    return out[:limit]


class FilterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=160)
    mode: str | None = Field(default=None, pattern="^(work|leads)$")
    keywords: list[str] | None = Field(default=None, max_length=50)
    exclude: list[str] | None = Field(default=None, max_length=50)
    remote_only: bool | None = None
    regions: list[str] | None = Field(default=None, max_length=50)
    salary_from: int | None = Field(default=None, ge=0, le=10_000_000)
    allow_no_salary: bool | None = None
    experience: list[str] | None = Field(default=None, max_length=4)
    sources: list[str] | None = Field(default=None, max_length=3)
    chat_id: str | None = Field(default=None, max_length=32)
    ai_min_score: int | None = Field(default=None, ge=0, le=100)
    profile: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_minutes: int | None = Field(default=None, ge=5, le=1440)
    hh_token: str | None = Field(default=None, max_length=300)   # "" — убрать


def _apply(row: JrFilter, data: dict) -> bool:
    """Записывает поля фильтра. True — поменялись условия отбора (отсеянное раньше стоит пересмотреть)."""
    criteria = False
    for key in ("keywords", "exclude", "regions"):
        if key in data and data[key] is not None:
            data[key] = _words(data[key], 50)
    if data.get("experience") is not None:
        data["experience"] = [e for e in data["experience"] if e in EXPERIENCE]
    if data.get("sources") is not None:
        data["sources"] = [s for s in dict.fromkeys(data["sources"]) if s in ALL]
    if "chat_id" in data:
        chat = (data["chat_id"] or "").strip()
        if chat and not re.fullmatch(r"-?\d{3,20}|@[A-Za-z0-9_]{4,32}", chat):
            raise HTTPException(422, "Чат: числовой id (для канала вида -100…) или @имя_канала")
        data["chat_id"] = chat or None
    for key, value in data.items():
        if value is None and key not in ("chat_id", "profile"):
            continue
        if key in ("keywords", "exclude", "remote_only", "regions", "salary_from", "allow_no_salary", "experience") \
                and getattr(row, key) != value:
            criteria = True
        setattr(row, key, value)
    return criteria


def _check(row: JrFilter) -> None:
    if not row.keywords:
        raise HTTPException(422, "Добавьте хотя бы одно ключевое слово")
    if not row.sources:
        raise HTTPException(422, "Выберите хотя бы один источник")
    if row.enabled and row.mode == "work" and not row.chat_id:
        raise HTTPException(422, "Укажите Telegram-чат: куда присылать вакансии")


async def _filter_out(db: AsyncSession, row: JrFilter) -> dict:
    since = runner.now() - timedelta(days=7)
    sent, liked, rated = (await db.execute(select(
        func.count(JrMatch.id).filter(JrMatch.sent_at >= since),
        func.count(JrMatch.id).filter(JrMatch.feedback == 1),
        func.count(JrMatch.id).filter(JrMatch.feedback.is_not(None)),
    ).where(JrMatch.filter_id == row.id))).one()
    leads7 = await db.scalar(select(func.count(JrMatch.id)).where(
        JrMatch.filter_id == row.id, JrMatch.status == "lead", JrMatch.created_at >= since))
    return {"id": row.id, "name": row.name, "mode": row.mode, "keywords": row.keywords or [], "exclude": row.exclude or [],
            "remote_only": row.remote_only, "regions": row.regions or [], "salary_from": row.salary_from,
            "allow_no_salary": row.allow_no_salary, "experience": row.experience or [], "sources": row.sources or [],
            "chat_id": row.chat_id, "ai_min_score": row.ai_min_score, "profile": row.profile, "enabled": row.enabled,
            "last_run_at": iso(row.last_run_at), "last_result": row.last_result or {},
            "stats": {"sent_7d": int(sent or 0), "leads_7d": int(leads7 or 0), "rated": int(rated or 0),
                      "liked_share": round(liked * 100 / rated) if rated else None}}


async def filter_or_404(db: AsyncSession, filter_id: int) -> JrFilter:
    row = await db.get(JrFilter, filter_id)
    if row is None or row.workspace_id != workspace_id():
        raise HTTPException(404, "Фильтр не найден")
    return row


@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    from app.services.notifications import telegram_configured
    cfg = await runner.get_settings(db)
    status = await db.get(AppSetting, runner.STATUS_KEY)
    admins = (await db.scalars(select(AdminUser).where(AdminUser.telegram_chat_id.is_not(None),
                                                      AdminUser.disabled_at.is_(None)))).all()
    chats = [{"id": a.telegram_chat_id, "title": f"Личный чат: {a.display_name or a.username}"
              + (f" (@{a.telegram_username})" if a.telegram_username else "")} for a in admins]
    used = (await db.scalars(select(JrFilter.chat_id).where(JrFilter.chat_id.is_not(None)).distinct())).all()
    chats += [{"id": c, "title": f"Канал / чат {c}"} for c in used if c not in {x["id"] for x in chats}]
    return {"bot_configured": telegram_configured(), "ai_available": scoring.available(),
            "settings": {"interval_minutes": cfg["interval_minutes"], "hh_token_set": bool(cfg.get("hh_token_encrypted"))},
            "sources": [{"id": s, "name": NAMES[s], **((status.value or {}).get(s, {}) if status else {})} for s in ALL],
            "chats": chats}


@router.patch("/settings")
async def patch_settings(payload: SettingsIn, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    row = await db.get(AppSetting, runner.SETTINGS_KEY)
    value = dict(row.value) if row and isinstance(row.value, dict) else {}
    if payload.interval_minutes is not None:
        value["interval_minutes"] = payload.interval_minutes
    if payload.hh_token is not None:
        token = payload.hh_token.strip()
        if token:
            value["hh_token_encrypted"] = encrypt_secret(token)
        else:
            value.pop("hh_token_encrypted", None)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=runner.SETTINGS_KEY, value=value))
    await db.commit()
    return {"interval_minutes": value.get("interval_minutes", runner.DEFAULT_SETTINGS["interval_minutes"]),
            "hh_token_set": bool(value.get("hh_token_encrypted"))}


@router.get("/filters")
async def list_filters(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    ws = workspace_id()
    await runner.ensure_starter(db, ws)
    await db.commit()
    rows = (await db.scalars(select(JrFilter).where(JrFilter.workspace_id == ws).order_by(JrFilter.id))).all()
    return {"items": [await _filter_out(db, r) for r in rows]}


@router.post("/filters", status_code=201)
async def create_filter(payload: FilterIn, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    data = payload.model_dump(exclude_unset=True)
    if not data.get("name"):
        raise HTTPException(422, "Назовите фильтр")
    row = JrFilter(workspace_id=workspace_id(), mode="work", keywords=[], exclude=[], regions=[], experience=[],
                   sources=["hh", "trudvsem", "habr"], salary_from=0, allow_no_salary=True, remote_only=False,
                   ai_min_score=0, enabled=False, last_result={})
    _apply(row, data)
    _check(row)
    db.add(row)
    await db.commit()
    return await _filter_out(db, row)


@router.patch("/filters/{filter_id}")
async def patch_filter(filter_id: int, payload: FilterIn, db: AsyncSession = Depends(get_db),
                       admin: AdminUser = Depends(require_admin)):
    row = await filter_or_404(db, filter_id)
    if _apply(row, payload.model_dump(exclude_unset=True)):
        # Условия поменялись: отсеянное правилами пересмотрим при следующем проходе (отправленное не повторяем).
        await db.execute(delete(JrMatch).where(JrMatch.filter_id == row.id, JrMatch.status == "skipped",
                                               JrMatch.company_id.is_(None)))
    _check(row)
    await db.commit()
    return await _filter_out(db, row)


@router.delete("/filters/{filter_id}", status_code=204)
async def delete_filter(filter_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    row = await filter_or_404(db, filter_id)
    await db.execute(delete(JrMatch).where(JrMatch.filter_id == row.id))
    await db.delete(row)
    await db.commit()


@router.post("/filters/{filter_id}/test")
async def test_post(filter_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    row = await filter_or_404(db, filter_id)
    if not row.chat_id:
        raise HTTPException(422, "Укажите Telegram-чат в фильтре")
    sample = Vacancy(source="hh", ext_id="test", title="Тест: радар вакансий подключён ✅", company="StepToLead",
                     remote=True, url="https://hh.ru", snippet=f"Фильтр «{row.name}». Так будут выглядеть новые вакансии.")
    try:
        await posting.send(row.chat_id, posting.render(sample, sources=["hh"]))
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", None)
        hint = {400: "Telegram не нашёл чат — проверьте id", 403: "Бот не может писать в чат — добавьте его в канал администратором"}
        raise HTTPException(409, hint.get(code, "Telegram не принял сообщение — проверьте, что бот портала подключён"))
    return {"ok": True}


@router.post("/filters/{filter_id}/run", status_code=202)
async def run_now(filter_id: int, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    row = await filter_or_404(db, filter_id)
    if not row.keywords or not row.sources:
        raise HTTPException(422, "В фильтре нет ключевых слов или источников")
    task = asyncio.create_task(runner.run_one(row.id))   # фоном: веб-запрос не ждёт сайтов и Telegram
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"queued": True}


class PreviewIn(FilterIn):
    days: int = Field(default=3, ge=1, le=14)


@router.post("/preview")
async def preview(payload: PreviewIn, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    """«Посмотреть, что найдёт»: поиск по условиям из формы, без записи в базу и без постов."""
    from app.domains.jobradar import core
    data = payload.model_dump(exclude_unset=True)
    days = data.pop("days", 3)
    row = JrFilter(keywords=[], exclude=[], regions=[], experience=[], sources=["hh", "trudvsem", "habr"],
                   salary_from=0, allow_no_salary=True, remote_only=False)
    _apply(row, {k: v for k, v in data.items() if k not in ("name", "enabled")})
    if not row.keywords or not row.sources:
        raise HTTPException(422, "Добавьте ключевые слова и выберите хотя бы один источник")
    cfg = await runner.get_settings(db)
    client = runner.make_client(cfg)
    query = runner.as_dict(row)
    since = runner.now() - timedelta(days=days)
    sources, found = {}, []

    async def one(source):
        try:
            got = await asyncio.wait_for(client.fetch(source, query, since), 60)
            sources[source] = {"name": NAMES[source], "found": len(got)}
            return got
        except Exception as exc:  # noqa: BLE001
            sources[source] = {"name": NAMES[source], "error": f"{type(exc).__name__}: {str(exc)[:150]}"}
            return []
    for got in await asyncio.gather(*(one(s) for s in row.sources)):
        found += got
    groups = core.group(list({v.uid: v for v in found}.values()))
    items = []
    for cards in groups.values():
        results = [core.matches(c, query) for c in cards]
        ok = any(r[0] for r in results)
        main = cards[0]
        items.append({"title": main.title, "company": main.company, "city": main.city, "remote": main.remote,
                      "salary_from": main.salary_from, "salary_to": main.salary_to, "url": main.url,
                      "experience": main.experience, "employer_url": main.employer.get("url"),
                      "sources": [NAMES.get(c.source, c.source) for c in cards], "ok": ok,
                      "reason": None if ok else results[0][1]})
    items.sort(key=lambda i: (not i["ok"], (i["company"] or "").lower()))
    matched = [i for i in items if i["ok"]]
    return {"days": days, "sources": [sources[s] for s in row.sources], "total": len(items), "matched": len(matched),
            "companies": len({(i["company"] or "").lower() for i in matched}), "items": items[:300]}


@router.get("/vacancies")
async def vacancies(filter_id: int | None = None,
                    status: str | None = Query(None, pattern="^(sent|lead|skipped|ai_low|failed)$"),
                    q: str | None = Query(None, max_length=120),
                    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                    db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    ws = workspace_id()
    query = select(JrMatch, JrVacancy, JrFilter.name).join(JrVacancy, JrVacancy.id == JrMatch.vacancy_id) \
        .join(JrFilter, JrFilter.id == JrMatch.filter_id).where(JrFilter.workspace_id == ws)
    if filter_id:
        query = query.where(JrMatch.filter_id == filter_id)
    if status:
        query = query.where(JrMatch.status == status)
    if q:
        like = f"%{q.strip().lower()}%"
        query = query.where(func.lower(JrVacancy.title).like(like) | func.lower(JrVacancy.company).like(like))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (await db.execute(query.order_by(JrMatch.created_at.desc(), JrMatch.id.desc()).limit(limit).offset(offset))).all()
    return {"total": int(total or 0), "items": [{
        "id": m.id, "filter": name, "filter_id": m.filter_id, "status": m.status,
        "status_name": STATUS_NAMES.get(m.status, m.status), "reason": m.reason, "score": m.score, "why": m.why,
        "sources": [NAMES.get(s, s) for s in m.sources or []], "feedback": m.feedback, "company_id": m.company_id,
        "sent_at": iso(m.sent_at), "created_at": iso(m.created_at),
        "vacancy": {"title": v.title, "company": v.company, "city": v.city, "remote": v.remote,
                    "salary_from": v.salary_from, "salary_to": v.salary_to, "url": v.url,
                    "published_at": iso(v.published_at)},
    } for m, v, name in rows]}
