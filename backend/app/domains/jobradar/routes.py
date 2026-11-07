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

from app.db import get_db
from app.domains.jobradar import platforms, posting, runner, scoring
from app.domains.jobradar.models import JrFilter, JrMatch, JrSource, JrTgChannel, JrVacancy
from app.domains.jobradar.sources import REGISTRY, Vacancy, short_error
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
    sources: list[str] | None = Field(default=None, max_length=12)
    source_settings: dict | None = None     # {"telegram": {"channels": [...]}, "flru": {...}}
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
        data["sources"] = [s for s in dict.fromkeys(data["sources"]) if s in REGISTRY]
    if data.get("source_settings") is not None:
        data["source_settings"] = _source_settings(data["source_settings"])
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


MAX_CHANNELS = 30


def _tg():
    """Модуль площадки Telegram (пишется отдельно) — None, пока его нет."""
    try:
        from app.domains.jobradar.sources import telegram_channels
        return telegram_channels
    except ModuleNotFoundError:
        return None


def _channel_name(text: str) -> str:
    tg = _tg()
    if tg is not None and hasattr(tg, "normalize_channel"):
        name, error = tg.normalize_channel(text)
    else:
        raw = (text or "").strip()
        if "/+" in raw or "joinchat" in raw:
            name, error = None, "закрытый канал, читать нельзя"
        else:
            raw = re.sub(r"^(https?://)?(t\.me/)?(s/)?@?", "", raw).split("/")[0].split("?")[0]
            name, error = (raw, None) if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", raw) else (None, "не похоже на канал")
    if error:
        raise HTTPException(422, error)
    return name


def _source_settings(raw: dict) -> dict:
    """Настройки площадок в фильтре — только известные ключи из filter_schema (и каналы Telegram)."""
    out = {}
    for code, values in (raw or {}).items():
        adapter = REGISTRY.get(code)
        if adapter is None or not isinstance(values, dict):
            continue
        clean = {}
        specs = {s["key"]: s for s in adapter.filter_schema}
        for key, value in values.items():
            if code == "telegram" and key == "channels":
                names = list(dict.fromkeys(_channel_name(str(v)) for v in (value or []) if str(v).strip()))
                if len(names) > MAX_CHANNELS:
                    raise HTTPException(422, f"Не больше {MAX_CHANNELS} каналов на фильтр")
                clean["channels"] = names
            elif key in specs:
                try:
                    clean[key] = platforms._coerce(specs[key], value)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(422, str(exc) or f"Неверное значение «{specs[key]['label']}»")
        out[code] = clean
    return out


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
            "source_settings": row.source_settings or {},
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
    admins = (await db.scalars(select(AdminUser).where(AdminUser.telegram_chat_id.is_not(None),
                                                      AdminUser.disabled_at.is_(None)))).all()
    chats = [{"id": a.telegram_chat_id, "title": f"Личный чат: {a.display_name or a.username}"
              + (f" (@{a.telegram_username})" if a.telegram_username else "")} for a in admins]
    used = (await db.scalars(select(JrFilter.chat_id).where(JrFilter.chat_id.is_not(None)).distinct())).all()
    chats += [{"id": c, "title": f"Канал / чат {c}"} for c in used if c not in {x["id"] for x in chats}]
    return {"bot_configured": telegram_configured(), "ai_available": scoring.available(),
            "settings": {"interval_minutes": cfg["interval_minutes"]}, "chats": chats}


@router.patch("/settings")
async def patch_settings(payload: SettingsIn, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    row = await db.get(AppSetting, runner.SETTINGS_KEY)
    value = dict(row.value) if row and isinstance(row.value, dict) else {}
    if payload.interval_minutes is not None:
        value["interval_minutes"] = payload.interval_minutes
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=runner.SETTINGS_KEY, value=value))
    if payload.hh_token is not None and "hh" in REGISTRY:   # старый экран: токен hh теперь в плашке hh.ru
        rows = await platforms.ensure(db)
        platforms.apply(REGISTRY["hh"], rows["hh"], {"token": payload.hh_token})
    await db.commit()
    return {"interval_minutes": value.get("interval_minutes", runner.DEFAULT_SETTINGS["interval_minutes"])}


