# End-to-End Validation — семейный сценарий

Проверка проведена на HEAD `9e49722` (frontend-фикс `9aca6f0`), 2026-11-01. Тесты на момент
проверки: **716 passed**, coverage **78.07 %** (4848/6210). Статусы: **PASS** — проверено и
работает, **PARTIAL** — проверено не полностью, **FAIL** — обнаружена проблема,
**NOT TESTED** — не проверялось.

Сценарий целиком: вход → библиотека → карточка workflow → параметры → отправка → статус →
результат → Gallery → история. Всё выполнялось **под рядовым пользователем** (роль `user`),
не под админом.

---

## 1. Как проверялось (и почему так)

Приёмочный стенд — отдельный экземпляр студии на `http://192.168.31.60:8126`:

```
каталог     ~/.hermes/profiles/developer/cache/scratch/e2e
БД          studio/data/studio.db   (своя, пустая)
ComfyUI     http://127.0.0.1:8188   (настоящий, тот же, что у production)
модели      ./models   -> симлинк на реальный каталог моделей
output      ./output   -> симлинк на реальный каталог выходов
пользователи e2e-admin (admin), e2e-user (user), e2e-family (user)
```

Так проверяется настоящая генерация, но production-БД, роли, пароли и systemd не затрагиваются.
Production в это время не перезапускался, его БД не изменялась.

Что НЕ делалось по условиям задачи: моделей не скачивали, миграций не запускали, роли и пароли
production не меняли, GPU scheduler не трогали, `git push` не делали, тяжёлых параллельных
генераций не запускали (все прогоны — по одному, очередь пустая).

---

## 2. Инвентарь 127 workflows (этап 1)

Источник: 127 определений `studio/registry/workflows/*.json` + живой ответ `GET /api/workflows`
под каждой ролью + проверка наличия файлов моделей на диске.

| Показатель | Значение |
|---|---|
| Всего определений | 127 |
| Модели на месте, ждут проверки (`ready_to_test`) | 121 |
| Проверено ранее (`available`) | 1 — `inpaint-turbo` |
| Не хватает моделей (`missing_models`) | 5 |
| Требуют загрузки файла пользователем | 58 |
| Движок host (CPU, инструменты студии) | 82 |
| Движок graph (ComfyUI) | 45 |

По категориям: image-editing 33, video-generation 28, audio-generation 27, image-generation 20,
image-enhancement 9, vision-language 9, characters 1.

### 2.1 Не хватает моделей — эти workflows сейчас не запустятся

| workflow | чего не хватает |
|---|---|
| `cn-depth-sdxl` | `controlnet/controlnet-depth-sdxl-1.0.safetensors`, `vision-models/depth-anything-v2-small.onnx` |
| `cn-dual-canny-depth` | `controlnet/controlnet-depth-sdxl-1.0.safetensors`, `controlnet/controlnet-canny-sdxl-1.0.safetensors`, `vision-models/depth-anything-v2-small.onnx` |
| `sam-bg-replace` | `vision-models/mobilesam.*`, `vision-models/sam-vit-base-*` |
| `sam-object-remove` | `vision-models/mobilesam.*`, `vision-models/sam-vit-base-*` |
| `sam-region-restyle` | `vision-models/mobilesam.*`, `vision-models/sam-vit-base-*` |

Остальные 122 запускаться могут по зависимостям; фактическая генерация проверена на трёх
(раздел 3), остальные — **NOT TESTED** (по условиям задачи не гоняем 127 тяжёлых прогонов).

### 2.2 Требуют загрузки изображения/видео

58 workflows: все `img-*`, `sam-*`, `cn-*`, `inpaint-*`, `outpaint-*`, `img2img-*`,
`vid-*` (кроме чисто генеративных), `asr-*`, `audio-from-video`. Механика загрузки проверена
на `img-thumb-640` (раздел 3.3).

### 2.3 Что подходит для первой проверки

Критерий: модели на месте, загрузка файла не нужна, дешёвый по времени. Первые 14:

