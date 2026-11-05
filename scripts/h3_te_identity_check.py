"""Сверка весов: файл vs модель после загрузки нодой.

Проверяет количество тензоров, соответствие ключей, shapes, dtype и значения:
для обычных весов — побитово, для fp8-квантованных — сравнение упакованного payload.
"""
import sys

import safetensors
import torch

sys.path.insert(0, "/opt/ComfyUI")
sys.path.insert(0, "/opt/ComfyUI/custom_nodes")

import folder_paths  # noqa: E402
import h3_gpu_clip_loader as h3node  # noqa: E402

PREFIX = "qwen3vl_32b."
name = "qwen3vl_32b_minimax_h3_fp8_scaled.safetensors"
path = folder_paths.get_full_path("text_encoders", name)

(clip,) = h3node.H3ClipLoaderGPU().load_clip(name, "minimax")
model_sd = clip.cond_stage_model.state_dict()

with safetensors.safe_open(path, framework="pt", device="cpu") as f:
    file_keys = list(f.keys())

plain_file = []
quant_file = []
for k in file_keys:
    if k.endswith("comfy_quant"):
        continue
    plain_file.append(k)

print(f"[id] тензоров в файле (без comfy_quant): {len(plain_file)}; в модели: {len(model_sd)}", flush=True)

# соответствие ключей: файл 'model.layers...' -> модель 'qwen3vl_32b.transformer.model.layers...'
model_keys = list(model_sd.keys())
suffix_index = {}
for mk in model_keys:
    suffix_index.setdefault(mk, mk)
mapping = {}
for k in plain_file:
    needle = "." + k
    candidates = [mk for mk in model_keys if mk.endswith(needle)]
    if len(candidates) == 1:
        mapping[k] = candidates[0]

print(f"[id] ключей модели без пары в файле: {len(plain_file) - len(mapping)}", flush=True)

plain_ok = plain_bad = 0
quant_ok = quant_bad = 0
shape_bad = []
dtype_bad = []

with safetensors.safe_open(path, framework="pt", device="cpu") as f:
    for k in plain_file:
        key = mapping.get(k)
        if key is None:
            continue
        ref = f.get_tensor(k)
        got = model_sd[key]
        if tuple(ref.shape) != tuple(got.shape):
            shape_bad.append((k, tuple(ref.shape), tuple(got.shape)))
            continue
        if str(ref.dtype) != str(got.dtype):
            dtype_bad.append((k, str(ref.dtype), str(got.dtype)))
            continue
        if ref.dtype == torch.float8_e4m3fn:
            q = getattr(got, "_qdata", got)
            if torch.equal(q.cpu(), ref):
                quant_ok += 1
            elif torch.equal(q.cpu().T.contiguous(), ref):
                quant_ok += 1
            else:
                quant_bad += 1
        else:
            if torch.equal(got.cpu(), ref):
                plain_ok += 1
            else:
                plain_bad += 1
        del ref

print(f"[id] обычные веса (bf16/f32): совпало {plain_ok}, расхождений {plain_bad}", flush=True)
print(f"[id] fp8-веса: payload совпал {quant_ok}, расхождений {quant_bad}", flush=True)
print(f"[id] расхождения shape: {len(shape_bad)}, dtype: {len(dtype_bad)}", flush=True)
if shape_bad[:3]:
    print(f"[id] пример shape: {shape_bad[:3]}", flush=True)
if dtype_bad[:3]:
    print(f"[id] пример dtype: {dtype_bad[:3]}", flush=True)

quant_mods = sum(1 for _, m in clip.cond_stage_model.named_modules()
                 if getattr(m, "quant_format", None) and getattr(m, "weight", None) is not None)
print(f"[id] модулей с квантованием: {quant_mods}; форматы: "
      f"{sorted({m.quant_format for _, m in clip.cond_stage_model.named_modules() if getattr(m, 'quant_format', None)})}", flush=True)
print("[id] ГОТОВО", flush=True)
