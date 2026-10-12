"""Направления клиента из брифа → воронки CRM.

В брифе (вопрос sales.funnels) клиент описывает направления: название, чек, цикл и этапы между «Заявкой» и «Продажей».
«Заявка» (LEAD), «Продажа» (WON) и «Отказ» (LOST) есть всегда; этап, отмеченный как «с него лид целевой», и все
следующие считаются квалифицированными (QUALIFIED) — так аналитика сразу считает CPQL и конверсии правильно.

Перенос в CRM — только по кнопке, с предпросмотром: ничего не удаляем, этапы со сделками не трогаем,
повторный перенос после правки брифа обновляет те же воронки (связь хранится в project.portal_state).
"""
import json
import re
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crm import CrmDeal, CrmPipeline, CrmStage
from app.models.marketing import Project

KEY = "sales.funnels"
STATE_KEY = "brief_pipelines"
MAX_DIRECTIONS, MAX_STAGES, MAX_NAME = 30, 15, 60
LEAD_NAME, WON_NAME, LOST_NAME = "Заявка", "Продажа", "Отказ"
COLORS = {"LEAD": "#006BFD", "QUALIFIED": "#8555E8", "WON": "#15A86B", "LOST": "#EF4A59"}

# Этапы между «Заявкой» и «Продажей»; q — индекс этапа, с которого лид считается целевым.
TEMPLATES = {
    "kitchens": {"name": "Кухни и мебель на заказ", "q": 0, "stages": ["Квалифицирован", "Замер назначен", "Замер проведён", "Дизайн-проект и расчёт", "Договор и предоплата"]},
    "houses": {"name": "Дома и бани", "q": 0, "stages": ["Квалифицирован", "Консультация / встреча", "Проект и смета", "Выезд на участок", "Договор"]},
    "renovation": {"name": "Ремонт квартир", "q": 0, "stages": ["Квалифицирован", "Выезд на объект", "Смета отправлена", "Смета согласована", "Договор"]},
    "windows": {"name": "Окна и потолки", "q": 0, "stages": ["Квалифицирован", "Замер назначен", "Замер проведён", "Расчёт отправлен", "Договор и предоплата"]},
    "dental": {"name": "Стоматология и клиники", "q": 0, "stages": ["Записан на консультацию", "Пришёл на приём", "План лечения выдан", "Начал лечение"]},
    "fences": {"name": "Заборы и ворота", "q": 0, "stages": ["Квалифицирован", "Замер назначен", "Замер проведён", "Смета отправлена", "Договор"]},
    "other": {"name": "Другое", "q": 0, "stages": ["Квалифицирован", "Консультация / встреча", "КП отправлено", "Договор"]},
}


def _name(value, label) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        raise HTTPException(422, f"Укажите {label}")
    return text[:MAX_NAME]


def parse(raw: str | None) -> list[dict]:
    """JSON из брифа → проверенный список направлений. Пусто или не JSON (старый текст) — пустой список."""
    if not raw or not str(raw).strip().startswith("{"):
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(422, "Направления в брифе сохранены с ошибкой — откройте их и сохраните заново") from None
    directions = data.get("directions") if isinstance(data, dict) else None
    if not isinstance(directions, list):
        return []
    if len(directions) > MAX_DIRECTIONS:
        raise HTTPException(422, f"Не больше {MAX_DIRECTIONS} направлений")
    result, seen = [], set()
    for index, item in enumerate(directions, start=1):
        if not isinstance(item, dict):
            continue
        name = _name(item.get("name"), f"название направления {index}")
        if name.casefold() in seen:
            raise HTTPException(422, f"Направление «{name}» указано дважды")
        seen.add(name.casefold())
        raw_stages = list(item.get("stages") or [])
        q_raw = item.get("qualified_from")
        q_raw = int(q_raw) if isinstance(q_raw, (int, float, str)) and str(q_raw).lstrip("-").isdigit() else None
        # Пустые этапы выкидываем; отметку «с него целевой» сдвигаем вместе с ними.
        q_kept = None
        stages = []
        for position, value in enumerate(raw_stages):
            if not str(value or "").strip():
                continue
            if q_raw is not None and position >= q_raw and q_kept is None:
                q_kept = len(stages)
            stages.append(_name(value, f"название этапа в «{name}»"))
        if len(stages) > MAX_STAGES:
            raise HTTPException(422, f"В «{name}» не больше {MAX_STAGES} промежуточных этапов")
        reserved = {LEAD_NAME.casefold(), WON_NAME.casefold(), LOST_NAME.casefold()}
        stages = [s for s in stages if s.casefold() not in reserved]
        if len({s.casefold() for s in stages}) != len(stages):
            raise HTTPException(422, f"В «{name}» есть одинаковые этапы")
        q = q_kept if q_kept is not None and q_kept < len(stages) else (None if not stages else 0)
        result.append({"id": str(item.get("id") or index)[:40], "name": name, "stages": stages, "qualified_from": q,
                       "check": str(item.get("check") or "").strip()[:30], "cycle_days": str(item.get("cycle_days") or "").strip()[:10]})
    return result


def desired(direction: dict) -> list[tuple[str, str]]:
    q = direction["qualified_from"]
    middle = [(name, "QUALIFIED" if q is not None and i >= q else "LEAD") for i, name in enumerate(direction["stages"])]
    return [(LEAD_NAME, "LEAD"), *middle, (WON_NAME, "WON"), (LOST_NAME, "LOST")]


