# Лидогенерация: источники компаний — контракт API для экрана

Источник — подключаемый модуль, который находит компании (`finder`) или дополняет уже найденные (`enricher`).
Экран рисуется только по данным сервера: схемы настроек и формы запуска приходят в ответе. Новый источник появляется
на экране без правок фронтенда.

Все пути ниже — `/api/admin/leadgen`. Доступ — только админ агентства. Ошибки — `{"detail": "текст по-русски"}`,
короткие, без адресов. Город и регион нигде не подставляются по умолчанию.

## Кто что делает

| Часть | Файлы | Кто |
|---|---|---|
| Контракт источников, раннер, настройки, источники, сигналы, веса, тесты | `backend/app/domains/leadgen/sources/*`, `core/scoring.py`, роуты | Claude |
| Экран «Источники компаний», запуск с оценкой и прогрессом, сигналы и тех-профиль в карточке, фильтры | `frontend/app/admin/leadgen/*` | Codex |

## Схема поля (settings_schema и query_schema)

```
Field = {"key": str, "label": str, "type": "text"|"number"|"bool"|"list"|"secret"|"select"|"region",
         "default"?: any, "hint"?: str, "options"?: [{"value", "label"}], "required"?: bool, "min"?: int, "max"?: int}
```
- `list` — многострочное поле, одно значение на строку.
- `secret` — ключ. В ответах сервера вместо значения приходит `{"set": true|false}`. Значение никогда не показывать.
  При сохранении: строка — новое значение, `""` — удалить ключ, поле не передано — не менять.
- `region` — город или регион свободным текстом. Сервер сам переводит его в код, пример ничего не подставляет.
- `select` — выбор из `options`.

## Источники

```
GET /sources → {"items": [Source]}

Source = {
  "code": str, "kind": "finder"|"enricher", "title": str, "icon": str, "purpose": str,
  "enabled": bool,
  "needs_key": bool, "ready": bool, "ready_reason": str|null,    // ready=false: почему не запустится (нет ключа и т.п.)
  "settings_schema": [Field], "settings": {key: value | {"set": bool}},
  "query_schema": [Field],                                        // finder: поля формы запуска; enricher: []
  "state": "ok"|"error"|"never"|"off", "last_ok_at": iso|null, "last_error": str|null,
  "signals": [{"kind": str, "label": str}]                        // какие сигналы ставит этот источник
}

PATCH /sources/{code} {"enabled"?: bool, "settings"?: {key: value}} → Source
POST  /sources/{code}/check → {"ok": bool, "message": str}          // до 60 с
```

Первый источник (шаг 1):
- `site_tech` — enricher «Тех-профиль сайта»;
- настройка — `pagespeed_key` (secret, необязательная: без неё скорость сайта не проверяется).

Дальше в этом порядке (коды фиксированы):
- `msp_registry` (finder, реестр МСП ФНС);
- `girbo` (enricher, выручка ГИР БО);
- `twogis` (finder);
- `vk` (finder);
- `reviews` (enricher).

## Оценка и запуск

```
POST /sources/{code}/estimate  {"query"?: {...}, "target"?: Target}
  → {"seconds": int, "units": int, "steps": [{"code", "name", "seconds"}], "companies"?: int}

POST /sources/{code}/run  {"query"?: {...}, "target"?: Target, "list_name"?: str}
  → 202 {"run_id": int}
  // 409 «Этот источник уже запущен», 422 — что не так в форме, 400 «Источник не настроен: …»

Target (только для enricher) = {"company_ids": [int]} | {"list_id": int} | {"segment_id": int}
  // не больше 500 компаний за запуск; enricher обрабатывает только компании с сайтом (остальные — skipped)
```
- finder: `query` — значения по `query_schema`. Каждый запуск finder создаёт свой список (как поиск по Директу).
- enricher: `target` — кого дополнить. Нового списка не создаёт.

## Прогресс и результат

