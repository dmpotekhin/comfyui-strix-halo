# End-to-End Validation — семейный сценарий

HEAD `9bd06cb` (поверх `9e49722`), проверка проведена 2026-11-01.

**Это не production acceptance.** Всё, что помечено VERIFIED, проверено на изолированном
экземпляре студии со своей БД; production-сервис в это время не перезапускался, его БД не
менялась, реальные пользователи в нём ничего не запускали. Что именно не проверено в
production — раздел 13.

Как читать пометки:

| Пометка | Значение |
|---|---|
| **VERIFIED** | проверено вживую на стенде, есть след: скриншот, строка БД, HTTP-ответ, замер |
| **TESTED** | покрыто автоматическим тестом, который падает при откате исправления |
| **NOT VERIFIED** | вживую не проверялось — либо в production, либо вообще |

---

## 1. Стенд (VERIFIED)

```
каталог      ~/.hermes/profiles/developer/cache/scratch/e2e
URL          http://192.168.31.60:8126
БД           studio/data/studio.db        своя, пустая на старте
ComfyUI      http://127.0.0.1:8188        настоящий, тот же движок, что у production
models       symlink на общий каталог моделей
output       symlink на общий каталог выходов
пользователи e2e-admin (admin), e2e-user (user), e2e-family (user)
```

Почему так: нужна настоящая генерация, но без права менять production-БД, роли, пароли и
systemd. Отдельный процесс, отдельная БД, тот же движок и те же определения workflow.

Ограничения задачи соблюдены: модели не скачивались, миграции не запускались, `git push` не
делался, роли/пароли/лимиты не менялись, параллельных тяжёлых генераций не было (все прогоны по
одному, очередь ни разу не заполнялась), production systemd не перезапускался, старые файлы не
удалялись.

---

## 2. Инвентарь 127 workflows (VERIFIED, read-only)

Источник: 127 определений `studio/registry/workflows/*.json`, ответ `GET /api/workflows` под
каждой ролью, проверка наличия файлов моделей на диске. Ничего не запускалось.

| Показатель | Значение |
|---|---|
| Всего определений | 127 |
| Модели на месте, ждут проверки (`ready_to_test`) | 121 |
| Проверено ранее (`available`) | 1 — `inpaint-turbo` |
| Не хватает моделей (`missing_models`) | 5 |
| Требуют загрузки файла пользователем | 58 |
| Движок host (CPU) | 82 |
| Движок graph (ComfyUI) | 45 |

Категории: image-editing 33, video-generation 28, audio-generation 27, image-generation 20,
image-enhancement 9, vision-language 9, characters 1.

### 2.1 Не готовы: нет моделей (5)

| workflow | чего не хватает |
|---|---|
| `cn-depth-sdxl` | `controlnet-depth-sdxl-1.0.safetensors`, `depth-anything-v2-small.onnx` |
| `cn-dual-canny-depth` | то же + `controlnet-canny-sdxl-1.0.safetensors` |
| `sam-bg-replace` | `mobilesam.*`, `sam-vit-base-*` |
| `sam-object-remove` | `mobilesam.*`, `sam-vit-base-*` |
| `sam-region-restyle` | `mobilesam.*`, `sam-vit-base-*` |

Итого 122 из 127 запускаться могут по зависимостям. Из них фактически проверены три
(раздел 3), остальные — **NOT VERIFIED** (127 тяжёлых прогонов в задачу не входили).

### 2.2 Требуют загрузить файл (58)

Все `img-*`, `img2img-*`, `inpaint-*`, `outpaint-*`, `cn-*`, `sam-*`, `vid-*` (кроме чисто
генеративных), `asr-*`, `audio-*`, `audio-from-video`. Механика загрузки проверена на
`img-thumb-640` (раздел 3.3).

### 2.3 Кандидаты на первую проверку

Модели на месте, загрузка не нужна, дёшево: `audio-from-video`, `audio-join`, `vid-join`,
`asr-transcribe`, `audio-slow-down`, `audio-speed-up`, `audio-trim-silence`, `vid-add-audio`,
`vid-fps-24/30/60`, `vid-poster`, `vid-rotate-90/180`, плюс `txt2img-turbo` и `tts-ru-piper`.

### 2.4 Что полезно семье

