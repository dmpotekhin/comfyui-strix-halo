# PROJECT LOG — comfyui-strix-halo

Проект: локальная AI-студия на ASUS ROG Z13 (Ryzen AI Max+ 395, Strix Halo, gfx1151, 128 GB unified).
Принцип: полностью локальная обработка, никаких облачных AI-сервисов, поэтапная работа с подтверждениями.

---

## 2026-10-26 01:00 — Старт. Протокол §3

**Действие:** задан обязательный вопрос о настройке BIOS.
**Ответ пользователя:** «96gb gpu» — в BIOS выделено 96 GB под встроенный GPU.
**Подтверждено фактами системы:** `mem_info_vram_total` = 103 079 215 104 B = 96.0 GiB.

**Вывод:** значение BIOS подтверждено из ОС, менять прошивку не требуется и не планируется.

---

## 2026-10-26 01:04 — ЭТАП 1: диагностика AMD GPU (УРОВЕНЬ A, только чтение)

**Назначение:** подтвердить, что GPU определяется системой, и зафиксировать фактическое состояние стека.
**Изменения системы:** нет. Создан только каталог проекта и файлы журнала (УРОВЕНЬ B).
**Побочные эффекты:** нет.
**Способ проверки:** повторный запуск тех же команд; сырые данные — `diagnostics/stage1-gpu.txt`.
**Восстановление:** не требуется (ничего не изменялось).

### Выполненные команды (все read-only)
`lsb_release -a`, `uname -r/-m`, `lscpu`, `free -h`, `df -h`, `lspci -nn`, `ls /dev/dri`,
`lsmod`, `id`, `command -v ...`, `docker --version`, `docker compose version`, `systemctl is-active docker`,
`cat /sys/class/drm/card1/device/mem_info_*`, `cat /proc/cmdline`, `vulkaninfo --summary`,
`journalctl -k -b | grep amdgpu`, `/sys/class/kfd/kfd/topology/nodes/*/properties`,
`getfacl`, `dpkg -l`, `docker ps -a`, `docker images`, `docker system df`, `ss -ltnp`, `docker info`.

### Факты
| Параметр | Значение |
|---|---|
| ОС | Ubuntu 26.04.1 LTS (resolute) |
| Ядро | 7.0.0-38-generic x86_64 |
| CPU | AMD Ryzen AI Max+ 395 w/ Radeon 8060S, 32 потока / 16 ядер, 1 NUMA node |
| RAM хоста | 30 GiB (96 GiB отдано iGPU), swap 8 GiB, доступно 23 GiB |
| Диск | / = 937 G, занято 82 G, свободно 809 G (10%) |
| GPU PCI | 1002:1586 Strix Halo (rev c1), audio 1002:1640 |
| GPU driver | amdgpu 3.64.0, CU 40, инициализирован на minor 1 (card1) |
| VRAM | 96.0 GiB (занято 1.5 GiB рабочим столом) |
| GTT | 15.2 GiB (auto; `amdgpu.gttsize` не задан) |
| KFD | node1: gfx_target_version 110501 (= gfx1151), simd_count 80, device_id 5510 |
| Vulkan | RADV «Radeon 8060S Graphics (RADV STRIX_HALO)», Mesa 26.0.8, API 1.4.335 (device) / 1.4.341 (loader), conformance 1.4.0.0 |
| Vulkan fallback | llvmpipe (CPU) присутствует как GPU1 |
| Права на GPU | ACL `user:dmitry:rw` на /dev/kfd и /dev/dri/renderD128 → доступ без смены групп |
| ROCm / HIP | НЕ установлены: нет /opt/rocm, нет libhsa-runtime64, нет rocm-smi/rocminfo |
| PyTorch | не установлен в системном python |
| Docker | 29.1.3 + Compose v2.40.3 (plugin), сервис active/enabled, пользователь в группе docker |
| Docker runtime | только runc (нет rocm/nvidia runtime) → GPU придётся пробрасывать как устройства |
| Docker MemTotal | 32.7 GB |
| Контейнеры | нет ни одного; порт 8188 свободен |
| Образы Docker | 6 шт. (2.7 GB), volumes 48.6 MB (0 активных), build cache 1.5 GB — не трогали |
| Существующий ComfyUI | отсутствует; есть только Hermes-скилл `~/.hermes/skills/creative/comfyui` |
| Прочие AI-ассеты | `~/ai/models` = 55 GB (3 GGUF LLM), `~/llama.cpp` (исходники) |

### Пять разных проверок (§4) — статус
1. GPU обнаружен ОС — **ДА** (lspci, amdgpu инициализирован, card1).
2. GPU доступен через Vulkan — **ДА** (RADV видит устройство STRIX_HALO).
3. GPU доступен из Docker — **НЕТ, не проверялось** (проверка в этапе 2).
4. ComfyUI использует GPU — **не проверено** (этап 3).
5. Модель реально считает на GPU — **не проверено** (этап 3).

