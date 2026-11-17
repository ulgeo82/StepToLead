# Этапы B–C: Telegram-аутрич в пространстве клиента и единые входящие аутрича (email + Telegram)

План — docs/PLAN.md, модель — docs/MODEL.md, инфраструктура Telegram — docs/TELEGRAM_INFRA.md.
Сервер — Claude, экраны — Codex. Пространство (клиент) — как в лидогене после этапа 1: заголовок `X-Workspace-Id`
или параметр `ws`, иначе агентство; переключатель — frontend/components/space-switcher.tsx.

## Этап B (сервер). Telegram-аутрич в пространстве клиента
Статус: сделан 17.11.2026 (Claude; тесты backend/tests/test_telegram_spaces.py; на прод не выкачен).
- `workspace_id` у telegram_accounts, proxies (пул «Аутрич»), campaigns. Остальное — через родителя: очередь,
  диалоги, сообщения, журнал — через кампанию; задачи сбора контактов — через аккаунт. При запуске
  db_upgrade.py ставит все старые записи агентству; записи с NULL читаются как агентские.
- Ручки /api/telegram-accounts, /api/proxies, /api/campaigns, /api/leads, /api/campaigns/{id}/automation…,
  /api/telegram-parser — в пространстве запроса (space.bind, backend/app/services/tg_space.py). Чужой id → 404.
- Пул CRM-профилей клиентов (pool=client) — инфраструктура агентства: виден и меняется только в пространстве
  агентства. Дубли прокси ищутся по всему порталу. Телефон аккаунта уникален на весь портал: «уже подключён у
  другого клиента».
- Аккаунт и прокси другого клиента нельзя поставить в кампанию/аккаунт; свободные прокси раздаются только свои.
- Вход по коду и старый импорт tdata теперь требуют прокси (без прокси — 422).
- Раннер кампаний пропускает отключённых клиентов; стоп-лист Telegram (telegram-blocklist) общий.
- Фронт: lib/space.ts — страницы /admin/campaigns, leads, accounts, parser, proxies и запросы /campaigns,
  /leads, /telegram-accounts, /telegram-parser, /proxies, /admin/outreach идут с выбранным клиентом.

## Этап C. Единые входящие аутрича

Статус сервера: сделан 17.11.2026 (Claude; app/services/outreach_inbox.py, app/api/routes/outreach_inbox.py,
модели app/models/outreach.py; тесты backend/tests/test_outreach_inbox.py; на прод не выкачен). Уточнения к API:
- `campaign` и `account` — id в своём канале (кампания лидогена / Telegram, ящик / аккаунт): задавать вместе с `channel`.
- Без выбранного клиента действия с одной перепиской (`/inbox/{key}…`) работают по всем активным клиентам — строки
  из «Все клиенты» открываются; с выбранным клиентом чужой ключ — 404.
- Назначение, lock и (для Telegram) метка/разобрано/горячий — таблица outreach_thread_states, по ветке: email — по
  компании (новый ответ лида не сбрасывает назначенного), Telegram — по диалогу. Новое входящее в Telegram снова
  делает переписку неразобранной.
- Ответ в Telegram: диалог переходит в состояние `manual` — раннер кампании читает входящие дальше (пока кампания
  запущена), но ИИ-автоответ и дожим для диалога выключены. Автор ответа — campaign_messages.author_id.
- `reply` подставляет переменные шаблона в text ({{company}}, {{sender_name}}, {{name}} — имя контакта, если известно);
  `template_id` — необязательная пометка. 409 — lock держит другой; 422 — reply_blocker; 502 — канал не принял.
- Метки в Telegram: interested/question — горячий; not_interested — диалог остановлен; unsubscribe — остановлен и в
  общий стоп-лист Telegram; любая метка — «разобрано». Статус лида кампании обновляется.
- `to-crm` для Telegram — сделка в воронке «Аутрич» пространства (контакт с @username, переписка — в комментарии);
  повторный вызов возвращает ту же сделку.
- `/inbox-counter` без выбранного клиента считает по всем активным клиентам (бейдж меню).
- `suggest` — ИИ из резерва агентства (bucket=agency), клиенту не списывается.

Операторы — сотрудники агентства (admin_users). Этап D добавляет клиентские ручки чтения поверх тех же входящих.

