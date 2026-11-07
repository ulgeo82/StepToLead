# Радар вакансий: площадки — контракт для параллельной работы

Каркас уже в коде: `sources/base.py` (Vacancy, Adapter, Context, ошибки, помощники) и `sources/__init__.py` (реестр).
Реестр сам подхватывает модули из `ORDER`; модуль, которого ещё нет, пропускается.

## Кто что делает

| Часть | Файлы | Кто |
|---|---|---|
| Каркас, модели, раннер, посты, лиды, API, сид | `sources/base.py`, `sources/__init__.py`, `sources/regions.py`, `models.py`, `db_upgrade.py`, `runner.py`, `posting.py`, `leads.py`, `routes.py`, `tests/test_jobradar.py` | Claude |
| Адаптеры trudvsem, habr, hh, superjob | `sources/trudvsem.py`, `sources/habr.py`, `sources/hh.py`, `sources/superjob.py` | Claude |
| Адаптеры getmatch, FL.ru, Telegram-каналы | `sources/getmatch.py`, `sources/flru.py`, `sources/telegram_channels.py`, `tests/test_jobradar_more_sources.py`, фикстуры `tests/fixtures/jobradar/getmatch_offers.json`, `flru.xml`, `tg_channel.html` | Codex |
| Экран | `frontend/app/admin/jobradar/*` | Codex |

Чужие файлы не правим. Нужна правка в чужом файле — пишем владельцу.

## Адаптер (sources/<code>.py)

```python
from app.domains.jobradar.sources.base import Adapter, Context, Vacancy, SourceSkipped, SourceFormatError

class FlruAdapter(Adapter):
    code = "flru"; title = "FL.ru"; icon = "🧩"; purpose = "Фриланс-проекты"
    settings_schema = []                       # глобальные настройки (плашка)
    filter_schema = [                          # настройки в фильтре (форма фильтра)
        {"key": "include_projects", "label": "Включать проекты", "type": "bool", "default": False},
        {"key": "budget_from", "label": "Бюджет от, ₽", "type": "number", "default": 0},
    ]
    async def fetch(self, f, since, settings, filter_settings, ctx) -> list[Vacancy]: ...
    async def healthcheck(self, settings, ctx) -> tuple[bool, str]: ...   # необязательно, есть по умолчанию

ADAPTER = FlruAdapter()
```

- HTTP только через `async with ctx.http() as http:` — там таймаут 20 с и User-Agent портала; в тестах `ctx.transport` подменяет сеть. Для t.me: `ctx.http(use_proxy=True)` (прокси из настроек площадки, если задан).
- Паузы — `await ctx.sleep(сек)`, не `asyncio.sleep` (в тестах без ожидания).
- `f` — условия фильтра: keywords, exclude, remote_only, regions, salary_from, allow_no_salary, experience, mode (`work` | `leads`), strict_city. Адаптер может отсеять заранее (ключевые слова/исключения), но окончательно фильтрует раннер (`core.matches`).
- Нет зарплаты на площадке в принципе → `salary_known=False`: фильтр зарплаты и опыта к вакансии не применяется, в посте «💰 з/п на сайте».
- Проект → `kind="project"`; бюджет/срок → `extra={"budget": int|None, "deadline": str|None}`. Бюджет-фильтр и флажок «Включать проекты» применяет сам адаптер FL.ru по `filter_settings`.
- getmatch: стек и формат → `extra={"stack": [..], "work_format": "remote|office|hybrid"}`; список за проход кэшировать в `ctx.cache["getmatch"]`.
- Структура не совпала → `raise SourceFormatError()`; нечего читать/нет ключа → `raise SourceSkipped("причина")`. Остальные исключения раннер превратит в короткую ошибку площадки, проход не падает.

### Telegram-каналы (telegram_channels.py) — дополнительно

- Код площадки: `code = "telegram"` (так же — ключ в `source_settings` и `Vacancy.source`). Глобальная настройка площадки:
  `settings_schema = [{"key": "proxy_id", "label": "Прокси для t.me", "type": "proxy", "default": None, "hint": "…"}]` —
  раннер сам превращает её в `ctx.proxy`.

