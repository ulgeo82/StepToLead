# Единый сбор и обогащение баз — 10.11.2026

## Что изменено

1. `email_by_inn`: opt-in `skip_recent` проверяется до обращения к провайдерам; успешные и пустые
   проверки кешируются на 90 дней. Пропуск не обновляет дату проверки. `recheck` доступен у запуска
   источника и функций базы; галочка по умолчанию выключена. Конфликт домена/ключа больше не теряет
   общую почту и не отправляет компанию на ручную проверку. Свободный домен получает `site_search` и ключ.
2. Директ: `then`, канонический порядок, совместимость с `enrich`, сохранение выбора в недельных повторах.
   Список и результаты поиска сохраняются до обогащения. Только новые рекламодатели и новые компании
   органики проходят общий исполнитель функций базы. Поиск и обогащение сохраняют свои очереди.
   Общий компонент «После поиска», оценка с цепочкой, прогресс и итог по шагам.
3. «Собрать контакты»: прежний путь API, добавлен `run_id`; общий конвейер
   site_contacts → email_check → name_clean → dadata → fit_ai. Платного email_by_inn нет.
   Свежие успешные site_check заранее исключаются; ранее оценённые компании не переоцениваются.
   Таблица и карточка обновляются по завершении прогресса. Импорт также больше не вызывает старый сборщик.
   `_enrich`, `_enrich_now`, `_enrich_one`, старые константы/подмены транспорта удалены.
4. Сайт и импорт больше не создают именные email-контакты; сводка сайта не содержит их в `emails`.
   Импорт считает и показывает `personal_dropped`, в том числе для адресов без домена компании.
   Добавлен отдельный безопасный dry-run/--apply скрипт очистки. Изменений схемы не потребовалось.

## text_excerpt

Единственное чтение этого ключа в приложении — `fit_ai.build_prompt`:
`backend/app/domains/leadgen/fit_ai.py`, фрагмент до 2500 символов из результата `site_check`.
Он даёт ИИ сведения с сайта для оценки соответствия портрету клиента. Поэтому текст не удаляется:
ни новый, ни существующий. `personalize` использует структурированные факты, `ai_queries` — введённый бриф;
ключ text_excerpt они не читают. Убрано прежнее удаление этого ключа в site_contacts.

## Очистка существующих контактов

Скрипт не запускался на production. Реальные количества неизвестны: их нужно получить проверкой ниже.
На тестовой БД: 1 неиспользуемый личный контакт удаляется, 5 сохраняются из-за ссылок, чужой workspace
и общий адрес не затрагиваются; повторное применение удаляет 0.

Проверяются все фактические FK установленной БД на lg_contacts (сейчас lg_touches и lg_enrollments),
касания по адресу без contact_id, стоп-лист по контакту/компании/домену и CRM-связи компании.
CRM использует crm_contacts, а не lg_contacts: для безопасности сохраняются все личные контакты
компании с crm_contact_id или crm_deal_id, даже если конкретный адрес не переносился в CRM.
Разбивка references может пересекаться; retained — число уникальных сохранённых контактов.

После доставки файлов и пересборки backend, на сервере из /opt/steptolead:

```sh
docker compose --env-file .env.prod -f compose.prod.yml exec backend python -m scripts.cleanup_personal_contacts
docker compose --env-file .env.prod -f compose.prod.yml exec backend python -m scripts.cleanup_personal_contacts --apply
```

Вторая команда необратима. Сначала проверить dry-run. Удаление выполняется одной транзакцией;
на PostgreSQL таблицы контактов и ссылок блокируются от конкурирующих записей на время применения.
Скрипт не запускается при старте. SQL-проверка без изменений данных:
`backend/scripts/cleanup_personal_contacts_preview.sql` (параметр agency_workspace_id).
При дополнительных FK в production точный отчёт даёт скрипт, автоматически обнаруживающий их.

## Проверить руками

- Директ: выбрать «После поиска», проверить оценку и итог цепочки; повтор не должен обогащать прежние компании.
- База: запустить «Почта по ИНН» дважды; во второй раз — «проверяли недавно». С галочкой — повторная проверка.
- Сбор контактов из общей базы и карточки: прогресс, затем обновлённые данные без ручного обновления страницы.
- Импорт общего и именного адреса: личный отсутствует в контактах, показан счётчик пропусков.
- Проверить dry-run очистки на сервере перед любым --apply.

## Проверки

Результат: `test_leadgen*` — **250 OK**, `test_jobradar*` — **63 OK**;
TypeScript — без ошибок; frontend/tests — **42 OK**; production-сборка — успешна.
`git diff --check` — без замечаний. Предупреждения тестовых битых PDF и существующее
DeprecationWarning в sequences.py не являются падениями; логику рассылок не меняли.

Команды:

```sh
cd backend/tests
PYTHONPATH=..:. python -m unittest discover -p 'test_leadgen*.py'
PYTHONPATH=..:. python -m unittest discover -p 'test_jobradar*.py'
cd ../../frontend
node_modules/.bin/tsc --noEmit -p .
node --test tests/*
node_modules/.bin/next build
```

## Файлы

