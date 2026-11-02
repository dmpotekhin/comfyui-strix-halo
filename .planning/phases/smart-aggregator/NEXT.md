# NEXT — Smart Workflow Discovery, продолжение 2026-11-03

Состояние на вечер 2026-11-02 (после закрытия PHASE 0–5).

## Где мы

* Репозиторий `/home/dmitry/workspace/comfyui-strix-halo`, ветка `main`, HEAD `8492418`,
  22 коммита впереди `origin/main`, **push не делался**.
* **36 путей не закоммичены** — это весь агрегатор (backend-пакет `discovery/`, 11 файлов тестов,
  frontend, документ, `.planning`).
* Сьют: **1025 passed, 0 failed, 110 с**, покрытие **79.36 %** (baseline до агрегатора — 861 / 79.29 %).
  `docs/STATUS.md` перегенерирован, `gen_status.py --check` → current.
* Документ: `docs/SMART_WORKFLOW_DISCOVERY.md` (378 строк). Статусы источников:
  WORKING 1, PARTIAL 6, REGISTERED 8, UNAVAILABLE 9 из 24.
* Production не тронут: MainPID `2168361`, health 200, БД read-only `user_version 3`, `disc_*` нет.
* Scratch-инстанс остановлен, порт 8128 закрыт. Рабочие артефакты прогонов: `cache/source_probe.json`,
  `cache/probe_sources.py` (gitignored, оставлены как доказательства).

## Как поднять окружение (3 команды)

```
cd /home/dmitry/workspace/comfyui-strix-halo/studio && ./.venv/bin/python -m pytest backend/tests -q
SCRATCH_KEEP=1 ./.venv/bin/python cache/scratch_discovery.py     # :8128, wfadmin / wfuser
cd /home/dmitry/workspace/comfyui-strix-halo && git status --porcelain | wc -l
```

`SCRATCH_KEEP=1` — режим «не пересоздавать базу» (добавлен вчера): без него скрипт делает
`rmtree` и вся история sync исчезает, что однажды и породило вопрос «почему 1 ресурс».

## Задачи на завтра, по приоритету

### T1. Коммит набора — решение за владельцем

Разбить 36 путей на 3 осмысленных коммита, каждый — после `credential-scan`:

1. backend: `backend/app/discovery/*`, `discovery_api.py`, `db.py` (schema 4), `api.py`, 11 файлов тестов;
2. frontend: `dist/assets/{app.js,api.js,app.css}`;
3. docs/planning: `docs/SMART_WORKFLOW_DISCOVERY.md`, `docs/STATUS.md`, `.planning/*`.

Критерий: `pytest` зелёный на каждом шаге, `gen_status.py --check` current, скан чистый.
Push — отдельным решением (22 коммита уже накопились).

### T2. Embedding-модель — нужно «да» или «нет»

Сейчас `semantic=false`, hashed n-граммы (`HASH_DIM = 512`), UI честно это пишет.
Модель `paraphrase-multilingual-MiniLM-L12-v2` ≈ 470 МБ в `~/.cache/comfyui-studio/embeddings/`.
Без явного согласия не скачиваю. Если «нет» — оставить как есть и снять пункт из открытых;
если «да» — скачать, прогнать `POST /api/discovery/index`, замерить качество на 5–6 запросах
(RU и EN), обновить §6 документа.

### T3. Довести источники (главный незакрытый пункт)

1. Прогнать sync по 8 GitHub-источникам со статусом REGISTERED
   (`comfy-templates`, `comfy-examples`, `gh-comfy-deploy`, `gh-ipadapter`, `gh-impact-pack`,
   `gh-diffustar`, `gh-aimpowerment`, `gh-thefiregroup`) — по одному, смотреть отчёт.
2. По результату перевести каждый в WORKING (получение и обработка проверены) или PARTIAL
   (только часть), с основанием в таблице.
3. Перепроверить 9 недоступных: Reddit требует OAuth (решить: делать ли токен или оставить
   UNAVAILABLE), YouTube/галереи — повторить пробу (таймаут мог быть сетевым).
4. Обновить §4 документа и убедиться, что `resources_count` в UI совпадает с отчётом прогона.

Критерий: у каждого источника либо факт получения ресурсов, либо записанная причина, почему нет.
Никаких «Connected» без ресурсов.

### T4. Покрытие тестами там, где оно слабое

* `media.py` — было ~51 %: PNG/WebP с workflow, PNG без workflow, битый архив, zip-бомба
  (256 записей / 16 МБ), архив с недопустимым расширением;
* `providers/web.py` — было ~54 %: robots.txt запрещает, robots.txt недоступен (fail-open/closed?),
  страница без ссылок;
* `index.py` / `embed.py` — инкрементальный индекс: `text_hash` не менялся → нет новой строки.

Критерий: каждый новый тест падает на сломанной логике (проверять RED перед GREEN).

### T5. Мелочи из аудита (по одной, с тестом или обоснованием)

* `rank.py`: `0.0.0.0` в `LOOPBACK_HOSTS` — на Linux это локальный адрес, но формально «все
  интерфейсы»; решить, убирать ли (сейчас частично перекрыт `check_public_url`).
* `scratch_discovery.py`: второй fixture (`anime-inpainting.json`) не создаётся — косметика
  dev-инстанса, но из-за неё «1 ресурс» вместо двух.
* Мобильная навигация остаётся вертикальным списком сверху — поведение существующей студии,
  не дефект; гамбургер не добавлять без запроса.
* Вне scope, но записать: `/api/me/usage` для admin считает только свои выходы (21) против
  Gallery (169); `_scope_output` не идемпотентна; `_reserve_output_dirs` оставляет пустые каталоги.

### T6. Что осталось непроверенным живьём

* `rerank.py` исполнялся только на loopback-сервере в тестах — настоящего локального
  LLM-эндпоинта не поднимали (и не нужно без решения).
* `sync.py`/`rank.py`/`rerank.py` кроме GitHub и Civitai не гонялись.
* Сравнение двух локальных workflow каталога не поддерживается (только локальный + найденный).

## Definition of Done для RC

1. `pytest` — 0 падений, покрытие не ниже 79.36 %;
2. `gen_status.py --check` — current;
3. `#/discover` и `#/sources` проверены в браузере на 1440/768/390 без горизонтального скролла;
4. `wfuser` не видит Sources, не может sync/index, не видит чужие данные;
5. таблица статусов источников отражает реальность, к каждой строке есть основание;
6. production не тронут (MainPID, health, `user_version 3`, нет `disc_*`);
7. набор закоммичен (или явно решено не коммитить), credential-scan чистый.

## Запреты, которые остаются в силе

* Не трогать production DB, не перезапускать сервис без подтверждения.
* Не ставить модели/custom nodes и не скачивать embeddings без согласия.
* Не коммитить и не пушить без явного решения владельца.
* Не ослаблять тесты ради зелёного результата; падающий тест сначала диагностируется.
* Секреты не печатать в вывод; перед каждым коммитом — `scan_credentials.py --staged`.
