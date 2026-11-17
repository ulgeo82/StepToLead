# Этап A: инфраструктура Telegram-аутрича — прокси, аккаунты, здоровье, аватарки

Статус: сервер сделан 17.11.2026 (Claude; тесты backend/tests/test_telegram_infra.py, на прод не выкачен). Экраны — Codex.

Контракт API для экранов (Codex) и сервера (Claude). Общий план — docs/PLAN.md, модель — docs/MODEL.md.
Все пути — /api (как сейчас: /api/proxies, /api/telegram-accounts), только админ агентства, ошибки —
{"detail": "текст по-русски"}. Прогресс долгих операций — формат services/progress.py, на экране TaskProgress
(frontend/components/task-progress.tsx) + общий индикатор задач (frontend/components/tasks.tsx, notifyTasks()).

## Правила
- Прокси и сессии — секреты: пароль прокси и сессия аккаунта никогда не уходят на экран и в логи.
- Аккаунт **никогда** не подключается к Telegram без прокси (с IP сервера). Нет прокси — статус «ждёт прокси».
- Для аутрича: 1 аккаунт = 1 прокси (max_accounts = 1 по умолчанию у пула «аутрич»). Пул CRM-профилей клиентов
  (pool="client") работает как раньше (до 3 профилей на прокси, services/client_proxies.py) — не ломать.
- Смена прокси у подключённого аккаунта — только с подтверждением (confirm_ip_change).

## 1. Прокси

Названия пулов на экране: pool="agency" → «Аутрич», pool="client" → «CRM-профили клиентов».
Статусы (одни на весь портал): untested — не проверен, working — работает, error — не работает.

```
GET /proxies?pool=agency|client → [Proxy]
Proxy = {
  id, scheme: "socks5"|"http"|"socks4", host, port, username, label, pool, max_accounts,
  dedicated_workspace_id, dedicated_workspace_name,
  status, last_error, last_checked_at, created_at,
  exit_ip: str|null,            // внешний IP, который видит Telegram
  latency_ms: int|null,
  rotating: bool,               // внешний IP сменился между проверками — предупреждение; такие не раздаются
                                // аккаунтам автоматически (руками можно — например, мобильные пулы)
  duplicate_of: int|null,       // другой прокси с тем же exit_ip
  health: {score: 0..100, level: "ok"|"warn"|"dead"|"unknown", uptime_24h: 0..100|null,
           fails_in_row: int, reasons: [str]},           // reasons — по-русски, коротко
  accounts: [{id, phone, name}],                         // аккаунты аутрича на этом прокси
  assigned_accounts: int, client_accounts: int           // как раньше
}

POST /proxies/bulk {entries: str, pool: "agency"|"client", scheme: "auto"|"socks5"|"http", max_accounts?: int}
  → 201 {imported, duplicates, errors: [str], batch_id: int|null}
  // строки сохраняются сразу (status untested), проверка идёт в фоне; batch_id — для прогресса.
  // Форматы строк: host:port, host:port:login:pass, login:pass@host:port, host:port@login:pass,
  //   scheme://login:pass@host:port; пустые строки и строки с # — пропуск.
  // scheme="auto": для строк без схемы пробуем SOCKS5, потом HTTP — сохраняется тот, что прошёл.
GET  /proxies/checks/{batch_id}/progress → ProgressSnapshot
POST /proxies/check {ids?: [int]}   → 202 {batch_id}       // без ids — все
POST /proxies/bulk-delete {ids: [int]} → {deleted, skipped: [{id, reason}]}   // занятые клиентскими профилями — skip
// Ответ POST /proxies/bulk для совместимости ещё содержит skipped = duplicates + ошибки.
PATCH /proxies/{id} {pool, max_accounts, dedicated_workspace_id, label?} → как раньше
DELETE /proxies/{id}                 → как раньше
```

## 2. Аккаунты

Статусы аккаунта (подписи на экране): online — в сети, disconnected — отключён, flood_wait — пауза Telegram,
error — ошибка, waiting_proxy — ждёт прокси, spamblock — спам-блок (пауза), code_sent / password_required — вход.

