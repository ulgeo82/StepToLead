"""Импорт базы из файла: строки таблицы -> компании (со склейкой дублей) -> запуск с историей и статистикой.

Каждый импорт — это LgSourceRun(source="import"): его видно в истории запусков, а компании импорта
открываются в базе фильтром run_id, как результаты поиска.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.leadgen import service
from app.domains.leadgen.core.table_import import Parsed
from app.domains.leadgen.models import LgSourceRun

SOURCE = "import"


def _personal(email: str, domain: str | None) -> bool:
    from app.domains.leadgen.site_enrich import _looks_personal
    return bool(domain) and _looks_personal(email, domain)


async def import_parsed(db: AsyncSession, workspace_id: int, parsed: Parsed, *, niche: str | None = None,
                        city: str | None = None, filename: str | None = None, user_id: int | None = None,
                        now: datetime | None = None) -> LgSourceRun:
    now = now or service.utcnow()
    run = LgSourceRun(workspace_id=workspace_id, source=SOURCE, status="running", created_by_id=user_id,
                      started_at=now, params={"filename": filename, "niche": niche, "city": city}, stats={})
    db.add(run)
    await db.flush()
    created, attached, review, skipped = [], [], 0, []
    for row in parsed.rows:
        contacts = [{"kind": "email", "value": e, "is_personal": _personal(e, row.domain)} for e in row.emails]
        contacts += [{"kind": "telegram", "value": t} for t in row.telegram]
        contacts += [{"kind": "whatsapp", "value": w} for w in row.whatsapp]
        result = await service.upsert_company(db, workspace_id, service.FindingIn(
            source=SOURCE, name=row.name, legal_name=row.legal_name, inn=row.inn, domain=row.domain,
            city=row.city or city, niche=row.niche or niche, phones=row.phones, contacts=contacts), now=now)
        if result.company is None:
            skipped.append({"line": row.line, "reason": "сайт-площадка (маркетплейс, соцсеть, агрегатор)",
                            "value": row.domain or row.name or ""})
            continue
        if result.match.action == "review":
            review += 1
        await service.recalc_score(db, result.company, now=now)
        (created if result.created else attached).append(result.company.id)
    ids = list(dict.fromkeys(created + attached))
    run.status, run.finished_at = "done", service.utcnow()
    run.stats = {"rows": len(parsed.rows) + len(parsed.invalid), "companies": len(ids), "created": len(created),
                 "already_in_base": len(set(attached) - set(created)), "needs_review": review,
                 "invalid": len(parsed.invalid) + len(skipped), "truncated": parsed.truncated,
                 "columns": parsed.columns, "errors": (parsed.invalid + skipped)[:50],
                 "company_ids": ids, "new_company_ids": created}
    await db.flush()
    return run