# ---------------------------------------------------------------- площадки

DAY = timedelta(hours=24)


async def _stats(db: AsyncSession, code: str) -> dict:
    since = runner.now() - DAY
    found = await db.scalar(select(func.count(JrVacancy.id)).where(JrVacancy.source == code, JrVacancy.seen_at >= since))
    base = select(func.count(JrMatch.id)).join(JrVacancy, JrVacancy.id == JrMatch.vacancy_id).where(JrVacancy.source == code)
    passed = await db.scalar(base.where(JrMatch.status != "skipped", JrMatch.created_at >= since))
    sent = await db.scalar(base.where(JrMatch.status.in_(("sent", "lead")), JrMatch.sent_at >= since))
    return {"found": int(found or 0), "passed": int(passed or 0), "sent": int(sent or 0)}


async def _source_out(db: AsyncSession, row: JrSource) -> dict:
    adapter = REGISTRY[row.code]
    state, message = platforms.state(adapter, row)
    return {"code": row.code, "title": adapter.title, "icon": adapter.icon, "purpose": adapter.purpose,
            "needs_key": adapter.needs_key, "enabled": row.enabled, "state": state, "message": message,
            "last_ok_at": iso(row.last_ok_at), "stats_24h": await _stats(db, row.code),
            "settings": platforms.public(adapter, row.settings), "settings_schema": adapter.settings_schema,
            "filter_schema": adapter.filter_schema,
            "proxy_options": await _proxy_options(db) if any(x.get("type") == "proxy" for x in adapter.settings_schema) else []}


async def _proxy_options(db: AsyncSession) -> list[dict]:
    """Прокси из пула портала для чтения t.me — адрес без логина и пароля."""
    from app.models.proxy import Proxy
    rows = (await db.scalars(select(Proxy).order_by(Proxy.id))).all()
    return [{"id": p.id, "label": f"{p.host}:{p.port}" + (f" · {p.label}" if p.label else ""), "status": p.status}
            for p in rows]


def _preview(v) -> dict:
    return {"title": v.title, "company": v.company, "url": v.url, "city": v.city, "remote": v.remote,
            "salary_from": v.salary_from, "salary_to": v.salary_to, "salary_known": getattr(v, "salary_known", True),
            "kind": getattr(v, "kind", "vacancy"),
            "published_at": iso(getattr(v, "published_at", None) or getattr(v, "published", None))}


class SourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    settings: dict | None = None


@router.get("/sources")
async def list_sources(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    rows = await platforms.ensure(db)
    await db.commit()
    return {"items": [await _source_out(db, rows[code]) for code in REGISTRY if code in rows]}


async def source_or_404(db: AsyncSession, code: str) -> JrSource:
    rows = await platforms.ensure(db)
    if code not in REGISTRY or code not in rows:
        raise HTTPException(404, "Площадка не найдена")
    return rows[code]


@router.patch("/sources/{code}")
async def patch_source(code: str, payload: SourceIn, db: AsyncSession = Depends(get_db),
                       admin: AdminUser = Depends(require_admin)):
    row = await source_or_404(db, code)
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.settings is not None:
        try:
            platforms.apply(REGISTRY[code], row, payload.settings)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)[:200])
        row.last_error = None   # ключ поменяли — старую ошибку не показываем
    await db.commit()
    return await _source_out(db, row)