- `filter_settings["channels"]`: список имён без @ (до 30). Читать `https://t.me/s/{канал}` через `ctx.http(use_proxy=True)`, пауза 2–3 с между каналами (`ctx.sleep`).
- Состояние: `ctx.channel_state[channel]` → last_post_id (None — первый раз: берём посты за `ctx.first_run_days`).
  Результат по каждому каналу — в `ctx.channel_result[channel] = {"status": "ok"|"unavailable"|"closed", "last_post_id": int|None, "last_post_at": iso|None, "found": int, "error": str|None}`. Раннер сохранит.
- Vacancy: `source="telegram"`, `ext_id=f"{channel}/{post_id}"`, `url=f"https://t.me/{channel}/{post_id}"`, `extra={"channel": channel}`, `salary_known` — True только если зарплату удалось вытащить.
- ИИ: если `ctx.ai_extract` не None — `await ctx.ai_extract(text)` → dict {title, company, salary_from, salary_to, remote, city} или None (обёртку в `<post>` и промт делает раннер). Без ИИ: title — первая строка, snippet — до 300 символов, удалёнка/город по словам (`looks_remote`, `has_word`).
- Для API нужны ещё две функции модуля:
  - `normalize_channel(text: str) -> tuple[str | None, str | None]` — «@name», «t.me/name», «https://t.me/s/name» → ("name", None); «t.me/+…», «joinchat» → (None, "закрытый канал, читать нельзя"); мусор → (None, "не похоже на канал").
  - `async check_channel(name: str, ctx: Context) -> dict` → `{"ok": bool, "status": "ok"|"unavailable"|"closed", "message": str, "title": str|None, "preview": list[Vacancy] (3 последних поста)}`.

## API для экрана (/api/admin/jobradar) — делает Claude

```
GET  /sources
→ {"items": [{
     "code", "title", "icon", "purpose", "needs_key",
     "enabled": bool,
     "state": "ok" | "needs_key" | "error" | "off" | "unknown",
     "message": str | null,          # «нужен ключ», «403: доступ запрещён» (до 80 симв., без URL)
     "last_ok_at": iso | null,
     "stats_24h": {"found": int, "passed": int, "sent": int},
     "settings": {key: value},        # секреты не отдаются: {"token": {"set": true}}
     "settings_schema": [...], "filter_schema": [...]
   }]}

PATCH /sources/{code}           {"enabled"?: bool, "settings"?: {key: value}}   # секрет: строка — задать, "" — убрать
POST  /sources/{code}/check     → {"ok", "message", "preview": [Preview×3]}     # healthcheck + 3 последние найденные

GET    /channels                → {"items": [{"channel", "status": "ok"|"unavailable"|"closed"|"unknown",
                                   "last_post_at", "passed_24h", "error", "filters": [{"id", "name"}]}]}
POST   /channels/check          {"channel": "@name"} → {"ok", "channel", "status", "message", "title", "preview": [Preview×3]}
                                # закрытый канал → 422 «закрытый канал, читать нельзя»
POST   /channels                {"channel": "@name", "filter_id": int}  → добавляет в source_settings.telegram.channels фильтра
DELETE /channels/{name}?filter_id=…                                       → убирает из фильтра

Preview = {"title", "company", "url", "city", "remote", "salary_from", "salary_to", "salary_known", "kind", "published_at"}
```

Фильтр (`GET /filters`, `POST/PATCH /filters/{id}`) — добавляется поле:
```
"source_settings": {"telegram": {"channels": ["name"]}, "flru": {"include_projects": false, "budget_from": 0}, "habr": {...}}
```
`sources: [code]` — как сейчас, это чипы-переключатели площадок в фильтре.

`last_result.by_source[code]` — как сейчас: `{"state": "ok"|"skipped"|"error", "found", "passed", "dropped", "reason", "note"|"error"}`.
Мини-разбивка в строке фильтра: `trudvsem 12→3 · habr 5→2 · tg 20→4` (found→passed), в title — reason.

`GET /overview` остаётся (бот, ИИ, интервал, известные чаты); источники в нём больше не нужны экрану — берите `/sources`.