### API (/api/admin/outreach)
```
GET /inbox?channel=email|telegram&status=unhandled|mine|hot|all&label=&q=&campaign=&account=&page=&per_page=50
    // ws — пространство; без ws и с all_clients=1 — по всем клиентам (для «все входящие»)
  → {"total", "counts": {"unhandled", "mine", "hot"}, "items": [Conversation]}
Conversation = {
  "key": "email:<touch_id>" | "tg:<dialog_id>",
  "channel": "email" | "telegram",
  "client": {"id", "name"},
  "campaign": {"id", "name"} | null,
  "contact": {"name": str|null, "address": str (почта или @username), "company": str|null},
  "sender": {"kind": "mailbox"|"account", "id", "label"},          // с какого ящика/аккаунта шла переписка
  "last": {"text": str (до 300 симв.), "at": iso, "direction": "in"|"out"},
  "label": "interested"|"question"|"later"|"not_interested"|"unsubscribe"|"auto"|"other"|null,
  "summary": str|null, "interest": 0..100|null, "hot": bool,
  "assignee": {"id", "name"} | null,
  "locked_by": {"id", "name", "until": iso} | null,                // кто сейчас пишет ответ
  "unread": bool, "handled": bool
}

GET  /inbox/{key} → Conversation + {"messages": [Message], "crm": {"deal_id", "stage"}|null, "can_reply": bool,
                                    "reply_blocker": str|null}
Message = {"id", "direction": "in"|"out", "text", "at", "status": "sent"|"delivered"|"failed"|"received",
           "author": {"kind": "lead"|"agency"|"client"|"auto"|"system", "name": str|null}}
           // auto — шаг цепочки/автоответ; agency/client — кто из людей ответил

POST /inbox/{key}/claim                 → Conversation          // взять себе
POST /inbox/{key}/assign {user_id|null} → Conversation
POST /inbox/{key}/lock                  → {"until"}              // «пишу ответ»: держится 60 с, экран продлевает
                                                                 // каждые 20 с, пока открыт редактор; 409 — держит другой
DELETE /inbox/{key}/lock                → 204
POST /inbox/{key}/reply {"text", "template_id"?} → Message       // с того же ящика/аккаунта, в ту же ветку;
                                                                 // 409 — пишет другой; 422 — reply_blocker
PATCH /inbox/{key} {"label"?, "handled"?} → Conversation          // метки как в email-входящих сейчас; для Telegram
                                                                 // те же последствия (стоп цепочки, стоп-лист, горячий)
POST /inbox/{key}/suggest → {"text"}                              // ИИ-подсказка ответа (счёт агентства), 503 без ИИ
POST /inbox/{key}/to-crm  → {"deal_id"}                           // горячий — в CRM клиента (как email сейчас)
GET  /inbox-counter → {"unhandled", "hot", "mine"}                // для бейджа в меню, опрос раз в 30 с

GET/POST/PATCH/DELETE /reply-templates[/{id}]  {"title", "text", "channel": "any"|"email"|"telegram"}
     // шаблоны ответов — по пространству; переменные {{name}}, {{company}}, {{sender_name}}
GET /operators → [{"id", "name"}]                                 // кому можно назначить
```
- Старые /api/admin/leadgen/inbox* остаются, пока экран не перейдёт.
- Telegram: входящие сообщения уже сохраняются раннером (campaign_messages status=received); ответ оператора —
  campaign_messages kind=manual через тот же аккаунт и его прокси (без прокси — reply_blocker). Если диалог ведёт
  ИИ-автоответ — ответ человека его выключает для этого диалога.
- В истории каждого сообщения — кто отправил (оператор агентства или сотрудник клиента).

### Экран (Codex): «Аутрич → Входящие» (/admin/outreach/inbox)
- Один экран для email и Telegram; переключатель клиента + «Все клиенты».
- Слева — список: вкладки «Не разобрано / Мои / 🔥 Горячие / Все», фильтры канал (иконка ✉ / ✈), метка, кампания,
  аккаунт/ящик, поиск; у строки — канал, клиент, контакт, начало последнего сообщения, метка, «назначено: …»,
  «✍ пишет Анна» (locked_by), непрочитанное.
- Справа — переписка (сообщения с подписью автора), карточка контакта и компании, CRM-сделка, «с какого ящика /
  аккаунта». Внизу — ответ: шаблоны, «✨ Подсказать ответ», «Отправить» (⌘+Enter). Пока редактор открыт и в нём есть
  текст — lock продлевается; если держит другой — редактор только для чтения и плашка «Сейчас отвечает …».
- Кнопки «Взять себе», «Назначить…», метки, «Разобрано», «В CRM».
- Горячие клавиши как сейчас (1–4 метки, E — разобрано, R — ответить, J/K — следующий/предыдущий, ⌘+Enter).
- Опрос списка раз в 5 с, пока вкладка видна; бейдж «Входящие» в меню — /inbox-counter.
- Ширина 390 px: список и переписка — два экрана с кнопкой «← Назад».

## Этап D. Аутрич в клиентском портале

Агентство включает «Показывать аутрич клиенту» в карточке клиента
(`PATCH /api/marketing/workspaces/{workspace_id}/outreach`, тело `{"show_outreach": true}`).
Флаг `client_workspaces.show_outreach` изначально выключен. `GET /api/portal/auth/me` сообщает видимость раздела.
Сотрудники активного пространства открывают «Аутрич» в клиентском меню (`/portal/outreach`).

Все ручки ниже используют пространство авторизованного пользователя портала. Заголовки и параметры выбора
пространства на доступ не влияют. При выключенном разделе ответ 403, при запросе чужой переписки 404.

- `GET /api/portal/outreach/summary` — `{companies, people, sent, replied, hot}`. Компании считаются по
  известным записям базы. Контакты учитываются отдельно по каналам, Telegram — по ID собеседника.
- `GET /api/portal/outreach/contacted?channel=&q=&page=&per_page=` — `{total, items}`. Строка содержит
  `{company, contact, channel, campaign, first_contact_at, status}` со статусом
  `sent | replied | interested | refused`. Для строки требуется фактическая отправка.
- `GET /api/portal/outreach/inbox?channel=&q=&page=&per_page=` и `GET /api/portal/outreach/inbox/{key}` —
  список и переписка на основе единых входящих. Отправитель — адрес ящика своего пространства или
  Telegram-имя аккаунта, если оно задано. Автор каждого сообщения — «Лид» или «Агентство».
  Служебные события и метка `auto` исключены, настройки каналов и ИИ отсутствуют в ответах.

Право «может отвечать» зарезервировано в `portal_users.can_reply_outreach` с дефолтом `false`.
Интерфейс его не меняет. Клиентские ручки ответа, ИИ-подсказок, назначения, блокировок, меток и переноса
в CRM отсутствуют. Обе колонки добавляются через `db_upgrade.py`.
