"""Сканеры: запросы, шаблоны, действия, перенос старых фильтров. Без сети — только база и чистые функции."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.jobradar.models import JrFilter, JrQuery, JrTemplate

ACTIONS = ("telegram", "companies", "client_crm")
QUERY_FIELDS = ("keywords", "exclude", "regions", "remote_only", "salary_from", "allow_no_salary", "experience")
SIGNALS = {"hh_marketing": "Ищут маркетолога", "hiring": "Нанимает"}
DEFAULT_ACTIONS = {"telegram": {"enabled": False, "chat_id": None},
                   "companies": {"enabled": False, "list_name": "Нанимает маркетолога", "signal": "hh_marketing"},
                   "client_crm": {"enabled": False, "responsible_user_id": None}}

MARKETING = ["маркетолог", "интернет-маркетолог", "специалист по маркетингу", "менеджер по маркетингу", "директолог",
             "таргетолог", "специалист по контекстной рекламе", "smm"]
NOT_CLIENT = ["стажер", "помощник", "преподаватель", "наставник", "куратор", "торговый представитель"]

TEMPLATES = [
    {"code": "hiring_marketing", "name": "Компании нанимают маркетолога", "icon": "📇", "position": 1,
     "description": "Лиды для агентства: компании, которые ищут маркетолога, директолога или SMM, — в базу компаний.",
     "payload": {"profile": "Компании малого и среднего бизнеса, которые ищут маркетолога в штат — им можно предложить "
                            "маркетинг на аутсорсе.",
                 "sources": ["trudvsem", "hh"], "source_settings": {},
                 "actions": {"companies": {"enabled": True, "list_name": "Нанимает маркетолога", "signal": "hh_marketing"}},
                 "queries": [{"name": "Маркетолог", "keywords": MARKETING, "exclude": NOT_CLIENT, "regions": ["Самара", "Тольятти"]}]}},
    {"code": "hiring_sales", "name": "Компании ищут РОПа", "icon": "📈", "position": 2,
     "description": "Компании, которые строят отдел продаж: ищут РОПа или коммерческого директора.",
     "payload": {"profile": "Компании, которые строят или перестраивают отдел продаж.",
                 "sources": ["trudvsem", "hh"], "source_settings": {},
                 "actions": {"companies": {"enabled": True, "list_name": "Ищут РОПа", "signal": "hiring"}},
                 "queries": [{"name": "РОП", "keywords": ["руководитель отдела продаж", "коммерческий директор",
                                                          "директор по продажам", "head of sales"],
                              "exclude": ["стажер", "помощник", "ассистент"], "regions": ["Самара", "Тольятти"]}]}},
    {"code": "jobs_for_me", "name": "Вакансии для себя", "icon": "🎯", "position": 3,
     "description": "Удалённые вакансии по вашей профессии — постами в Telegram с кнопками 👍/👎.",
     "payload": {"profile": "", "sources": ["trudvsem", "habr", "getmatch", "hh"], "source_settings": {},
                 "actions": {"telegram": {"enabled": True, "chat_id": None}},
                 "queries": [{"name": "Основной", "keywords": ["маркетолог", "интернет-маркетолог", "директолог",
                                                               "performance-маркетолог", "performance marketing"],
                              "exclude": ["senior", "head of", "руководитель отдела"], "remote_only": True,
                              "salary_from": 40000, "experience": ["noExperience", "between1And3"]}]}},
    {"code": "freelance", "name": "Фриланс-проекты", "icon": "🧩", "position": 4,
     "description": "Проекты с FL.ru и вакансии на проект — постами в Telegram.",
     "payload": {"profile": "", "sources": ["flru", "habr"],
                 "source_settings": {"flru": {"include_projects": True, "budget_from": 0}},
                 "actions": {"telegram": {"enabled": True, "chat_id": None}},
                 "queries": [{"name": "Проекты", "keywords": ["таргет", "директ", "контекстн", "smm", "маркетинг"],
                              "remote_only": True}]}},
    {"code": "blank", "name": "Пустой", "icon": "➕", "position": 9,
     "description": "Всё настраиваете сами: запросы, площадки и действия.",
     "payload": {"profile": "", "sources": ["trudvsem"], "source_settings": {}, "actions": {},
                 "queries": [{"name": "Запрос 1", "keywords": []}]}},
]


async def ensure_templates(db: AsyncSession) -> None:
    """Сид шаблонов: добавляет недостающие, свои правки в базе не трогает.
    Безопасен при гонке (два запроса экрана или воркер одновременно): каждая вставка в своей точке сохранения."""
    known = set((await db.scalars(select(JrTemplate.code))).all())
    for t in TEMPLATES:
        if t["code"] in known:
            continue
        try:
            async with db.begin_nested():
                db.add(JrTemplate(kind="jobs", **t))
        except IntegrityError:   # уже добавил параллельный запрос
            pass


def actions_of(f: JrFilter) -> dict:
    """Действия сканера с умолчаниями. Старый фильтр без actions: «лиды» = база компаний, чат = пост в Telegram."""
    stored = dict(f.actions or {})
    if not stored:
        stored = {"telegram": {"enabled": bool(f.chat_id), "chat_id": f.chat_id},
                  "companies": {"enabled": f.mode == "leads"}}
    out = {}
    for code, default in DEFAULT_ACTIONS.items():
        out[code] = {**default, **(stored.get(code) or {})}
    if out["companies"].get("signal") not in SIGNALS:
        out["companies"]["signal"] = "hh_marketing"
    return out


def enabled_actions(f: JrFilter) -> list[str]:
    return [code for code, a in actions_of(f).items() if a.get("enabled")]


def query_dict(q: JrQuery | dict, *, strict_city: bool) -> dict:
    get = (lambda k, d=None: q.get(k, d)) if isinstance(q, dict) else (lambda k, d=None: getattr(q, k, d))
    return {"keywords": list(get("keywords") or []), "exclude": list(get("exclude") or []),
            "regions": list(get("regions") or []), "remote_only": bool(get("remote_only", False)),
            "salary_from": int(get("salary_from") or 0), "allow_no_salary": bool(get("allow_no_salary", True)),
            "experience": list(get("experience") or []), "strict_city": strict_city}


def strict_city(f: JrFilter) -> bool:
    """Для лидов важен город самой компании, а не «удалёнка из любого города»."""
    acts = actions_of(f)
    return bool(acts["companies"]["enabled"] or acts["client_crm"]["enabled"])


def merged(queries: list[dict]) -> dict:
    """Один запрос к площадке на весь сканер: объединение слов и городов, самые мягкие условия.
    Точный отбор по каждому запросу — потом, в core.matches. Так площадка опрашивается один раз за проход."""
    if not queries:
        return {"keywords": [], "exclude": [], "regions": [], "remote_only": False, "salary_from": 0,
                "allow_no_salary": True, "experience": [], "strict_city": False}
    words = list(dict.fromkeys(w for q in queries for w in q["keywords"]))
    all_russia = any(not q["regions"] for q in queries)
    regions = [] if all_russia else list(dict.fromkeys(r for q in queries for r in q["regions"]))
    any_exp = any(not q["experience"] for q in queries)
    return {"keywords": words, "exclude": [], "regions": regions,
            "remote_only": all(q["remote_only"] for q in queries),
            "salary_from": min(q["salary_from"] for q in queries),
            "allow_no_salary": any(q["allow_no_salary"] for q in queries),
            "experience": [] if any_exp else list(dict.fromkeys(e for q in queries for e in q["experience"])),
            "strict_city": False}


async def queries_of(db: AsyncSession, f: JrFilter, *, only_enabled: bool = True) -> list[JrQuery]:
    rows = (await db.scalars(select(JrQuery).where(JrQuery.filter_id == f.id)
                             .order_by(JrQuery.position, JrQuery.id))).all()
    return [q for q in rows if q.enabled] if only_enabled else list(rows)


async def ensure_queries(db: AsyncSession, f: JrFilter) -> None:
    """Перенос старого фильтра: его условия становятся первым запросом сканера, режим — действиями."""
    if f.id is None:
        return
    exists = await db.scalar(select(JrQuery.id).where(JrQuery.filter_id == f.id).limit(1))
    if exists is None:
        db.add(JrQuery(filter_id=f.id, name="Основной", enabled=True, position=0,
                       keywords=list(f.keywords or []), exclude=list(f.exclude or []), regions=list(f.regions or []),
                       remote_only=bool(f.remote_only), salary_from=int(f.salary_from or 0),
                       allow_no_salary=True if f.allow_no_salary is None else bool(f.allow_no_salary),
                       experience=list(f.experience or [])))
    if not f.actions:
        f.actions = actions_of(f)
    await db.flush()


async def sync_legacy(db: AsyncSession, f: JrFilter) -> None:
    """Поля первого запроса — копией в сам сканер (старый API /filters и старые экраны видят условия)."""
    first = (await queries_of(db, f, only_enabled=False))[:1]
    if first:
        for k in QUERY_FIELDS:
            setattr(f, k, getattr(first[0], k))
    acts = actions_of(f)
    f.chat_id = acts["telegram"]["chat_id"] if acts["telegram"]["enabled"] else f.chat_id
    f.mode = "leads" if acts["companies"]["enabled"] or acts["client_crm"]["enabled"] else "work"


async def create_from_template(db: AsyncSession, workspace_id: int, *, name: str, template: JrTemplate | None,
                               project_id: int | None) -> JrFilter:
    payload = dict(template.payload or {}) if template else {}
    actions = {code: {**DEFAULT_ACTIONS[code], **((payload.get("actions") or {}).get(code) or {})} for code in ACTIONS}
    if project_id:   # сканер клиента: база компаний — агентская, вместо неё лид в CRM клиента
        if actions["companies"]["enabled"]:
            actions["companies"]["enabled"] = False
            actions["client_crm"]["enabled"] = True
    f = JrFilter(workspace_id=workspace_id, name=name, project_id=project_id, kind=template.kind if template else "jobs",
                 template=template.code if template else None, mode="work", keywords=[], exclude=[], regions=[],
                 experience=[], remote_only=False, salary_from=0, allow_no_salary=True,
                 sources=list(payload.get("sources") or ["trudvsem"]),
                 source_settings=dict(payload.get("source_settings") or {}), actions=actions,
                 ai_min_score=int(payload.get("ai_min_score") or 0), profile=payload.get("profile") or None,
                 enabled=False, last_result={})
    db.add(f)
    await db.flush()
    for i, q in enumerate(payload.get("queries") or [{"name": "Запрос 1", "keywords": []}]):
        db.add(JrQuery(filter_id=f.id, position=i, name=q.get("name") or f"Запрос {i + 1}", enabled=True,
                       keywords=list(q.get("keywords") or []), exclude=list(q.get("exclude") or []),
                       regions=list(q.get("regions") or []), remote_only=bool(q.get("remote_only", False)),
                       salary_from=int(q.get("salary_from") or 0), allow_no_salary=bool(q.get("allow_no_salary", True)),
                       experience=list(q.get("experience") or [])))
    await db.flush()
    await sync_legacy(db, f)
    return f
