# Stage S — итог (MobileSAM + RealESRGAN x4plus + Whisper large-v3)

Статус: **установлено, подключено, прогнано живьём**. Всё ниже — измеренные факты, не оценки.

Одобренная область: MobileSAM ONNX, RealESRGAN x4plus, Whisper large-v3 q5_0. **ViT-B и Moondream2 не
скачивались** — они остаются неодобренными.

## Что установлено

| Файл | Размер | sha256 | Кто использует |
|---|---|---|---|
| `studio/tools/vision-models/mobilesam.encoder.onnx` | 28 195 125 B | `4125037c5e24d6ea58e201b20e8d8fbbbd1135c0b881e34a8074b8c4f07e6918` | 6 SAM-workflow |
| `studio/tools/vision-models/mobilesam.decoder.onnx` | 16 514 086 B | `b0735abf07c7affddf20fffc3ce750f44af387ee6a7323880e909389ed15d279` | те же 6 |
| `models/upscale_models/real_esrgan_x4plus.pth` | 67 040 989 B | `4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1` | 3 upscale-workflow |
| `studio/tools/whisper-models/ggml-large-v3-q5_0.bin` | 1 081 140 203 B | `d75795ecff3f83b5faa89d1900604ad8c780abd5739fae406de19f23ecd98ad1` | 2 ASR-workflow |

Пара MobileSAM взята из `PulpCut/mobilesam-onnx` как самосогласованный набор ViT-T (экспорт кодера и
декодера должны совпадать; смешивать половины из разных репозиториев нельзя). Это **+44.7 MB** сверх
одобренной области: файл `nnny/onnx-mobile-sam/mobile_sam.onnx` (16 509 653 B), который и был в
предложении, содержит только декодер (`image_embeddings [1,256,64,64]`) и в одиночку не работает.
Старый файл остался на диске как балласт, ни один workflow на него не ссылается.

## Workflow: 12 новых, все прогнаны

| id | Что делает | Время живого прогона |
|---|---|---|
| `sam-cutout` | вырезает объект по точке, прозрачный фон | 2.4 s |
| `sam-mask` | маска объекта в оттенках серого | ~2 s |
| `sam-bg-blur` | объект резкий, фон размыт (`blur`, px) | 5.5 s |
| `sam-overlay` | превью маски поверх картинки | ~2 s |
| `sam-object-remove` | маска → InvertMask → inpaint (SDXL-Turbo) | 7.2 s |
| `sam-bg-replace` | объект сохраняется, фон перерисовывается | 6.8 s |
| `upscale-x4plus` | Real-ESRGAN ×4 | 41.0 s (первая загрузка модели), затем 1.7 s |
| `img-detail-2x-x4plus` | ×4 → area-shrink до ×2 | ~1.2 s |
| `img-detail-3x-x4plus` | ×4 → area-shrink до ×3 | ~1.2 s |
| `asr-large` | Whisper large-v3, русский | 11.7 s на 2.3 s речи |
| `asr-translate-en` | Whisper с `-tr` (перевод в английский) | 11.9 s |
| `vid-burn-subs` | вжигание субтитров в видео (`font_size`, `crf`) | 0.5 s на клип 640×360 / 4.0 s |

Реестр теперь отдаёт **116** определений (было 104). `asr-large` на реальном русском клипе вернул
корректный текст — ради этого и брали large-v3 вместо `small`.

## Дефекты, найденные прогоном (не чтением кода)

1. **Ложный `VERIFIED` при установке.** `models/upscale_models` принадлежит root, `mv` падал с EACCES,
   а скрипт печатал «VERIFIED», потому что проверял хеш, но не код возврата `mv`. Файл оставался в
   `studio/cache/downloads`, ComfyUI его не видел. Исправлено: установка через `docker cp` + `chown`,
   и в оба download-скрипта добавлена проверка самого файла в месте назначения (существует + размер).
2. **Цепочки SAM не запускались:** `missing_models` — ONNX-пара не является моделью ComfyUI. Введена
   конвенция `host:studio/tools/vision-models` (inventory её уже понимает), цепочки стали `ready_to_test`.
3. **Полярность маски в inpaint.** `LoadImage` читает MASK как `1 − alpha`, поэтому cutout (объект
   непрозрачен) давал маску ФОНА: `sam-object-remove` фактически перерисовывал фон, оставляя объект.
   Добавлен `InvertMask` для remove; bg-replace оставлен без него — там это и нужно.
4. Два параметра (`blur`, `font_size`/`crf`) были привязаны к псевдо-узлу `host`, чего в графе нет —
   ловилось тестом `test_every_bind_target_exists_in_its_graph`. Убрана привязка: параметры без `bind`
   уходят в host-инструмент, что и подтверждено (`font_size=28` дал файл 125 048 B против 104 331 B).

## Чего не сделано и почему

В предложении было **45** именованных workflow (`8 + 8 + 9 + 8 одиночных + 12 цепочек`). Доставлено
**12**, и это не «недоделали», а следствие сужения области:

* **17 отпали вместе с моделями** — 8 под SAM ViT-B и 9 под Moondream2 (обе не одобрены).
* часть из 12 цепочек тоже требует Moondream2 или ViT-B (caption→…, moondream detect→…), то есть
  нереализуема в одобренном наборе;
* оставшиеся одиночные workflow частично дублировали то, что в реестре уже было (об этом прямо
  сказано в `docs/stage_s_proposal.md`, раздел про RealESRGAN).

Итог: 4 host-инструмента SAM (`sam-cutout`, `sam-mask`, `sam-bg-blur`, `sam-overlay`), 2 цепочки
(`sam-object-remove`, `sam-bg-replace`), 3 апскейла (`upscale-x4plus`, `img-detail-2x/3x-x4plus`),
2 ASR (`asr-large`, `asr-translate-en`) и один новый host-инструмент без модели (`vid-burn-subs`).

Ограничения качества, которые честнее назвать сразу:

* `sam-object-remove` работает механически правильно, но заливка на 6 шагах SDXL-Turbo грубая, а
  маска по одной точке не покрывает хвост/конечности — нужен многоточечный промпт (`--points`).
* `asr-translate-en` на коротких фрагментах врёт: `-tr` переводит по кускам без контекста («проверка
  локального синтеза речи, студия…» → «Checking the local Sinti-Zarechi»). Для перевода лучше брать
  `asr-large` и переводить текст, а не полагаться на `-tr`.

## Тесты

`./studio/.venv/bin/python -m pytest studio/backend/tests -q` → **177 passed, 85 warnings in 11.92 s**.
Из них новое: `test_model_drift.py` (11), `test_hosttools.py` +2 (нормализация каналов/частоты в
склейке аудио), `test_preflight_options.py` +3 (лимит 512 px/сторона в outpaint, предупреждение о
коротком списке тегов ACE-Step).
