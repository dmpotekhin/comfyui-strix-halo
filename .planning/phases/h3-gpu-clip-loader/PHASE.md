# PHASE: H3 CLIP Loader (GPU)

Статус: **Stage 1–4 пройдены 2026-11-05 13:25 MSK** — FAST-генерация на ноде
`H3 CLIP Loader (GPU)` завершилась успешно, файл `output/glide_00001.mkv`.
Открыто: постоянная поставка `ffmpeg` в образ; решение по флагам `compose.yaml`.

Задача: убрать OOM при загрузке Qwen3-VL 32B FP8 (24,7 ГиБ) в ComfyUI 0.44.0 на
AMD Strix Halo / ROCm 7.2. Вариант A: отдельная custom-нода, без правки ядра ComfyUI.

Полный контекст, замеры, порядок этапов и открытые вопросы: `RESUME-2026-11-05.md`
в корне проекта.

## Этапы

| Этап | Статус | Критерий приёмки |
|---|---|---|
| Реализация ноды | DONE | `H3ClipLoaderGPU` импортируется, отдаёт `CLIP`, ошибки понятные |
| Замеры памяти | DONE | пик анонимной RAM 1,86 ГиБ (было 24,73), VRAM 26,80 ГиБ |
| Идентичность весов | DONE | 1369/1369 тензоров, 0 расхождений shape/dtype, fp8 сохранён |
| Stage 1 — smoke в ComfyUI | PASS | нода в `/object_info`, энкодер грузится, RAM без пика |
| Stage 2 — копия workflow | PASS | копия валидна, оригинал не изменён |
| Stage 3 — FAST-генерация | PASS | `output/glide_00001.mkv`, 768×448, 124 кадра, 337,84 с |
| Stage 4 — регрессия | PASS (статически) | остальные workflow валидны, штатный `CLIPLoader` работает |
| ffmpeg в образе | OPEN | сейчас скопирован в контейнер вручную, теряется при пересоздании |

## Команда продолжения

```bash
cd ~/workspace/comfyui-strix-halo && ./scripts/h3_gpu_stage_check.sh --restart
```

## Файлы фазы

- `custom_nodes/h3_gpu_clip_loader/__init__.py` — нода (206 строк)
- `workflows/h3-studio/H3_STUDIO_1_FAST_GPULoader.json` — копия FAST под новую ноду
- `scripts/h3_gpu_stage_check.sh` — прогон Stage 1–4 одной командой
- `scripts/h3_te_peak_trace.py`, `scripts/h3_te_identity_check.py`,
  `scripts/h3_te_gpu_probe.py`, `scripts/h3_te_node_test.py`, `scripts/h3_read_variants.py` — замеры
- `docs/H3_STUDIO_BEGINNER_GUIDE_RU.md` (раздел 15), `docs/H3_STUDIO_RESOURCES.md` (2.1)
- логи: `logs/h3_te_v5.log`, `logs/h3_te_identity.log`, `logs/h3_gpu_stage_check.log`
