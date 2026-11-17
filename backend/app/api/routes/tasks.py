"""Общий список долгих операций админки (GET /api/admin/tasks) — для индикатора задач в шапке и для
восстановления прогресса, когда возвращаешься на страницу. Операции идут на сервере независимо от вкладки;
здесь только их снимки (services/progress.py) и ссылка на экран, где операцию запустили."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import require_admin
from app.core.config import settings
from app.db import get_db
from app.services import progress

router = APIRouter(prefix="/admin/tasks", tags=["admin-tasks"], dependencies=[Depends(require_admin)])

_KEY = re.compile(r"^(?P<area>[a-z]+):(?P<kind>[a-z-]+):(?P<id>\d+)$")


async def _leadgen_runs(db: AsyncSession, ids: set[int]) -> dict[int, tuple[str, dict, int]]:
    if not ids:
        return {}
    from app.domains.leadgen.models import LgSourceRun
    rows = (await db.execute(select(LgSourceRun.id, LgSourceRun.source, LgSourceRun.params, LgSourceRun.workspace_id)
                             .where(LgSourceRun.id.in_(ids)))).all()
    return {rid: (source or "", params or {}, ws) for rid, source, params, ws in rows}


def _with_space(link: str | None, workspace_id: int | None) -> str | None:
    """Задача идёт в пространстве клиента: «Открыть» должно переключить раздел на него (?ws=)."""
    if not link or not workspace_id:
        return link
    return f"{link}{'&' if '?' in link else '?'}ws={workspace_id}"


def _leadgen_link(run_id: int, source: str, params: dict) -> tuple[str | None, str]:
    if source.startswith("list:"):
        return f"/admin/leadgen/lists/{source[5:]}", "list"
    if source == "yandex_direct":
        return f"/admin/leadgen/search?run={run_id}", "search"
    if source == "company_enrichment":
        return "/admin/leadgen/companies", "contacts"
    if source:
        return f"/admin/leadgen/sources?run={run_id}", "source"
    return None, "other"


@router.get("")
async def tasks(db: AsyncSession = Depends(get_db)):
    items = progress.listing()
    parsed = [(_KEY.match(t["key"]), t) for t in items]
    runs = await _leadgen_runs(db, {int(m["id"]) for m, _ in parsed if m and m["area"] == "leadgen"})
    list_ids = {int(src[5:]) for src, _, _ in runs.values() if src.startswith("list:") and src[5:].isdigit()}
    clients = {}
    if runs:
        from app.models.marketing import ClientWorkspace
        clients = dict((await db.execute(select(ClientWorkspace.id, ClientWorkspace.name).where(
            ClientWorkspace.id.in_({ws for _, _, ws in runs.values()})))).all())
    names = {}
    if list_ids:
        from app.domains.leadgen.models import LgList
        names = dict((await db.execute(select(LgList.id, LgList.name).where(LgList.id.in_(list_ids)))).all())
    out = []
    for m, t in parsed:
        link, page, run_id, source, workspace = None, "other", None, None, None
        if m and m["area"] == "jobradar":
            link, page = f"/admin/jobradar/{m['id']}", f"jobradar-{m['kind']}"
            run_id = int(m["id"])
        elif m and m["area"] == "telegram":     # инфраструктура Telegram-аутрича (services/telegram_jobs.py)
            link = "/admin/proxies" if m["kind"] == "proxy-check" else "/admin/accounts"
            page, run_id = f"telegram-{m['kind']}", int(m["id"])
        elif m and m["area"] == "leadgen":
            run_id = int(m["id"])
            source, params, ws = runs.get(run_id, ("", {}, None))
            link, page = _leadgen_link(run_id, source, params)
            link = _with_space(link, ws)
            if source.startswith("list:") and source[5:].isdigit() and names.get(int(source[5:])):
                t = {**t, "title": f"{t['title']} · {names[int(source[5:])]}"}
            if ws and clients.get(ws):
                workspace = {"id": ws, "name": clients[ws], "is_home": ws == settings.agency_workspace_id}
                t = {**t, "title": f"{clients[ws]} · {t['title']}"}
        out.append({**t, "link": link, "page": page, "run_id": run_id, "source": source, "workspace": workspace})
    return {"items": out}
