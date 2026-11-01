# Multi-user Acceptance — ComfyUI Studio

Приёмка проведена на HEAD `8834029`; production активирован 2026-11-01 в 17:12:48 MSK и с тех пор
работает на этом коде. Тесты: 709 passed, coverage 78.06 % (4827/6184). Статусы:
**PASS** — проверено и работает, **PARTIAL** — реализовано, проверено не полностью,
**FAIL** — обнаружена проблема, **NOT TESTED** — не проверялось на живых данных.

Границы проверялись двумя способами: на production — только анонимные запросы (реальный вход и
подделка cookie запрещены условиями), на тестах — реальные cookie/CSRF в временных БД.
Там, где нужен вход, статус `PARTIAL`, а не `PASS`.

---

## 1. Архитектура текущей реализации

```
studio/
  backend/app/
    main.py      сборка приложения: Layout -> Settings -> create_app
    api.py       46 маршрутов; здесь же границы (_workflow_for, _media_allowed,
                 _check_input_ownership, _check_submission_budget)
    auth.py      Principal; cookie-сессии (studio_session HttpOnly + studio_csrf),
                 CSRF для небезопасных методов; token_id = sha256(cookie) = sessions.id
    rbac.py      семантика ролей: RolePolicy, may_run, visible_workflow_ids,
                 max_active_jobs, max_jobs_per_day, max_output_bytes, capabilities
    db.py        SQLite (WAL, schema v3): jobs, outputs, users, sessions, events,
                 job_telemetry + read-only агрегаты usage_for(), jobs_today()
    jobs.py      submit/rerun, очередь, воркеры
    registry.py  загрузка 127 workflow, валидация графов, resolved_options
  registry/
    workflows/*.json   127 определений (13 категорий)
    rbac.json          матрица ролей (данные, не код)
  frontend/dist/assets/  SPA: app.js, api.js, app.css (сборки из src нет — правится dist)
  data/studio.db         production SQLite
```

Три принципа, на которых всё держится:

1. **Политика — данные.** `app/rbac.py` — семантика, `studio/registry/rbac.json` — матрица.
   Перечитывается при старте и по `POST /api/engine/refresh`. Deny-by-default: роль, которой нет
   в файле, не получает ничего; admin нельзя ограничить файлом.
2. **Границы — на сервере.** Фронтенд только рисует то, что разрешил сервер. Каждый отказ
   проверяется тестом на HTTP-ответ (401/403/404/429), а не на состояние UI.
3. **Один владелец правила.** Список изображений в picker'е и проверка при submit используют
   одну и ту же `_media_allowed`; лимиты считают одни и те же `db.usage_for()` / `db.jobs_today()`.

## 2. Роли

| | admin | user | child |
|---|---|---|---|
| workflow | все 127 | все 127 | 55 из 127 |
| модели | все папки | все папки | 5 папок |
| загрузка файлов | да | да | да |
| одновременных задач | без лимита роли (действует общий потолок сервера) | 3 | 1 |
| запусков в сутки (UTC) | нет | нет | 40 |
| места под outputs | нет | нет | 10 ГБ |
| чужие файлы (upload/outputs) | доступны | нет | нет |
| сессии, usage, настройки | свои | свои | свои |
| admin-only: `/api/users*`, `/api/settings`, `/api/events`, `/api/telemetry/*`, `/api/engine/refresh`, `/api/models/downloads*`, `/api/models/plan` | да | 403 | 403 |

Список workflow ребёнка: категории `image-editing` (33), `enhancement` (9), `vision-language` (9)
плюс 4 именованных (`format-square`, `format-portrait`, `format-landscape`, `style-anime`).
Папки моделей: `checkpoints`, `upscale_models`, `vae`, `vision-models`, `vlm-models` —
выведены из этих 55 workflow, а не назначены на глаз.

## 3. Действующие ограничения и квоты

| Ограничение | Значение | Где проверяется | Ответ |
|---|---|---|---|
| Запрещённый workflow | по политике | `_workflow_for()` до создания job | 403 `workflow '…' is not available for your account` |
| Неизвестный workflow | — | там же | 404 |
| Активные задачи | child 1 / user 3 | `_active_job_limit()` | 429 с числами |
| Запуски за сутки (UTC) | child 40 | `_check_submission_budget()` | 429 + warn-событие |
| Место под outputs | child 10 ГБ (десятичные) | `_check_submission_budget()` (первый тормоз) | 429 `storage limit reached: …` |
| Чужой upload в image-параметре | — | `_check_input_ownership()` до записи job | 403 + warn-событие |
| Чужой output | — | `_media_allowed("output", …)` | 403 |
| Загрузка файлов | allow_upload по роли | `may_upload()` | 403 |
| CSRF | все методы кроме GET/HEAD/OPTIONS/TRACE | `auth.py` | 403 |

