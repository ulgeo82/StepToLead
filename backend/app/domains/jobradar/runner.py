"""Проход радара: источники -> фильтр -> склейка -> (ИИ) -> пост в Telegram / компания в базу.

Фоном раз в N минут (настройка, по умолчанию 20). Окно поиска — с прошлого прохода минус 30 минут, первый раз — 3 дня.
Упавший источник пишет ошибку в статус и не валит проход. Посты — не чаще ~20 в минуту в один чат.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings as app_settings
from app.core.crypto import decrypt_secret
from app.db import SessionLocal
from app.domains.jobradar import core, leads, posting, scoring
from app.domains.jobradar.models import JrFilter, JrMatch, JrVacancy
from app.domains.jobradar.sources import ALL, Client, Vacancy, fingerprint
from app.models.system import AppSetting

logger = logging.getLogger("uvicorn.error.jobradar")
SETTINGS_KEY = "jobradar"
STATUS_KEY = "jobradar_status"
DEFAULT_SETTINGS = {"interval_minutes": 20, "first_run_days": 3, "overlap_minutes": 30}
POST_GAP = 3.0            # секунд между постами: ~20 в минуту, лимит Telegram для каналов
TICK = 60
MAX_POSTS = 60            # за один проход одного фильтра — остальное дойдёт следующим проходом

# Подменяются в тестах.
session_factory = SessionLocal
client_factory = None     # () -> sources.Client
send = posting.send
complete = None           # подмена ai.complete
sleep = asyncio.sleep

STARTER = {
    "name": "Работа для меня", "mode": "work",
    "keywords": ["интернет-маркетолог", "интернет маркетолог", "маркетолог", "директолог",
                 "специалист по контекстной рекламе", "таргетолог", "маркетолог-аналитик", "лидогенерация",
                 "performance"],
    "exclude": ["торговый представитель", "мерчендайзер", "senior", "руководитель отдела", "head of"],
    "remote_only": True, "regions": [], "salary_from": 40000, "allow_no_salary": False,
    "experience": ["noExperience", "between1And3"], "sources": ["hh", "trudvsem", "habr"], "ai_min_score": 0,
    "profile": "Интернет-маркетолог: Яндекс Директ, VK Реклама, Авито, SMM, сайты, аналитика, аутрич. Ищу удалёнку "
               "в России от 40 000 ₽, без опыта или от 1 года, несколько каналов продвижения.",
    "enabled": False,
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


async def get_settings(db: AsyncSession) -> dict:
    row = await db.get(AppSetting, SETTINGS_KEY)
    return {**DEFAULT_SETTINGS, **(row.value if row and isinstance(row.value, dict) else {})}


def make_client(cfg: dict) -> Client:
    if client_factory is not None:
        return client_factory()
    return Client(hh_token=decrypt_secret(cfg.get("hh_token_encrypted")) if cfg.get("hh_token_encrypted") else None)


def as_dict(f: JrFilter) -> dict:
    return {"keywords": f.keywords or [], "exclude": f.exclude or [], "remote_only": f.remote_only,
            "regions": f.regions or [], "salary_from": f.salary_from or 0, "allow_no_salary": f.allow_no_salary,
            "experience": f.experience or [], "strict_city": f.mode == "leads"}


async def ensure_starter(db: AsyncSession, workspace_id: int) -> None:
    """Стартовый фильтр «Работа для меня» — выключенным, один раз."""
    row = await db.get(AppSetting, SETTINGS_KEY)
    cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
    if cfg.get("starter_seeded"):
        return
    db.add(JrFilter(workspace_id=workspace_id, last_result={}, **STARTER))
    cfg["starter_seeded"] = True
    if row:
        row.value = cfg
    else:
        db.add(AppSetting(key=SETTINGS_KEY, value=cfg))
    await db.flush()


async def _status(db: AsyncSession, source: str, error: str | None, moment: datetime) -> None:
    row = await db.get(AppSetting, STATUS_KEY)
    value = dict(row.value) if row and isinstance(row.value, dict) else {}
    item = dict(value.get(source) or {})
    if error:
        item.update(error=error[:300], error_at=moment.isoformat())
    else:
        item.update(ok_at=moment.isoformat(), error=None)
    value[source] = item
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=STATUS_KEY, value=value))


def _row(v: Vacancy) -> dict:
    return {"fingerprint": fingerprint(v.title, v.company), "source": v.source, "ext_id": v.ext_id[:160],
            "title": (v.title or "")[:400], "company": (v.company or "")[:400] or None, "city": (v.city or "")[:160] or None,
            "salary_from": v.salary_from, "salary_to": v.salary_to, "currency": v.currency, "remote": v.remote,
            "experience": v.experience or None, "url": (v.url or "")[:1000] or None, "snippet": v.snippet or None,
            "employer": v.employer or {}, "published_at": v.published}


async def save_vacancies(db: AsyncSession, found: list[Vacancy]) -> dict[str, JrVacancy]:
    uids = list({v.uid for v in found})
    rows = {r.uid: r for r in (await db.scalars(select(JrVacancy).where(JrVacancy.uid.in_(uids or [""])))).all()}
    for v in found:
        if v.uid in rows:
            continue
        rows[v.uid] = JrVacancy(uid=v.uid, **_row(v))
        db.add(rows[v.uid])
    await db.flush()
    return rows


@dataclass
class Result:
    found: int = 0
    matched: int = 0
    sent: int = 0
    leads: int = 0
    skipped: int = 0
    ai_low: int = 0
    failed: int = 0
    errors: dict = field(default_factory=dict)


async def run_filter(db: AsyncSession, f: JrFilter, *, moment: datetime | None = None) -> Result:
    """Один проход одного фильтра. Коммитит сам (по ходу отправки — чтобы не слать повторно после сбоя)."""
    moment = moment or now()
    filter_id = f.id
    cfg = await get_settings(db)
    client = make_client(cfg)
    result = Result()
    since = (aware(f.last_run_at) - timedelta(minutes=int(cfg["overlap_minutes"]))) if f.last_run_at \
        else moment - timedelta(days=int(cfg["first_run_days"]))
    query = as_dict(f)
    found: list[Vacancy] = []
    ok_sources = 0
    for source in [s for s in f.sources or [] if s in ALL]:
        try:
            got = await client.fetch(source, query, since)
            found += got
            ok_sources += 1
            await _status(db, source, None, moment)
        except Exception as exc:  # noqa: BLE001 — один сайт упал, остальные работают
            text = f"{type(exc).__name__}: {str(exc)[:200]}"
            result.errors[source] = text
            logger.warning("jobradar source %s failed: %s", source, text)
            await _status(db, source, text, moment)
    found = list({v.uid: v for v in found}.values())   # одно и то же по разным ключевым словам
    result.found = len(found)
    rows = await save_vacancies(db, found)
    groups = core.group(found)
    known = set((await db.scalars(select(JrMatch.fingerprint).where(
        JrMatch.filter_id == f.id, JrMatch.fingerprint.in_(list(groups) or [""])))).all())

    fresh: list[list[Vacancy]] = []
    for fp, cards in groups.items():
        if fp in known:
            continue
        ok, why = core.matches(cards[0], query)
        if not ok:   # проверяем и остальные карточки: на другом сайте могли указать зарплату или удалёнку
            ok = any(core.matches(c, query)[0] for c in cards[1:])
        if not ok:
            db.add(JrMatch(filter_id=f.id, fingerprint=fp, vacancy_id=rows[cards[0].uid].id, status="skipped",
                           reason=why, sources=sorted({c.source for c in cards})))
            result.skipped += 1
            continue
        fresh.append(cards)
    result.matched = len(fresh)
    await db.commit()

    scores: dict[str, tuple[int, str]] = {}
    if f.ai_min_score and fresh:
        scores = await scoring.score(db, f.id, f.profile or "", [g[0] for g in fresh], workspace_id=f.workspace_id,
                                     complete=complete)
    posted = 0
    for cards in fresh:
        main = cards[0]
        fp = fingerprint(main.title, main.company)
        names = sorted({c.source for c in cards}, key=lambda s: core.PRIORITY.get(s, 9))
        value, why = scores.get(main.uid, (None, ""))
        match = JrMatch(filter_id=f.id, fingerprint=fp, vacancy_id=rows[main.uid].id, status="pending", score=value,
                        why=why or None, sources=names)
        if value is not None and value < f.ai_min_score:
            match.status, match.reason = "ai_low", f"ИИ: {value}/100 — ниже порога {f.ai_min_score}"
            result.ai_low += 1
            db.add(match)
            await db.commit()
            continue
        lead = False
        if f.mode == "leads":
            company_id, action = await leads.add_company(db, f.workspace_id, cards, client, now=moment)
            match.company_id = company_id
            if action in ("added", "known"):
                lead, match.status = True, "lead"
                result.leads += 1
            else:
                match.status, match.reason = "skipped", {"blocked": "компания в стоп-листе",
                                                          "skipped": "не удалось определить компанию"}[action]
                result.skipped += 1
                db.add(match)
                await db.commit()
                continue
        if not f.chat_id or posted >= MAX_POSTS:
            if match.status == "pending":   # некуда слать / лимит прохода — вернёмся в следующий раз
                result.failed += posted >= MAX_POSTS
                continue
            db.add(match)
            await db.commit()
            continue
        db.add(match)
        await db.flush()   # нужен id для кнопок
        try:
            if posted:
                await sleep(POST_GAP)
            text = posting.render(main, score=value, why=why, sources=names, lead=lead)
            match.message_id = await send(f.chat_id, text, posting.keyboard(match.id))
            match.chat_id, match.sent_at = str(f.chat_id), now()
            if match.status == "pending":
                match.status = "sent"
            posted += 1
            result.sent += 1
            await db.commit()
        except Exception as exc:  # noqa: BLE001 — Telegram не принял: остальное дошлём в следующий проход
            logger.warning("jobradar telegram send failed type=%s", type(exc).__name__)
            await db.rollback()
            result.failed += 1
            result.errors["telegram"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            break
    f = await db.get(JrFilter, filter_id)
    if ok_sources and not result.failed:   # всё упало или Telegram не принял — окно не сдвигаем, дошлём потом
        f.last_run_at = moment
    f.last_result = {**asdict(result), "at": moment.isoformat()}
    await db.commit()
    return result


# ---------------------------------------------------------------- фон

async def run_due(moment: datetime | None = None) -> int:
    moment = moment or now()
    async with session_factory() as db:
        cfg = await get_settings(db)
        every = timedelta(minutes=max(5, int(cfg["interval_minutes"])))
        ids = [f.id for f in (await db.scalars(select(JrFilter).where(JrFilter.enabled.is_(True)))).all()
               if not f.last_run_at or aware(f.last_run_at) <= moment - every]
    for filter_id in ids:
        await run_one(filter_id)
    return len(ids)


_lock: asyncio.Lock | None = None


async def run_one(filter_id: int) -> Result | None:
    """Проход одного фильтра по очереди: ручной «Проверить сейчас» и фон не идут одновременно."""
    global _lock
    _lock = _lock or asyncio.Lock()
    async with _lock:
        async with session_factory() as db:
            f = await db.get(JrFilter, filter_id)
            if f is None:
                return None
            try:
                return await run_filter(db, f)
            except Exception:
                logger.exception("jobradar filter %s failed", filter_id)
                await db.rollback()
                return None


async def _lease():
    """В проде бэкенд может быть в нескольких процессах: проходит тот, кто держит ключ в Redis."""
    try:
        from redis.asyncio import Redis
        redis = Redis.from_url(app_settings.redis_url)
        owner = secrets.token_hex(8)
        ok = await redis.set("steptolead:jobradar:lease", owner, nx=True, ex=TICK * 10)
        return redis, owner if ok else None
    except Exception:  # noqa: BLE001 — без Redis работаем как один процесс
        return None, "local"


async def run_worker() -> None:
    await asyncio.sleep(90)
    if app_settings.agency_workspace_id:
        try:
            async with session_factory() as db:
                await ensure_starter(db, app_settings.agency_workspace_id)
                await db.commit()
        except Exception:
            logger.exception("jobradar seed failed")
    while True:
        redis, owner = await _lease()
        try:
            if owner:
                await run_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("jobradar pass failed")
        finally:
            if redis is not None:
                try:
                    if owner and owner != "local" and await redis.get("steptolead:jobradar:lease") in (owner, owner.encode()):
                        await redis.delete("steptolead:jobradar:lease")
                    await redis.aclose()
                except Exception:  # noqa: BLE001
                    pass
        await asyncio.sleep(TICK)
