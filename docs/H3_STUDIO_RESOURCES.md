# H3 Studio — что установлено и откуда скачано

Манифест ресурсов сборки H3 Studio (MiniMax H3) в
`~/workspace/comfyui-strix-halo`.

Дата сборки: 2026-11-04.
Окружение: ComfyUI 0.44.0, PyTorch 2.9.1+rocm, ROCm 7.2, AMD Ryzen AI Max+ 395
(gfx1151, 96 GiB выделено под GPU), образ `comfyui-gfx1151:h3-v0.44.0`.

---

## 1. Образ и базовые компоненты

| Компонент | Версия / источник | Примечание |
|---|---|---|
| Базовый образ | `comfyui-gfx1151:v0.2` (существовал) | не пересобирался, использован как основа |
| Новый образ | `comfyui-gfx1151:h3-v0.44.0` | тонкий слой поверх базы |
| ComfyUI | 0.44.0 (`/opt/ComfyUI`) | поднят с 0.15.0 до версии, которая содержит ноды MiniMax H3 |
| PyTorch | 2.9.1+rocm7.2 (ROCm) | **ROCm-сборка сохранена**, CUDA-колёса не ставились |
| Docker-файл | `Dockerfile.h3` в корне проекта | слой: git clone ComfyUI v0.44.0 + pip install requirements |
| Compose | `compose.yaml` (бэкап: `compose.yaml.bak-h3`) | образ переопределяется переменной `COMFY_IMAGE` |
| Порт | `127.0.0.1:8188` | фактический порт (в ТЗ упоминался 8000) |

Почему 0.44.0: H3-ноды (`MiniMaxH3LatentUpscaler3D`, `BlockSparseAttention`,
`LTXVConcatAVLatent` и др.) появились в ядре ComfyUI после 0.15. Обновили только
ядро и его зависимости, PyTorch/ROCm оставили нетронутыми (проверено в сборке:
`assert 'rocm' in torch.__version__`).

---

## 2. Custom nodes

Все — git clone в `~/workspace/comfyui-strix-halo/custom_nodes/`,
монтируются в контейнер как `/opt/ComfyUI/custom_nodes`.

| Пакет | Источник | Зачем |
|---|---|---|
| ComfyUI-CGlide | https://github.com/CGlide/ComfyUI-CGlide | H3 Studio: `CSGlideCastCS`, `CSGlideVideoCS`, `CSGlideJoinCS`, `CSGlidePreviewCS`, `CSGlideH3DiffLoraCS`, `CSGlideUpscaleHoldCS` — авторская обвязка H3 (промпт, референсы, сборка клипов) |
| Comfyui_Minimax_h3_latent_Upscaler | https://github.com/LBH-123-AI/Comfyui-Latent-Upscale | `MinimaxH3LatentUpscaler3D` — latent-upscale 3D-свёрткой (шаг 2K) |
| ComfyUI-KJNodes | https://github.com/kijai/ComfyUI-KJNodes | вспомогательные ноды автора (модельные утилиты) |
| ComfyUI-GGUF | https://github.com/city96/ComfyUI-GGUF | загрузка GGUF (на будущее; в текущих workflow не используется) |
| h3_gpu_clip_loader | локальный код: `custom_nodes/h3_gpu_clip_loader/` (не git-репозиторий) | нода `H3ClipLoaderGPU` («H3 CLIP Loader (GPU)»): грузит Qwen3-VL 32B FP8 прямо в VRAM, обходя CPU-аллокацию 24,7 ГиБ — см. 2.1 |

Дополнительно в образ установлены pip-пакеты, без которых KJNodes не импортируется:
`color-matcher`, `mss`, `opencv-python-headless`.

Проверка: все 4 пакета импортируются без ошибок (лог контейнера), в `/object_info`
доступны 39 нод MiniMax H3 и все ноды CGlide.

### 2.1 H3 CLIP Loader (GPU) — зачем нужен

Штатный `CLIPLoader` в 0.44.0 читает safetensors через `load_torch_file`, который не
принимает device (`comfy/sd.py:1699` → `comfy/utils.py:193`), поэтому весь state dict
(24,7 ГиБ) сначала создаётся в анонимной RAM. На Strix Halo с его 30 ГиБ RAM и
96 ГиБ VRAM это OOM-kill до начала генерации.