Квота **проверяется при отправке, но не резервируется**: две задачи, поданные одновременно чуть
ниже лимита, могут вместе его превысить. Файлы вне таблицы `outputs` в расчёт не входят.
Это осознанный компромисс, а не недосмотр.

## 4. Реализованные API

Всего 46 маршрутов. Добавлено в Multi-user:

| Метод | Путь | Назначение | Ответ без сессии |
|---|---|---|---|
| GET | `/api/auth/sessions` | где я залогинен: created_at, last_seen, expires_at, client_ip, user_agent, флаг current (ни id, ни токена) | 401 |
| POST | `/api/auth/sessions/revoke-others` | выйти на всех устройствах кроме текущего | 401 (с сессией без CSRF — 403) |
| GET | `/api/me/usage` | свои totals + действующие лимиты | 401 |
| GET | `/api/users/usage` | то же по всем аккаунтам, только admin | 401 / 403 |

`revoke-others` удаляет строки с `EXISTS`-гардом: при одновременном нажатии с двух устройств
побеждает один запрос, проигравший получает `401 this session was signed out by another device`,
а не ложный `200`.

## 5. Проверки безопасности

| Граница | Чем доказано | Статус |
|---|---|---|
| Child не запускает запрещённый workflow | `test_child_submission_of_a_denied_workflow_writes_no_job`, `test_child_workflow_detail_and_checks_are_gated`, `test_child_cannot_rerun_a_denied_workflow`, `test_child_sees_only_allowed_workflows` | PASS |
| Child не использует чужие uploads | `test_child_cannot_run_a_job_on_another_accounts_upload`, `test_rerun_cannot_replay_another_accounts_upload` | PASS |
| Child не читает чужие outputs | `test_child_cannot_read_another_accounts_output`, `test_output_subfolder_cannot_be_walked_aside` (сырой и percent-encoded `..`), `test_child_cannot_see_or_rerun_an_admin_job` | PASS |
| Дневной лимит | `test_child_daily_allowance_blocks_the_next_run`, `test_rerun_counts_against_the_daily_allowance`, `test_allowance_is_per_account_not_per_day_total` | PASS |
| Дисковая квота | `test_child_storage_allowance_blocks_new_runs`, `test_rerun_cannot_bypass_the_storage_allowance`, `test_storage_allowance_does_not_apply_to_adults` | PASS |
| User не читает admin-usage | `test_household_usage_is_admin_only` | PASS |
| Нельзя отозвать чужие сессии | `test_revoke_others_does_not_touch_other_accounts`, `test_sessions_require_a_session` | PASS |
| revoke-others сохраняет текущую | `test_revoke_others_keeps_the_caller_signed_in`, `test_current_session_is_flagged`, `test_concurrent_revoke_others_leaves_exactly_one_session` | PASS |
| Admin не ограничен | `test_admin_is_unaffected_by_the_child_policy`, `test_admin_is_not_limited_to_its_own_uploads`, `test_admin_cannot_be_restricted_by_the_file` | PASS |
| Каждый маршрут отказывает постороннему | `test_every_route_refuses_an_outsider` (43 маршрута × 401), `test_a_refusal_never_leaks_the_inside` | PASS |

Все проверки — на сервере. Запуск: `pytest tests/test_rbac.py tests/test_sessions.py
tests/test_usage.py tests/test_isolation.py tests/test_routes_smoke.py` → 171 тест, все зелёные.

## 6. Результаты тестирования

| | Было (база) | Стало |
|---|---|---|
| Тестов | 596 / 77.17 % | **709 passed**, 0 failed |
| Coverage | 77.17 % | **78.06 %** (4827 / 6184) |
| Файлов тестов | — | + `test_sessions.py` (11), + `test_routes_smoke.py` (89), + секции в `test_rbac.py` |

Порог coverage не снижался, тесты не ослаблялись. Production DB за прогоны не изменилась:
jobs 140, outputs 156, users 3, sessions 3, events 391, job_telemetry 10 — те же значения до и после.

## 7. Production activation status

**АКТИВИРОВАНО 2026-11-01 17:12:48 MSK.** Владелец выполнил перезапуск:

