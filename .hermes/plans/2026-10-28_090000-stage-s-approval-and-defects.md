# План на 28.10.2026 — comfyui-strix-halo, после Stage 5

> Записан вечером 27.10 по просьбе: «запиши в plan на завтра». Ничего не исполнять без явного «поехали».

**Где мы стоим на входе:** Stage 5 Phase 1 закрыта и измерена — 104 workflow (37 graph / 65 host /
2 chain), 31 host-инструмент, 29/29 живых прогонов, `161 passed`, скачано 0 B.
Подробности: `docs/stage5_status.md`. Phase 2 — `docs/stage_s_proposal.md` (6 файлов,
5 273 376 045 B = 4.9112 GiB), **ждёт одобрения, ничего не скачано**.

**Девять задач. Первая — решение, остальные восемь можно делать без загрузок.**

---

## Task 0 (2 мин). Решение по Stage S — блокирует половину остальных

Открыть `docs/stage_s_proposal.md`, раздел бюджета, и прочитать три строки cross-check:
RealESRGAN частично уже покрыт, MobileSAM 16.5 MB — пробник, turbo f16 — третий вариант whisper.

**Варианты:**
- `да` — вся шестёрка 4.9112 GiB (SAM ViT-B + Moondream2 + whisper large-v3-q5_0 + RealESRGAN x4plus).
- `только SAM` — 375.9 MB, открывает 6 workflow, риск нулевой.
- `только whisper` — 1.03 GiB, чинит распознавание русского (сейчас «синтеза речи» → «интеллекта»).
- `пока нет` — тогда Task 1–8 без изменений, а 45 workflow из документа остаются на бумаге.

**Проверка:** в ответе пользователя есть одно из четырёх слов. Без него Task 1 не начинается.

---

## Task 1 (60–90 мин, только после `да` в Task 0). Скачать и поставить Stage S

**Файлы:** создать `script/stage_s_download.sh`, `logs/stage_s_download.log`;
изменить `studio/registry/models_catalog.json`, `docs/implementation_status.md`.

1. Скрипт: для каждого файла `curl -C - -L --retry 5 -o studio/cache/downloads/<name> <url>`,
   затем `sha256sum` и сравнение с таблицей из `docs/stage_s_proposal.md`. Ошибка хеша — файл
   удаляется, скрипт выходит с кодом 1.
2. Запуск в фоне: `./script/stage_s_download.sh 2>&1 | tee -a logs/stage_s_download.log`
   (ожидаемо 4.9112 GiB; на этой линии ~40–60 мин, резать по одному файлу, чтобы обрыв не съел всё).
3. Установка без контейнера: ONNX → `studio/tools/vision-models/`, ggml → `studio/tools/whisper-models/`,
   RealESRGAN → `models/upscale_models/`. PyTorch/ROCm не трогаем вообще.
4. `models_catalog.json`: по записи на файл (url, licence, size, sha256). Тест из Task 2 это проверит.

**Проверка:** `sha256sum -c` по всем шести — OK; `ls -l` размеры совпадают с таблицей до байта;
`curl -s localhost:8000/api/models/catalog | python3 -m json.tool | grep -c '"installed": true'` = 11.

---

## Task 2 (30 мин). Закрыть дрейф каталога моделей — 6 записей + тест

**Факт:** в `models/` одиннадцать файлов, в `studio/registry/models_catalog.json` — пять записей.
Нет: `CLIP-ViT-H-14`, `wan2.2_ti2v_5B_fp16`, `umt5_xxl_fp8_e4m3fn_scaled`, `wan2.2_vae`,
`ip-adapter-plus_sdxl_vit-h`, `RealESRGAN_x4.pth`. Плюс у `sd_xl_turbo` и `animagine-xl-4.0`
`sha256: MISSING`.

**Файлы:** `studio/registry/models_catalog.json`, `studio/backend/tests/test_models_catalog.py` (новый).

1. `sha256sum` для sd_xl_turbo и animagine (по 6.9 GB, ~2–3 мин каждый) — запустить в фоне,
   пока делается Task 3.
2. Дописать шесть записей: `folder`, `file`, `size_bytes` (из `stat -c %s`), `sha256`, `licence`,
   `source_url`, `installed: true`.
3. Тест: каждый непустой файл в `models/**` имеет запись в каталоге, каждый `sha256` — 64 hex-символа,
   каждый `size_bytes` совпадает с реальным размером на диске.

**Проверка:** `pytest studio/backend/tests/test_models_catalog.py -q` → 3 passed;
`/api/models/catalog` отдаёт 11 записей, все `installed: true`.

---

## Task 3 (40 мин). Измерить WAN 2.2 — единственные workflow без единого прогона

**Факт:** `video-i2v-wan22` и `video-t2v-wan22` отдаются как `ready_to_test`, модели на диске есть
(9.99 GB + 6.74 GB + 1.41 GB + 2.53 GB), но ни один прогон не зафиксирован. Это дыра в отчётности:
в UI они выглядят рабочими.

**Файлы:** `docs/stage5_status.md` (таблица прогонов), возможно `studio/registry/workflows/video-*.json`
(если дефолты окажутся нерабочими).