| Задача | workflow | Движок | Время |
|---|---|---|---|
| Картинка по описанию | `txt2img-turbo`, `format-*`, `style-*` | graph | 3–4 с |
| Аниме-иллюстрация | `txt2img-animagine`, `style-anime` | graph | ~10 с |
| Озвучка русского текста | `tts-ru-piper` | host | <1 с |
| Миниатюра/сжатие для отправки | `img-thumb-320/640`, `img-resize-1280`, `img-convert-webp` | host | <1 с |
| Улучшить старое фото | `upscale-realesrgan`, `img-detail-2x` | graph | 5–15 с |
| Убрать лишнее с фото | `inpaint-remove`, `inpaint-turbo` | graph | 10–20 с |
| Вырезать фон | `cutout-u2net`, `sam-cutout` | host | 1–3 с |
| Расшифровать голосовое | `asr-transcribe` | host | ~0.1× длины |
| Собрать видео из клипов | `vid-join`, `vid-add-audio` | host | секунды |
| Оживить кадр | `video-t2v-wan22` | graph | десятки секунд |

---

## 3. Три workflow от начала до конца (VERIFIED)

Все три — **под рядовым пользователем** (`user`), не под админом. Шаги: карточка → параметры →
`client_token` → `POST /api/jobs` → создание job → статус → результат → Gallery → история.

### 3.1 `txt2img-turbo` — граф ComfyUI

| Шаг | Итог |
|---|---|
| Открытие карточки из библиотеки | VERIFIED — `#/library`, `article.wf-card[data-id]` |
| Параметры | VERIFIED — prompt, negative, width, height, steps, cfg, batch_size, seed |
| `client_token` | VERIFIED — UUID v4; на insecure-context через `randomId()` (фикс `9aca6f0`) |
| `POST /api/jobs` | VERIFIED — 200 за **95 мс** |
| Создание job | VERIFIED — `queued` → `running` → `completed` |
| Статус | VERIFIED — прогресс, длительность, `completed 100 %` |
| Результат | VERIFIED — PNG в карточке, `naturalWidth 1024` |
| Gallery / история | VERIFIED |

Модель: `checkpoints/sd_xl_turbo_1.0_fp16.safetensors` (6.46 ГБ). Лицензия **CC BY-NC-SA 4.0** —
некоммерческое использование; карточка предупреждает об этом. Для семейного использования
подходит, для коммерческого — нет.

Время: первый прогон **11 с** (загрузка модели в VRAM), дальше **3–4 с** на 1024²/4 шага;
768²/6 шагов — **3 с**. Скриншоты: `02-txt2img-params.png`, `03-txt2img-result.png`.

### 3.2 `tts-ru-piper` — host-инструмент

| Шаг | Итог |
|---|---|
| Параметры | VERIFIED — text (textarea), voice (select), length_scale |
| Submit и job | VERIFIED |
| Результат | VERIFIED — WAV в `<audio>`, `readyState 4`, длительность 4.6 с |
| Gallery / история | VERIFIED |

Модель: голос `rhasspy/piper-voices ru_RU-irina-medium` (MIT), CPU, `no GPU`. Время
**0.6–0.9 с**, RTF 0.045. Скриншот: `04-audio-result.png`.

### 3.3 `img-thumb-640` — host-инструмент с загрузкой файла

| Шаг | Итог |
|---|---|
| Загрузка через штатный виджет | VERIFIED — файл в select как `<user>_<ts>_<hash>_test_upload.png` |
| Submit | VERIFIED |
| Результат | VERIFIED — JPEG 640×427 в карточке |
| Gallery / история | VERIFIED |

Моделей нет, PIL. Время **0.5 с**. Имя загруженного файла получает префикс пользователя, чужой
файл подставить нельзя. Скриншот: `05-thumb-result.png`.

---

## 4. Роли: Admin и User (VERIFIED на стенде, TESTED автотестами)

Child **не активировался** — по условиям нужно отдельное подтверждение.

| Проверка | admin `e2e-admin` | user `e2e-family` |
|---|---|---|
| Вход | VERIFIED | VERIFIED |
| Доступные workflows | 127 | 127 |
| Доступные модели | 12 файлов, 44.92 ГБ | те же 12 |
| Свои задачи | видит все (6) | только свои (4) |
| Свои результаты | Gallery 6 (все) | Gallery 4 (только свои) |
| Dashboard allowance | «Runs today 0 · no limit», `0 of 3 slots busy` | «Runs today 3 · no limit», `0 of 3 slots busy` |

Граница владения по HTTP, не по состоянию UI:

```
user  -> чужой /api/jobs/<id>      404
user  -> чужой /api/outputs/...    404
user  -> свой  /api/outputs/...     200
admin -> тот же чужой job           200
аноним -> /api/jobs                 401
```

