"""Журнал расходов на данные по клиенту (lg_spend). Пишет раннер источников по завершении шага; здесь же сводка.

Сумма — оценка по фактическому объёму: число платных запросов × цена из настроек источника. Точного счёта от
поставщиков портал не получает, поэтому расхождение с их кабинетом возможно — журнал отвечает на вопрос
«во что клиенту обошлись данные», а не заменяет биллинг.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.leadgen import service
from app.domains.leadgen.models import LgSpend

TZ = ZoneInfo("Europe/Moscow")


def local_day(now: datetime | None = None) -> date:
    return service._aware(now or service.utcnow()).astimezone(TZ).date()


async def record(db: AsyncSession, workspace_id: int, kind: str, cost: dict | None, *, run_id: int | None = None,
                 now: datetime | None = None) -> LgSpend | None:
    """cost — как у CompanySource.cost(): {"requests": int, "rub": float | None}. Бесплатное не пишем. Не коммитит."""
    if not cost:
        return None
    units = int(cost.get("requests") or 0)
    rub = cost.get("rub", cost.get("cost_rub"))
    if units <= 0 and not rub:
        return None
    row = LgSpend(workspace_id=workspace_id, day=local_day(now), kind=kind[:32], units=units,
                  rub=round(float(rub), 4) if rub is not None else None, run_id=run_id)
    db.add(row)
    await db.flush()
    return row


async def summary(db: AsyncSession, workspace_id: int, *, days: int = 30, now: datetime | None = None) -> dict:
    since = local_day(now) - timedelta(days=days - 1)
    rows = (await db.execute(select(LgSpend.kind, func.sum(LgSpend.units), func.sum(LgSpend.rub),
                                    func.count(LgSpend.id).filter(LgSpend.rub.is_(None)))
                             .where(LgSpend.workspace_id == workspace_id, LgSpend.day >= since)
                             .group_by(LgSpend.kind).order_by(LgSpend.kind))).all()
    items = [{"kind": kind, "units": int(units or 0), "rub": round(float(rub or 0), 2), "unpriced_runs": int(unpriced or 0)}
             for kind, units, rub, unpriced in rows]
    return {"days": days, "since": since.isoformat(), "items": items,
            "units": sum(i["units"] for i in items), "rub": round(sum(i["rub"] for i in items), 2)}