### Ошибки
Нет ошибок amdgpu/прошивок в текущей загрузке. Единственная запись в журнале — AppArmor DENIED для snap `firmware-notifier` на чтение `/proc/sys/vm/max_map_count` (к проекту не относится, не влияет).

### Обнаруженные ограничения и риски
1. **RAM хоста — 30 GiB.** 96 GiB закреплено за iGPU как VRAM. Следствия: запас для CPU-части (загрузка моделей, сборка Docker-образов, ffmpeg, кэш ФС) ограничен; одновременный запуск тяжёлых моделей на CPU + GPU нежелателен. Для ComfyUI это терпимо (веса живут в VRAM), но крупные CPU-операции и распаковку моделей надо планировать.
2. **GTT всего 15.2 GiB** (auto = половина видимой RAM). Модели, которым нужен GTT-пул сверх VRAM, будут упираться. Альтернатива — уменьшить UMA в BIOS и поднять `amdgpu.gttsize` (УРОВЕНЬ C, отдельное решение, сейчас не делаем).
3. **ROCm отсутствует.** Пока единственный доступный вычислительный backend — Vulkan (RADV). Значит, пригодность конкретного ComfyUI-образа зависит от того, на чём он построен: ROCm-HIP (нужен ROCm 7.x с поддержкой gfx1151) или Vulkan. Это ключевой вопрос этапа 2.
4. host-стек новее типичных гайдов (Ubuntu 26.04, ядро 7.0, Mesa 26.0.8) — сторонние инструкции под 24.04/ядро 6.x могут не совпадать; приоритет у фактической диагностики.

### Итог этапа 1
GPU корректно определяется ядром, KFD распознаёт gfx1151, Vulkan-стек (RADV) рабочий на уровне «устройство видно и инициализировано».
Аппаратное ускорение **пока не доказано** — доказательство только через реальный вычислительный прогон (этап 3).

**Следующий шаг (ожидает разрешения):** этап 2 — проверка Docker-образа `ghcr.io/ignatberesnev/comfyui-gfx1151:latest` (доступность, что внутри, какой backend), подготовка `compose.yaml`, запуск ComfyUI на localhost:8188.

---

## 2026-10-26 01:15 — ЭТАП 2, шаг 5.1: проверка образа (registry API, без скачивания слоёв)

**Назначение:** убедиться, что образ существует, актуален и пригоден, до загрузки 11 ГБ.
**Изменения системы:** нет (только HTTP-запросы к registry).
**Восстановление:** не требуется.

### Результат проверки
| Проверка | Результат |
|---|---|
| `ghcr.io/ignatberesnev/comfyui-gfx1151` | **403 `DENIED` — образа в GHCR НЕТ**; ссылка из ТЗ нерабочая |
| `docker.io/ignatberesnev/comfyui-gfx1151` | существует; теги ровно `v0.1`, `v0.2`; **тега `latest` нет** |
| v0.2 digest | 19 слоёв, 10.93 GiB сжато, linux/amd64, собран 2026-02-25 |
| База | `rocm/pytorch:rocm7.2_ubuntu24.04_py3.12_pytorch_release_2.9.1` |
| Backend | **ROCm/HIP 7.2** (не Vulkan); PATH/LD_LIBRARY_PATH → /opt/rocm |
| Cmd | `check-comfyui.sh && python3 /opt/ComfyUI/main.py --listen 0.0.0.0 --use-flash-attention --gpu-only` |
| Прочее | EXPOSE 8188, User=root, Volumes не объявлены, FLASH_ATTENTION_TRITON_AMD_ENABLE=TRUE |
| Апстрим-права | `cap_add SYS_PTRACE` + `seccomp:unconfined` + `shm 8G` (+ автор сам пишет, что не уверен, зачем) |
| Тестировалось автором | ArchLinux 6.18.9, opencl-amd 7.2.0-1; НЕ Ubuntu 26.04 / ядро 7.0 |

**Вывод:** gfx1151 у ROCm 7.2 поддерживается нативно → `HSA_OVERRIDE_GFX_VERSION` не нужен.
Образ принят с оговорками: возраст ~8 мес., baked-in ComfyUI 0.15.0.

### Созданные файлы (УРОВЕНЬ B, новые)
- `compose.yaml` — `127.0.0.1:8188`, `shm_size 4g`, devices `/dev/kfd` + `/dev/dri`,
  `group_add video,render`, data-only mounts (`models`, `input`, `output`, `workflows`),
  `restart: "no"`, `stop_grace_period: 30s`. **Без** privileged / host-network / SYS_PTRACE /
  seccomp:unconfined / `$HOME`. Проверен: `docker compose config` — валиден.
- `.env.example` — параметры образа/порта/shm.

**Отличия от апстрима и причины:** shm 8G→4G (хост-RAM 30 GiB); не монтируется `/opt/ComfyUI`
целиком (воспроизводимость кода); не включены SYS_PTRACE и seccomp:unconfined (минимум прав).

