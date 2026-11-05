"""Сравнение способов чтения safetensors в VRAM по пиковой анонимной памяти."""
import gc
import sys
import threading

import torch

sys.path.insert(0, "/opt/ComfyUI")

PEAK = {"anon": 0}
STOP = {"v": False}


def anon():
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("RssAnon:"):
                return int(line.split()[1]) * 1024
    return 0


def sampler():
    while not STOP["v"]:
        PEAK["anon"] = max(PEAK["anon"], anon())
        import time

        time.sleep(0.03)


threading.Thread(target=sampler, daemon=True).start()

import safetensors  # noqa: E402

PATH = "/opt/ComfyUI/models/text_encoders/qwen3vl_32b_minimax_h3_fp8_scaled.safetensors"


def run(label, mode):
    import time

    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()
    base = anon()
    PEAK["anon"] = base
    t0 = time.time()
    held = []
    with safetensors.safe_open(PATH, framework="pt", device="cuda" if mode != "cpu_then_gpu" else "cpu") as f:
        for i, k in enumerate(f.keys()):
            t = f.get_tensor(k)
            if mode == "cpu_then_gpu":
                g = t.to("cuda", non_blocking=False)
                del t
                t = g
            held.append(t)
            if mode == "sync" and i % 50 == 0:
                torch.cuda.synchronize()
    torch.cuda.synchronize()
    print(f"[{label}] {len(held)} тензоров, {time.time() - t0:.1f} с, "
          f"пик anon {PEAK['anon'] / 2**30:.2f} ГиБ (база {base / 2**30:.2f}), "
          f"VRAM alloc {torch.cuda.memory_allocated() / 2**30:.2f} ГиБ", flush=True)
    del held, t
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()
    print(f"[{label}] после освобождения anon {anon() / 2**30:.2f} ГиБ, VRAM alloc {torch.cuda.memory_allocated() / 2**30:.2f} ГиБ", flush=True)


for lbl, m in (("sync каждые 50", "sync"), ("cpu->gpu", "cpu_then_gpu")):
    run(lbl, m)

STOP["v"] = True
print("[итог] ГОТОВО", flush=True)