| Факт | Значение |
|---|---|
| Состояние | `active (running)` с 17:12:48 MSK, `NRestarts` 0 |
| Main PID | 1813280 (прежний процесс был 963616) |
| Командная строка | `.venv/bin/python -m app.main --host 192.168.31.60 --port 8000` |
| Рабочий каталог | `studio/backend`, интерпретатор `/usr/bin/python3.14` + venv `studio/.venv` |
| Код на диске = код HEAD | `app/api.py` md5 `02dcb142…`, `app/rbac.py` md5 `3011ed0d…` — совпадают с репозиторием |
| Frontend | отдаваемые `assets/app.js` и `assets/api.js` побайтово равны файлам HEAD |
| Журнал | `Application startup complete`, за последние 1000 строк ни одного `ERROR`/`Traceback` |
| Health Studio | 200: `comfy_connected: true`, ComfyUI 0.15.0, torch 2.9.1+rocm7.2.0, 127 workflow, 675 нод, VRAM free 95.2 ГБ |
| Health ComfyUI | 200 на `/system_stats` |

Миграции не выполнялись и не потребовались: `schema_version` = 3, `migration_history` содержит
только записи 1–3 с датой 2026-10-28. Проверка выполнена на снимке БД (`sqlite3` backup из
read-only соединения), production-файл не открывался на запись.

Проверка production API второго этапа (без авторизации, без подделки cookie):

| Маршрут | Код | Тело / заголовок |
|---|---|---|
| `GET /api/health` | 200 | полный health-объект, путей и секретов не содержит |
| `GET /api/auth/me` | 401 | `authentication required`, `WWW-Authenticate: Session` |
| `GET /api/auth/sessions` | 401 | было 404 до активации |
| `GET /api/me/usage` | 401 | было 404 |
| `GET /api/users/usage` | 401 | было 404 |
| `GET /api/workflows` | 401 | требует сессии |
| `GET /api/models` | 401 | требует сессии |

Дополнительно проверены 23 остальных GET-маршрута (`/api/jobs`, `/api/gallery`, `/api/events`,
`/api/settings`, `/api/stats`, `/api/telemetry/*`, `/api/models/catalog`,
`/api/models/downloads/status`, `/api/outputs/{file}`, `/api/workflows/{id}/*` и др.): все дают
401, ни в одном ответе нет `Traceback`, `/home/`, `sqlite3` или `SELECT `. `POST /api/jobs`,
`POST /api/uploads`, `POST /api/auth/sessions/revoke-others` без сессии — 401 на этапе
аутентификации, до обработчика и до записи в БД. Неизвестный путь — 404, не 500.

## 8. Что реально проверено

* **Production запускает код HEAD.** Совпадение md5 `app/api.py`, `app/rbac.py` и отдаваемых
  `assets/*.js` с репозиторием; процесс стартовал 17:12:48 MSK и с тех пор не перезапускался.
* **Маршруты активны в production.** `/api/auth/sessions`, `/api/me/usage`, `/api/users/usage`
  и `POST …/revoke-others` отвечают 401 вместо прежних 404/405.
* **Отказы без утечек.** 30 маршрутов в production: ни `Traceback`, ни `/home/`, ни `sqlite3`,
  ни `SELECT `; неизвестный путь → 404, не 500; POST без сессии отсекается до записи в БД.
* **SPA в production.** Playwright: заголовок «🐴 Local AI Creator Studio», форма входа с полями
  Username / Password и кнопкой Sign in, `app.css` / `app.js` / `api.js` → 200, JS-исключений
  ноль, единственный неуспешный запрос — `/api/auth/me` 401 (корректно без сессии), карточка
  «Your allowance» отсутствует — так и должно быть до входа.
* **Старые данные читаются новым кодом.** Снимок production DB открыт классом из HEAD:
  `schema_version` 3, новых миграций не применено, `integrity_check` ok, 140 job, 156 outputs,
  3 пользователя, 3 сессии, 10 строк телеметрии читаются и сериализуются в JSON без ошибок;
  все 156 файлов результатов на диске (204 780 180 байт: 100 изображений, 21 текст, 18 видео,
  17 аудио), битых JSON-блобов в `params`/`graph`/`outputs` — 0.
* **Совместимость владения старыми строками.** 130 из 140 job и 144 из 156 outputs имеют
  `user_id = ''` — они созданы до появления аккаунтов. Такие строки остаются доступны только
  admin (`_media_allowed`: «Legacy files … belong to nobody and stay admin-only»); admin видит
  все 140/156, аккаунты sveta и polina — по нулю, потому что своего они ещё не запускали.
  Потери данных нет, утечки тоже.