@router.post("/sources/{code}/check")
async def check_source(code: str, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    row = await source_or_404(db, code)
    adapter = REGISTRY[code]
    ctx = await platforms.context(db)
    try:
        ok, message = await asyncio.wait_for(adapter.healthcheck(platforms.plain(adapter, row.settings), ctx), 60)
    except Exception as exc:  # noqa: BLE001
        ok, message = False, short_error(exc)
    if adapter.ready(platforms.plain(adapter, row.settings)) is None:
        platforms.mark(row, runner.now(), None if ok else message)
    latest = (await db.scalars(select(JrVacancy).where(JrVacancy.source == code)
                               .order_by(JrVacancy.seen_at.desc(), JrVacancy.id.desc()).limit(3))).all()
    await db.commit()
    return {"ok": ok, "message": message[:120], "preview": [_preview(v) for v in latest]}


# ---------------------------------------------------------------- Telegram-каналы

class ChannelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: str = Field(min_length=2, max_length=200)
    filter_id: int | None = None


@router.get("/channels")
async def list_channels(db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    ws = workspace_id()
    filters = (await db.scalars(select(JrFilter).where(JrFilter.workspace_id == ws).order_by(JrFilter.id))).all()
    states = {(r.filter_id, r.channel): r for r in (await db.scalars(select(JrTgChannel))).all()}
    since = runner.now() - DAY
    out: dict[str, dict] = {}
    for f in filters:
        for name in ((f.source_settings or {}).get("telegram") or {}).get("channels") or []:
            item = out.setdefault(name, {"channel": name, "status": "unknown", "last_post_at": None, "error": None,
                                         "passed_24h": 0, "filters": []})
            item["filters"].append({"id": f.id, "name": f.name})
            st = states.get((f.id, name))
            if st is not None:
                if st.status != "unknown" and (item["status"] in ("unknown", "ok") or st.status == "ok"):
                    item["status"], item["error"] = st.status, st.error
                if st.last_post_at and (not item["last_post_at"] or iso(st.last_post_at) > item["last_post_at"]):
                    item["last_post_at"] = iso(st.last_post_at)
    for name, item in out.items():
        item["passed_24h"] = int(await db.scalar(select(func.count(JrMatch.id)).join(
            JrVacancy, JrVacancy.id == JrMatch.vacancy_id).where(
            JrVacancy.source == "telegram", JrVacancy.ext_id.like(f"{name}/%"), JrMatch.status != "skipped",
            JrMatch.created_at >= since)) or 0)
    return {"items": sorted(out.values(), key=lambda i: i["channel"].lower())}


@router.post("/channels/check")
async def check_channel(payload: ChannelIn, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    name = _channel_name(payload.channel)
    tg = _tg()
    if tg is None or not hasattr(tg, "check_channel"):
        raise HTTPException(503, "Площадка Telegram ещё не подключена")
    ctx = await platforms.context(db)
    try:
        result = await asyncio.wait_for(tg.check_channel(name, ctx), 60)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "channel": name, "status": "unavailable", "message": short_error(exc), "title": None,
                "preview": []}
    if result.get("status") == "closed":
        raise HTTPException(422, "закрытый канал, читать нельзя")
    return {"ok": bool(result.get("ok")), "channel": name, "status": result.get("status") or "unknown",
            "message": str(result.get("message") or "")[:120], "title": result.get("title"),
            "preview": [_preview(v) for v in (result.get("preview") or [])[:3]]}


@router.post("/channels")
async def add_channel(payload: ChannelIn, db: AsyncSession = Depends(get_db), admin: AdminUser = Depends(require_admin)):
    name = _channel_name(payload.channel)
    if not payload.filter_id:
        raise HTTPException(422, "Выберите фильтр, в который добавить канал")
    row = await filter_or_404(db, payload.filter_id)
    settings = dict(row.source_settings or {})
    tg = dict(settings.get("telegram") or {})
    channels = list(tg.get("channels") or [])
    if name not in channels:
        if len(channels) >= MAX_CHANNELS:
            raise HTTPException(422, f"Не больше {MAX_CHANNELS} каналов на фильтр")
        channels.append(name)
    tg["channels"] = channels
    settings["telegram"] = tg
    row.source_settings = settings
    if "telegram" in REGISTRY and "telegram" not in (row.sources or []):
        row.sources = [*(row.sources or []), "telegram"]
    await db.commit()
    return await _filter_out(db, row)


@router.delete("/channels/{name}")
async def delete_channel(name: str, filter_id: int = Query(...), db: AsyncSession = Depends(get_db),
                         admin: AdminUser = Depends(require_admin)):
    row = await filter_or_404(db, filter_id)
    settings = dict(row.source_settings or {})
    tg = dict(settings.get("telegram") or {})
    tg["channels"] = [c for c in tg.get("channels") or [] if c.lower() != name.lower().lstrip("@")]
    settings["telegram"] = tg
    row.source_settings = settings
    await db.execute(delete(JrTgChannel).where(JrTgChannel.filter_id == row.id,
                                               func.lower(JrTgChannel.channel) == name.lower().lstrip("@")))
    await db.commit()
    return await _filter_out(db, row)


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
                   sources=[c for c in ("trudvsem", "habr") if c in REGISTRY], salary_from=0, allow_no_salary=True,
                   remote_only=False, ai_min_score=0, enabled=False, last_result={}, source_settings={})
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
    row = JrFilter(keywords=[], exclude=[], regions=[], experience=[], sources=[c for c in ("trudvsem", "habr") if c in REGISTRY],
                   salary_from=0, allow_no_salary=True, remote_only=False, mode="work", source_settings={})
    _apply(row, {k: v for k, v in data.items() if k not in ("name", "enabled")})
    if not row.keywords or not row.sources:
        raise HTTPException(422, "Добавьте ключевые слова и выберите хотя бы один источник")
    ctx = await platforms.context(db, first_run_days=days, ai_extract=runner.ai_extractor(workspace_id()))
    query = runner.as_dict(row)
    since = runner.now() - timedelta(days=days)
    try:
        found, by_source, _errors = await asyncio.wait_for(runner.collect(db, row, since, ctx, runner.now(), record=False), 120)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Площадки отвечают слишком долго — попробуйте меньше слов или период")
    sources = [{"code": code, "name": posting.source_name(code), "found": b.get("found", 0) if b.get("state") == "ok" else None,
                "skipped": b.get("note") if b.get("state") == "skipped" else None,
                "error": b.get("error") if b.get("state") == "error" else None} for code, b in by_source.items()]
    groups = core.group(list({v.uid: v for v in found}.values()))
    items = []
    for cards in groups.values():
        results = [core.matches(c, query) for c in cards]
        ok = any(r[0] for r in results)
        main = cards[0]
        items.append({"title": main.title, "company": main.company, "city": main.city, "remote": main.remote,
                      "salary_from": main.salary_from, "salary_to": main.salary_to, "url": main.url,
                      "experience": main.experience, "employer_url": main.employer.get("url"),
                      "sources": [posting.source_name(c.source) for c in cards], "ok": ok, "kind": main.kind,
                      "salary_known": main.salary_known,
                      "reason": None if ok else results[0][1]})
    items.sort(key=lambda i: (not i["ok"], (i["company"] or "").lower()))
    matched = [i for i in items if i["ok"]]
    return {"days": days, "sources": sources, "total": len(items), "matched": len(matched),
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
        "sources": [posting.source_name(s) for s in m.sources or []], "codes": m.sources or [], "feedback": m.feedback, "company_id": m.company_id,
        "sent_at": iso(m.sent_at), "created_at": iso(m.created_at),
        "vacancy": {"title": v.title, "company": v.company, "city": v.city, "remote": v.remote,
                    "salary_from": v.salary_from, "salary_to": v.salary_to, "url": v.url, "kind": v.kind,
                    "salary_known": v.salary_known, "published_at": iso(v.published_at)},
    } for m, v, name in rows]}
