# Радар: сканеры — контракт API для экрана

Сканер — главная сущность радара (раньше «фильтр»). Внутри — запросы, площадки и действия.
Плашки площадок (`/sources`, `/channels`) — как в `SOURCES.md`, общие на все сканеры.

## Кто что делает

| Часть | Файлы | Кто |
|---|---|---|
| Модели, перенос фильтров, раннер, действия, шаблоны, API, тесты | `backend/app/domains/jobradar/*` (кроме `sources/getmatch.py`, `flru.py`, `telegram_channels.py`) | Claude |
| Экран: список сканеров, страница сканера, мастер «из шаблона» | `frontend/app/admin/jobradar/*` | Codex |

Старые `/filters` пока работают: это тот же сканер, у которого поля первого запроса лежат прямо в нём. Экрану их больше не использовать.

Все пути ниже — `/api/admin/jobradar`.

## Шаблоны

```
GET /templates → {"items": [{"code", "name", "description", "icon", "kind": "jobs",
                               "preview": {"queries": int, "sources": [code], "actions": [code]}}]}
```
Коды шаблонов: `hiring_marketing`, `hiring_sales`, `jobs_for_me`, `freelance`, `blank`.

## Сканеры

```
GET /scanners → {"items": [ScannerShort]}

ScannerShort = {
  "id", "name", "enabled", "kind": "jobs", "template": code|null,
  "owner": null | {"project_id", "name"},                       // null — агентство
  "sources": [code], "actions": [code],                          // включённые действия: telegram | companies | client_crm
  "queries": int,                                                // сколько включённых запросов
  "last_run_at": iso|null,
  "last_result": {"found", "matched", "sent", "leads", "deals", "skipped", "ai_low", "failed",
                  "by_source": {code: {state, found, passed, dropped, reason, note|error}},
                  "by_query": {query_id: {"name", "found", "passed"}}, "errors": {code: text}},
  "stats": {"found_7d", "sent_7d", "leads_7d", "deals_7d", "rated", "liked_share": int|null}
}

POST /scanners {"name", "project_id"?: int, "template"?: code} → Scanner   // из шаблона: запросы, площадки, действия
GET  /scanners/{id} → Scanner
PATCH /scanners/{id} {"name"?, "project_id"?: int|null, "enabled"?, "profile"?, "ai_min_score"?,
                      "sources"?: [code], "source_settings"?: {code: {...}}, "actions"?: Actions} → Scanner
DELETE /scanners/{id} → 204

Scanner = ScannerShort + {
  "profile": str|null, "ai_min_score": 0..100,
  "source_settings": {"telegram": {"channels": ["name"]}, "flru": {"include_projects": bool, "budget_from": int}, ...},
  "actions": Actions,
  "query_items": [Query]
}

Actions = {
  "telegram":   {"enabled": bool, "chat_id": "-100…"|"@name"|null},
  "companies":  {"enabled": bool, "list_name": str, "signal": "hh_marketing" | "hiring"},   // только для агентства
  "client_crm": {"enabled": bool, "responsible_user_id": int|null}                         // только если owner задан
}
```
Лид в CRM клиента создаётся только с телефоном или почтой компании: из самой вакансии (Работа России, hh — если открыты)
или из базы компаний агентства по ИНН (только общие контакты, не личные). Нет контакта — находка остаётся `found` с причиной.
Проверки при включении сканера (422 с текстом по-русски):
- нет ни одного включённого запроса;
- не выбрана ни одна площадка;
- не включено ни одно действие;
- у действия telegram нет chat_id;
- `client_crm` включён у сканера без клиента;
- `companies` включён у клиентского сканера.

## Запросы

```
Query = {"id", "name", "enabled", "keywords": [str], "exclude": [str], "regions": [str], "remote_only": bool,
         "salary_from": int, "allow_no_salary": bool, "experience": [code], "position": int}

POST   /scanners/{id}/queries {Query без id} → Query
PATCH  /queries/{qid} {поля Query} → Query
DELETE /queries/{qid} → 204          // последний запрос удалить можно; включить сканер без запросов — нельзя
```
Опыт — коды hh: `noExperience`, `between1And3`, `between3And6`, `moreThan6`. Города — свободный текст, «Самара» совпадает и с «Самарской областью».

## Запуск, предпросмотр, лента

```
POST /scanners/{id}/run     → 202 {"queued": true}       // фоном, результат — в last_result через 1–2 мин
POST /scanners/{id}/test    → {"ok": true}               // тестовый пост в chat_id действия telegram
POST /scanners/{id}/preview {"days": 1..14, "queries"?: [Query без id]}
  → {"days", "sources": [{"code", "name", "found"|null, "skipped"|null, "error"|null}],
     "total", "matched", "companies",
     "by_query": [{"id"|null, "name", "found", "passed"}],
     "items": [{"title", "company", "city", "remote", "salary_from", "salary_to", "salary_known", "kind",
                "url", "employer_url", "sources": [name], "ok", "reason", "query": name|null}]}
     // queries не переданы — берутся включённые запросы сканера; переданы — черновики из формы (ничего не сохраняется)

GET /scanners/{id}/feed?status=sent|lead|deal|found|skipped|ai_low&query_id=&limit=&offset=
  → {"total", "items": [{"id", "status", "status_name", "reason", "score", "why", "sources": [name], "query": name|null,
                         "feedback", "company_id", "deal_id", "sent_at", "created_at",
                         "vacancy": {"title", "company", "city", "remote", "salary_from", "salary_to", "salary_known",
                                     "kind", "url", "published_at"}}]}
```
Статусы находки:
- `sent` — отправлено в Telegram;
- `lead` — компания в базе;
- `deal` — лид в CRM клиента;
- `found` — найдено, но действие не выполнено: нет Telegram-действия или у компании нет телефона и почты для CRM (причина в `reason`);
- `skipped` — отсеяно, причина в `reason`;
- `ai_low` — отсеял ИИ.

## Клиенты и команда (для «Для кого» и «Ответственный»)

```
GET /api/admin/leadgen/clients → {"items": [{"project_id", "project", "workspace_id", "name",
                                             "team": [{"id", "name", "role"}]}]}
```

## Каналы Telegram

Как в `SOURCES.md`, только вместо `filter_id` — `scanner_id` (старое имя тоже принимается):
- `POST /channels {"channel", "scanner_id"}`;
- `DELETE /channels/{name}?scanner_id=`;
- в `GET /channels` у канала поле `"scanners": [{"id", "name"}]`, вместо прежнего `filters`.
