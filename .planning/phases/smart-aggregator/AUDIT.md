# PHASE 0 — Аудит: Smart Workflow Discovery для ComfyUI Studio

Дата: 2026-11-02. Репозиторий `~/workspace/comfyui-strix-halo`, ветка `main`, HEAD `8492418`.
Дерево грязное: 21 путь от предыдущей стабилизации Workflow Studio (не закоммичено, коммитов не делаю).

## 1. Что уже есть (переиспользуем, не дублируем)

| Слой | Факт | Файл |
|---|---|---|
| Backend | FastAPI, 11 766 строк в `studio/backend/app` | `app/api.py` (`create_app`, `AppState`) |
| Роуты | Регистрация через `register_*_routes(app, state)` после объявления основных | `api.py:1351–1358` |
| Авторизация | Все запросы аутентифицированы; `get_current_user`, `require_admin`, `Principal` | `app/auth.py`, `app/api.py` |
| RBAC | `rbac.json`, роли admin/user/child, `Rbac.load` + `/api/engine/refresh` | `app/rbac.py` |
| БД | sqlite3, `Database` с блокировкой, WAL, `MIGRATIONS` до версии **3** | `app/db.py` |
| Таблицы | jobs 150, outputs 169, users 3, sessions 5, events 440, job_telemetry 20, schema_migrations 3 | `data/studio.db` |
| Каталог workflow | 127 JSON в `studio/registry/workflows/` (обёртка + API prompt), `Registry` + `WorkflowDef` | `app/registry.py` |
| Конвертация | `detect_format`, `unwrap_with_meta`, `ui_to_api`, `api_to_ui`, `graph_models`, `validate_workflow`, `widget_slots` | `app/workflow_io.py` (805) |
| Пользовательские workflow | Файловый store, ревизии, границы владельца, `uwf_…` | `app/workflow_store.py`, `app/workflow_api.py` |
| Проверка графа | `graphcheck`, `inventory.derive_models / missing_nodes_in / analyze` | `app/graphcheck.py`, `app/inventory.py` |
| Движок | `ComfyClient` с кэшем `/object_info`, `JobManager` (очередь, полосы GPU/CPU) | `app/comfy.py`, `app/jobs.py` |
| Тесты | 46 модулей, 861 тест, покрытие 79.3 % (`--cov=backend/app`) | `backend/tests/` |
| Frontend | SPA без сборки, `dist/assets/app.js` (роуты `ROUTES`, `VIEWS`), `api.js` (`studio.*`) | `dist/assets/` |
| Статус | Генератор `gen_status.py` читает `cache/audit/{junit.xml,coverage.json}` | `studio/tools/gen_status.py` |

Вывод: каркас для discovery встраивается как ещё один пакет роутов и ещё одна миграция БД.
Никакого второго приложения, второго JobManager, второго каталога и второй системы пользователей.

## 2. Реальные возможности источников (проверено 2026-11-02, HTTP с реальной сети)

| # | Источник | Код | Что реально доступно | Способ |
|---|---|---|---|---|
| 1 | comfy.org/workflows | 200, HTML 1.5 МБ | ~2067 ссылок `/workflows/<slug>` в разметке | discovery-only (парсинг ссылок), API нет |
| 2 | Comfy-Org/workflow_templates | 200 (GitHub API) | дерево репозитория, JSON-шаблоны, commit SHA | GitHub API |
| 3 | comfyanonymous/ComfyUI_examples | 200 (GitHub API) | дерево, PNG с встроенным workflow, JSON | GitHub API + PNG-метаданные |
| 4 | openart.ai/workflows | 200, HTML | только разметка; публичного API без ключа нет | discovery-only |
| 5 | comfyworkflows.com | SSL/connect fail (`000`) | недоступен с этой машины | помечаем `unreachable`, не выдаём за рабочий |
| 6 | civitai.com | 200 JSON | `/api/v1/models?types=LORA`, `types=Workflows`, поиск, базовые модели, статистика | публичный API |
| 7 | runcomfy.com | 200, HTML | разметка страниц workflow | discovery-only |
| 8 | comfyui.org/en/workflows | 200, HTML | разметка | discovery-only |
| 9 | comfyvault.app | 200, HTML | разметка | discovery-only |
| 10 | comfy.icu | 200, HTML | разметка (cloud-сервис) | discovery-only |
| 11–18 | GitHub-коллекции | 200 (API) | деревья, JSON/PNG/ZIP, commit SHA | GitHub API |
| 19 | huggingface.co | 200 JSON | `/api/models?search=`, дерево файлов | публичный API |
| 20–21 | reddit.com/r/comfyui, r/StableDiffusion | **403** | анонимный `.json` заблокирован | `blocked` — честный статус, не «подключён» |
| 22 | youtube.com | 200, HTML 1.5 МБ | `ytInitialData` в разметке поиска | discovery-only по HTML |
| 23 | esheep.com | DNS fail | домен не резолвится | `unreachable` |
| 24 | aigodlike.com | 200, HTML (Nuxt SSR) | разметка | discovery-only |

