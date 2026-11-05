"""Замер пути загрузки H3-энкодера с тензорами сразу в VRAM.

Ничего не меняет: читает safetensors, строит CLIP тем же штатным API, что и нода,
и печатает RSS/VRAM в контрольных точках. Защита: при RSS > LIMIT процесс убивает
себя сам, чтобы не подвесить хост.

Запуск (внутри контейнера): python3 /tmp/h3_te_gpu_probe.py <путь к safetensors>
"""
import gc
import os
import sys
import threading
import time

import torch

LIMIT = 26 * 2**30  # 26 ГиБ — выше этого не идём
PEAK = {"rss": 0}


def rss_bytes():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    return 0


def vram():
    free, total = torch.cuda.mem_get_info()
    return total - free, total


def guard():
    while True:
        r = rss_bytes()
        if r > PEAK["rss"]:
            PEAK["rss"] = r
        if r > LIMIT:
            print(f"!!! RSS {r / 2**30:.2f} GiB превысил {LIMIT / 2**30:.0f} GiB — аварийный останов", flush=True)
            os._exit(9)
        time.sleep(0.2)


threading.Thread(target=guard, daemon=True).start()

path = sys.argv[1]
import safetensors  # noqa: E402
import comfy.sd  # noqa: E402
import comfy.utils  # noqa: E402

print(f"[проба] файл {path} ({os.path.getsize(path) / 2**30:.2f} ГиБ)", flush=True)
print(f"[проба] RSS до: {rss_bytes() / 2**30:.2f} ГиБ | VRAM занято: {vram()[0] / 2**30:.2f} ГиБ", flush=True)

t0 = time.time()
sd = {}
with safetensors.safe_open(path, framework="pt", device="cuda") as f:
    meta = f.metadata()
    keys = list(f.keys())
    for k in keys:
        sd[k] = f.get_tensor(k)

read_s = time.time() - t0
print(f"[проба] прочитано {len(sd)} тензоров за {read_s:.1f} с", flush=True)
print(f"[проба] RSS после чтения: {rss_bytes() / 2**30:.2f} ГиБ | VRAM: {vram()[0] / 2**30:.2f} ГиБ", flush=True)
sample = list(sd.values())[:400]
print(f"[проба] устройства: {sorted({str(v.device) for v in sample})}", flush=True)
print(f"[проба] dtype: {sorted({str(v.dtype) for v in sample})}", flush=True)

# та же нормализация квантов, что делает comfy.sd.load_clip
t1 = time.time()
sd, meta = comfy.utils.convert_old_quants(sd, model_prefix="", metadata=meta)
print(f"[проба] convert_old_quants {time.time() - t1:.1f} с | RSS {rss_bytes() / 2**30:.2f} ГиБ | VRAM {vram()[0] / 2**30:.2f} ГиБ", flush=True)

dev = torch.device("cuda:0")
t2 = time.time()
clip = comfy.sd.load_text_encoder_state_dicts(
    [sd],
    clip_type=comfy.sd.CLIPType.MINIMAX,
    model_options={
        "load_device": dev,
        "offload_device": torch.device("cpu"),
        "initial_device": dev,
    },
)
print(f"[проба] CLIP построен за {time.time() - t2:.1f} с", flush=True)
print(f"[проба] RSS после CLIP: {rss_bytes() / 2**30:.2f} ГиБ | VRAM: {vram()[0] / 2**30:.2f} ГиБ", flush=True)
print(f"[проба] пик RSS за весь путь: {PEAK['rss'] / 2**30:.2f} ГиБ", flush=True)

# где живут веса модели и в каком dtype
try:
    params = list(clip.cond_stage_model.parameters())
    print(f"[проба] параметров: {len(params)}", flush=True)
    print(f"[проба] устройства весов: {sorted({str(p.device) for p in params})}", flush=True)
    print(f"[проба] dtype весов: {sorted({str(p.dtype) for p in params})}", flush=True)
    gpu_bytes = sum(p.numel() * p.element_size() for p in params if str(p.device).startswith("cuda"))
    print(f"[проба] суммарно на GPU: {gpu_bytes / 2**30:.2f} ГиБ", flush=True)
except Exception as e:  # noqa: BLE001
    print(f"[проба] не удалось осмотреть веса: {e}", flush=True)

# контроль целостности: выбранные маленькие тензоры сравниваем с файлом, прочитанным на CPU
small = [k for k in keys if sd[k].numel() <= 4_000_000][:5]
ok = 0
for k in small:
    with safetensors.safe_open(path, framework="pt", device="cpu") as f:
        ref = f.get_tensor(k)
    same = torch.equal(ref, sd[k].cpu()) if sd[k].dtype == ref.dtype else False
    ok += int(same)
    print(f"[проба] сверка {k}: {sd[k].dtype} {tuple(sd[k].shape)} идентично={same}", flush=True)
print(f"[проба] сверено {ok}/{len(small)} контрольных тензоров", flush=True)

del sd
gc.collect()
print(f"[проба] RSS после освобождения state dict: {rss_bytes() / 2**30:.2f} ГиБ | VRAM: {vram()[0] / 2**30:.2f} ГиБ", flush=True)
print("[проба] ГОТОВО", flush=True)