Корень: `/Users/egordementev/Documents/Codex/2026-08-31/referenced-chatgpt-conversation-this-is-an`.

Backend, приложение и контракты:

```text
backend/app/domains/leadgen/SOURCES.md
backend/app/domains/leadgen/WORKSPACE.md
backend/app/domains/leadgen/importer.py
backend/app/domains/leadgen/routes.py
backend/app/domains/leadgen/scheduler.py
backend/app/domains/leadgen/site_enrich.py
backend/app/domains/leadgen/source_routes.py
backend/app/domains/leadgen/source_runner.py
backend/app/domains/leadgen/sources/base.py
backend/app/domains/leadgen/sources/email_by_inn.py
backend/app/domains/leadgen/sources/fit_ai.py
backend/app/domains/leadgen/sources/site_contacts.py
backend/app/domains/leadgen/workspace_routes.py
backend/scripts/cleanup_personal_contacts.py (новый)
backend/scripts/cleanup_personal_contacts_preview.sql (новый)
```

Backend, тесты:

```text
backend/tests/test_leadgen_api.py
backend/tests/test_leadgen_dadata.py
backend/tests/test_leadgen_email_by_inn.py
backend/tests/test_leadgen_site.py
backend/tests/test_leadgen_sources_step3.py
backend/tests/test_leadgen_unified_enrichment.py (новый)
```

Frontend:

```text
frontend/app/admin/leadgen/ImportDialog.tsx
frontend/app/admin/leadgen/companies/page.tsx
frontend/app/admin/leadgen/contact-progress.tsx (новый)
frontend/app/admin/leadgen/lists/[id]/function-panel.tsx
frontend/app/admin/leadgen/lists/[id]/page.tsx
frontend/app/admin/leadgen/search/page.tsx
frontend/app/admin/leadgen/sources/after-search.tsx (новый)
frontend/app/admin/leadgen/sources/source-run.tsx
frontend/app/admin/leadgen/sources/source-types.ts
frontend/tests/company-sources.test.mjs
```

Этот отчёт: docs/codex/2026-11-10-unified-enrichment-report.md.

## Особенности и границы

- До поиска количество компаний неизвестно: оценка цепочки — до 500 компаний, по 10 на запрос,
  как оценочный порядок величины, не фактическое обещание расходов/результата.
- Очистка CRM-связанных компаний намеренно консервативная (описано выше).
- Радар, direct_search.py, рассылки, CRM, входящий поток и .env/.env.prod не изменялись.
- Изменения не закоммичены, не отправлены и не залиты. Production-очистка не выполнялась.

## Уточнения после проверки перед заливкой — 10.11.2026

1. **Кеш «Почты по ИНН».** 90-дневный пропуск требует не только свежего `done`/`skipped`, но и
   непустого `previous.result.tried`: реестры действительно должны были запрашиваться. Отсутствие ИНН,
   подходящая существующая почта и отсутствие ключа не запускают этот период ожидания.
   `CompanyView.contacts` дополнен полями `verify_status` и `bounced`; `only_missing` учитывает только
   общие адреса без `invalid` и без признака возврата. Добавлены тесты появления ИНН после пропуска,
   invalid/bounced-адресов, корректного общего адреса и настоящих недавних проверок реестра.
2. **Оценка ЦА.** Общий адаптер fit_ai снова разрешает переоценку. Пропуск «компания уже оценена»
   перенесён в раннер и действует только при `params.kind == "enrichment"` («Собрать контакты»).
   Интеграционный тест проверяет отсутствие вызова ИИ при сборе контактов и повторный вызов при запуске
   `fit_ai` из рабочего места базы для той же ранее оценённой компании.
3. **Сбой цепочки Директа.** При исключении из `_execute_list` запуск `yandex_direct` завершается
   со статусом `done`; короткая ошибка сохраняется в `run.error` и `stats.result.errors.chain`.
   Прогресс цепочки показывает ошибку. Остальные виды запусков при таком исключении по-прежнему получают
   `failed`. Тест проверяет сохранённую компанию и участие завершённого поиска в `_previous_domains`.

В этом уточнении изменены только:

```text
backend/app/domains/leadgen/source_runner.py
backend/app/domains/leadgen/sources/email_by_inn.py
backend/app/domains/leadgen/sources/fit_ai.py
backend/tests/test_leadgen_email_by_inn.py
backend/tests/test_leadgen_unified_enrichment.py
docs/codex/2026-11-10-unified-enrichment-report.md
```

Параллельные правки Claude, включая общий индикатор задач, не изменялись.
Коммит, заливка и очистка production не выполнялись.

Повторные проверки после этих трёх исправлений:

- `test_leadgen*`: **255 тестов, OK**;
- `test_jobradar*`: **63 теста, OK**;
- `tsc --noEmit -p .`: без ошибок;
- `node --test tests/*`: **42 теста, OK**;
- `git diff --check`: без замечаний.

Тесты с искусственным исключением цепочки ожидаемо пишут traceback в серверный лог;
это часть проверяемого сценария, не падение тестов. Production-сборка в этом уточнении
не перезапускалась: фронтенд не менялся, выполнены запрошенные TypeScript и frontend/tests.