```
audio-from-video    host  vram=0  Extract audio track from a video
audio-join          host  vram=0  Join — concatenate audio files
vid-join            host  vram=0  Video — join clips
asr-transcribe      host  vram=0  Transcribe speech — whisper.cpp
audio-slow-down     host  vram=0  Speed — 0.9× slower
audio-speed-up      host  vram=0  Speed — 1.15× faster (no pitch change)
audio-trim-silence  host  vram=0  Cleanup — trim leading/trailing silence
vid-add-audio       host  vram=0  Narrate a clip — mux audio onto video
vid-fps-24/30/60    host  vram=0  Video — convert frame rate
vid-poster          host  vram=0  Video — poster frame
vid-rotate-90/180   host  vram=0  Video — rotate
```

Плюс два «лица» системы: `txt2img-turbo` (8 ГБ VRAM, 3 с) и `tts-ru-piper` (CPU, 0.6 с).

### 2.4 Наиболее полезные для обычного семейного использования

| Задача | workflow | Движок | Время |
|---|---|---|---|
| Быстрая картинка по описанию | `txt2img-turbo`, `format-*`, `style-*` | graph | 3–4 с |
| Иллюстрация в аниме-стиле | `txt2img-animagine`, `style-anime` | graph | ~10 с |
| Озвучка русского текста | `tts-ru-piper` | host | <1 с |
| Миниатюра/сжатие для отправки | `img-thumb-320/640`, `img-resize-1280`, `img-convert-webp` | host | <1 с |
| Улучшить старое фото | `upscale-realesrgan`, `img-detail-2x` | graph | 5–15 с |
| Убрать лишнее с фото | `inpaint-remove`, `inpaint-turbo` | graph | 10–20 с |
| Вырезать фон | `cutout-u2net`, `sam-cutout`* | host | 1–3 с |
| Расшифровать голосовое | `asr-transcribe` | host | ~0.1× длины |
| Собрать видео из клипов | `vid-join`, `vid-add-audio` | host | секунды |
| Оживить кадр | `video-t2v-wan22` | graph | десятки секунд |

\* `sam-*` с заменой фона требуют моделей (см. 2.1); `sam-cutout` без замены — на месте.

---

## 3. End-to-end прогоны (этап 2)

### 3.1 `txt2img-turbo` — граф ComfyUI, картинка

| Шаг | Результат |
|---|---|
| Открытие карточки из библиотеки | PASS — `#/library`, клик по `article.wf-card[data-id]` |
| Отображение параметров | PASS — prompt, negative, width, height, steps, cfg, batch_size, seed |
| Генерация `client_token` | PASS — валидный UUID v4, на insecure-context через `randomId()` |
| `POST /api/jobs` | PASS — 200 за **95 мс** |
| Создание job | PASS — `queued` → `running` → `completed` |
| Отображение статуса | PASS — прогресс, длительность, `completed 100 %` |
| Получение результата | PASS — PNG отрисован в карточке, `naturalWidth 1024` |
| Gallery | PASS — появился в галерее |
| История | PASS — строка в `#/jobs` со статусом и временем |

Модель: `checkpoints/sd_xl_turbo_1.0_fp16.safetensors` (6.46 ГБ). Лицензия **CC BY-NC-SA 4.0** —
только некоммерческое использование, о чём карточка честно предупреждает.

Замеры: холодный первый прогон **11 с** (загрузка модели в VRAM), дальше **3–4 с** на 1024²,
4 шага; 768² / 6 шагов — **3 с**.

### 3.2 `tts-ru-piper` — host-инструмент, звук

| Шаг | Результат |
|---|---|
| Карточка и параметры | PASS — text (textarea), voice (select), length_scale |
| Submit и создание job | PASS |
| Результат | PASS — WAV в `<audio>`, `readyState 4`, длительность 4.6 с |
| Gallery / история | PASS |

Модель: `rhasspy/piper-voices ru_RU-irina-medium` (MIT), CPU, `no GPU`. Время: **0.6–0.9 с**,
RTF 0.045.

### 3.3 `img-thumb-640` — host-инструмент с загрузкой файла

| Шаг | Результат |
|---|---|
| Загрузка через штатный виджет (`input[type=file]`) | PASS — файл появился в select как `<user>_<ts>_<hash>_test_upload.png` |
| Submit | PASS |
| Результат | PASS — JPEG 640×427 в карточке |
| Gallery / история | PASS |