---

## 2026-10-26 01:25 — ЭТАП 2, шаг 5.2/5.3: загрузка образа и запуск контейнера

**Подтверждение пользователя:** «Да: тяни v0.2 и запускай контейнер» (УРОВЕНЬ C — загрузка >10 ГБ).

### Команды, изменяющие систему
| # | Назначение | Команда | Результат | Побочный эффект | Проверка | Восстановление |
|---|---|---|---|---|---|---|
| 1 | загрузка образа | `docker compose pull` | успешно, exit 0 (~10 мин, ~19 МБ/с) | +44.5 GB в /var/lib/docker | `docker images` | `docker image rm ignatberesnev/comfyui-gfx1151:v0.2` |
| 2 | запуск | `docker compose up -d` | контейнер `comfyui-strix-halo` Up | занят 127.0.0.1:8188; создана сеть `comfyui-strix-halo_default` | `docker ps`, HTTP-проба | `docker compose down` (данные на хосте остаются) |
| 3 | проверка персистентности | `docker compose restart` (с разрешения пользователя) | Up, HTTP 200 | простой ~35 с | маркер-файл + `/system_stats` | — |

**Размер на диске: 44.5 GB распакованного (мой прогноз 20–25 GB оказался неверным — поправка внесена).**
Диск `/`: было 82 G занято / 809 G свободно → стало 123 G / 767 G свободно.

### Факты после запуска (из логов контейнера и API)
```
Total VRAM 98304 MB, total RAM 31212 MB
pytorch version: 2.9.1+rocm7.2.0.git7e1940d4
AMD arch: gfx1151 ; ROCm version: (7, 2)
Device: cuda:0 AMD Radeon Graphics : native
Using Flash Attention ; ComfyUI 0.15.0 ; frontend 1.39.16 ; Python 3.12.3
```
- `GET /` → 200 (4322 B); `GET /system_stats` → 200, `devices[0].vram_total = 103 079 215 104 B (96.0 GiB)`, свободно 95.85 GiB.
- `GET /queue` → 200; `GET /object_info` → **638 нод**, есть CheckpointLoaderSimple / KSampler / EmptyLatentImage.
- Внутри контейнера: `/dev/kfd` (gid 990), `/dev/dri/card1`, `/dev/dri/renderD128`; torch: `hip 7.2.26015`, `is_available=True`, `gcnArchName=gfx1151`, `total_vram 96.0 GiB`.
- **SYS_PTRACE и seccomp:unconfined НЕ потребовались** — HIP инициализировался на минимальных правах.
- Изоляция: `192.168.31.60:8188` → connection refused, `172.17.0.1:8188` → refused. Только loopback.
- Персистентность: файл, записанный из контейнера в `/opt/ComfyUI/output`, виден на хосте; файл,
  положенный на хост в `workflows/`, виден в контейнере в `/opt/ComfyUI/user/default/workflows`.
  После `restart` оба файла на месте, HTTP снова 200.

### Важная деталь
`check-comfyui.sh` при первом старте вывел «No ComfyUI detected, copying a built-in (pre-cloned) one...»:
образ содержит код в `/opt/ComfyUI-pre-cloned` и копирует его в `/opt/ComfyUI` (слой контейнера).
Следствие: код ComfyUI живёт в контейнере, а не на хосте — при пересоздании контейнера копирование
повторится; обновление кода = замена образа/ручное вмешательство. Это осознанное решение.

### Ошибки и помехи
1. `docker compose up -d` в foreground был заблокирован защитой инструмента (не ошибка системы) → запущено в фоне.
2. `docker compose restart` потребовал явного подтверждения; первая попытка истекла по таймауту и **не выполнялась**; после ответа пользователя выполнена.
3. Иных ошибок нет: в логах ComfyUI ни одного traceback.

### Статус пяти проверок (§4)
1. GPU обнаружен ОС — ДА.
2. GPU виден через Vulkan — ДА (RADV).
3. **GPU доступен из Docker — ДА** (устройства проброшены, torch видит gfx1151).
4. **Приложение видит GPU — ДА** (`/system_stats`: `cuda:0 AMD Radeon Graphics : native`).
5. Реальные вычисления на GPU — **ещё НЕ проверено** (этап 3): тяжёлых прогонов не было.

**Итог этапа 2:** ComfyUI работает в контейнере на localhost:8188 с доступом к GPU, данные постоянны.
**Следующий шаг:** этап 3 — доказать вычисления на GPU без скачивания моделей (matmul на `device='cuda'`
через интерпретатор образа с замером времени и дельты VRAM) и затем минимальный workflow.

---

## 2026-10-26 02:05 — ЭТАП 3: доказательство вычислений на GPU + минимальный workflow

**Изменения системы:** скачана одна модель 6.46 GiB (с подтверждения пользователя), запущен один лёгкий
workflow. Конфигурация Docker, драйверы, ядро не менялись.

