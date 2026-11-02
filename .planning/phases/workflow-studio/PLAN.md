# Phase: Workflow Studio (создание, импорт, редактирование, запуск workflow)

Классификация: **architecture-change** (новые API, новый формат хранения, конвертация UI↔API).

## 1. Аудит: что уже есть (2026-11-01)

| Область | Факт |
|---|---|
| Каталог workflow | 127 JSON в `studio/registry/workflows/`, формат — **API prompt** + типизированные `parameters`, `models`, `custom_nodes`. Загружается `Registry.reload()` в `WorkflowDef` (pydantic). |
| API | `/api/workflows`, `/api/workflows/{id}`, `/api/workflows/categories`, `/api/workflows/{id}/validate`, `/api/jobs` (submit/rerun/cancel), `/api/gallery`, `/api/outputs`, `/api/models`, `/api/inputs`, `/api/users`… |
| Валидация | `graphcheck.check_workflow` (ноды/модели/связи по `/object_info`) + `inventory.derive_models`, `missing_nodes_in`, `analyze`. |
| Запуск | Один движок: `JobManager.submit()` → `registry.validate_and_build()` → префикс `users/<id>` → `ComfyClient.submit()` (POST `/prompt`) → опрос `/history`. |
| Модели | `Inventory` (файлы по папкам) + `models_catalog.json`; `resolve_folder()` знает 14 соответствий class→folder. |
| ComfyUI | 0.15.0, `127.0.0.1:8188`, Docker `comfyui-strix-halo`. Host-порт UI — `8189` (Caddy + basic auth). |
| Workflow-директория ComfyUI | `./workflows` → `/opt/ComfyUI/user/default/workflows` (bind mount). Существующий precedent: `smoke_sdxl_turbo.api.json`. |
| UI-формат | **Не поддерживается нигде** в backend: `widgets_values`/`links`/`last_node_id` — 0 совпадений. |
| Frontend | Vanilla JS в `studio/frontend/dist/assets/` (`app.js` 855 строк, хеш-роутер `ROUTES`, `api.js` — тонкий клиент). Сборки нет, dist = исходник. |
| Пользовательские workflow | Отсутствуют: каталог только системный, per-user хранения нет. |

## 2. Чего не хватает

1. Понимания UI workflow JSON (импорт, экспорт, конвертация в API).
2. Открытия штатного редактора ComfyUI из Studio с передачей конкретного workflow.
3. Обратного импорта того, что пользователь сохранил в ComfyUI.
4. Пользовательского каталога (создание/копирование/удаление, изоляция владельцем).
5. Генератора workflow и системы шаблонов.
6. Единого отчёта валидации с разделением ошибок/предупреждений/зависимостей.

## 3. Решения

- **Хранилище пользовательских workflow — файлы, не БД.** `studio/data/workflows/users/<user_id>/<id>.json`. Причина: миграция схемы production-БД (v3 → v4) потребовала бы рестарта сервиса и запрещена условиями. Файлы дают изоляцию через путь (тот же принцип, что `output/users/<id>/`), атомарную запись и ревизии без ALTER TABLE.
- **Один движок запуска.** Пользовательский workflow превращается в `WorkflowDef(execution="graph")` и регистрируется в `Registry.extra` — дальше работает существующий `JobManager.submit()` со всеми гарантиями (префикс владельца, RBAC, checks, очередь).
- **Редактор — только штатный ComfyUI.** Studio пишет UI JSON в каталог workflow ComfyUI (bind mount), кнопка открывает `http://<host>:8189/`. Никакого своего canvas. Обратный путь — импорт из того же каталога.
- **Конвертация — только по `/object_info`.** Неизвестный класс → `missing_nodes`, а не догадка.
- **Параметры запуска** для пользовательских workflow извлекаются детерминированно из графа (text/seed/steps/width/height/denoise), а не «все входы всех нод».
- **Генератор детерминированный**: шаблон → проверка классов в `/object_info` → проверка моделей → сборка API-графа → конвертация в UI. Нет доступной ноды/модели → честный отказ со списком нехватки. Никаких внешних LLM.

## 4. Риски совместимости

- `Registry.reload()` вызывается в рантайме (`/api/engine/refresh`) и затёр бы пользовательские определения → добавляется `Registry.extra`, который переживает reload.
- Каталог из 127 workflow не трогается: пользовательские записи живут отдельно, id с префиксом `uwf_`.
- `POST /api/jobs` принимает `workflow_id` строкой → добавляется проверка владельца для `uwf_`-id, иначе участник мог бы запустить чужой граф.
- Каталог ComfyUI workflow общий для всех пользователей процесса → имена файлов получают префикс пользователя (`studio-<user>-<slug>`), чтобы двое не перезаписали друг друга.
- Формат ComfyUI 0.15.0: `COMBO`-входы приходят как `["COMBO", {"options": [...]}]`, старые — как `[[...], {}]`; поддержаны оба.

## 5. Этапы

1. `workflow_io.py` — определение формата, UI→API, API→UI, отчёт валидации.
2. `workflow_store.py` — пользовательский каталог, ревизии, экспорт.
3. `workflow_gen.py` — шаблоны + детерминированный генератор.
4. `workflow_api.py` — эндпоинты; регистрация в `create_app`; `Registry.extra`; `WorkflowDef` из пользовательских записей; guard в `POST /api/jobs`.
5. Frontend: страница `#/create` (Generator + Import), кнопки каталога.
6. Тесты: unit (конвертация, шаблоны, хранилище) + API-тесты (импорт, валидация, изоляция).
7. Живая проверка на scratch-сервере, документация.

## Статус 2026-11-02

Реализация и стабилизация **выполнены**, набор **не закоммичен** (по брифу «не делай commit
автоматически»). Итог и доказательства — `docs/WORKFLOW_STUDIO_STABILIZATION.md`,
точка продолжения — `docs/RESUME-2026-11-02.md`.

* 8 шаблонов, 14 маршрутов `/api/wf/*`, хранилище с ревизиями и границей по владельцу.
* Импорт понимает каталожные обёртки, UI Workflow JSON и API Prompt JSON, host-tool — отдельный
  формат; произвольный JSON остаётся нераспознанным.
* Пользовательские workflow видны только владельцу (`uwf_…`, чужой id → 404), системный каталог
  остаётся 127.
* Сьют 861 тест / 79.3 %; браузерные сценарии A–E пройдены на scratch.
* Открыто: коммит слайсами, `STUDIO_EDITOR_URL` для ссылки на редактор, push.
