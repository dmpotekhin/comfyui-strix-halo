"""Проверка реальной ноды H3ClipLoaderGPU: память, dtype, целостность весов.

Запускается внутри контейнера, использует сам модуль custom_nodes/h3_gpu_clip_loader.
Защита: при RSS > 26 ГиБ процесс убивает себя, чтобы не подвесить хост.
"""
import gc
import sys
import threading
import time

import torch

sys.path.insert(0, "/opt/ComfyUI")
sys.path.insert(0, "/opt/ComfyUI/custom_nodes")

LIMIT = 26 * 2**30
PEAK = {"rss": 0, "vram": 0}


def rss_bytes():
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    return 0


def vram_used():
    free, total = torch.cuda.mem_get_info()
    return total - free, total


def sampler():
    while True:
        r = rss_bytes()
        v, _ = vram_used()
        PEAK["rss"] = max(PEAK["rss"], r)
        PEAK["vram"] = max(PEAK["vram"], v)
        if r > LIMIT:
            print(f"!!! RSS {r / 2**30:.2f} ГиБ > {LIMIT / 2**30:.0f} ГиБ — аварийный останов", flush=True)
            sys.stdout.flush()
            import os

            os._exit(9)
        time.sleep(0.2)


threading.Thread(target=sampler, daemon=True).start()

import h3_gpu_clip_loader as h3node  # noqa: E402

name = "qwen3vl_32b_minimax_h3_fp8_scaled.safetensors"
print(f"[тест] RSS до: {rss_bytes() / 2**30:.2f} ГиБ | VRAM: {vram_used()[0] / 2**30:.2f} ГиБ", flush=True)

t0 = time.time()
(clip,) = h3node.H3ClipLoaderGPU().load_clip(name, "minimax")
print(f"[тест] нода вернула CLIP за {time.time() - t0:.1f} с; тип {type(clip).__name__}", flush=True)
print(f"[тест] RSS после: {rss_bytes() / 2**30:.2f} ГиБ | VRAM: {vram_used()[0] / 2**30:.2f} ГиБ", flush=True)
print(f"[тест] пик RSS: {PEAK['rss'] / 2**30:.2f} ГиБ | пик VRAM: {PEAK['vram'] / 2**30:.2f} ГиБ", flush=True)

params = list(clip.cond_stage_model.parameters())
dtypes = {}
for p in params:
    dtypes[str(p.dtype)] = dtypes.get(str(p.dtype), 0) + p.numel() * p.element_size()
gpu_bytes = sum(p.numel() * p.element_size() for p in params if str(p.device).startswith("cuda"))
print(f"[тест] параметров {len(params)}, устройства {sorted({str(p.device) for p in params})}", flush=True)
print(f"[тест] байты по dtype: { {k: round(v / 2**30, 2) for k, v in sorted(dtypes.items())} }", flush=True)
print(f"[тест] суммарно на GPU: {gpu_bytes / 2**30:.2f} ГиБ", flush=True)

# целостность: сравниваем с файлом те же ключи, что остались обычными весами
import safetensors  # noqa: E402
import folder_paths  # noqa: E402

path = folder_paths.get_full_path("text_encoders", name)
model_sd = clip.cond_stage_model.state_dict()
checked = matched = 0
with safetensors.safe_open(path, framework="pt", device="cpu") as f:
    keys = list(f.keys())
    for k in keys:
        if checked >= 5 or k.endswith("comfy_quant"):
            continue
        t = f.get_tensor(k)
        if t.numel() > 2_000_000 or k not in model_sd:
            continue
        ref = model_sd[k]
        same = ref.dtype == t.dtype and ref.shape == t.shape and torch.equal(ref.cpu(), t)
        checked += 1
        matched += int(same)
        print(f"[тест] сверка {k}: {t.dtype} {tuple(t.shape)} идентично={same}", flush=True)
print(f"[тест] сверено {matched}/{checked} тензоров (ноль сверок = ключи переформатированы квантованием)", flush=True)

del clip, params, model_sd
gc.collect()
torch.cuda.empty_cache()
print(f"[тест] после освобождения: RSS {rss_bytes() / 2**30:.2f} ГиБ | VRAM {vram_used()[0] / 2**30:.2f} ГиБ", flush=True)
print("[тест] ГОТОВО", flush=True)