### 3.1. Синтетический тест (модели не нужны)
Скрипт `diagnostics/gpu_matmul_test.py` скопирован в контейнер через `docker cp`, запущен
`/opt/venv/bin/python3`. Полный вывод — `diagnostics/stage3-matmul.txt`.

| Тест | Время | Производительность |
|---|---|---|
| GPU fp16 4096×4096×4096 | 11.94 мс/итер | **11.51 TFLOP/s** |
| GPU fp32 4096×4096×4096 | 66.52 мс/итер | 2.07 TFLOP/s |
| CPU fp32 4096×4096×4096 (16 ядер Zen5) | 139.62 мс/итер | 0.98 TFLOP/s |

- `torch.cuda.get_device_name(0)` = `AMD Radeon Graphics`; `gcnArchName` = `gfx1151`; hip `7.2.26015`.
- torch сообщает `multi_processor_count = 20` — это внутренний счёт WGP, не противоречит 40 CU из amdgpu.
- sysfs VRAM во время GPU-теста: 1 674 022 912 → 2 083 581 952 B; torch peak alloc 160 MiB.
- `gpu_busy_percent` в коротком тесте 9 % — метрика с грубым окном, для iGPU слабый ориентир.

### 3.2. Устойчивая нагрузка (стабильность)
Скрипт `diagnostics/gpu_sustained_test.py`, вывод — `diagnostics/stage3-sustained.txt`:

```
767 итераций 3072x3072 fp16 за 3.50 с  ->  12.70 TFLOP/s
gpu_busy_percent: min=4 max=54   (снаружи сразу после теста: 41 %)
sysfs vram_used: 1898340352 -> 2025648128 (+121.4 MiB)
mem_info_gtt_used: 147 181 568 B (147 MiB) — GTT не рос
ошибок/OOM: нет; контейнер после нагрузки Up, HTTP 200
```

**Почему это доказательство, а не впечатление:** 11.5–12.7 TFLOP/s fp16 недостижимы на CPU
(CPU fp32 дал 0.98 TFLOP/s); рост шёл в device-памяти (VRAM), а GTT (системная RAM) не менялся —
значит вычисления и данные были на GPU, CPU-fallback исключён.

### 3.3. Модель для минимального workflow
**Подтверждение пользователя:** «A: SDXL-Turbo fp16, 6.46 GiB».

| Параметр | Значение |
|---|---|
| Файл | `sd_xl_turbo_1.0_fp16.safetensors` |
| Источник | Hugging Face `stabilityai/sdxl-turbo` (официальный репозиторий Stability AI) |
| Размер | 6 938 081 905 B = 6.46 GiB |
| Куда | `models/checkpoints/` (bind-mount в `/opt/ComfyUI/models/checkpoints`) |
| Загрузка | внутри контейнера `curl -L --fail --retry 3 -C -`, ~10.6 МБ/с, ~11 мин |
| Целостность | sha256 `e869ac7d6942cb327d68d5ed83a40447aadf20e0c3358d98b2cc9e270db0da26` — **совпал** |
| Лицензия | Stability AI Community (бесплатно, в т.ч. коммерчески до $1M выручки) |

Удаление при необходимости: `rm models/checkpoints/sd_xl_turbo_1.0_fp16.safetensors` (только с разрешения).

### 3.4. Минимальный workflow
`workflows/smoke_sdxl_turbo.api.json` — API-формат, 8 нод: CheckpointLoaderSimple → 2×CLIPTextEncode →
EmptyLatentImage(512×512) → KSampler(seed 42, steps 4, cfg 1.0, euler/normal, denoise 1.0) → VAEDecode → SaveImage.
Отправка: `/usr/bin/python3 diagnostics/run_smoke.py` (stdlib, POST `/prompt`, опрос `/history`). Лог — `diagnostics/stage3-workflow.txt`.

```
prompt_id: ff806309-c5af-4352-9d9f-484661c349f7 | node_errors: нет
статус: success | completed: True
изображение: smoke_sdxl_turbo_00001_.png (455 222 B)
время от отправки до результата: 6.27 с
VRAM после загрузки модели: 11 446 153 216 B (11.4 GiB)
```

**Визуальная проверка результата:** изображение открыто и просмотрено — связная мультяшная сцена
(рыжий лисёнок в траве с ромашками), соответствует промпту, шума и артефактов нет.
Т.е. проверен не только факт «workflow не упал», но и осмысленность выхода.

### Статус пяти проверок (§4) — все пять закрыты
1. Обнаружение ОС — ДА · 2. Vulkan — ДА · 3. Доступ из Docker — ДА · 4. Приложение видит GPU — ДА ·
5. **Реальные вычисления на GPU — ДА** (11.5–12.7 TFLOP/s fp16, рост VRAM, gpu_busy до 54 %).

### Замечания и ограничения
- Первая генерация 512×512 при 4 шагах — 6.27 с вместе с загрузкой модели в VRAM (11.4 GiB).
  Скорость на 1024×1024 и на реальных сценах мультфильма будет измерена отдельно.