* **Сессии пережили перезапуск.** Сессии живут в SQLite, а не в памяти: сессия admin,
  созданная 07:52 UTC, после рестарта осталась действительной (истекает 19:52 UTC), две
  просроченные сессии от 28 октября остались просроченными.
* **Два события `studio backend started`** появились в `events` (391 → 393) — это штатная
  запись при старте сервиса от двух перезапусков владельца, а не работа приёмки.
* **Совместимость среда/тесты.** Сервис и тесты используют один и тот же интерпретатор и
  site-packages (venv `studio/.venv`, Python 3.14.4): fastapi 0.135.3, pydantic 2.14.1,
  uvicorn 0.46.0. Зелёный pytest означает то же окружение, что и production.
* **Безопасность.** 171 тест на границы ролей и квот (см. §5), все на реальных HTTP-запросах
  с настоящими cookie/CSRF, но в временных БД.

## 9. Что пока не проверено

* **Карточка «Your allowance» визуально.** Она за логином; реальный вход в этой задаче запрещён.
  Проверено косвенно: контракт ключей закреплён тестом `test_dashboard_allowance_contract`,
  бандл грузится без ошибок, без сессии карточка не рисуется.
* **Реальный вход/выход, смена пароля, восстановление после истечения сессии** — требуют
  разрешения на реальные credentials.
* **Роль `child` в production не назначена.** Проверено чтением таблицы `users`:
  `dmitry2` — admin, `sveta` и `polina` — user. Детская политика (55 workflow, 5 папок моделей,
  1 задача, 40 запусков в сутки, 10 ГБ) существует и покрыта тестами, но на живых аккаунтах не
  применяется. Это решение владельца, а не дефект: смена роли — запись в production DB,
  в этой задаче запрещена.
* **Поведение под нагрузкой**: два одновременных запуска чуть ниже квоты могут вместе её
  превысить (квота не резервируется) — известно и задокументировано, но не измерено.
* **Производительность** `usage_for()` на больших объёмах outputs не измерялась.

## 10. Известные ограничения

1. **403 по запрещённому workflow и по маршруту загрузки не пишутся в аудит.** Бюджетные отказы
   и отказ по чужому upload — пишутся (warn + user_id). Осознанный пробел, чинится отдельно.
2. **`/api/workflows/{id}/benchmark` и `/compare` принимают любой id**, но агрегируют только
   телеметрию вызывающего: для запрещённого workflow ребёнок получит пустые группы. Утечки нет,
   несогласованность с `/{id}` (403) оставлена сознательно.
3. **Квота не резервируется** (см. §3) и считает только файлы из таблицы `outputs`.
4. **Категории в политике — канонические.** Файл workflow может писать `image-enhancement`,
   схема приводит к `enhancement`; загрузчик политики применяет ту же таблицу алиасов.
   Исправлено в этой сессии (см. §11), теперь оба написания работают.
5. **Один процесс, одна машина.** Sessions живут в SQLite; горизонтального масштабирования нет
   и не планируется (local-first).

## 11. Итоговый статус

Во всех строках: **Код** — реализовано ли в HEAD; **Тесты** — покрыто ли зелёным тестом;
**Production** — проверено ли на живом сервисе (анонимный доступ проверен везде, авторизованные
сценарии — нет, потому что реальный вход в этой задаче запрещён).