Нода `H3ClipLoaderGPU` (`custom_nodes/h3_gpu_clip_loader/__init__.py`):

- читает `safetensors.safe_open(..., framework="pt", device="cuda")` — тензоры попадают сразу в VRAM;
- читает порциями по 1 ГиБ, переоткрывая файл: пока handle открыт, внутренний буфер
  safetensors растёт вместе с числом прочитанных тензоров (на этом файле — до 24,7 ГиБ
  анонимной памяти). Порции держат пик на уровне 1,86 ГиБ;
- служебные ключи `*comfy_quant` (JSON-блобы, не веса) оставляет на CPU, потому что
  `comfy/ops.py:1287,1661,1774` вызывает для них `.numpy()`;
- отдаёт state dict в штатный `comfy.sd.load_text_encoder_state_dicts` с
  `load_device=cuda`, `initial_device=cuda`, `offload_device=cpu`, `CLIPType.MINIMAX`;
- fp8 не пересчитывает: все 467 квантованных модулей остаются `float8_e4m3fn`.

Замеры (отдельный процесс в контейнере, файл `qwen3vl_32b_minimax_h3_fp8_scaled.safetensors`):

| Показатель | Штатный CLIPLoader | H3ClipLoaderGPU |
|---|---|---|
| пик анонимной RAM | 24,7 ГиБ (OOM-kill) | 1,86 ГиБ |
| пик RSS (anon + file) | ~26 ГиБ | 2,36 ГиБ |
| время чтения | ~62 с | 10,4 с |
| VRAM после загрузки | не доходило | 26,8 ГиБ |
| сохранность весов | — | fp8 сохранён; 1369/1369 тензоров совпали (902 обычных побитово, 467 fp8 по payload) |

Сверка весов: `scripts/h3_te_identity_check.py`; замер пика памяти: `scripts/h3_te_peak_trace.py`.

Использование: в workflow заменить ноду `CLIPLoader` на `H3 CLIP Loader (GPU)`
(входы: файл энкодера, тип `minimax`; выхода — `CLIP`, как у штатной).

---

## 3. Модели

Каталог на хосте: `~/workspace/comfyui-strix-halo/models/`
(внутри контейнера — `/opt/ComfyUI/models/`).
Скачивание: `scripts/h3_download.sh` (идемпотентный, умеет докачивать).

### 3.1 Основные (нужны для FAST и QUALITY)

| Файл | Размер | Назначение | Источник |
|---|---|---|---|
| `diffusion_models/minimax_h3_fl2va_pruned_fp8_scaled.safetensors` | 19.52 GiB | основная модель H3 (first-last-frame → video+audio), fp8-квантование | https://huggingface.co/Comfy-Org/MiniMax-H3 |
| `text_encoders/qwen3vl_32b_minimax_h3_fp8_scaled.safetensors` | 24.71 GiB | текст-энкодер Qwen3-VL-32B в fp8 (понимает промпт и референсные картинки) | https://huggingface.co/rzgar/qwen3-vl-32b-minimax-h3-fp8-comfyui |
| `vae/minimax_h3_video_vae_fp16.safetensors` | 4.85 GiB | видео-VAE (декод латентов в кадры) | Comfy-Org/MiniMax-H3 |
| `vae/minimax_h3_audio_vae_fp32.safetensors` | 0.56 GiB | аудио-VAE (звук генерируется вместе с видео) | Comfy-Org/MiniMax-H3 |
| `loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors` | 1.82 GiB | turbo-LoRA на 4 шага, обучена под 768p → режим FAST | Comfy-Org/MiniMax-H3 |
| `loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` | 1.82 GiB | turbo-LoRA на 8 шагов → режим QUALITY | Comfy-Org/MiniMax-H3 |

### 3.2 Редактирование и 2K