- `gpu_busy_percent` не является надёжным индикатором для iGPU; выводы строятся на TFLOP/s и VRAM/GTT.
- Каталоги `models/` принадлежат root (созданы контейнером) — модели кладём изнутри контейнера,
  sudo на хосте не требуется. Чтобы писать туда с хоста, понадобится `chown` (УРОВЕНЬ C).

**Итог этапа 3:** аппаратное ускорение подтверждено замерами, минимальный workflow отработал,
результат осмысленный. **Следующий шаг:** этап 4 — проверить интеграцию Hermes-скилла `comfyui`
v5.1.0 с локальным сервером (health_check.py и т.п.), затем этап 5 — таблица моделей-кандидатов.

---

## 2026-10-26 02:10 — ЭТАП 4: интеграция Hermes-скилла `comfyui` v5.1.0

**Изменения системы:** ничего не устанавливалось. Исправлен один баг в копии скилла профиля
(`~/.hermes/profiles/developer/skills/creative/comfyui/scripts/health_check.py`),
прогнано два health-check и один полный workflow через штатный раннер.

### 4.1. Что за скилл
Найдено **две копии** скилла v5.1.0:
- `~/.hermes/skills/creative/comfyui` — общая (профиль по умолчанию);
- `~/.hermes/profiles/developer/skills/creative/comfyui` — копия профиля developer (правится мной).
Скрипты (`health_check.py`, `run_workflow.py`, `check_deps.py`, `run_batch.py`) написаны на stdlib
(urllib), внешних зависимостей не требуют; запускаются хостовым Python 3.14.4.

### 4.2. Первый health_check — обнаружен баг скилла
```
verdict: fail
reasons: ["smoke-test submission failed", "comfy-cli not on PATH (lifecycle commands won't work)"]
server: reachable=True, ComfyUI 0.15.0, checkpoint 1 -> sd_xl_turbo_1.0_fp16.safetensors
smoke_test: HTTP 400 prompt_outputs_failed_validation
  "images, received_type(LATENT) mismatch input_type(IMAGE)"
  node 9 (SaveImage).linked_node = ["3", 0]
```
**Причина:** встроенный в скилл `SMOKE_WORKFLOW` соединял `SaveImage` напрямую с выходом `KSampler`
(LATENT) — узел `VAEDecode` отсутствовал. Такой workflow не может пройти валидацию ни на одном сервере:
баг скилла, а не проблема стенда. Ошибка воспроизведена в обеих копиях (в профиле и в общей).

**Исправление** (патч в копии профиля): добавлен узел `8 = VAEDecode(samples=["3",0], vae=["4",2])`,
`SaveImage.images = ["8", 0]`.

### 4.3. Повторный health_check после исправления
```
exit=0   verdict: warn
reasons: ["comfy-cli not on PATH (lifecycle commands won't work)"]
server : reachable=True | ComfyUI 0.15.0
ckpt   : 1 -> ['sd_xl_turbo_1.0_fp16.safetensors']
smoke  : ran=True, submitted=True, prompt_id=41ff57d9-…, cancelled_after_submit=True
```
Единственная оставшаяся причина warn — отсутствие `comfy-cli`. Это утилита управления жизненным
циклом (`comfy start/stop`, установка нод); при работе через Docker она не нужна.
Установка = УРОВЕНЬ C (системный пакет через pipx), поэтому не ставилась.

### 4.4. Полный цикл через штатный раннер
```
run_workflow.py --workflow workflows/smoke_sdxl_turbo.api.json \
  --args '{"prompt":"...лисьонок...","negative_prompt":"...","seed":123}' --output-dir ./outputs_skill
Submitted: prompt_id=47061cd8-05fd-4a3a-b3a5-e3788fd8141f
status: success | warnings: []
downloaded: outputs_skill/smoke_sdxl_turbo_00002_.png (477 909 B, chown dmitry)
```
Инъекция параметров работает: при seed 123 картинка визуально отличается от seed 42 (другая поза и
композиция), обе осмысленные. Файл скачан на хост под пользователем dmitry, а не root — удобно.

### 4.5. Проверка зависимостей workflow (`check_deps`)
```
is_ready: true | installed_node_count: 638 | required_node_count: 6
required: CLIPTextEncode, CheckpointLoaderSimple, EmptyLatentImage, KSampler, SaveImage, VAEDecode
missing_nodes: [] | missing_models: [] | missing_embeddings: [] | is_cloud: false
```

### 4.6. Итог
- Скилл рабочий: сервер найден, задание отправлено, результат скачан, предупреждений нет.
- Найден и исправлен реальный дефект скилла (отсутствовал `VAEDecode` в smoke-workflow).
- Побочный артефакт: отменённый smoke-тест успел записать `output/smoke_00001_.png` (256×256, 101 КБ).
- Очередь пуста, VRAM после работы 9.5 GiB (модель остаётся загруженной — это нормально).

