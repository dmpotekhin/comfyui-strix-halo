"""H3 CLIP Loader (GPU) — загрузка текстового энкодера MiniMax H3 сразу в VRAM.

Зачем: штатный `comfy.sd.load_clip()` читает safetensors без указания устройства
(`comfy/sd.py:1699` -> `comfy/utils.py:193  device = torch.device("cpu")`), поэтому
весь state dict (24,7 ГиБ для Qwen3-VL 32B FP8) сначала материализуется в
анонимной памяти процесса. На Strix Halo, где после carve-out под iGPU системе
остаётся 30 ГиБ, это даёт OOM-kill ещё до переноса весов на устройство.

Нода читает те же тензоры сразу на GPU и отдаёт их штатному построителю CLIP
(`comfy.sd.load_text_encoder_state_dicts`) с load_device/initial_device = cuda.
Файлы моделей, dtype и квантование не меняются: никакого пересчёта весов нет.
"""

import gc
import os
import time

import safetensors
import torch

import comfy.sd
import comfy.utils
import folder_paths

CLIP_TYPE = "minimax"
NODE_DISPLAY_NAME = "H3 CLIP Loader (GPU)"


def _rss_gib():
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return 0.0


def _vram_gib():
    free, total = torch.cuda.mem_get_info()
    return (total - free) / 2**30, total / 2**30


_DTYPE_BYTES = {
    "F64": 8, "F32": 4, "F16": 2, "BF16": 2, "I64": 8, "I32": 4,
    "I16": 2, "I8": 1, "U64": 8, "U32": 4, "U16": 2, "U8": 1,
    "BOOL": 1, "F8_E4M3": 1, "F8_E5M2": 1,
}

CHUNK_GIB = 1.0  # размер порции между переоткрытиями файла


def _tensor_bytes(shape, dtype_name):
    n = 1
    for dim in shape:
        n *= dim
    return n * _DTYPE_BYTES.get(str(dtype_name), 1)


def _read_to_gpu(path, chunk_gib=CHUNK_GIB):
    """Читает safetensors прямо в VRAM порциями. Возвращает (state_dict, metadata, n_keys).

    Порции нужны из-за поведения safetensors: пока handle открыт, его внутренний
    читающий буфер растёт вместе с числом прочитанных тензоров — на файле 24,7 ГиБ
    это давало пик анонимной памяти ~24,7 ГиБ, то есть ровно тот OOM, от которого
    мы уходим. Переоткрытие файла на каждую порцию держит анонимную память на
    уровне ~0,3 ГиБ (замерено).

    Исключение — служебные ключи `*comfy_quant`: это маленькие JSON-блобы, которые
    `comfy/ops.py` разбирает через `.numpy()` (ops.py:1287, 1661, 1774), а на
    CUDA-тензор numpy не конвертируется. Их держим на CPU — это байты, не веса.
    """
    sd = {}
    try:
        with safetensors.safe_open(path, framework="pt", device="cpu") as handle:
            metadata = handle.metadata()
            plan = [
                (key, _tensor_bytes(handle.get_slice(key).get_shape(), handle.get_slice(key).get_dtype()))
                for key in handle.keys()
            ]

        groups, current, batch = [], 0, []
        for key, nbytes in plan:
            batch.append(key)
            current += nbytes
            if current >= chunk_gib * 2**30:
                groups.append(batch)
                batch, current = [], 0
        if batch:
            groups.append(batch)

        done = 0
        for group in groups:
            with safetensors.safe_open(path, framework="pt", device="cuda") as handle:
                for key in group:
                    tensor = handle.get_tensor(key)
                    sd[key] = tensor.cpu() if key.endswith("comfy_quant") else tensor
                    del tensor
            torch.cuda.synchronize()  # отпускаем копирующие буферы до следующей порции
            done += len(group)
            print(f"[H3 CLIP GPU] прочитано {done}/{len(plan)} тензоров | VRAM {_vram_gib()[0]:.1f} ГиБ", flush=True)
    except Exception:
        sd.clear()  # при ошибке не держим частично прочитанное
        gc.collect()
        raise
    return sd, metadata, len(sd)