| Функция | Код реализован | Тесты | Production | Итог |
|---|---|---|---|---|
| Admin | да — `is_admin` обходит workflow/модели/владение/квоты | `test_admin_is_unaffected_by_the_child_policy`, `test_admin_is_not_limited_to_its_own_uploads`, `test_admin_cannot_be_restricted_by_the_file` | аккаунт `dmitry2` (admin), сессия жива после рестарта; admin-ответы по HTTP не воспроизводились (вход запрещён) | PARTIAL |
| User | да — все 127 workflow, все папки, 3 активные задачи, без суточных и дисковых лимитов | `test_limits_follow_the_policy`, `test_storage_allowance_does_not_apply_to_adults` | аккаунты `sveta`, `polina` (role=user), своих job у них 0; вход не выполнялся | PARTIAL |
| Child | да — 55 workflow, 5 папок, 1 задача, 40 запусков/сутки, 10 ГБ | `test_child_sees_only_allowed_workflows`, `test_child_daily_allowance_blocks_the_next_run`, `test_child_storage_allowance_blocks_new_runs` и др. | ни одному production-аккаунту роль `child` не назначена | NOT TESTED |
| Workflow restrictions | да — `_workflow_for()` до создания job | 403/404-тесты, `test_the_child_sees_every_workflow_its_categories_name` (реальные файлы) | `/api/workflows` 401 без сессии; 403 на запрещённый workflow требует входа | PARTIAL |
| Model restrictions | да — список папок из политики и `folder_map` | тесты picker'а и запрета папок | `/api/models` 401 без сессии; содержимое picker'а без входа не проверяется | PARTIAL |
| Upload ownership | да — `_check_input_ownership()` в submit **и** rerun | `test_child_cannot_run_a_job_on_another_accounts_upload`, `test_rerun_cannot_replay_another_accounts_upload` | `POST /api/uploads` 401 до обработчика; реальная загрузка не выполнялась | PARTIAL |
| Output isolation | да — `_media_allowed("output", …)`, только `users/<caller>/` | `test_child_cannot_read_another_accounts_output`, `test_output_subfolder_cannot_be_walked_aside` | `/api/outputs/{file}` 401; 156 старых файлов читаются, 144 из них legacy и остаются admin-only | PARTIAL |
| Daily limits | да — `jobs_today()` по UTC + `_check_submission_budget()` | `test_child_daily_allowance_blocks_the_next_run`, `test_rerun_counts_against_the_daily_allowance` | child не назначен, живого 429 не было | PARTIAL |
| Storage quota | да — сумма `outputs.size` против `max_output_gb` | `test_child_storage_allowance_blocks_new_runs`, `test_storage_allowance_does_not_apply_to_adults` | то же | PARTIAL |
| Session management | да — `/api/auth/sessions`, `revoke-others` с `EXISTS`-гардом | 11 тестов `test_sessions.py`, включая конкурентный | маршруты активны (401 вместо 404/405), сессии пережили рестарт; список и отзыв с живой сессией не проверялись | PARTIAL |
| Usage dashboard | да — `/api/me/usage` + `/api/users/usage` (admin) | 8 тестов `test_usage.py`, контракт ключей карточки | эндпоинты активны (401); карточка за логином | PARTIAL |

Сводно по инфраструктуре: архитектура и схема v3 — **PASS** (миграции не потребовались),
активация в production — **PASS** (сервис на коде HEAD), тесты и coverage — **PASS**
(709 / 78.06 %), SPA без JS-ошибок — **PASS**, чтение старых данных новым кодом — **PASS**.

**Найдено и исправлено в ходе приёмки**

1. Политика называла категорию `image-enhancement`, а схема приводит её к `enhancement` —
   ребёнок молча видел 46 workflow вместо 55 (терялись 9 апскейлов/деталей). Исправлено в
   `registry/rbac.json`, загрузчик политики нормализует категории той же таблицей алиасов,
   добавлены 4 теста на реальных файлах реестра и политики.
2. `rerun` переотправлял сохранённые params в обход проверки владения файлами — теперь проходит
   ту же `_check_input_ownership` (найдено в предыдущей сессии, покрыто тестом).

### Вердикт: **READY WITH LIMITATIONS**

Multi-user Foundation активирован: production работает на коде HEAD, все новые маршруты отвечают
как задумано, старые данные читаются без миграций, 709 тестов зелёные, SPA не падает.
Блокирующих проблем нет — семью можно заводить в работу сегодня.

Что мешает назвать это «READY» без оговорок, и что с этим делать:

1. **Роль `child` никому не назначена.** Все три аккаунта — admin и два user; детские лимиты
   живут только в тестах. Владельцу: `POST /api/users/{id}` с `{"role": "child"}` под admin-сессией
   (это запись в production DB, поэтому вне рамок этой задачи).
2. **Авторизованные сценарии не проверены живыми запросами** — реальный вход запрещён условиями.
   Первый вход в каждый аккаунт стоит сделать глазами: карточка «Your allowance», список
   устройств, запрещённый workflow у ребёнка.
3. **Аудит отказов 403** по workflow и по загрузке пока не пишется (бюджетные отказы пишутся).
4. **Квота не резервируется** при одновременной отправке и считает только файлы таблицы `outputs`.
5. **`benchmark`/`compare` не гейтятся политикой** — утечки нет (только своя телеметрия), но
   403 для запрещённого workflow там не будет.