| Файл | Размер | Назначение | Источник |
|---|---|---|---|
| `diffusion_models/minimax_h3_ref2va_pruned_fp8_scaled.safetensors` | 19.52 GiB | модель reference-to-video: держит персонажа по референсам (EDITING, 2K) | Comfy-Org/MiniMax-H3 |
| `loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors` | 1.82 GiB | turbo-LoRA на 4 шага для ref2v-модели | Comfy-Org/MiniMax-H3 |
| `latent_upscale_models/minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors` | 0.64 GiB | latent-upscaler (3D conv) для перехода к 2K | https://huggingface.co/Efficient-Large-Model/Open-H3-Regenerate-2K |
| `loras/open_h3_regenerate_2k_lora.safetensors` | 11.60 GiB | LoRA Open-H3 Regenerate 2K: дорисовка деталей на финальном проходе | Efficient-Large-Model/Open-H3-Regenerate-2K |
| `loras/sharp_curve_branches.safetensors` | 1.15 GiB | вспомогательная LoRA автора (резкость/кривые на 2K-проходе) | Efficient-Large-Model/Open-H3-Regenerate-2K |

### 3.3 Preview

| Файл | Размер | Назначение | Источник |
|---|---|---|---|
| `vae_approx/taeh3.safetensors` | ~10 MiB | TAE — быстрый аппроксимирующий декодер для живого превью в ноде `CSGlidePreviewCS` | https://huggingface.co/Kijai/MiniMax-H3-TAE |

### 3.4 Апскейлер (не H3, отдельный workflow)

| Файл | Размер | Назначение | Источник |
|---|---|---|---|
| `upscale_models/real_esrgan_x4plus.pth` | 64 MiB | RealESRGAN x4+ для `H3_STUDIO_4_UPSCALER` (уже был в системе) | https://github.com/xinntao/Real-ESRGAN |

### 3.5 Что НЕ скачивалось и почему

| Ресурс | Причина |
|---|---|
| `diffusion_models/minimax_h3_fl2va_bf16` (61.7 GiB), `_pruned_bf16` (37.5 GiB) | избыточны: fp8_scaled даёт то же качество при вдвое меньшем весе и влезает в 96 GiB вместе с TE |
| `text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq` (14.6 GiB) | требует FP4-инструкций, которых нет в ROCm-сборке PyTorch |
| `diffusion_models/minimax_h3_fl2va_int8_convrot`, `_w6a8` | альтернативные кванты под CUDA-ядра; на ROCm fp8 работает нативно |
| `model_patches/minimax_h3_fun_controlnet_union*` | ControlNet для H3 — отдельная тема, в задаче не запрашивался |
| `loras/pika_h3_flex_*` | сторонние LoRA другого пайплайна |
| `embeddings/minimaxh3_*` (10 шт.) | готовые текстовые эмбеддинги эффектов; не нужны для регенерации по фото, можно добавить позже |
| Gumroad-материалы (glide8) | платный доступ, не обходился |

---

## 4. Workflow

Директория: `~/workspace/comfyui-strix-halo/workflows/h3-studio/`
(внутри контейнера — `/opt/ComfyUI/user/default/workflows/h3-studio/`,
поэтому они видны в меню **Workflows** в UI).

| Файл | Режим | Разрешение | Кадры | Шаги | LoRA |
|---|---|---|---|---|---|
| `H3_STUDIO_1_FAST.json` | fl2va | 768×448 | 121 | 4 | fl2v_turbo_4step |
| `H3_STUDIO_2_QUALITY.json` | fl2va | 1344×768 | 294 | 8 | fl2v_turbo_8step |
| `H3_STUDIO_3_2K_FINAL.json` | ref2va → upscale → refine | 1344×768 → 2K | 294 | 4 + 3 | ref2v + open_h3_regenerate_2k |
| `H3_STUDIO_4_UPSCALER.json` | без H3 (RealESRGAN) | x4 → 2048 px | — | — | — |
| `H3_STUDIO_5_EDITING.json` | ref2va | 960×544 | 121 | 4 | ref2v_turbo_4step |

Основа: авторские workflow из `custom_nodes/ComfyUI-CGlide/example_workflows/`
(`Minimax_h3_Studio.json` и `Minimax_h3_Studio_Regenerate_2K.json`).

Что изменено относительно авторских файлов:

1. Имена файлов моделей приведены к тем, что реально есть в репозиториях
   (у автора — CUDA-кванты int8/nvfp4 и версии LoRA 8-step v1.1).
