"""Источники компаний в базе (lg_sources): вкл/выкл, настройки, статус. Логика настроек — общая с площадками
радара (jobradar/platforms.py: plain / public / apply / state / mark), здесь только свои строки таблицы."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.jobradar.platforms import apply, mark, plain, public, state  # noqa: F401 — переиспользуем
from app.domains.leadgen.models import LgSource
from app.domains.leadgen.sources import REGISTRY, Context

transport = None   # подмена сети в тестах (httpx.MockTransport)
sleep = None       # подмена пауз в тестах


async def ensure(db: AsyncSession) -> dict[str, LgSource]:
    """Строки для всех источников реестра; новый источник появляется сам, с настройками по умолчанию."""
    rows = {r.code: r for r in (await db.scalars(select(LgSource))).all()}
    for code, adapter in REGISTRY.items():
        if code in rows:
            continue
        row = LgSource(code=code, enabled=adapter.default_enabled, settings=adapter.defaults())
        try:
            async with db.begin_nested():
                db.add(row)
            rows[code] = row
        except IntegrityError:
            rows[code] = await db.scalar(select(LgSource).where(LgSource.code == code))
    return rows


def context() -> Context:
    ctx = Context(transport=transport)
    if sleep is not None:
        ctx.sleep = sleep
    return ctx