1. `./studio/.venv/bin/python studio/tools/smoke_run.py video-i2v-wan22 --set image=ref_raccoon.png --timeout 900`
2. То же для `video-t2v-wan22`.
3. Зафиксировать: время, разрешение, число кадров (`ffprobe -show_entries stream=nb_frames`), размер,
   пик VRAM (`rocm-smi --showmemuse` в фоне или `docker stats --no-stream`).
4. Если 900 с не хватает — снизить `length`/`steps` в JSON до значений, которые реально проходят,
   и записать это как дефолт с честным комментарием.

**Проверка:** два mp4 на диске, оба открываются `ffprobe`, в `docs/stage5_status.md` две строки
с реальными секундами. Либо — честная запись «не проходит, вот ошибка, помечено EXPERIMENTAL».

---

## Task 4 (20 мин). `audio-join`: нормализовать каналы, а не надеяться на ffmpeg

**Факт из ограничений Stage 5:** склейка не выравнивает число каналов и не приводит битность —
моно + стерео дают неожиданный результат.

**Файлы:** `studio/backend/app/hosttools.py` (функция `ffmpeg_audio_join`),
`studio/backend/tests/test_stage5_workflows.py`.

1. Тест: склеить `_s5_a1.wav` (44.1 kHz, моно) и `_s5_a2.wav` (22.05 kHz, моно) — ожидать один файл
   с `channels=1` и `sample_rate=44100`; затем склеить моно + стерео — ожидать `channels=2`.
2. Реализация: перед `concat` прогнать каждый вход через
   `-ac <max(всех)> -ar <max(всех)> -c:a pcm_s16le`, только потом склейка.
3. Проверить, что длительность суммы ≈ сумме длительностей (±50 мс).

**Проверка:** два новых теста зелёные, `ffprobe` на результате даёт заявленные каналы.

---

## Task 5 (15 мин). Outpaint: запретить расширение больше 512 px на сторону

**Факт:** за пределами ~512 px контент «уплывает»; сейчас ничто не мешает выставить 2000.

**Файлы:** `studio/registry/workflows/outpaint-turbo.json` (или генератор + `--force`),
`studio/backend/tests/test_stage5_workflows.py`.

1. Тест: параметр расширения 2000 → валидация реестра возвращает ошибку с текстом
   «outpaint is limited to 512 px per side on SDXL-Turbo; larger expansions drift».
2. В JSON — `max: 512` у параметра расширения и `notes` с причиной.

**Проверка:** тест зелёный, `GET /api/workflows/outpaint-turbo` показывает `max: 512`.

---

## Task 6 (10 мин). ACE-Step: предупреждать о коротких тегах, а не молча давать NaN

**Факт:** теги короче ~115 символов при кириллическом тексте дают мёртвый аудиофайл (латент
целиком NaN, 11.7 KB на выходе). Дефолт уже 148 символов, но пользователь может укоротить.

**Файлы:** `studio/backend/app/registry.py` или `jobs.py` (там, где собирается warning-событие).

1. Если `len(tags) < 115` и текст содержит кириллицу — в ответ job добавить `warning`:
   «ACE-Step на этой сборке ROCm замолкает при тегах короче 115 символов с кириллицей».
2. Тест на то, что warning есть при 60 символах и его нет при 148.

**Проверка:** прогон с короткими тегами даёт `silent: true` **и** warning в ответе.

---

## Task 7 (5 мин, вопрос). Репозиторий не под git

`git status` → `fatal: not a git repository`. Значит ни один шаг Stage 3/R/5 не закоммичен,
истории нет.

**Спросить:** `git init` + первый коммит (после `credential-scan`), или проект сознательно живёт
без VCS? Если да — план: `git init`, `.gitignore` на `models/`, `output/`, `studio/cache/`,
`studio/tools/vision-venv/`, `studio/tools/whisper-src/build/`, затем один коммит «импорт проекта
после Stage 5».

**Проверка:** `git log --oneline | head -1` печатает строку; `git status --short` не содержит
`models/` и `output/`.

---

## Task 8 (20 мин). Финальный отчёт и синхронизация документов

1. `./studio/.venv/bin/python -m pytest studio/backend/tests -q | tail -3` — ожидаем
   `≥166 passed` (161 + Task 2/4/5/6).
2. `./studio/.venv/bin/python studio/tools/gen_registry_docs.py` — каталог и матрица заново.
3. Обновить `docs/implementation_status.md`: число тестов, число записей каталога, статус WAN.
4. Пользователю — короткий отчёт: что решено по Stage S, что скачано (байты, sha256), какие
   workflow добавились, какие прогоны прошли, что осталось.

**Проверка:** отчёт содержит только те числа, которые получены командами в этом же прогоне.

---

## Что НЕ делать

- Не скачивать ничего до ответа в Task 0.
- Не трогать контейнер ROCm: всё новое — host-side (ONNX CPU, whisper CPU, llama.cpp host).
- Не менять `cn-dual-canny-depth` дефолты 0.15 / 0.4 — они измерены по трём рендерам.
- Не заявлять «готово» без живого прогона: у Stage 5 это правило уже поймало два дефекта
  (перерисовка краёв ControlNet и дописанные кадры при ускорении видео).

## Порядок, если время ограничено

Task 0 → Task 2 → Task 3 → Task 8. Это закрывает дрейф каталога и единственную непроверенную
область, не требуя ни одного байта загрузок. Task 1 запускается только после `да` и идёт фоном
параллельно с остальным.