```
GET /runs/{run_id}/progress → ProgressSnapshot | {"state": "idle"}
```
`ProgressSnapshot` — ровно тот же формат, что у радара (`backend/app/services/progress.py`). Экран использует
`TaskProgress` и `EstimateHint` из `frontend/components/task-progress.tsx`:
- `url` = `/admin/leadgen/runs/{id}/progress`;
- `since` = время нажатия.

```
GET /runs/{run_id} → прежний ответ + {
  "source": code,
  "list_id": int|null,                          // finder: список с найденными
  "result": {"found": int, "new": int, "updated": int, "skipped": int,
             "signals": {kind: int}, "errors": {code: str}}
}
GET /runs → прежний список, у каждого запуска + "source"
```
По окончании прогресса экран показывает итог (`message` снимка) и ссылку:
- для finder — на список `list_id`;
- для enricher — на базу с фильтром по этим компаниям.

## Сигналы

В карточке компании (`GET /companies/{id}` → `signals`) и в фильтре базы (`signal=…`) — как сейчас. Новые коды и подписи
(добавить в `signalLabels` в `frontend/app/admin/leadgen/shared.tsx`):

| kind | Подпись |
|---|---|
| `no_analytics` | Нет Метрики на сайте |
| `no_calltracking` | Нет коллтрекинга |
| `has_calltracking` | Есть коллтрекинг |
| `slow_site` | Медленный сайт на телефоне |
| `outdated_site` | Сайт давно не обновлялся |
| `new_company` | Новая компания (до 12 мес.) |
| `revenue_fit` | Выручка в целевом диапазоне |
| `lost_leads_reviews` | Отзывы: теряют заявки |
| `vk_active` | Активное сообщество ВК |
| `vk_abandoned` | Заброшенное сообщество ВК |
| `new_site` | Новый сайт |

Подпись приходит и с сервера (`Source.signals`). Если кода нет в `signalLabels`, экран показывает подпись с сервера,
а если нет и её — сам код.

## Тех-профиль сайта в карточке компании

```
GET /companies/{id} → прежний ответ + "tech": Tech|null

Tech = {
  "checked_at": iso,
  "platform": "tilda"|"wordpress"|"bitrix"|"other"|null,
  "analytics": ["yandex_metrika", "google_analytics"],        // пусто — счётчиков нет
  "calltracking": ["calltouch"|"comagic"|"roistat"|"mango"|"callibri"|...],
  "chat": ["jivo"|"bitrix24"|"talk-me"|...],
  "crm_forms": ["amocrm"|"bitrix24"|...],
  "quiz": bool, "privacy_policy": bool,
  "footer_year": int|null,
  "pagespeed_mobile": int|null,                                 // 0–100; null — не проверяли (нет ключа)
  "tag_manager": bool                                           // стоит Google Tag Manager: Метрика и коллтрекинг
                                                                // могут грузиться через него и не быть видны —
                                                                // тогда сигналы no_analytics / no_calltracking не ставятся
}
```
Подписи значений — на экране, неизвестный код показывать как есть.

## Переменные для писем (появятся позже, после шага 1)

`{{site_platform}}`, `{{has_metrika}}`, `{{has_calltracking}}` — подстановка в шаблонах цепочек. Экрану пока
ничего не делать.


---

# Шаг 2: ИИ-подбор запросов, «Поисковая выдача», «Нанимают в нише»

Всё ниже — дополнение к контракту выше. Прежние поля и ответы не меняются.

## Расширение схемы поля (для всех источников)

```
Field += {
  "ai"?: {"purpose": "direct"|"search"|"jobs"},   // у поля list: рядом кнопка «✨ Подобрать ИИ»
  "options"?: [...]                               // теперь и для типа multi
}
type += "multi"   // несколько значений из options (чекбоксы/чипы); значение — массив value
```
- Кнопка «✨ Подобрать ИИ» открывает окно, где вводится:
  - **ниша** — обязательно;
  - **бриф** — необязательно, текстом или PDF;
  - **город или регион** — необязательно.

  Затем окно вызывает `POST /ai/queries` (ниже). Найденные строки показываются списком с галочками, выбранные дописываются в поле, без дублей.
- Тот же ИИ-подбор с `purpose: "direct"` нужен в существующем экране поиска по Директу (`frontend/app/admin/leadgen/search/page.tsx`), рядом с полем ключей.