**Следующий шаг:** этап 5 — таблица моделей-кандидатов (качество изображения/стиль для мультфильма,
апскейл, анимация/видео) с размерами, источниками и лицензиями, без скачивания.

---

## 2026-10-26 02:25 — ЭТАП 5: модели-кандидаты (только исследование, скачиваний нет)

**Изменения системы:** нет. Только запросы к `/object_info` локального сервера и к HF API (метаданные).
Свободно на диске: 760 GB из 937 GB (занято 130 GB).

### 5.1. Что реально поддерживает наш ComfyUI 0.15.0 (проверено по `/object_info`, 638 нод)

| Архитектура / функция | Узлы | Вывод |
|---|---|---|
| SD1.5 / SDXL | CheckpointLoaderSimple, KSampler, VAEDecode, VAELoader | поддерживается |
| SD3 / SD3.5 | EmptySD3LatentImage, ModelSamplingSD3, CLIPLoader | поддерживается |
| FLUX | UNETLoader, DualCLIPLoader, FluxGuidance | поддерживается |
| Апскейл моделями | UpscaleModelLoader, ImageUpscaleWithModel, ImageScaleBy | поддерживается |
| Видео SVD (image→video) | SVD_img2vid_Conditioning, VideoLinearCFGGuidance, ImageOnlyCheckpointLoader, CLIPVisionLoader | поддерживается |
| Видео Wan | WanImageToVideo, WanFirstLastFrameToVideo, CreateVideo, SaveVideo, SaveWEBM | поддерживается |
| Видео LTX / Hunyuan / Cosmos / Mochi | LTXVConditioning, EmptyLTXVLatentVideo, EmptyHunyuanLatentVideo, HunyuanImageToVideo, EmptyMochiLatentVideo, Cosmos* | поддерживается |
| Консистентность (без custom nodes) | ReferenceLatent, StyleModelApply, CLIPVisionEncode | частично |
| AnimateDiff | ADE_* / AnimateDiffLoader* | **НЕТ в ядре**, нужен custom node |
| IP-Adapter | IPAdapterApply / IPAdapterModelLoader | **НЕТ в ядре**, нужен custom node |
| Аудио-узлы | LoadAudio, SaveAudio, AudioEncoderLoader | есть (для этапов 6–7) |

**Важное предупреждение по безопасности:** в образе есть узлы, которые обращаются к облачным API —
`Kling*`, `Grok*`, `Magnific*`, `Recraft*`, `Stability*`, `Bria*`, `ByteDance*`, `Wavespeed*`, `HitPaw*`.
Их наличие в списке нод не означает, что ими можно пользоваться: по правилам проекта это отправка
данных наружу. **Не используются.** Все генерации — только локальные узлы (`CheckpointLoaderSimple`,
`UNETLoader`, `KSampler`, `WanImageToVideo`, `UpscaleModelLoader` и т.п.).

### 5.2. Кандидаты: стиль изображения

| Модель | Файл | Размер | Лицензия | Комментарий |
|---|---|---|---|---|
| **animagine-xl-4.0** | animagine-xl-4.0.safetensors | 6.46 GiB | openrail++ | SDXL, аниме/иллюстрация, 630k загрузок; дружелюбная детская графика |
| SDXL base 1.0 | sd_xl_base_1.0.safetensors | 6.46 GiB | openrail++ | база, требует длинного стилевого промпта |
| SD 3.5 medium | sd3.5_medium.safetensors | 4.76 GiB | Stability Community, **gated** | лучше держит промпт, но требуется HF-токен и принятие лицензии |
| FLUX.1-schnell (fp8) | flux1-schnell-fp8 + t5xxl_fp8 + ae + clip_l | ~21 GiB | apache-2.0 | качество выше, но в 3 раза больше и заметно медленнее на ROCm |

### 5.3. Кандидаты: анимация / видео

| Маршрут | Файлы | Размер | Лицензия | Что даёт |
|---|---|---|---|---|
| **SVD xt (image→video)** | svd_xt.safetensors | 8.90 GiB | Stability «other» (проверить перед коммерцией) | «живая картинка»: 2–4 с мягкого движения из готового кадра, без custom nodes |
| **Wan 2.2 TI2V 5B** | wan2.2_ti2v_5B_fp16 + umt5_xxl_fp8 + wan2.2_vae | 16.89 GiB | apache-2.0 | настоящая анимация с движением камеры и персонажа, медленнее |
| LTX-Video 2B distilled fp8 | ltxv-2b-0.9.8-distilled-fp8 + t5xxl_fp8 | ~8.7 GiB | «other» | быстрый компромисс, качество ниже Wan |
| AnimateDiff (SD1.5) | mm_sd_v15_v2 (1.56) + custom node | ~1.6 GiB | apache-2.0 | требует установки custom node; качество ниже, база SD1.5 |
| Wan 2.1 I2V 14B | 7 шардов | 63+ GiB | apache-2.0 | избыточно для ноутбука по времени, не предлагается |