TESTED: `tests/test_isolation.py`, `tests/test_media_paths.py`, `tests/test_api.py` — зелёные в
полном прогоне (раздел 10).

Рядовому `user` доступны все 127 workflows — это соответствует политике: ограничения задуманы
для `child`. Роль `user` = «взрослый, можно всё».

---

## 5. Gallery и история (VERIFIED на стенде)

- Галерея читает таблицу `outputs`: `user` видит только свои выходы, `admin` — все.
- История (`#/jobs`) хранит статус, прогресс, длительность, кнопки rerun/open.
- До исправления (раздел 8) миниатюры в галерее и плеер в карточке задачи были битыми; после
  фикса на стенде проверено обратное: картинка 1024 px отрисована, аудио `readyState 4`.
- Наследие: два файла прогонов *до* фикса лежат в общей `output/studio/` (`turbo_00008_.png`,
  `turbo_00009_.png`) и остаются недоступны владельцу. Файлы тестовые, не удалялись.

Скриншоты: `06-gallery.png`, `07-jobs.png`, `08-dashboard-user.png`, `09-dashboard-admin.png`.

---

## 6. Модели и лицензии (VERIFIED)

| Что | Файл/пакет | Размер | Лицензия |
|---|---|---|---|
| txt2img-turbo | `checkpoints/sd_xl_turbo_1.0_fp16.safetensors` | 6.46 ГБ | CC BY-NC-SA 4.0 (некоммерческая) |
| tts-ru-piper | голос `rhasspy/piper-voices ru_RU-irina-medium` | — | MIT |
| img-thumb-640 | моделей нет (PIL) | — | — |

Всего на диске 12 файлов моделей, 44.92 ГБ. Ничего не скачивалось.

---

## 7. Производительность и память (VERIFIED на стенде)

96 ГБ VRAM, `GPU_MAX_CONCURRENT=1`, очередь пустая, прогоны по одному.

| Величина | Значение |
|---|---|
| Ответ `POST /api/jobs` | **95 мс** |
| Время до начала выполнения | **0 с** (все прогоны) |
| turbo 1024²/4 шага | **11 с** первый, **3–4 с** далее |
| turbo 768²/6 шагов | **3 с** |
| TTS piper | **0.6–0.9 с** |
| Thumbnail 640 | **0.5 с** |
| Результат в UI после Run | **2.4–5.2 с** |
| Занято RAM (пик) | **11.3 ГБ** |
| Свободно RAM (минимум) | **19.9 ГБ** |
| RSS ComfyUI (пик) | **5.4 ГБ** |
| VRAM свободно во время прогонов | 88.7 / 96 ГБ |
| Ошибки ComfyUI | **нет** — 37 строк лога за окно тестов, ни traceback, ни error |

NOT VERIFIED: поведение под одновременной нагрузкой двоих, длинные видео-задачи WAN 2.2,
поведение при заполненной очереди. Не проверялось намеренно — параллельные тяжёлые генерации
были запрещены условиями.

---

## 8. Найденные и исправленные дефекты

Оба дефекта об одном: файл, который пользователь создал, оказывался вне его папки. Первый —
в графовых задачах, второй — в host-задачах. Оба воспроизведены на стенде, исправлены в
`9bd06cb`, покрыты тестами, проверены повторным живым прогоном после фикса.

### 8.1 D1 — результат графовой задачи писался в общую папку (VERIFIED, TESTED)

`app/jobs.py`. `submit()` переписывает `filename_prefix` на `users/<id>/…` и сохраняет граф в
job, но runner (`_run_graph_phase`) **пересобирает граф из params заново** — префикс терялся.
Файл падал в `output/studio/`, а `/api/outputs` отказывает в отдаче файла из общей папки её же
владельцу: job «completed», в галерее битая картинка.

Почему не всплывало раньше: все живые проверки до этого шли под админом, а админу отдаётся всё.

Исправление: префикс применяется повторно после пересборки. Тест
`test_the_graph_that_runs_writes_under_its_owner` — падает при откате.

Второй шов того же дефекта: строка job хранила `outputs` с путями относительно проекта, а
таблица `outputs` — уже со скоупом. Итог: галерея играет, плеер в карточке задачи — 404.
Исправление: пути скоупятся один раз, до записи в оба места. Тест
`test_host_job_reports_outputs_scoped_to_their_owner` — падает при откате.

### 8.2 D2 — host-инструмент не мог писать рядом со своими выходами (VERIFIED, TESTED)

