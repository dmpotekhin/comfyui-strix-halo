# PHASE 0 — План реализации Smart Workflow Discovery

Пакет `studio/backend/app/discovery/`, миграция 4 в `app/db.py`, роуты `/api/discovery/*`,
две вьюхи в существующем SPA. Шаги идут в порядке выполнения; каждый заканчивается тестами.

| Шаг | Модуль | Содержание | Проверка |
|---|---|---|---|
| 1 | `db.py` | миграция 4: `disc_sources`, `disc_resources`, `disc_links`, `disc_favorites`, `disc_adaptations`, `disc_embeddings`; хелперы `run/fetch` | `test_db_migrations` + новый тест версии 4 |
| 2 | `discovery/model.py` | канонический ресурс, `content_hash`, `graph_sig`, `slug`, нормализация тегов | `test_discovery_model` |
| 3 | `discovery/store.py` | upsert с дедупликацией, провенанс, фильтры, избранное, статусы источников | `test_discovery_store` |
| 4 | `discovery/sources.py` | каталог 24 источников с приоритетом, типом и честной возможностью | `test_discovery_sources` |
| 5 | `discovery/net.py` | один шов для HTTP (UA, таймаут, JSON/text, лимит байт, обработка 403/429) | мок в тестах |
| 6 | `discovery/providers/*` | `base`, `github`, `civitai`, `huggingface`, `reddit`, `youtube`, `web` | `test_discovery_providers` на записанных ответах |
| 7 | `discovery/media.py` | PNG tEXt/iTXt/zTXt, WebP EXIF/XMP, безопасный ZIP | `test_discovery_media` |
| 8 | `discovery/analyze.py` | анализ графа: ноды, модели, custom nodes, категория с confidence | `test_discovery_analyze` |
| 9 | `discovery/compat.py` | READY/PARTIAL/MISSING_NODES/MISSING_MODELS/INCOMPATIBLE/UNKNOWN | `test_discovery_compat` |
| 10 | `discovery/text.py` | токенизация, RU→EN лексикон, BM25 | `test_discovery_search` |
| 11 | `discovery/search.py` | гибрид (lexical + поля + совместимость), фильтры, пагинация, offline | то же |
| 12 | `discovery/ranking.py` | детерминированный скоринг + опциональный локальный LLM со строгой валидацией | `test_discovery_ranking` |
| 13 | `discovery/adapt.py` | структурные операции, diff, применение к копии, ревалидация | `test_discovery_adapt` |
| 14 | `discovery/compare.py` | таблица сравнения + необязательное резюме | `test_discovery_compare` |
| 15 | `discovery/api.py` | `/api/discovery/sources`, `/search`, `/resource/{id}`, `/sync`, `/adapt/*`, `/compare`, `/favorites`, `/stats` | `test_discovery_api` |
| 16 | frontend | `#/discover`, `#/sources`, карточки, фильтры, действия | `node --check`, браузер |
| 17 | docs | `docs/SMART_WORKFLOW_DISCOVERY.md`, `STATUS` через генератор | `gen_status.py --check` |

## Границы

- Никаких скачиваний моделей и установок custom nodes. LoRA — только метаданные.
- Синхронизация — фоновая задача внутри существующего приложения (поток + lock), без Redis/Celery.
- Импорт внешнего workflow — только по явному действию пользователя и только через `workflow_io`.
- Все новые роуты под той же авторизацией; `sync` и `apply` — только admin.
- Скрытых фиктивных статусов нет: каждый источник показывает `api_available`, `download_available`,
  `status` и `last_error` из реального прогона.
