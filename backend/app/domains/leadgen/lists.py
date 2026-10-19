"""Списки компаний поверх единой базы: каждый запуск поиска/импорт — свой список, плюс ручные списки.

База одна и без дублей (компания = одна запись). Список — это «где и когда компания находилась»:
одна компания может быть в нескольких списках. Выгрузки и аутрич работают по спискам.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, case, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.leadgen import service
from app.domains.leadgen.models import LgCompany, LgContact, LgList, LgListItem, LgSourceRun

SOURCE_NAMES = {"yandex_direct": "Поиск", "import": "Импорт", "yandex_organic": "Поиск", "twogis": "2ГИС", "hh": "HH"}


def run_list_name(run: LgSourceRun) -> str:
    params = run.params or {}
    when = (run.finished_at or run.created_at or service.utcnow())
    parts = [params.get("niche") or SOURCE_NAMES.get(run.source, "Поиск"), params.get("city"),
             when.strftime("%d.%m.%Y")]
    return " · ".join(p for p in parts if p)[:180]


def run_company_ids(run: LgSourceRun) -> tuple[list[int], set[int]]:
    stats = run.stats or {}
    ids = list(dict.fromkeys(int(i) for i in (stats.get("company_ids") or []) + (stats.get("organic_company_ids") or [])))
    new = {int(i) for i in (stats.get("new_company_ids") or []) + (stats.get("organic_new_ids") or [])}
    return ids, new


async def add_companies(db: AsyncSession, lst: LgList, company_ids: list[int], new_ids: set[int] | None = None,
                        now: datetime | None = None) -> int:
    """Добавить в список (повтор — без дубля). Возвращает, сколько добавлено."""
    if not company_ids:
        return 0
    have = set((await db.scalars(select(LgListItem.company_id).where(
        LgListItem.list_id == lst.id, LgListItem.company_id.in_(company_ids)))).all())
    alive = set((await db.scalars(select(LgCompany.id).where(
        LgCompany.workspace_id == lst.workspace_id, LgCompany.id.in_(company_ids)))).all())
    added = 0
    for cid in dict.fromkeys(company_ids):
        if cid in have or cid not in alive:
            continue
        db.add(LgListItem(list_id=lst.id, company_id=cid, is_new=cid in (new_ids or set()),
                          added_at=now or service.utcnow()))
        added += 1
    await db.flush()
    return added


async def sync_run(db: AsyncSession, run: LgSourceRun) -> LgList | None:
    """Список запуска: создаётся один раз, при повторных синхронизациях дополняется."""
    if run.status != "done":
        return None
    ids, new = run_company_ids(run)
    lst = await db.scalar(select(LgList).where(LgList.run_id == run.id))
    if lst is None:
        lst = LgList(workspace_id=run.workspace_id, name=run_list_name(run), kind="run", run_id=run.id,
                     created_by_id=run.created_by_id, created_at=run.finished_at or run.created_at)
        db.add(lst)
        await db.flush()
    await add_companies(db, lst, ids, new, now=run.finished_at or run.created_at)
    return lst


async def ensure_run_lists(db: AsyncSession, workspace_id: int) -> int:
    """Прошлые и повторные (по расписанию) запуски без списка получают его автоматически."""
    has_list = exists().where(LgList.run_id == LgSourceRun.id)
    runs = (await db.scalars(select(LgSourceRun).where(LgSourceRun.workspace_id == workspace_id,
                                                       LgSourceRun.status == "done", ~has_list)
                             .order_by(LgSourceRun.id))).all()
    for run in runs:
        await sync_run(db, run)
    return len(runs)


async def stats(db: AsyncSession, list_ids: list[int]) -> dict[int, dict]:
    """Счётчики списков одной пачкой: всего, новых, с контактами, подходят по ИИ, ещё не выгружались."""
    if not list_ids:
        return {}
    has_contact = exists().where(LgContact.company_id == LgCompany.id, LgContact.bounced.is_(False),
                                 LgContact.kind.in_(("phone", "email", "whatsapp", "telegram")))
    rows = (await db.execute(
        select(LgListItem.list_id,
               func.count(LgCompany.id),
               func.sum(case((LgListItem.is_new.is_(True), 1), else_=0)),
               func.sum(case((has_contact, 1), else_=0)),
               func.sum(case((LgCompany.fit_label == "fit", 1), else_=0)),
               func.sum(case((LgCompany.last_exported_at.is_(None), 1), else_=0)))
        .join(LgCompany, and_(LgCompany.id == LgListItem.company_id, LgCompany.stage != "hidden"))
        .where(LgListItem.list_id.in_(list_ids)).group_by(LgListItem.list_id))).all()
    out = {lid: {"total": 0, "new": 0, "with_contacts": 0, "fit": 0, "not_exported": 0} for lid in list_ids}
    for lid, total, new, contacts, fit, fresh in rows:
        out[lid] = {"total": int(total or 0), "new": int(new or 0), "with_contacts": int(contacts or 0),
                    "fit": int(fit or 0), "not_exported": int(fresh or 0)}
    return out


async def company_lists(db: AsyncSession, company_ids: list[int]) -> dict[int, list[dict]]:
    """Для строк базы: в каких (не архивных) списках компания."""
    if not company_ids:
        return {}
    rows = (await db.execute(select(LgListItem.company_id, LgList.id, LgList.name, LgList.run_id)
                             .join(LgList, LgList.id == LgListItem.list_id)
                             .where(LgListItem.company_id.in_(company_ids), LgList.archived_at.is_(None))
                             .order_by(LgList.id.desc()))).all()
    out: dict[int, list[dict]] = {}
    for cid, lid, name, run_id in rows:
        out.setdefault(cid, []).append({"id": lid, "name": name, "run_id": run_id})
    return out


async def company_ids_in(db: AsyncSession, list_ids: list[int]) -> list[int]:
    return list(dict.fromkeys((await db.scalars(select(LgListItem.company_id)
                                                .where(LgListItem.list_id.in_(list_ids)))).all()))


def list_row(lst: LgList, counts: dict | None = None) -> dict:
    return {"id": lst.id, "name": lst.name, "kind": lst.kind, "run_id": lst.run_id, "note": lst.note,
            "archived": lst.archived_at is not None,
            "archived_at": lst.archived_at.isoformat() if lst.archived_at else None,
            "created_at": lst.created_at.isoformat() if lst.created_at else None,
            **(counts or {"total": 0, "new": 0, "with_contacts": 0, "fit": 0, "not_exported": 0})}