## ИИ-подбор запросов

```
POST /ai/queries  {"purpose": "direct"|"search"|"jobs", "niche": str, "brief"?: str, "region"?: str, "count"?: 10..60}
  → {"queries": [str], "note": str|null}
POST /ai/queries/pdf  multipart: file (PDF до 10 МБ) + те же поля формой
  → то же; текст брифа сервер достаёт из PDF сам

422 «Укажите нишу» / «Не удалось прочитать PDF»; 503 «ИИ не подключён в настройках портала»
```
- `direct` — ключевые фразы, по которым в этой нише покупают рекламу в Директе: коммерческие, с «заказать / цена / под ключ», без города, если регион задан отдельно.
- `search` — запросы для органической выдачи, по которым находятся сайты компаний ниши, а не агрегаторы и статьи.
- `jobs` — названия вакансий, которые нанимает компания этой ниши (например, «замерщик окон», «дизайнер кухонь», «администратор стоматологии»).
- Город никогда не подставляется сам: только если его передали в `region`.
- Расход ИИ — из резерва агентства (bucket `agency`). Модель на экране не называется.

## Оценка: цена запросов

```
POST /sources/{code}/estimate → прежний ответ + "cost"?: {"requests": int, "rub": number|null}
```
Экран показывает рядом с оценкой времени: «Запросов к поиску: 40 (≈ 1 ₽)». Если `rub: null`, показывается только число запросов.

## Источник `serp` — «Поисковая выдача» (finder)

Компании из органической выдачи Яндекса по запросам ниши. Это глубже, чем топ-10 в поиске по Директу.

```
query_schema = [
  {"key": "queries", "type": "list", "label": "Запросы (по одному на строку)", "required": true,
   "ai": {"purpose": "search"}},
  {"key": "region", "type": "region", "label": "Город или регион", "required": true},
  {"key": "depth", "type": "number", "label": "Страниц выдачи на запрос", "default": 2, "min": 1, "max": 5},
  {"key": "niche", "type": "text", "label": "Ниша (метка в базе)"}
]
settings_schema = [{"key": "price_per_1000", "type": "number", "label": "Цена 1000 запросов, ₽", "default": 25}]
```
- Агрегаторы, каталоги и доски объявлений отсекаются тем же списком доменов, что и в поиске по Директу (настройки лидогенерации).
- Один домен — одна компания.
- Сигналы: `organic_top` (уже есть) — в первой десятке хотя бы по одному запросу.

## Источник `hiring` — «Нанимают в нише» (finder)

Компании, у которых сейчас открыты вакансии ниши. Площадки — те же, что в радаре вакансий: ключи и токены берутся с плашек радара, второй раз их не вводят.

```
query_schema = [
  {"key": "titles", "type": "list", "label": "Кого ищут (названия вакансий)", "required": true,
   "ai": {"purpose": "jobs"}},
  {"key": "region", "type": "region", "label": "Город или регион", "required": true},
  {"key": "days", "type": "number", "label": "Вакансии за последние, дней", "default": 14, "min": 1, "max": 30},
  {"key": "platforms", "type": "multi", "label": "Площадки", "default": ["trudvsem", "hh"],
   "options": [{"value": "trudvsem", "label": "Работа России"}, {"value": "hh", "label": "hh.ru"}]},
  {"key": "hh_url", "type": "text", "label": "Или ссылка поиска hh.ru",
   "hint": "Скопируйте адрес страницы поиска на hh.ru — возьмём её условия вместо полей выше"}
]
```
- Площадка без токена или выключенная в радаре пропускается. Причина видна в шагах прогресса и в `result.errors`, например «hh.ru: нужен токен приложения (dev.hh.ru)».
- Одна компания — одна строка, даже если вакансий несколько. Из вакансии берутся ИНН и контакты работодателя.
- Крупные сети (список ИНН исключений с плашки «Работа России») и кадровые агентства не попадают.
- Сигнал: `hiring` (уже есть), `payload` = `{"title", "url", "platform"}` по первой вакансии.