Моделей нет. Время: **0.5 с**. Загруженный файл попадает в `input/` с префиксом пользователя —
чужой файл подставить нельзя (проверено отдельно, раздел 4).

---

## 4. Роли: Admin и User (этап 3)

Child **не активировался** (по условиям — нужно отдельное подтверждение).

| Проверка | admin (`e2e-admin`) | user (`e2e-family`) |
|---|---|---|
| Вход | PASS | PASS |
| Доступные workflows | 127 | 127 |
| Доступные модели | 12 файлов, 44.92 ГБ | те же 12 |
| Свои задачи | видит все (6) | только свои (4) |
| Свои результаты | Gallery 6 (все) | Gallery 4 (только свои) |
| Dashboard allowance | «Runs today 0 · no limit», `0 of 3 slots busy` | «Runs today 3 · no limit», `0 of 3 slots busy` |

Граница владения (HTTP, не состояние UI):

```
user  -> чужой /api/jobs/<id>           404
user  -> чужой /api/outputs/...         404
user  -> свой  /api/outputs/...          200
admin -> тот же чужой job                200
аноним -> /api/jobs                      401
```

Замечания к ролям:

- `PARTIAL`: `/api/me/usage` для админа считает только его собственные выходы (0), тогда как
  Gallery показывает все 6. Не ошибка доступа, но на Dashboard админ видит «Output space used
  0.0 GB» при непустой галерее — расхождение формулировки.
- Рядовому `user` доступны все 127 workflows — это соответствует политике (ограничения
  задуманы для `child`), но стоит помнить, что роль `user` = «взрослый, можно всё».

---

## 5. Производительность (этап 4)

Замеры на 96 ГБ VRAM, `GPU_MAX_CONCURRENT=1`, очередь пустая.

| Величина | Значение |
|---|---|
| Время ответа `POST /api/jobs` | **95 мс** |
| Время до начала выполнения (очередь) | **0 с** (все прогоны) |
| Генерация turbo 1024²/4 шага | **11 с** первый, **3–4 с** далее |
| Генерация turbo 768²/6 шагов | **3 с** |
| TTS piper | **0.6–0.9 с** |
| Thumbnail 640 | **0.5 с** |
| Появление результата в UI | **2.4–5.2 с** после Run |
| Занято RAM (пик) | **11.3 ГБ** |
| Свободно RAM (минимум) | **19.9 ГБ** |
| RSS ComfyUI (пик) | **5.4 ГБ** |
| VRAM свободно во время прогонов | 88.7 / 96 ГБ |
| Ошибки ComfyUI | **нет** — 37 строк лога за окно тестов, ни traceback, ни error |

---

## 6. Найденные дефекты

### 6.1 FAIL (исправлено) — результат писался в общую папку, а не владельцу

`app/jobs.py`. `submit()` переписывает `filename_prefix` в графе на `users/<id>/…` и сохраняет
этот граф в job, но runner (`_run_graph_phase`) пересобирает граф из `params` **заново**, и
префикс терялся. Файл падал в общий `output/studio/`, а `_media_allowed` отказывает в отдаче
файла из общей папки её же владельцу: job «completed», в галерее битая картинка.

Почему не всплывало раньше: все живые проверки до этого шли под админом, а админу отдаётся всё.

Исправление: префикс владельца применяется и в runner'е. Тест
`test_the_graph_that_runs_writes_under_its_owner` (проверен на откате — падает).

### 6.2 FAIL (исправлено) — host-инструменты не могли писать в свою же папку

ComfyUI в контейнере работает как root и первым создаёт `output/users/<id>/studio` root-овым;
host-инструменты (piper, thumbnail, ffmpeg) работают от обычного пользователя и получали
`[Errno 13] Permission denied: cannot reserve output name` — job падал.

Исправление: `_reserve_output_dirs()` создаёт папки выхода от лица студии **до** отправки графа,
так что root ничего нового не создаёт.

### 6.3 FAIL (исправлено) — run-view показывал битый плеер при рабочей галерее