def as_text(raw: str | None) -> str:
    """Для базы знаний ИИ и markdown брифа."""
    lines = []
    for d in parse(raw):
        extra = ", ".join(x for x in (f"средний чек {d['check']}" if d["check"] else "", f"цикл {d['cycle_days']} дн." if d["cycle_days"] else "") if x)
        lines.append(f"Направление «{d['name']}»{f' ({extra})' if extra else ''}: " + " → ".join(n for n, _ in desired(d)[:-1]))
    return "\n".join(lines)


async def _deal_counts(db: AsyncSession, stage_ids: list[int]) -> dict[int, int]:
    if not stage_ids:
        return {}
    rows = await db.execute(select(CrmDeal.stage_id, func.count()).where(
        CrmDeal.stage_id.in_(stage_ids), CrmDeal.archived_at.is_(None)).group_by(CrmDeal.stage_id))
    return dict(rows.all())


async def _target_pipeline(db: AsyncSession, project: Project, direction: dict, index: int, mapping: dict, used: set):
    pid = mapping.get(direction["id"])
    if pid:
        row = await db.get(CrmPipeline, int(pid))
        if row and row.project_id == project.id and not row.archived_at and row.id not in used:
            return row
    if index == 0:
        row = await db.scalar(select(CrmPipeline).where(CrmPipeline.project_id == project.id, CrmPipeline.archived_at.is_(None))
                              .order_by(CrmPipeline.is_default.desc(), CrmPipeline.id).limit(1))
        if row and row.id not in used:
            return row
    row = await db.scalar(select(CrmPipeline).where(CrmPipeline.project_id == project.id, CrmPipeline.archived_at.is_(None),
                                                    func.lower(CrmPipeline.name) == direction["name"].lower()).limit(1))
    return row if row and row.id not in used else None


async def plan(db: AsyncSession, project: Project, directions: list[dict], *, apply: bool = False) -> list[dict]:
    """Что произойдёт с воронками. apply=True — сразу выполнить (вызывающий делает commit)."""
    if not directions:
        raise HTTPException(422, "В брифе не заполнены направления и этапы продаж")
    state = dict(project.portal_state or {})
    mapping = dict(state.get(STATE_KEY) or {})
    used: set[int] = set()
    report = []
    for index, direction in enumerate(directions):
        pipeline = await _target_pipeline(db, project, direction, index, mapping, used)
        item = {"direction": direction["name"], "pipeline": pipeline.name if pipeline else None,
                "action": "update" if pipeline else "create", "stages": [], "kept": [], "archived": []}
        if pipeline is None and apply:
            pipeline = CrmPipeline(workspace_id=project.workspace_id, project_id=project.id, name=direction["name"],
                                   is_default=not await db.scalar(select(CrmPipeline.id).where(
                                       CrmPipeline.project_id == project.id, CrmPipeline.archived_at.is_(None)).limit(1)))
            db.add(pipeline); await db.flush()
        existing = [] if pipeline is None or pipeline.id is None else (await db.scalars(select(CrmStage).where(
            CrmStage.pipeline_id == pipeline.id, CrmStage.archived_at.is_(None)).order_by(CrmStage.position, CrmStage.id))).all()
        counts = await _deal_counts(db, [s.id for s in existing])
        free = list(existing)
        chosen = []
        for name, kind in desired(direction):
            match = next((s for s in free if s.name.casefold() == name.casefold() and
                          (s.analytics_type == kind or (not counts.get(s.id) and kind not in {"WON", "LOST"} and s.analytics_type not in {"WON", "LOST"}))), None)
            if match is None and name in (LEAD_NAME, WON_NAME, LOST_NAME):
                # «Новый лид» → «Заявка» и т. п.: служебные этапы переиспользуем по типу.
                match = next((s for s in free if s.analytics_type == kind), None)
            if match:
                free.remove(match)
            chosen.append((name, kind, match))
            item["stages"].append({"name": name, "type": kind, "status": "new" if match is None else
                                   "renamed" if match.name != name else "same"})
        for stage in free:
            (item["kept"] if counts.get(stage.id) else item["archived"]).append(
                {"name": stage.name, "deals": counts.get(stage.id, 0)})
        if apply:
            position = 0
            for name, kind, match in chosen:
                if kind in {"WON", "LOST"}:
                    continue
                stage = match or CrmStage(pipeline_id=pipeline.id, name=name, analytics_type=kind, required_fields=[])
                stage.name, stage.analytics_type, stage.color, stage.position = name, kind, COLORS[kind], position
                if match is None:
                    db.add(stage)
                position += 1
            for stage in free:  # этапы со сделками остаются (перед «Продажей»), пустые — в архив
                if counts.get(stage.id):
                    stage.position = position; position += 1
                else:
                    stage.archived_at = datetime.now(timezone.utc)
            for name, kind, match in chosen:
                if kind not in {"WON", "LOST"}:
                    continue
                stage = match or CrmStage(pipeline_id=pipeline.id, name=name, analytics_type=kind, required_fields=[])
                stage.name, stage.analytics_type, stage.color, stage.position = name, kind, COLORS[kind], position
                if match is None:
                    db.add(stage)
                position += 1
            pipeline.name = direction["name"][:180]
            mapping[direction["id"]] = pipeline.id
            used.add(pipeline.id)
        elif pipeline is not None:
            used.add(pipeline.id)
        report.append(item)
    if apply:
        project.portal_state = {**state, STATE_KEY: mapping}
        await db.flush()
    return report
