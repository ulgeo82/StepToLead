"""Рабочее место базы (WORKSPACE.md, разделы 2–3): таблица списка с фильтрами по колонкам, совпадения, сводка,
очистка, правка ячейки, «на проверку».

Строки собираются в памяти по всему списку (списки — сотни/тысячи компаний), поэтому фильтр, сортировка, сводка
и очистка считают одно и то же: очистка preview и apply берут компании одной функцией.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.leadgen import service
from app.domains.leadgen.core.normalize import (
    clean_company_name, normalize_domain, normalize_phone, text_key, validate_inn,
)
from app.domains.leadgen.models import (
    LgCompany, LgCompanyKey, LgContact, LgEnrichment, LgList, LgListItem, LgSignal,
)

MAX_ROWS = 10000
EMAIL_RANK = {"valid": 0, "unknown": 1, "risky": 2, "invalid": 3}
MESSENGERS = ("whatsapp", "telegram", "vk")
STATUSES = ("valid", "risky", "invalid", "unknown")

# key, label, filter: text — «содержит» + пусто/не пусто; values — выбор значений; empty — только пусто/не пусто
COLUMNS = [
    ("display_name", "Компания", "text"), ("domain", "Сайт", "text"), ("inn", "ИНН", "text"),
    ("city", "Город", "text"), ("niche", "Ниша", "text"), ("email", "Почта", "text"),
    ("email_status", "Статус почты", "values"), ("phone", "Телефон", "text"),
    ("messengers", "Мессенджеры", "values"), ("signal", "Сигналы", "values"), ("fit", "ИИ", "values"),
    ("score", "Скоринг", "text"), ("revenue_rub", "Выручка", "text"), ("stage", "Стадия", "values"),
    ("source", "Источник", "values"), ("added_at", "Добавлена", "text"),
    ("personal_line", "Персонализация", "text"), ("needs_review", "На проверке", "empty"),
]
SUMMARY_COLUMNS = ("city", "niche", "domain", "source", "signal", "fit", "email_status", "stage")
CLEANUP_RULES = ("no_email", "bad_email", "not_fit", "chains", "duplicates")


class WorkspaceError(Exception):
    def __init__(self, status: int, detail: str, **extra):
        super().__init__(detail)
        self.status, self.detail, self.extra = status, detail, extra


def _iso(v):
    v = service._aware(v)
    return v.isoformat() if v else None


def _low(v) -> str:
    return str(v if v is not None else "").lower().replace("ё", "е")


async def get_list(db: AsyncSession, ws: int, list_id: int) -> LgList:
    lst = await db.get(LgList, list_id)
    if lst is None or lst.workspace_id != ws:
        raise WorkspaceError(404, "Список не найден")
    return lst


# Откуда компания пришла в базу — только источники находок. Дообогащение (чистка названий, DaData, поиск сайта…)
# тоже пишет provenance, но источником компании не является.
ORIGINS = {"yandex_direct": "Директ", "yandex_organic": "Поисковая выдача", "hh": "hh.ru", "trudvsem": "Работа России",
           "twogis": "2ГИС", "import": "Импорт", "manual": "Вручную"}


def _source(c: LgCompany) -> str | None:
    prov = c.provenance or {}
    entries = [(v.get("at") or "", v.get("source")) for k, v in prov.items()
               if isinstance(v, dict) and v.get("source") in ORIGINS and k != "stage_before_hide"]
    return ORIGINS[min(entries)[1]] if entries else None


async def rows(db: AsyncSession, ws: int, list_id: int) -> list[dict]:
    """Все строки списка (без скрытых — их видно только с фильтром stage=in:hidden)."""
    await get_list(db, ws, list_id)
    pairs = (await db.execute(select(LgCompany, LgListItem.added_at).join(
        LgListItem, LgListItem.company_id == LgCompany.id).where(
        LgListItem.list_id == list_id, LgCompany.workspace_id == ws).limit(MAX_ROWS))).all()
    ids = [c.id for c, _ in pairs]
    if not ids:
        return []
    contacts: dict[int, list[LgContact]] = {}
    for x in (await db.scalars(select(LgContact).where(LgContact.company_id.in_(ids)))).all():
        contacts.setdefault(x.company_id, []).append(x)
    now = service.utcnow()
    signals: dict[int, set[str]] = {}
    for cid, kind in (await db.execute(select(LgSignal.company_id, LgSignal.kind).where(
            LgSignal.company_id.in_(ids), (LgSignal.expires_at.is_(None)) | (LgSignal.expires_at > now)))).all():
        signals.setdefault(cid, set()).add(kind)
    personal = {cid: (res or {}).get("line") for cid, res in (await db.execute(select(
        LgEnrichment.company_id, LgEnrichment.result).where(LgEnrichment.company_id.in_(ids),
                                                            LgEnrichment.step == "personalize"))).all()}
    out = []
    for c, added in pairs:
        from app.domains.leadgen.routes import company_row
        mine = contacts.get(c.id, [])
        emails = sorted([x for x in mine if x.kind == "email" and not x.is_personal and not x.bounced],
                        key=lambda x: (EMAIL_RANK.get(x.verify_status or "unknown", 1), not x.is_primary, x.id))
        phone = next((x.value for x in sorted(mine, key=lambda x: (not x.is_primary, x.id)) if x.kind == "phone"), None)
        best = emails[0] if emails else None
        row = {**company_row(c),
               "best_email": {"value": best.value, "verify_status": best.verify_status or "unknown"} if best else None,
               "emails": len(emails), "phone": phone,
               "messengers": sorted({x.kind for x in mine if x.kind in MESSENGERS}),
               "signals": sorted(signals.get(c.id, ())), "revenue_rub": c.revenue_rub, "source": _source(c),
               "added_at": _iso(added), "personal_line": personal.get(c.id), "duplicate_group": None}
        out.append(row)
    _duplicates(out, contacts)
    return out


def _duplicates(items: list[dict], contacts: dict[int, list[LgContact]]) -> None:
    """Один телефон, или одно нормализованное название в одном городе, или один домен у разных записей."""
    parent = {r["id"]: r["id"] for r in items}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    buckets: dict[str, list[int]] = {}
    for r in items:
        keys = {f"p:{normalize_phone(x.value)}" for x in contacts.get(r["id"], [])
                if x.kind in ("phone", "whatsapp") and normalize_phone(x.value)}
        name = text_key(clean_company_name(r["display_name"] or r["legal_name"]))
        if name:
            keys.add(f"n:{name}|{text_key(r['city']) or ''}")
        if r["domain"]:
            keys.add(f"d:{r['domain']}")
        for k in keys:
            buckets.setdefault(k, []).append(r["id"])
    for members in buckets.values():
        for other in members[1:]:
            a, b = find(members[0]), find(other)
            if a != b:
                parent[max(a, b)] = min(a, b)
    groups: dict[int, list[int]] = {}
    for r in items:
        groups.setdefault(find(r["id"]), []).append(r["id"])
    for r in items:
        root = find(r["id"])
        if len(groups[root]) > 1:
            r["duplicate_group"] = f"g{root}"


# ---------------------------------------------------------------- фильтры, поиск, сортировка

def value(row: dict, key: str):
    """Значение колонки для фильтра/сортировки/сводки. Списковые колонки — list."""
    if key == "email":
        return (row["best_email"] or {}).get("value")
    if key == "email_status":
        return (row["best_email"] or {}).get("verify_status") or "none"
    if key == "signal":
        return row["signals"]
    if key == "fit":
        return row["fit_label"]
    return row.get(key)


def _empty(v) -> bool:
    return v in (None, "", [], "none")


def parse_filters(params: dict[str, str]) -> list[tuple[str, str, object]]:
    known = {k for k, _, _ in COLUMNS}
    out = []
    for raw_key, cond in params.items():
        if not raw_key.startswith("f."):
            continue
        key = raw_key[2:]
        if key not in known:
            raise WorkspaceError(422, f"Нет колонки «{key}»")
        cond = (cond or "").strip()
        if cond in ("empty", "notempty"):
            out.append((key, cond, None))
        elif cond.startswith("contains:"):
            out.append((key, "contains", _low(cond[9:]).strip()))
        elif cond.startswith("in:"):
            out.append((key, "in", {v.strip() for v in cond[3:].split(",") if v.strip()}))
        else:
            raise WorkspaceError(422, f"Неверное условие фильтра «{key}»")
    return out


def _match(row: dict, key: str, op: str, arg) -> bool:
    v = value(row, key)
    if op == "empty":
        return _empty(v)
    if op == "notempty":
        return not _empty(v)
    if op == "contains":
        items = v if isinstance(v, list) else [v]
        return any(arg in _low(i) for i in items if i is not None)
    values = v if isinstance(v, list) else [v]
    return any(str(i) in arg for i in values if i is not None) or (None in values and "none" in arg)


def apply(items: list[dict], *, q: str | None = None, filters=()) -> list[dict]:
    want_hidden = any(k == "stage" and op == "in" and "hidden" in arg for k, op, arg in filters)
    out = [r for r in items if want_hidden or r["stage"] != "hidden"]
    for key, op, arg in filters:
        out = [r for r in out if _match(r, key, op, arg)]
    if q and q.strip():
        needle = _low(q).strip()
        out = [r for r in out if any(needle in _low(value(r, k)) for k, _, _ in COLUMNS)]
    return out


def sort(items: list[dict], key: str | None, direction: str) -> list[dict]:
    key = key or "added_at"
    if key not in {k for k, _, _ in COLUMNS}:
        raise WorkspaceError(422, f"Нет колонки «{key}»")
    present = [r for r in items if not _empty(value(r, key))]
    missing = [r for r in items if _empty(value(r, key))]

    def k(r):
        v = value(r, key)
        if isinstance(v, list):
            v = ", ".join(v)
        return (0, v, r["id"]) if isinstance(v, (int, float)) else (1, _low(v), r["id"])
    present.sort(key=k, reverse=direction == "desc")
    return present + missing   # пустые — всегда в конце


async def table(db, ws, list_id, *, q=None, sort_key=None, direction="desc", page=1, per_page=100,
                params: dict | None = None) -> dict:
    filters = parse_filters(params or {})
    items = sort(apply(await rows(db, ws, list_id), q=q, filters=filters), sort_key, direction)
    start = (max(1, page) - 1) * per_page
    return {"total": len(items), "items": items[start:start + per_page],
            "columns": [{"key": k, "label": label, "filter": f} for k, label, f in COLUMNS]}


async def summary(db, ws, list_id, column, *, q=None, params=None) -> dict:
    if column not in SUMMARY_COLUMNS:
        raise WorkspaceError(422, "Сводка по этой колонке недоступна")
    items = apply(await rows(db, ws, list_id), q=q, filters=parse_filters(params or {}))
    counts: dict = {}
    for r in items:
        v = value(r, column)
        for one in (v if isinstance(v, list) else [v]) or [None]:
            one = None if _empty(one) else one
            counts[one] = counts.get(one, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0] is None, _low(kv[0])))
    return {"total": len(items), "items": [{"value": v, "count": n} for v, n in ordered]}


# ---------------------------------------------------------------- очистка

def cleanup_targets(items: list[dict], rule: str, params: dict) -> list[dict]:
    if rule not in CLEANUP_RULES:
        raise WorkspaceError(422, "Неизвестное правило очистки")
    live = [r for r in items if r["stage"] != "hidden"]
    if rule == "no_email":
        return [r for r in live if not r["best_email"]]
    if rule == "bad_email":       # лучшая почта невалидна — значит, валидных нет совсем
        return [r for r in live if r["best_email"] and r["best_email"]["verify_status"] == "invalid"]
    if rule == "not_fit":
        return [r for r in live if r["fit_label"] == "no"]
    if rule == "chains":
        limit = int(params.get("revenue_max") or 500_000_000)
        return [r for r in live if {"chain", "big_company"} & set(r["signals"])
                or (r["revenue_rub"] and r["revenue_rub"] > limit)]
    return [r for r in live if r["duplicate_group"] and not (r["needs_review"] or "").startswith("duplicate")]


async def cleanup(db, ws, list_id, rule, params, *, apply_it: bool) -> dict:
    targets = cleanup_targets(await rows(db, ws, list_id), rule, params)
    if not apply_it:
        return {"count": len(targets), "sample": targets[:10]}
    ids = [r["id"] for r in targets]
    if rule == "duplicates":
        return {"updated": await mark_review(db, ws, ids, "duplicate", prefix="")}
    return {"updated": await hide(db, ws, ids)}


async def hide(db, ws, ids) -> int:
    """Как «Скрыть» в базе (routes.hide_companies): компании с идущей цепочкой не трогаем."""
    from app.domains.leadgen.routes import _busy_reason
    done = 0
    for c in (await db.scalars(select(LgCompany).where(LgCompany.workspace_id == ws, LgCompany.id.in_(ids)))).all():
        if c.stage == "hidden" or await _busy_reason(db, c):
            continue
        c.provenance = {**(c.provenance or {}), "stage_before_hide": c.stage}
        c.stage = "hidden"
        done += 1
    await db.flush()
    return done


# ---------------------------------------------------------------- «на проверку» и правка ячейки

async def mark_review(db, ws, ids, reason: str | None, *, prefix: str = "manual:") -> int:
    reason = (reason or "").strip()[:30] or "проверить"
    value_ = f"{prefix}{reason}"[:40]
    n = 0
    for c in (await db.scalars(select(LgCompany).where(LgCompany.workspace_id == ws, LgCompany.id.in_(ids)))).all():
        c.needs_review = value_
        n += 1
    await db.flush()
    return n


async def clear_review(db, ws, ids) -> int:
    n = 0
    for c in (await db.scalars(select(LgCompany).where(LgCompany.workspace_id == ws, LgCompany.id.in_(ids)))).all():
        if c.needs_review:
            c.needs_review, n = None, n + 1
    await db.flush()
    return n


EDITABLE = ("display_name", "domain", "inn", "city", "niche", "personal_line")
MAX_LINE = 200


async def edit(db, ws, company_id: int, data: dict) -> LgCompany:
    c = await db.get(LgCompany, company_id)
    if c is None or c.workspace_id != ws:
        raise WorkspaceError(404, "Компания не найдена")
    if "personal_line" in data:
        await _edit_line(db, c, data.pop("personal_line"))
    values: dict = {}
    for key, raw in data.items():
        if key not in EDITABLE:
            raise WorkspaceError(422, f"Поле «{key}» нельзя править")
        text = (raw or "").strip() if isinstance(raw, str) or raw is None else str(raw)
        if key == "inn" and text:
            if not validate_inn(text):
                raise WorkspaceError(422, "Неверный ИНН: не сходится контрольная сумма")
            text = validate_inn(text)
        if key == "domain" and text:
            text = normalize_domain(text)
            if not text:
                raise WorkspaceError(422, "Не похоже на адрес сайта")
        values[key] = text[:240] if text else None
    for key, kind in (("inn", "inn"), ("domain", "domain")):
        new = values.get(key)
        if new and new != getattr(c, key):
            other = await db.scalar(select(LgCompany.id).where(LgCompany.workspace_id == ws, LgCompany.id != c.id,
                                                              getattr(LgCompany, key) == new).limit(1))
            if other:
                label = "ИНН" if key == "inn" else "сайт"
                raise WorkspaceError(409, f"Этот {label} уже у другой компании (№{other})", company_id=other)
    now = service.utcnow()
    prov = dict(c.provenance or {})
    for key, new in values.items():
        old = getattr(c, key)
        setattr(c, key, new)
        prov[key] = {"source": "manual", "url": None, "at": now.isoformat()}
        if key in ("inn", "domain") and old != new:
            if old:
                await db.execute(LgCompanyKey.__table__.delete().where(
                    LgCompanyKey.company_id == c.id, LgCompanyKey.kind == key, LgCompanyKey.value_norm == old))
            if new:
                db.add(LgCompanyKey(workspace_id=ws, company_id=c.id, kind=key, value_norm=new))
    c.provenance = prov
    c.city_key, c.niche_key = text_key(c.city), text_key(c.niche)
    await db.flush()
    return c


async def _edit_line(db, company: LgCompany, raw) -> None:
    """Ручная правка строки персонализации: помечается manual — повторный запуск «Персонализации» её не перепишет.
    Пустая строка — убрать (дальше снова может писать ИИ)."""
    text = " ".join(str(raw or "").split())
    if len(text) > MAX_LINE:
        raise WorkspaceError(422, f"Строка длиннее {MAX_LINE} символов")
    row = await db.scalar(select(LgEnrichment).where(LgEnrichment.company_id == company.id,
                                                     LgEnrichment.step == "personalize"))
    if row is None:
        row = LgEnrichment(company_id=company.id, step="personalize", result={})
        db.add(row)
    facts = (row.result or {}).get("facts") or []
    if text:
        row.result, row.status, row.error = {"line": text, "facts": facts, "manual": True}, "done", None
    else:
        row.result, row.status = {"facts": facts}, "skipped"
    row.ran_at = service.utcnow()
