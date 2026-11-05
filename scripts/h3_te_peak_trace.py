"""v5: пик анонимной памяти при загрузке через ноду + проверка результата."""
import gc
import sys
import threading
import time

import torch

sys.path.insert(0, "/opt/ComfyUI")
sys.path.insert(0, "/opt/ComfyUI/custom_nodes")

PEAK = {"anon": 0, "rss": 0, "file": 0}
STOP = {"v": False}


def mem():
    out = {"rss": 0, "anon": 0, "file": 0}
    with open("/proc/self/status") as fh:
        for line in fh:
            for key, field in (("VmRSS:", "rss"), ("RssAnon:", "anon"), ("RssFile:", "file")):
                if line.startswith(key):
                    out[field] = int(line.split()[1]) * 1024
    return out


def sampler():
    while not STOP["v"]:
        m = mem()
        for k in ("rss", "anon", "file"):
            PEAK[k] = max(PEAK[k], m[k])
        time.sleep(0.03)


threading.Thread(target=sampler, daemon=True).start()

import h3_gpu_clip_loader as h3node  # noqa: E402
import safetensors  # noqa: E402
import folder_paths  # noqa: E402

name = "qwen3vl_32b_minimax_h3_fp8_scaled.safetensors"
path = folder_paths.get_full_path("text_encoders", name)

t0 = time.time()
(clip,) = h3node.H3ClipLoaderGPU().load_clip(name, "minimax")
STOP["v"] = True
time.sleep(0.2)

print(f"[v5] загрузка {time.time() - t0:.1f} с; CLIP {type(clip).__name__}", flush=True)
print(f"[v5] пик RSS {PEAK['rss'] / 2**30:.2f} ГиБ = anon {PEAK['anon'] / 2**30:.2f} + file {PEAK['file'] / 2**30:.2f}", flush=True)
print(f"[v5] сейчас: anon {mem()['anon'] / 2**30:.2f} ГиБ | VRAM alloc {torch.cuda.memory_allocated() / 2**30:.2f} ГиБ", flush=True)

kinds = {}
for p in clip.cond_stage_model.parameters():
    key = f"{type(p).__name__}/{p.dtype}"
    kinds[key] = kinds.get(key, 0) + 1
print(f"[v5] параметры: {kinds}", flush=True)

sample_mod = None
for mod_name, mod in clip.cond_stage_model.named_modules():
    if getattr(mod, "quant_format", None) and getattr(mod, "weight", None) is not None:
        sample_mod = (mod_name, mod)
        break
if sample_mod:
    n, m = sample_mod
    print(f"[v5] пример квантованного: {n} формат={m.quant_format} класс={type(m.weight).__name__} dtype={m.weight.dtype}", flush=True)

# деquantized-сверка: значение веса после загрузки против байтов в файле
model_sd = clip.cond_stage_model.state_dict()
checked = matched = 0
with safetensors.safe_open(path, framework="pt", device="cpu") as f:
    allkeys = list(f.keys())
    for k in allkeys:
        if checked >= 6:
            break
        if k not in model_sd:
            continue
        ref = f.get_tensor(k)
        got = model_sd[k]
        if ref.numel() > 1_500_000 or ref.dtype != got.dtype or tuple(ref.shape) != tuple(got.shape):
            continue
        same = torch.equal(got.cpu(), ref)
        checked += 1
        matched += int(same)
        print(f"[v5] сверка {k}: {ref.dtype} {tuple(ref.shape)} идентично={same}", flush=True)
print(f"[v5] сверено {matched}/{checked} (0 сверок = все ключи квантованы, проверяем иначе)", flush=True)

if checked == 0 and sample_mod:
    n, m = sample_mod
    w = m.weight
    if hasattr(w, "dequantize"):
        dq = w.dequantize()
        print(f"[v5] вес {n} после dequantize: dtype {dq.dtype}, abs mean {dq.abs().mean().item():.6f}, "
              f"first values {[round(x, 5) for x in dq.flatten()[:4].tolist()]}", flush=True)

print("[v5] ГОТОВО", flush=True)