`jobs.outputs` (JSON на строке задачи — из него рисуется карточка) хранился без префикса
владельца, тогда как таблица `outputs` (её читает галерея) — с префиксом. Итог: галерея играет,
а плеер в карточке задачи запрашивает `subfolder=studio` и получает 404.

Исправление: пути скоупятся один раз, в `_run_host_phase`, и в БД, и в строку задачи. Тест
`test_host_job_reports_outputs_scoped_to_their_owner` (проверен на откате — падает).

### 6.4 Открыто — пустой prompt создаёт задачу

`requestSubmit()` с пустым `prompt` создаёт job (она честно завершилась картинкой). UI помечает
поле как обязательное, но `registry.py` намеренно не применяет проверку «обязательности» к
типам `string`/`text`, поэтому сервер пустую строку принимает.

Не исправлено: правка меняет семантику валидации для 127 определений и требует отдельного
решения. Предложение: для параметров с `required: true` типов `text`/`string` отклонять пустую
строку после `strip()`, кроме случаев, где пустое значение осмысленно (negative prompt —
у него не должно быть `required`).

### 6.5 Открыто — артефакты прогонов до фикса

Два ранних выходных файла лежат в общей папке `output/studio/` (`turbo_00008_.png`,
`turbo_00009_.png`) — владелец видит их как битые миниатюры, потому что файл вне его дерева.
Файлы тестовые; удалить их может владелец каталога. Новые прогоны пишут только в
`output/users/<id>/`.

---

## 7. Скриншоты

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

---

## 8. Ответы на вопросы

**1. Какие workflows реально работают от начала до конца.** Проверены три: `txt2img-turbo`
(граф ComfyUI), `tts-ru-piper` (host, звук), `img-thumb-640` (host + загрузка файла). По
зависимостям могут работать 122 из 127, из них 58 требуют загрузки файла. Остальные 5 не
запустятся без моделей (см. 2.1).

**2. Какие модели используются.** `sd_xl_turbo_1.0_fp16.safetensors` (6.46 ГБ, CC BY-NC-SA —
некоммерческая); piper-голос `ru_RU-irina-medium` (MIT, CPU); thumbnail — без моделей, PIL.
Всего на диске 12 файлов моделей, 44.92 ГБ.

**3. Сколько времени занимает генерация.** Картинка 1024² — 3–4 с (первый раз 11 с),
768² — 3 с; озвучка — 0.6–0.9 с; миниатюра — 0.5 с; результат в UI — через 2.4–5.2 с после
нажатия Run. Очередь не создаётся вовсе при одиночных запусках.

**4. Работает ли Gallery и история.** Да, для обеих ролей: галерея показывает только свои
выходы у `user` и все у `admin`, история хранит статус, прогресс и длительность. До фиксов
миниатюры в галерее и плеер в карточке задачи были битыми — это устранено.

**5. Есть ли проблемы с Admin/User.** Изоляция держится (404/404/401 на чужие объекты), различие
allowance корректно. Осталась косметика: админский Dashboard считает объём только по своим
выходам при полной галерее.

**6. Насколько Studio готова к ежедневному семейному использованию.** После этих трёх фиксов —
**готова для базового сценария**: «картинка по описанию», «озвучка», «миниатюра/сжатие фото»,
просмотр результата и истории под обычным пользователем, без вмешательства админа. Ограничения:
5 workflows без моделей, пустой prompt не отсекается, production ещё работает на старом коде и
требует рестарта сервиса, чтобы фиксы заработали для семьи.

---

## 9. Что изменилось в коде

```
studio/backend/app/jobs.py                  префикс владельца в runner'е; _reserve_output_dirs;
                                            скоупинг outputs для строки задачи
studio/backend/tests/test_jobs.py           +2 теста (оба падают на откате фикса)
docs/E2E_VALIDATION.md                      этот документ
docs/img/e2e/*.png                          9 скриншотов
```

Production не изменялся: сервис `comfyui-studio.service` активен на MainPID 1813280, БД
(140 jobs / 156 outputs / 3 users / 3 sessions, integrity ok, schema v3) не тронута, миграций и
push не было, роли и пароли не менялись, модели не скачивались.