ComfyUI в контейнере работает как root и первым создавал `output/users/<id>/studio` root-овым.
Host-инструменты (piper, thumbnail, ffmpeg) работают от обычного пользователя и получали
`[Errno 13] Permission denied: cannot reserve output name` — задача падала целиком.

Исправление: `_reserve_output_dirs()` создаёт папки выхода от лица студии **до** отправки
графа (по definition-relative префиксам, то есть до переписывания), с проверкой, что путь не
выходит за дерево владельца.

### 8.3 Статус

| | Исправлено | TESTED | VERIFIED на стенде | VERIFIED в production |
|---|---|---|---|---|
| D1 — префикс в runner'е | да | да | да | **нет** |
| D1 — outputs строки job | да | да | да | **нет** |
| D2 — root-owned папки | да | да | да | **нет** |

---

## 9. Ограничения и известные проблемы

1. **5 workflows без моделей** — `cn-depth-sdxl`, `cn-dual-canny-depth`, `sam-bg-replace`,
   `sam-object-remove`, `sam-region-restyle`. Без скачивания моделей не чинится.
2. **Пустой prompt создаёт задачу** (VERIFIED, воспроизведено). Разбор — раздел 11; семантика
   валидации намеренно не менялась.
3. **Наследие до фикса** — файлы в общей `output/studio/` недоступны владельцу; не удалялись.
4. **Админский Dashboard** считает объём только по своим выходам (0.0 GB), тогда как галерея
   показывает все 6. Косметика, не ошибка доступа.
5. **Лицензия SDXL Turbo** — некоммерческая; для коммерческого использования нужна другая модель.
6. **Реальные пользовательские действия в production не выполнялись** — по условиям задачи.

---

## 10. Автоматические тесты (TESTED)

```
полный набор:   716 passed, 0 failed, 0 skipped
длительность:   82.30 с
coverage:       78.08 % (4849 / 6210 statements)
порог coverage: не менялся
```

Добавлено этой правкой (`tests/test_jobs.py`, +73 строки):

- `test_the_graph_that_runs_writes_under_its_owner` — граф, который реально уходит в движок,
  пишет под владельцем, и папка существует до отправки графа;
- `test_host_job_reports_outputs_scoped_to_their_owner` — `subfolder` совпадает в строке job и
  в таблице outputs.

Оба проверены на откате исправления: падают. Существующие тесты не ослаблены.

Старое поведение (задача без владельца) закреплено независимо:
`test_isolation.py::test_scope_output_is_a_noop_without_an_owner` и обычные `submit(...)` без
`user_id` в `test_jobs.py`.

---

## 11. Пустой prompt: масштаб и минимальное предложение

Проверено офлайн, без запуска задач (`Registry.validate_and_build` на реальных определениях):

```
ACCEPTED  txt2img-turbo          prompt:''     -> prompt: ''
ACCEPTED  txt2img-turbo          prompt:'   '  -> prompt: '   '
ACCEPTED  tts-ru-piper           text:''       -> text: ''
ACCEPTED  asr-json               audio:''      -> audio: ''
ACCEPTED  img-watermark-center   text:''       -> text: ''
```

Причина в `registry.py`: проверка обязательности намеренно пропускает типы `string`/`text`,
поэтому `required: true` на текстовом поле сегодня ничего не значит.

Масштаб (по 127 определениям, read-only):

| ключ | смысл | workflow |
|---|---|---|
| `prompt` | свободный текст | 43 |
| `text` | свободный текст | 9 |
| `negative` | свободный текст | 3 |
| `formats`, `subtitles`, `tags`, `files` | служебный текст | 6 |
| `video` | путь к файлу | 25 |
| `audio` | путь к файлу | 18 |

Итого `required: true` на текстовом поле стоит у **97** workflow (105 пар «workflow — параметр»):
**52** — свободный текст, **42** — путь к уже загруженному файлу (у 4 из них есть ещё
служебное поле), **3** — только служебный текст.

Минимальное решение (не применено, ждёт решения):

- в `validate_and_build` для параметра `required=True` типа `string`/`text` считать значение
  пустым при `str(value).strip() == ""` и отдавать ту же ошибку `required`, что и для остальных
  типов — одна ветка, три строки, без новых полей и миграций;
- `negative` / `formats` / `subtitles` / `tags` не ломаются: это значения по умолчанию; если
  пустое значение там допустимо, у них просто не должно стоять `required: true` (правка
  определения, а не кода);