Итого: реальная загрузка файлов — GitHub (3 + 8 коллекционных + любые пользовательские репозитории),
Civitai (метаданные LoRA/workflow), Hugging Face (метаданные + файлы). Остальные — обнаружение ссылок,
и это указывается в UI как `discovery only`, а не как «Connected».

GitHub без токена: 60 запросов/час. Отсюда: инкрементальная синхронизация по commit SHA,
ограничение файлов на прогон, кэш ответов, ошибки одного репозитория не останавливают остальные.

## 3. Точки интеграции

1. **БД**: миграция 4 в `app/db.py` — аддитивные таблицы `disc_*` (ресурсы, ссылки-происхождение,
   статусы источников, избранное, адаптации, embeddings). Существующие таблицы не меняются;
   `CURRENT_SCHEMA_VERSION` 3 → 4. Миграция идемпотентна, откатывается целиком при ошибке.
   Production применит её только при рестарте, который делает владелец.
2. **Backend**: новый пакет `app/discovery/`; регистрация `register_discovery_routes(app, state)`
   рядом с `register_workflow_routes`. `AppState` получает `discovery` (store + providers + registry).
3. **Поиск**: гибрид BM25 + фильтры + (опционально) embeddings; модель не скачивается —
   backend `onnx-minilm` активируется только когда файл модели уже лежит на диске.
4. **Валидация/совместимость**: `workflow_io.detect_format/validate_workflow`, `graph_models`,
   `registry.node_info`, `inventory` — без новых проверялок графа.
5. **Импорт в Studio**: найденный ресурс импортируется через существующий `/api/wf/import`
   (тот же путь, что у файла с диска), провенанс пишется в `source_meta`.
6. **Frontend**: две вьюхи в существующем SPA — `#/discover` (Smart Search) и `#/sources`
   (управление источниками), плюс карточки результатов с теми же действиями, что у библиотеки.

## 4. Ограничения среды, влияющие на дизайн

- В `studio/.venv` нет `numpy`, `torch`, `sentence-transformers`, `faiss`, `sqlite_vec`.
  Поэтому индекс — чистый Python (BM25 + нормализация), векторы — опциональный слой.
- `studio/tools/vision-venv` содержит `onnxruntime 1.31.1` и `numpy 2.5.4` — готовый путь для
  ONNX-эмбеддингов без установки torch, но модели на диске нет.
- Локального OpenAI-совместимого LLM-эндпоинта для ранжирования сейчас не поднято (порты 8188 —
  ComfyUI, 8000 — Studio, 2019/8189 — Caddy; llama-server не запущен). Ранжирование работает
  детерминированной формулой, LLM-слой включается настройкой.
- Сеть есть: GitHub, Civitai, HF отвечают; Reddit 403, comfyworkflows и esheep недоступны.

## 5. Риски и как они закрыты

| Риск | Решение |
|---|---|
| Изменение production БД | только аддитивная миграция 4; тесты на временных БД; `integrity_check` до/после |
| Недоверенный JSON снаружи | структурное распознавание через `workflow_io`, лимиты размера, никакого исполнения, ZIP без traversal |
| Дубликаты одного workflow | `content_hash` + `graph_sig` (мультимножество class_type и входов) + нормализованный заголовок, провенанс в `disc_links` |
| Rate limit GitHub | инкремент по `commit_sha`, бюджет запросов на прогон, кэш, пауза при 403 |
| Скачивание моделей без спроса | discovery никогда не качает веса; LoRA — только метаданные |
| Поломка существующего | новые тесты + полный regression suite; существующие роуты не меняются |