### 5.4. Апскейл и консистентность

| Инструмент | Размер | Лицензия | Комментарий |
|---|---|---|---|
| **Real-ESRGAN x4plus** | 0.06 GiB | BSD-подобная (проверить файл лицензии репозитория) | обязательный минимум, узлы уже есть |
| IP-Adapter SDXL (опционально) | ~2.5 GiB + custom node | apache-2.0 | фиксация лица/стиля между кадрами, но требует установки custom node |

### 5.5. Логика рекомендации
Для «доброго мультфильма по детскому сочинению» главное — узнаваемый мягкий стиль и дешёвое
производство десятков кадров. SDXL-семейство даёт 1024² за секунды и лицензию, позволяющую
показывать результат. Маршрут «кадр → SVD → монтаж» превращает статичные иллюстрации в
«ожившие страницы книги» — ровно тот эффект, который уместен для детской истории,
и стоит на порядок дешевле, чем полноценная видеогенерация Wan.

**Следующий шаг:** получить выбор пользователя и скачать только согласованный набор.

---

## 2026-10-26 02:55 — ЭТАП 6: загрузка набора C, стилевые тесты, апскейл, первый тест Wan

**Решение пользователя:** вариант **C** — аниме/иллюстрация SDXL + полная анимация Wan 2.2 TI2V 5B (23.4 GiB).

### 6.1. Загрузка (каждый файл проверен по sha256, при несовпадении не подменялся)
Скрипт `diagnostics/download_wan.sh`, лог `diagnostics/stage6-download.log`.

| Файл | Байт | Куда | Результат |
|---|---|---|---|
| animagine-xl-4.0.safetensors | 6 938 434 056 | models/checkpoints | sha256 OK, 02:08→02:19 |
| RealESRGAN_x4.pth | 67 040 989 | models/upscale_models | sha256 OK |
| wan2.2_vae.safetensors | 1 409 400 960 | models/vae | sha256 OK |
| umt5_xxl_fp8_e4m3fn_scaled.safetensors | 6 735 906 897 | models/text_encoders | sha256 OK, →02:33 |
| wan2.2_ti2v_5B_fp16.safetensors | 9 999 658 848 | models/diffusion_models | sha256 OK, →02:49 |

Итого **25 150 441 750 Б = 23.42 GiB** за 41 мин (~10–12 МБ/с). Свободно на диске после: 741 GB.
Все пять моделей сервер увидел без перезапуска (`UNETLoader`/`CLIPLoader`/`VAELoader`/`CheckpointLoaderSimple`).

### 6.2. Стилевые тесты animagine-xl-4.0
`diagnostics/style_tests.py` → `outputs_style/style_0000{1..4}_.png`, лог `diagnostics/stage6-style.log`.
Режим: 1024×1024, 28 шагов, cfg 6, euler_ancestral/normal, seed 777–780.

| № | Сцена | Время | Проверка глазами |
|---|---|---|---|
| 01 | лисёнок и девочка в жёлтом шарфе, осенняя тропа | 28.0 c | соответствует: акварельная книжная иллюстрация, чистые линии |
| 02 | сова в круглых очках на сосновой ветке | 26.0 c | соответствует, персонаж обаятельный, дефектов нет |
| 03 | бумажный кораблик на реке на рассвете | 26.0 c | нарисовал **флотилию** корабликов вместо одного |
| 04 | дом с дымком в снегу, тёплый свет в окне | 26.0 c | свет в окне и фигурки внутри есть, **дымка нет** |

**Вывод:** стиль попадает точно (мягкая акварель, детская книжка), но точность по количеству объектов
и деталям промпта слабая. Для сцен мультфильма понадобится итеративная правка промптов, а не «написал и забыл».
Стабильная скорость: **26–28 с на кадр 1024²**.

### 6.3. Апскейл
`diagnostics/run_upscale_test.py` → `outputs_upscale/upscale_x4_00001_.png`:
1024×1024 → **4096×4096** за **38.1 с**, PNG 15.55 МБ. Узлы `UpscaleModelLoader` + `ImageUpscaleWithModel` работают.

### 6.4. Первый тест Wan 2.2 TI2V 5B (image → video)
`diagnostics/run_wan_test.py`, лог `diagnostics/stage6-wan.log`.
Вход: `outputs_style/style_00001_.png`. Параметры: 640×640, 49 кадров, 30 шагов, cfg 5.0, uni_pc/simple,
`ModelSamplingSD3 shift 8.0`, fps 24, h264.

```
prompt_id b584c2e0-…  node_errors: {}
время: 282.3 c (4.7 мин)
выход: outputs_video/wan_test_00001_.mp4 — 640×640, h264, 24 fps, 49 кадров, 2.04 c, 0.25 МБ
```

Проверка кадров 0/24/48 (извлечены ffmpeg, просмотрены):
- персонажи сохраняют идентичность на протяжении клипа, деформаций лиц и лишних конечностей нет;
- есть настоящее движение: у девочки меняется положение рук и шарфа, лисёнок смещается, листья летят,
  камера медленно наезжает.