```
GET /telegram-accounts → [Account]   // как TelegramAccountRead + поля ниже
Account += {
  proxy: {id, host, port, scheme, status, health_level}|null,
  health: {score: 0..100, level: "ok"|"warn"|"dead"|"unknown", reasons: [str],
           spamblock: {state: "none"|"limited"|"unknown", until: iso|null, checked_at: iso|null},
           sent_today: int, sent_24h: int, replies_7d: int, errors_24h: int,
           age_days: int|null, profile: {photo: bool, username: bool, bio: bool}}
}
GET /telegram-accounts/health-summary
  → {accounts: {total, ok, warn, dead, waiting_proxy, spamblock}, proxies: {total, ok, warn, dead, free}}

POST /telegram-accounts/import-batch  multipart: files[] (ZIP с tdata, до 200, каждый ≤100 МБ),
     mode: "auto"|"order", proxy_ids?: "1,5,9" (для order — по порядку файлов), label?: str
  → 202 {batch_id, queued, rejected: [{filename, error}], without_proxy: int}
  // auto: каждому аккаунту — свободный рабочий прокси пула «Аутрич» (меньше всего занят, без смены IP,
  //   прокси-дубли по внешнему IP — в последнюю очередь); order: i-й файл — i-й прокси из proxy_ids
  //   (поровну, только пул «Аутрич», не мёртвые — иначе 422). Прокси назначается ДО первого входа.
  // auto: архив и доступный в tdata telegram_user_id проверяются локально до резервирования.
  //   Резерв места возвращается после повтора или ошибки. Ожидающие файлы дожидаются текущих конвертаций.
  // tdata нельзя превратить в аккаунт без входа в Telegram, поэтому файл, которому не хватило прокси, НЕ
  //   подключается: в итогах status "waiting_proxy" и подсказка «добавьте прокси и загрузите файл снова».
  //   without_proxy — предварительная оценка нехватки, повторы и ошибки могут освободить места.
GET /telegram-accounts/imports/{batch_id}/progress → ProgressSnapshot
GET /telegram-accounts/imports/{batch_id} → {state, items: [{filename, status: "imported"|"waiting_proxy"|
     "skipped"|"error", account_id, phone, proxy: "host:port"|null, error}], counts, free_proxies: int|null}
  // free_proxies — оставшиеся свободные места с учётом max_accounts; null, пока пачка выполняется.
POST /telegram-accounts/{id}/replace-proxy {proxy_id: int|null, confirm_ip_change: bool}
  → Account    // proxy_id null — портал сам берёт свободный; без confirm у подключённого — 422
POST /telegram-accounts/assign-proxies {ids?: [int]} → {assigned, still_waiting}   // раздать ждущим
POST /telegram-accounts/spamcheck {ids?: [int]} → 202 {batch_id}   // спросить @SpamBot (фоном)
GET  /telegram-accounts/spamcheck/{batch_id}/progress → ProgressSnapshot
```
Старые /import-tdata и /{id}/proxy остаются для совместимости (экран переходит на новые).

## 3. Аватарки
```
POST /telegram-accounts/avatars/preview  multipart: file, strength: "low"|"medium" → {variants: [data_url ×4]}
POST /telegram-accounts/avatars  multipart: files[] (1–20 фото JPG/PNG/WEBP ≤8 МБ), account_ids: "1,2,3",
     strength: "low"|"medium", pause_seconds?: int (по умолчанию 40)
  → 202 {batch_id}   // фото раздаются по кругу; каждому аккаунту — своя уникальная версия; паузы между аккаунтами
GET /telegram-accounts/avatars/{batch_id}/progress → ProgressSnapshot
```
Уникализация: случайная обрезка и масштаб (до 6–10 %), лёгкий поворот (≤2°), сдвиг яркости/контраста/насыщенности
(±5–8 %), зеркало с вероятностью 50 %, незаметный шум, пересжатие JPEG с разным качеством, удаление EXIF.

## 4. Здоровье
**Прокси** (проверка всех пулов каждые 5 минут, повтор при сбое): score = доступность за 24 ч (основа) − штрафы:
fails_in_row ≥ 3 → dead; latency > 2000 мс → warn; rotating → warn «IP меняется — не для аккаунтов»;
duplicate_of → warn «тот же IP, что у …». Прокси dead → его аккаунты аутрича получают паузу (не переподключаются
без прокси), в Telegram агентства — уведомление (как сейчас о сбоях), на экране — «Заменить».
**Аккаунт**: dead — сессия не авторизована / аккаунт удалён / прокси dead; warn — spamblock limited, flood_wait,
ошибок отправки за 24 ч ≥ 3, профиль без фото или username, возраст < 7 дней. score = 100 − штрафы.
Спам-блок: диалог с @SpamBot (/start), разбор ответа «no limits / ограничен до …»; авто раз в сутки для
аккаунтов в кампаниях и по кнопке. limited → статус spamblock, кампании его пропускают; снятие — автоматически.

## 5. Экраны (Codex)
- Навигация: «Аутрич → Telegram»: Кампании, Очередь, Аккаунты, **Прокси**, Сбор контактов. Пункт «Система →
  Прокси Telegram» остаётся (тот же экран, фильтр по пулу).
- **Прокси**: сводка (всего / работают / предупреждения / мертвы / свободны), фильтр по пулу, таблица: адрес, тип,
  внешний IP, задержка, здоровье (цвет + подсказка reasons), аккаунты, статус; отметка строк → «Проверить»,
  «Удалить»; «Заменить» у мёртвых. Массовое добавление: textarea + пул + тип (Авто/SOCKS5/HTTP) + подсказка
  форматов → после отправки TaskProgress проверки и итог (добавлено / дубли / ошибки по строкам).
- **Аккаунты**: сводка здоровья; карточки или таблица со здоровьем (цвет, балл, reasons), спам-блок, отправлено
  сегодня, прокси (здоровье прокси), фильтры (здоровье, статус, без прокси); массовые действия по отмеченным:
  «Проверить спам-блок», «Раздать прокси ждущим», «Поставить аватарки».
  Импорт: до 200 ZIP, режим «Автоматически раздать прокси» / «По порядку» (выбор прокси в нужном порядке,
  счётчик «файлов N — прокси M»), предупреждение если свободных прокси меньше, чем файлов; дальше TaskProgress и
  таблица итогов.
- **Аватарки**: загрузка 1–20 фото, сила «Слабо / Средне», превью 4 вариантов, выбор аккаунтов, пауза,
  запуск → TaskProgress. Подсказка: «Уникальная аватарка не защищает от бана сама — главное лимиты и тексты».
- Ширина 390 px: таблицы прокручиваются внутри блока. Пароли прокси нигде не показываются.