def _build_clip(path, disable_dynamic=False):
    """Полный путь загрузки. Возвращает объект CLIP."""
    device = torch.device("cuda:0")
    size_gib = os.path.getsize(path) / 2**30

    free, total = torch.cuda.mem_get_info()
    if free / 2**30 < size_gib * 1.15:
        raise RuntimeError(
            f"H3 CLIP Loader (GPU): недостаточно VRAM для {os.path.basename(path)} "
            f"({size_gib:.1f} ГиБ нужно, свободно {free / 2**30:.1f} ГиБ из {total / 2**30:.1f} ГиБ). "
            "Освободите VRAM (выгрузите другие модели) и повторите."
        )

    rss0 = _rss_gib()
    used0, _ = _vram_gib()
    t0 = time.time()

    sd, metadata, n_keys = _read_to_gpu(path)
    print(
        f"[H3 CLIP GPU] прочитано {n_keys} тензоров за {time.time() - t0:.1f} с | "
        f"RSS {rss0:.2f} -> {_rss_gib():.2f} ГиБ | VRAM {used0:.2f} -> {_vram_gib()[0]:.2f} ГиБ",
        flush=True,
    )

    try:
        if sd:
            sd, metadata = comfy.utils.convert_old_quants(sd, model_prefix="", metadata=metadata)
        clip = comfy.sd.load_text_encoder_state_dicts(
            [sd],
            embedding_directory=folder_paths.get_folder_paths("embeddings"),
            clip_type=getattr(comfy.sd.CLIPType, CLIP_TYPE.upper()),
            model_options={
                "load_device": device,
                "offload_device": torch.device("cpu"),
                "initial_device": device,
            },
            disable_dynamic=disable_dynamic,
        )
    except Exception:
        sd.clear()
        gc.collect()
        raise

    del sd
    gc.collect()

    print(
        f"[H3 CLIP GPU] CLIP готов за {time.time() - t0:.1f} с | "
        f"RSS {_rss_gib():.2f} ГиБ | VRAM {_vram_gib()[0]:.2f} ГиБ",
        flush=True,
    )
    return clip


def _load_patcher(path, disable_dynamic=False):
    """Фабрика для cached_patcher_init: перечитывает веса тоже в VRAM, а не в RAM."""
    return _build_clip(path, disable_dynamic=disable_dynamic).patcher


class H3ClipLoaderGPU:
    @classmethod
    def INPUT_TYPES(cls):
        files = folder_paths.get_filename_list("text_encoders")
        return {
            "required": {
                "clip_name": (files,),
                "type": ([CLIP_TYPE], {"default": CLIP_TYPE}),
            }
        }

    RETURN_TYPES = ("CLIP",)
    RETURN_NAMES = ("clip",)
    FUNCTION = "load_clip"
    CATEGORY = "h3"

    def load_clip(self, clip_name, type=CLIP_TYPE):
        if type.lower() != CLIP_TYPE:
            raise ValueError(f"H3 CLIP Loader (GPU): поддерживается только type={CLIP_TYPE!r}, получено {type!r}")
        if not torch.cuda.is_available():
            raise RuntimeError("H3 CLIP Loader (GPU): CUDA/HIP-устройство недоступно, загрузка в VRAM невозможна")

        path = folder_paths.get_full_path("text_encoders", clip_name)
        if path is None or not os.path.isfile(path):
            raise FileNotFoundError(
                f"H3 CLIP Loader (GPU): файл энкодера не найден: {clip_name} "
                f"(искали в {folder_paths.get_folder_paths('text_encoders')})"
            )

        clip = _build_clip(path)
        # Штатный загрузчик умеет пересоздавать patcher не-динамическим клоном;
        # без этой ссылки ComfyUI либо упадёт, либо перечитает веса в RAM.
        clip.patcher.cached_patcher_init = (_load_patcher, (path,))
        return (clip,)


NODE_CLASS_MAPPINGS = {"H3ClipLoaderGPU": H3ClipLoaderGPU}
NODE_DISPLAY_NAME_MAPPINGS = {"H3ClipLoaderGPU": NODE_DISPLAY_NAME}