- риск: 42 workflow с путём в `string` начнут отдавать внятную ошибку «required» вместо падения
  внутри инструмента. Это улучшение, но поведение UI меняется — нужен отдельный прогон и ваше
  согласие.

Альтернатива без правки кода — снять `required` с полей, где пустое значение законно: 97 файлов
против трёх строк.

---

## 12. Code review изменений `9bd06cb`

Проверено: `studio/backend/app/jobs.py` (`submit`, `_run_graph_phase`, `_run_host_phase`,
`_prefix_outputs`, `_reserve_output_dirs`, `_scoped_root`, `_scope_output`),
`studio/backend/tests/test_jobs.py`, `docs/STATUS.md`, `docs/E2E_VALIDATION.md`, скриншоты.

По пунктам задания:

- **Повторное применение префикса** — корректно. `submit()` префиксует сохранённый граф, runner
  пересобирает граф из определения (без префикса) и префиксует заново **один раз**;
  `_prefix_outputs` идемпотентна (проверяет `value.startswith(prefix + "/")`).
- **Корректность outputs в БД** — `_run_host_phase` скоупит список **до** `add_outputs`, поэтому
  строка job (её рисует карточка) и таблица `outputs` (её читает галерея) совпадают.
- **Права на директории** — `_reserve_output_dirs` вызывается до отправки графа, по
  definition-relative префиксам, с проверкой `resolve().relative_to(base)`; выход за дерево
  владельца отбрасывается.
- **Совместимость со старыми задачами** — при `user_id == ""` обе функции no-op, поведение
  legacy-задач не изменилось; покрыто `test_scope_output_is_a_noop_without_an_owner` и обычными
  `submit(...)` без владельца. Строки в БД не переписываются.
- **Изоляция admin/user** — `_media_allowed` не менялся; `test_isolation.py` и
  `test_media_paths.py` зелёные; живые 404/404/401 на стенде повторены после фикса.

Потенциальные вопросы (описаны, **не исправлялись**):

1. `_scope_output` не идемпотентна: если host-инструмент когда-нибудь вернёт `subfolder`, уже
   начинающийся с `users/<id>/`, получится `users/<id>/users/<id>/…`. Сегодня ни один инструмент
   так не делает; страховка — одна строка (`if sub.startswith(prefix + "/")`), по вашему решению.
2. Выходы админа теперь тоже уходят в `users/<admin>/…`, а не в общую папку. Для админа это
   незаметно (ему отдаётся всё), но это изменение поведения, и тестом оно не закреплено.
3. Тест D2 проверяет существование папки, но не её владельца и записываемость — на стенде это
   подтверждено живым прогоном piper, в тесте — только косвенно.
4. `_reserve_output_dirs` создаёт папки до запуска: у отменённых задач могут оставаться пустые
   каталоги. Безвредно.

---

## 13. Сводка статусов

| Пункт | VERIFIED (стенд) | TESTED | NOT VERIFIED |
|---|---|---|---|
| `txt2img-turbo` end-to-end | да | частично (job lifecycle) | production |
| `tts-ru-piper` end-to-end | да | частично | production |
| `img-thumb-640` end-to-end | да | частично | production |
| Изоляция admin/user (404/404/401) | да | да | production |
| Gallery и история | да | да | production |
| Allowance на Dashboard | да | да (`test_dashboard_allowance_contract`) | production |
| D1 — префикс в runner'е | да | да | **production** |
| D1 — outputs строки job | да | да | **production** |
| D2 — root-owned папки | да | да | **production** |
| Остальные 122 workflow | нет | нет | нет |
| Параллельная нагрузка семьи | нет | нет | нет |
| Пустой prompt | да (дефект воспроизведён) | нет | — |

**Production acceptance не проводился.** Фиксы лежат в `main` локально и не работают в
production, пока `comfyui-studio.service` не перезапущен, — это отдельное решение владельца,
в этой задаче перезапуск запрещён.

---

## 14. Скриншоты

```
docs/img/e2e/01-library.png          библиотека workflow
docs/img/e2e/02-txt2img-params.png   параметры txt2img-turbo
docs/img/e2e/03-txt2img-result.png   результат: completed 4.3s + картинка
docs/img/e2e/04-audio-result.png     результат озвучки (плеер)
docs/img/e2e/05-thumb-result.png     результат миниатюры 640 px
docs/img/e2e/06-gallery.png          галерея пользователя
docs/img/e2e/07-jobs.png             история задач
docs/img/e2e/08-dashboard-user.png   Dashboard под user
docs/img/e2e/09-dashboard-admin.png  Dashboard под admin
```