2. Удалены CUDA-only ноды `ModelAttentionBackend` и `ModelPatchTorchSettings`
   (на ROCm не существуют; цепочка модели перекоммутирована напрямую).
3. Удалены клиентские ноды `SetNode`/`GetNode` (требовали расширения
   Custom-Scripts) — связи перенаправлены напрямую.
4. Добавлена нода `Load Image` (вход первого кадра) в FAST, QUALITY, EDITING, 2K.
5. Windows-пути в LoRA-нодах (`MiniMaxH3\sharp_curve...`) заменены на чистые имена.
6. Кодек видео переключён на `H.264 (compatible)` — гарантированно играется везде
   (авторский H.265 4:4:4 10-bit на AMD-энкодере может не подняться).
7. Группы переименованы на русский, добавлены пояснительные заметки:
   INPUT / PROMPT / GENERATION SETTINGS / PREVIEW / OUTPUT.

Регенератор workflow: `scripts/h3_make_workflows.py` (пересобирает все 5 файлов
из авторских и проверяет, что все типы нод существуют в запущенном ComfyUI).

---

## 5. Скрипты

| Файл | Что делает |
|---|---|
| `scripts/h3_download.sh` | качает модели в `_h3_dl/`, потом раскладывает по `models/` (через `docker cp`, т.к. каталоги принадлежат root). Идемпотентен: уже скачанное пропускает, `.part` докачивает |
| `scripts/h3_make_workflows.py` | собирает 5 workflow + валидирует типы нод по `/object_info` |
| `scripts/h3_api_test.py` | конвертирует workflow в API-формат, ставит в очередь, ждёт результат (используется для проверки) |
| `scripts/h3_finish.sh` | ждёт завершения загрузки и прогоняет проверку FAST/QUALITY в `logs/h3_verification.log` |
| `scripts/h3_te_gpu_probe.py` | пробное чтение энкодера прямо в VRAM с замером RSS/VRAM и стражем по памяти |
| `scripts/h3_te_peak_trace.py` | загрузка энкодера через ноду с замером пиковой анонимной памяти |
| `scripts/h3_te_identity_check.py` | сверка весов файл ↔ модель: ключи, shape, dtype, `torch.equal` по всем 1369 тензорам |
| `scripts/h3_te_node_test.py`, `scripts/h3_read_variants.py` | вспомогательные замеры (сравнение способов чтения, dtype, устройства параметров) |

Логи: `logs/h3_download.log`, `logs/docker_build*.log`, `logs/h3_verification.log`.

---

## 6. Статус проверки

| Проверка | Результат |
|---|---|
| Контейнер поднят на 0.44.0 | ✅ |
| ROCm-torch сохранён (2.9.1+rocm7.2) | ✅ |
| 4 custom-пакета импортируются | ✅ |
| 39 нод MiniMax H3 + все ноды CGlide в `/object_info` | ✅ |
| Все 5 workflow валидны по типам нод | ✅ |
| API принимает граф (осталась только проверка наличия файлов моделей) | ✅ |
| Апскейлер: полный цикл генерации 2048×2048 | ✅ (29.5 с) |
| Генерация H3 (FAST) | ⏳ ожидает загрузки моделей |
| Нода `H3ClipLoaderGPU` написана и проверена изолированно (RAM 1,86 ГиБ, веса 1369/1369) | ✅ |
| Регистрация ноды в запущенном ComfyUI (`/object_info`) | ⏳ нужен перезапуск контейнера |

Ограничения на момент сборки:

- Полный цикл H3 не прогнан: качается ~90 GiB моделей, к моменту отчёта
  загружена часть (см. `logs/h3_download.log`). Проверка запустится
  автоматически после загрузки (`scripts/h3_finish.sh`).
- `BlockSparseAttention` (2K) — нода из ядра ComfyUI с Triton-ядром `sol-attn`;
  на ROCm может не подняться. Если 2K-проход упадёт на ней — переключить
  backend в ноде или отключить её (bypass), качество upscale от этого не страдает.
- TAE-превью зависит от `taeh3.safetensors`; при его отсутствии превью
  работает медленнее (обычный декод), генерация не ломается.
