# Multi-user Acceptance — ComfyUI Studio

Приёмка проведена на HEAD `c1d2f61` (плюс исправления этой сессии). Три статуса:
**PASS** — проверено и работает, **PARTIAL** — реализовано, но проверено не полностью,
**FAIL** — обнаружена проблема.

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

**НЕ АКТИВИРОВАНО.** `comfyui-studio.service` работает с 13:50:32 MSK на старом коде
(PID 963616, NRestarts 0). Причина: `sudo` требует интерактивного пароля
(`sudo -n true` → `interactive authentication is required`), а по правилу задачи обходить это
запрещено. Перезапуск должен выполнить владелец:

```
sudo systemctl restart comfyui-studio.service
```

Что изменится после перезапуска (код HEAD, проверен на отдельном инстансе, см. §8):

| Проверка | Сейчас (старый код) | После рестарта |
|---|---|---|
| `GET /api/auth/sessions` | 404 | 401 без сессии, список с сессией |
| `GET /api/me/usage` | 404 | 401 без сессии, totals с сессией |
| `GET /api/users/usage` | 404 | 401 без сессии, 403 для не-admin |
| `POST /api/auth/sessions/revoke-others` | 405 | 401 без сессии |
| Проверка владения upload при submit/rerun | нет | 403 на чужой файл |
| Карточка «Your allowance» в Dashboard | скрыта (эндпоинта нет) | показывается из `/api/me/usage` |

Миграции не нужны: схема v3 уже содержит таблицу `sessions` с колонками
`id, user_id, csrf, created_at, expires_at, last_seen, client_ip, user_agent`.

## 8. Что реально проверено

* **Код HEAD поднимается как настоящий HTTP-сервер.** Отдельный процесс
  (`uvicorn`, порт 8123) с временным layout и пустой БД, production не затронут:
  `/api/health` → 200 за 0.5 с, приложение стартует, схема создаётся сама.
* **Маршруты зарегистрированы.** На этом инстансе `/api/auth/sessions`, `/api/me/usage`,
  `/api/users/usage` → **401**, `POST …/revoke-others` → **401**; на живом старом сервисе те же
  пути → **404/405**. Разница — прямое доказательство, что код в HEAD новее развёрнутого.
* **Отказы без утечек.** Ни один ответ (43 маршрута) не содержит `Traceback`, `/home/`,
  `sqlite3`, `SELECT `; неизвестный путь → 404, не 500.
* **SPA.** Playwright: страница входа открывается на обоих инстансах (живом и новом),
  `app.css` / `app.js` / `api.js` → 200, JS-исключений ноль, единственный 401 — `/api/auth/me`
  (ожидаемо без сессии), интерфейс не ломается.
* **Production DB.** `PRAGMA integrity_check` = ok, `user_version` = 3, WAL; счётчики строк
  совпадают с состоянием до приёмки.
* **Совместимость среда/тесты.** Сервис и тесты используют один и тот же интерпретатор и
  site-packages (venv `studio/.venv`, Python 3.14.4): fastapi 0.135.3, pydantic 2.14.1,
  uvicorn 0.46.0. Зелёный pytest означает то же окружение, что и production.
* **Безопасность.** 171 тест на границы ролей и квот (см. §5), все на реальных HTTP-запросах
  с настоящими cookie/CSRF, но в временных БД.

## 9. Что пока не проверено

* **Активация.** После рестарта не проверялись: живые ответы новых маршрутов, отображение
  карточки «Your allowance» с реальными цифрами, поведение существующих сессий после деплоя.
* **Карточка «Your allowance» визуально.** Она за логином; реальный вход в этой задаче запрещён.
  Проверено косвенно: контракт ключей закреплён тестом `test_dashboard_allowance_contract`,
  бандл грузится без ошибок, при отсутствии эндпоинта карточка скрывается (`.catch(() => null)`).
* **Реальный вход/выход, смена пароля, восстановление после истечения сессии** — требуют
  разрешения на реальные credentials.
* **Поведение под нагрузкой**: два одновременных запуска чуть ниже квоты могут вместе её
  превысить (квота не резервируется) — это известно и задокументировано, но не измерено.
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

| Раздел | Статус |
|---|---|
| Архитектура и схема БД (v3, миграции не нужны) | PASS |
| Роли и серверная политика | PASS |
| Квоты: активные задачи, суточный лимит, диск | PASS |
| Владение файлами (upload → submit → rerun) | PASS |
| Сессии: список, отзыв чужих, конкурентность | PASS |
| Маршруты: отказ постороннему, отсутствие утечек | PASS |
| Тесты и coverage (709 / 78.06 %) | PASS |
| SPA: загрузка, отсутствие JS-ошибок | PASS |
| Карточка «Your allowance» с реальными данными | PARTIAL — за логином, контракт закреплён тестом |
| Реальный вход/выход, смена пароля | PARTIAL — не выполнялись по ограничению задачи |
| Активация в production (рестарт) | FAIL — `sudo` требует пароль владельца |
| Аудит отказов 403 | PARTIAL — зафиксировано как ограничение |

**Найдено и исправлено в ходе приёмки**

1. Политика называла категорию `image-enhancement`, а схема приводит её к `enhancement` —
   ребёнок молча видел 46 workflow вместо 55 (терялись 9 апскейлов/деталей). Исправлено в
   `registry/rbac.json`, загрузчик политики нормализует категории той же таблицей алиасов,
   добавлены 4 теста на реальных файлах реестра и политики.
2. `rerun` переотправлял сохранённые params в обход проверки владения файлами — теперь проходит
   ту же `_check_input_ownership` (найдено в предыдущей сессии, покрыто тестом).

### Вердикт: **NOT READY**

Код и границы готовы (все проверки PASS), но production работает на старом коде, поэтому
многоuser-функции трём семейным аккаунтам сейчас недоступны. Единственная блокирующая причина —
перезапуск сервиса, требующий пароля владельца. После одного `sudo systemctl restart
comfyui-studio.service` и повторной проверки §7 статус переходит в **READY WITH LIMITATIONS**
с ограничениями из §10 (аудит 403, несогласованный гейт benchmark/compare, нерезервируемая квота).