**Честное ограничение:** первый кадр видео **не пиксельно совпадает** с исходной картинкой —
Wan TI2V перерисовывает сцену, используя кадр как условие. Композиция и персонажи узнаваемы,
но это не «оживление ровно того же рисунка». Если нужна точная стыковка с иллюстрациями,
понадобится либо приём «первый кадр отдельно + монтаж», либо SVD вместо Wan.

### 6.5. Оценка бюджета времени для мультфильма
- кадр 1024²: ~27 с → 20 сцен ≈ 9 мин;
- клип 2 с: ~4.7 мин → 20 клипов ≈ 1 ч 35 мин (плюс перегенерации при браке);
- апскейл 4K: ~38 с на кадр.
Для мультфильма 1.5–2 мин реалистично 3–6 часов машинного времени с учётом переделок.

**Итог этапа 6:** все согласованные модели скачаны и проверены; стиль выбран и проверен визуально;
апскейл работает; видеоветка Wan работает и даёт живое движение. Готовы к сценарию.
**Следующий шаг:** этап 7 — сценарий по детскому сочинению (нужен текст или фото сочинения от пользователя),
раскадровка на 8–12 сцен, затем контрольные кадры.

---

## 2026-10-26 03:00 — пауза сессии до завтра

Работа остановлена по просьбе пользователя (уходит спать). Все этапы 1–6 закрыты и записаны.
Создан `RESUME-2026-10-27.md`: как продолжить с этапа 7 одним сообщением, как проверить контейнер
после перезагрузки ноутбука, план этапа 7 (сценарий → раскадровка → утверждение), открытые вопросы
(формат сочинения, длительность фильма, голос рассказчика, нужен ли SVD) и действующие запреты.

Состояние на момент паузы: контейнер `comfyui-strix-halo` — Up (около часа), порт 127.0.0.1:8188,
моделей 23.42 GiB, свободно на диске 737 GB, фоновых процессов нет, ничего не удалялось
и не перекачивалось. Новых скачиваний и системных изменений после этапа 6 не было.

### 03:10 — скрипты запуска (УРОВЕНЬ B, только файлы проекта)

Пользователь выключает ноутбук и попросил способ поднимать студию одной командой. Созданы:

- `start.sh` — проверяет docker-демон (ждёт до 60 с), наличие `/dev/kfd` и `/dev/dri`,
  выполняет `docker compose up -d`, ждёт HTTP 200 от `/system_stats` (до 180 с),
  печатает адрес, устройство, VRAM и число чекпойнтов. Ничего не скачивает и не меняет в системе.
- `stop.sh` — `docker compose stop`, данные и модели остаются.

Проверено фактически:
- синтаксис (`bash -n`) — ок;
- повторный запуск при работающем контейнере — «готово за ~1 с», 2 чекпойнта видны;
- **холодный старт**: `docker compose stop` → `./start.sh` → `Started`, HTTP 200 через 5 с,
  устройство `cuda:0 AMD Radeon Graphics`, VRAM 96 GiB, 2 чекпойнта;
- контейнер оставлен запущенным.

Автозапуск не включался: в `compose.yaml` остаётся `restart: "no"`.
Возможные варианты по желанию пользователя (не применялись): `restart: unless-stopped`
в compose или systemd-юнит пользователя. `RESUME-2026-10-27.md` обновлён — запуск теперь через `./start.sh`.

### 03:20 — остановка студии по просьбе пользователя (освобождение ресурсов)

Пользователь закончил работу и попросил завершить процессы, чтобы Docker не потреблял ресурсы.

- `docker compose stop` → `Container comfyui-strix-halo Stopped`. Состояние `Exited (137)`:
  процесс ComfyUI не завершился по SIGTERM за отведённые 10 с и был добит — для сервера
  без незаписанных данных это нормально; модели, output и workflows на месте.
- Память хоста: было `4.9Gi` занято / `25Gi` доступно → стало `4.3Gi` / `26Gi`.
  Сам контейнер держал 942.9 MiB RSS, CPU 0 %.
- Порт 8188 освобождён (прослушиваний нет).
- Фоновых процессов проекта нет: загрузок `curl` с Hugging Face, `download_wan.sh`,
  `run_wan_test.py`, `style_tests.py` не запущено.
- Docker-демон оставлен работать: `active`, RSS 81 MiB — накладные расходы незначительны,
  и он нужен для `./start.sh` без sudo. Остановка демона — УРОВЕНЬ C, не выполнялась.
- Образ (44.5 GB) и модели (23.42 GiB) не удалялись, перекачивать завтра ничего не нужно.

Состояние к паузе: контейнер остановлен, порт свободен, `./start.sh` поднимает студию за ~5 с.
`STATUS.md` и `RESUME-2026-10-27.md` обновлены пометкой об остановленном контейнере.
