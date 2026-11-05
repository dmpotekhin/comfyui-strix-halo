#!/usr/bin/env python3
"""Собирает H3 Studio workflow из оригинальных примеров CGlide.

Что делает:
  * подменяет имена моделей на те файлы, которые реально скачаны;
  * выставляет параметры под режим (FAST / QUALITY / 2K / UPSCALE / EDIT);
  * убирает CUDA-специфичные pass-through ноды (ModelAttentionBackend,
    ModelPatchTorchSettings) — на ROCm они либо невалидны, либо падают;
  * добавляет LoadImage -> first_frame для режима fl2va;
  * переименовывает группы по-русски и кладёт MarkdownNote с инструкцией.

Запуск: python3 scripts/h3_make_workflows.py
"""
import copy
import json
import os
import sys

ROOT = os.path.expanduser("~/workspace/comfyui-strix-halo")
SRC = os.path.join(ROOT, "custom_nodes/ComfyUI-CGlide/example_workflows")
OUT = os.path.join(ROOT, "workflows/h3-studio")

M = {
    "unet_fl2va": "minimax_h3_fl2va_pruned_fp8_scaled.safetensors",
    "unet_ref2va": "minimax_h3_ref2va_pruned_fp8_scaled.safetensors",
    "te": "qwen3vl_32b_minimax_h3_fp8_scaled.safetensors",
    "vae_video": "minimax_h3_video_vae_fp16.safetensors",
    "vae_audio": "minimax_h3_audio_vae_fp32.safetensors",
    "lora_fl2v4": "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors",
    "lora_fl2v8": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
    "lora_ref2v4": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
    "upscaler": "minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors",
    "lora2k": "open_h3_regenerate_2k_lora.safetensors",
    "sharp": "sharp_curve_branches.safetensors",
    "tae": "taeh3.safetensors",
}

# старый basename -> новый basename
REMAP = {
    "minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors": None,  # зависит от варианта
    "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors": M["te"],
    "minimax_h3_video_vae_fp16.safetensors": M["vae_video"],
    "minimax_h3_audio_vae_fp32.safetensors": M["vae_audio"],
    "minimax_h3_ref2v_turbo_8step_v1.1_768p_comfyui_bf16.safetensors": M["lora_fl2v8"],
    "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors": M["lora_ref2v4"],
    "minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors": M["upscaler"],
    "open_h3_regenerate_2k_lora.safetensors": M["lora2k"],
    "sharp_curve_branches.safetensors": M["sharp"],
    "taeh3.safetensors": M["tae"],
}


def nid(d):
    return max(n["id"] for n in d["nodes"]) + 1


def maxlink(d):
    return max([l[0] for l in d["links"]] or [0]) + 1


def by_type(d, t):
    return [n for n in d["nodes"] if n["type"] == t]


def remap_strings(obj, unet_new):
    """Рекурсивно меняет basename'ы файлов моделей (включая строку h3_data)."""
    rep = dict(REMAP)
    rep["minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors"] = unet_new
    if isinstance(obj, str):
        out = obj
        for old, new in rep.items():
            if old and old in out:
                out = out.replace(old, new)
        return out
    if isinstance(obj, list):
        return [remap_strings(x, unet_new) for x in obj]
    if isinstance(obj, dict):
        return {k: remap_strings(v, unet_new) for k, v in obj.items()}
    return obj


def remove_node(d, node_id):
    """Удаляет pass-through ноду (MODEL -> MODEL), сшивая связи."""
    n = next((x for x in d["nodes"] if x["id"] == node_id), None)
    if n is None:
        return
    in_link = next((i.get("link") for i in n.get("inputs", []) if i.get("link") is not None), None)
    if in_link is None:
        return
    src = next((l for l in d["links"] if l[0] == in_link), None)
    if src is None:
        return
    up_id, up_slot = src[1], src[2]
    out_links = [l for l in d["links"] if l[1] == node_id]
    for l in out_links:
        dst_node = next((x for x in d["nodes"] if x["id"] == l[3]), None)
        if dst_node is None:
            continue
        for i in dst_node.get("inputs", []):
            if i.get("link") == l[0]:
                i["link"] = l[0]
    # перенаправляем исходящие связи на входной источник
    new_links = []
    for l in d["links"]:
        if l[1] == node_id:
            new_links.append([l[0], up_id, up_slot, l[3], l[4], l[5]])
        elif l[0] == in_link:
            continue
        elif l[3] == node_id:
            continue
        else:
            new_links.append(l)
    d["links"] = new_links
    # убираем вышестоящий исходящий линк и заменяем на новые
    up = next((x for x in d["nodes"] if x["id"] == up_id), None)
    if up:
        for o in up.get("outputs", []):
            if o.get("links"):
                o["links"] = [x for x in o["links"] if x != in_link]
                for l in out_links:
                    o["links"].append(l[0])
    d["nodes"] = [x for x in d["nodes"] if x["id"] != node_id]


def set_h3(d, **kw):
    """Правит поля state-строки H3 Studio."""
    cast = by_type(d, "CSGlideCastCS")[0]
    wv = cast["widgets_values"]
    state = json.loads(wv[0] if isinstance(wv, list) else wv)
    for k, v in kw.items():
        if k == "slots_prompt":
            continue
        state[k] = v
    txt = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    if isinstance(wv, list):
        wv[0] = txt
    else:
        cast["widgets_values"] = txt


def add_load_image(d, label="INPUT IMAGE — загрузите сюда своё фото"):
    """Добавляет LoadImage и подключает к first_frame H3 Studio."""
    cast = by_type(d, "CSGlideCastCS")[0]
    lid = nid(d)
    link_id = maxlink(d)
    img = {
        "id": lid,
        "type": "LoadImage",
        "pos": [-1720, 3980],
        "size": [340, 380],
        "flags": {},
        "order": 0,
        "mode": 0,
        "inputs": [],
        "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [link_id]},
                    {"name": "MASK", "type": "MASK", "links": None}],
        "properties": {"Node name for S&R": "LoadImage"},
        "widgets_values": ["H3_EXAMPLE.png", "image"],
        "title": label,
        "color": "#232",
        "bgcolor": "#353",
    }
    d["nodes"].append(img)
    d["links"].append([link_id, lid, 0, cast["id"], 3, "IMAGE"])
    ins = cast.setdefault("inputs", [])
    for i, item in enumerate(ins):
        if item.get("name") == "first_frame":
            item["link"] = link_id
            break
    else:
        ins.append({"name": "first_frame", "type": "IMAGE", "link": link_id})
        ins.append({"name": "last_frame", "type": "IMAGE", "link": None})
    return lid


def add_note(d, text, pos, size=(560, 520)):
    base = by_type(d, "MarkdownNote")
    if base:
        n = copy.deepcopy(base[0])
    else:
        n = {"id": 0, "type": "MarkdownNote", "inputs": [], "outputs": [], "properties": {}}
    n["id"] = nid(d)
    n["pos"] = list(pos)
    n["size"] = list(size)
    n["title"] = "ИНСТРУКЦИЯ"
    n["widgets_values"] = [text]
    if isinstance(n.get("properties"), dict):
        n["properties"]["text"] = text
    d["nodes"].append(n)
    return n["id"]


def rename_groups(d, mapping):
    for g in d.get("groups", []):
        t = g.get("title", "")
        for old, new in mapping.items():
            if t == old:
                g["title"] = new


def set_widget(d, node_type, values, index_map=None):
    n = by_type(d, node_type)[0]
    wv = n["widgets_values"]
    if index_map is None:
        n["widgets_values"] = values
    else:
        for i, v in index_map.items():
            wv[i] = v


NOTE_COMMON = """### Как пользоваться этим workflow

1. **INPUT IMAGE** — нажмите *choose file to upload* и выберите своё фото.
2. **ПРОМПТ** — пишется внутри большой ноды **H3 Studio** (поле `prompt`):
   кликните по ноде, текст вводится в её панели.
   Опишите: что происходит, камера, свет, звук.
3. **Queue** (кнопка Run / Queue Prompt справа) — запуск.
4. Результат: папка `output/`, файл `.mp4`/`.mkv` с именем, указанным в ноде
   **Glide Video** (низ-право).

Размер кадра и длительность задаются в панели H3 Studio
(`width`, `height`, `length` — length в кадрах, 24 fps).
"""


def inline_set_get(d):
    """Убирает ноды SetNode/GetNode (это клиентские ноды из Custom-Scripts).

    Все GetNode перенаправляются напрямую на источник, который подавался
    в SetNode. Так workflow не зависит от дополнительного расширения UI.
    """
    sets = {}
    for n in by_type(d, "SetNode"):
        wv = n.get("widgets_values") or [""]
        name = wv[0] if isinstance(wv, list) else wv
        in_link = next((i.get("link") for i in n.get("inputs", []) if i.get("link") is not None), None)
        if in_link is None:
            continue
        src = next((l for l in d["links"] if l[0] == in_link), None)
        if src is None:
            continue
        sets[name] = (src[1], src[2], in_link)
    for n in by_type(d, "GetNode"):
        wv = n.get("widgets_values") or [""]
        name = wv[0] if isinstance(wv, list) else wv
        if name not in sets:
            continue
        up_id, up_slot, _ = sets[name]
        moved = []
        for l in d["links"]:
            if l[1] == n["id"]:
                l[1], l[2] = up_id, up_slot
                moved.append(l[0])
        up = next((x for x in d["nodes"] if x["id"] == up_id), None)
        if up:
            for o in up.get("outputs", []):
                if o.get("links"):
                    for m in moved:
                        if m not in o["links"]:
                            o["links"].append(m)
    keep = {"SetNode", "GetNode"}
    d["nodes"] = [x for x in d["nodes"] if x["type"] not in keep]
    d["links"] = [l for l in d["links"]
                  if not any(l[0] == v[2] for v in sets.values())]


def normalize_paths(d):
    """Убирает Windows-префиксы папок из имён файлов моделей."""
    for n in d["nodes"]:
        if n["type"] in ("UNETLoader", "CLIPLoader", "VAELoader", "LoraLoaderModelOnly",
                         "CSGlideH3DiffLoraCS", "MinimaxH3LatentUpscaler3D"):
            wv = n.get("widgets_values")
            if isinstance(wv, list) and wv and isinstance(wv[0], str):
                wv[0] = os.path.basename(wv[0].replace("\\", "/"))


MODEL_NOTE = """### Какие модели используются

| Файл | Куда скачан | Зачем |
|---|---|---|
| minimax_h3_fl2va_pruned_fp8_scaled.safetensors | models/diffusion_models | генерация видео из фото (fl2va) |
| minimax_h3_ref2va_pruned_fp8_scaled.safetensors | models/diffusion_models | мультиреференс / редактирование |
| qwen3vl_32b_minimax_h3_fp8_scaled.safetensors | models/text_encoders | текстовый энкодер (понимает промпт) |
| minimax_h3_video_vae_fp16.safetensors | models/vae | декодирует видео-латент в кадры |
| minimax_h3_audio_vae_fp32.safetensors | models/vae | декодирует звук |
| taeh3.safetensors | models/vae_approx | быстрый предпросмотр |
| minimax_h3_fl2v_turbo_*.safetensors | models/loras | ускорение: 4 или 8 шагов |
| open_h3_regenerate_2k_lora.safetensors | models/loras | дорисовка деталей в 2K |
| minimax_h3_latent_upscaler_3d_conv_v1_bf16 | models/latent_upscale_models | latent upscale |

Полный список с размерами и ссылками — в файле
`docs/H3_STUDIO_RESOURCES.md`.
"""


def replace_author_notes(d):
    for n in d["nodes"]:
        if n["type"] == "MarkdownNote" and n["id"] in (116, 117):
            n["title"] = "МОДЕЛИ: что установлено"
            n["widgets_values"] = [MODEL_NOTE]
            if isinstance(n.get("properties"), dict):
                n["properties"]["text"] = MODEL_NOTE
            n["size"] = [640, 430]


def build(name, base_file, unet, note, groups, mutate, load_image=False, remove=()):
    path = os.path.join(SRC, base_file)
    d = json.load(open(path))
    d = remap_strings(d, unet)
    # CUDA-only pass-through ноды
    for t in remove:
        for n in by_type(d, t):
            remove_node(d, n["id"])
    inline_set_get(d)
    rename_groups(d, groups)
    mutate(d)
    normalize_paths(d)
    replace_author_notes(d)
    if load_image:
        add_load_image(d)
    add_note(d, note, (-2570, 5150), (700, 560))
    d["extra"] = d.get("extra", {})
    os.makedirs(OUT, exist_ok=True)
    dst = os.path.join(OUT, name)
    with open(dst, "w") as f:
        json.dump(d, f, ensure_ascii=False)
    print("written", dst, "nodes:", len(d["nodes"]))


def PREFIX(d, p):
    """Меняет только пресет кодека и префикс файла, остальное — как у автора."""
    set_widget(d, "CSGlideVideoCS", None, {1: "H.264 (compatible)",
                                          2: f"video/{p}", 5: True})

GROUPS_A = {"Models": "МОДЕЛИ (не менять без необходимости)",
            "H3 Studio": "H3 STUDIO: ФОТО + ПРОМПТ + НАСТРОЙКИ",
            "Sampling & Decoding": "ГЕНЕРАЦИЯ → ПРЕВЬЮ → СОХРАНЕНИЕ"}
GROUPS_B = {"Models": "МОДЕЛИ (не менять без необходимости)",
            "H3 Studio": "H3 STUDIO: ФОТО + ПРОМПТ + НАСТРОЙКИ",
            "Upscale": "UPSCALE + ДОРИСОВКА ДО 2K"}

DUMMY = ("ModelAttentionBackend", "ModelPatchTorchSettings")


def fast(d):
    set_widget(d, "BasicScheduler", ["beta", 4, 1])
    set_widget(d, "LoraLoaderModelOnly", [M["lora_fl2v4"], 1])
    set_h3(d, mode="fl2va", width=768, height=448, length=121,
           prompt="A cinematic shot of this subject, gentle camera movement, natural light, soft ambient sound.")
    PREFIX(d, "H3_FAST")


def quality(d):
    set_widget(d, "BasicScheduler", ["beta", 8, 1])
    set_widget(d, "LoraLoaderModelOnly", [M["lora_fl2v8"], 1])
    set_h3(d, mode="fl2va", width=1344, height=768, length=294,
           prompt="A cinematic shot of this subject, slow dolly-in, golden hour light, detailed textures, natural sound.")
    PREFIX(d, "H3_QUALITY")


def editing(d):
    # ref2va-модель => турбо-LoRA именно для ref2v (4 шага)
    set_widget(d, "BasicScheduler", ["beta", 4, 1])
    set_widget(d, "LoraLoaderModelOnly", [M["lora_ref2v4"], 1])
    set_h3(d, mode="ref2va", width=960, height=544, length=121,
           prompt="Keep the same character and composition as the references, change the background to a rainy neon-lit street at night, cinematic.")
    PREFIX(d, "H3_EDIT")


def two_k(d):
    set_h3(d, mode="ref2va", width=1344, height=768, length=294,
           prompt="A cinematic shot of this subject, slow dolly-in, golden hour light, "
                  "detailed textures, film grain, natural ambient sound.")
    PREFIX(d, "H3_2K")


def build_upscaler():
    """Отдельный простой апскейлер картинок (4x-UltraSharp, ESRGAN).

    Работает на любом изображении, в т.ч. на кадре из готового видео.
    Полностью на нативных нодах ComfyUI — без внешних зависимостей.
    """
    nodes = [
        {"id": 1, "type": "LoadImage", "pos": [40, 120], "size": [340, 380], "flags": {},
         "order": 0, "mode": 0, "inputs": [],
         "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [1]},
                     {"name": "MASK", "type": "MASK", "links": None}],
         "properties": {"Node name for S&R": "LoadImage"},
         "title": "1. INPUT IMAGE — загрузите картинку",
         "widgets_values": ["H3_EXAMPLE.png", "image"]},
        {"id": 2, "type": "UpscaleModelLoader", "pos": [420, 120], "size": [340, 82], "flags": {},
         "order": 1, "mode": 0, "inputs": [],
         "outputs": [{"name": "UPSCALE_MODEL", "type": "UPSCALE_MODEL", "links": [2]}],
         "properties": {"Node name for S&R": "UpscaleModelLoader"},
         "title": "2. Модель увеличения",
         "widgets_values": ["real_esrgan_x4plus.pth"]},
        {"id": 3, "type": "ImageUpscaleWithModel", "pos": [420, 250], "size": [300, 58], "flags": {},
         "order": 2, "mode": 0,
         "inputs": [{"name": "upscale_model", "type": "UPSCALE_MODEL", "link": 2},
                    {"name": "image", "type": "IMAGE", "link": 1}],
         "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [3]}],
         "properties": {"Node name for S&R": "ImageUpscaleWithModel"},
         "title": "3. Увеличение x4"},
        {"id": 4, "type": "ImageScale", "pos": [760, 250], "size": [300, 130], "flags": {},
         "order": 3, "mode": 4 if False else 0,
         "inputs": [{"name": "image", "type": "IMAGE", "link": 3}],
         "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [4]}],
         "properties": {"Node name for S&R": "ImageScale"},
         "title": "4. Точный размер (по желанию)",
         "widgets_values": ["lanczos", 2048, 2048, "disabled"]},
        {"id": 5, "type": "SaveImage", "pos": [1100, 250], "size": [420, 460], "flags": {},
         "order": 4, "mode": 0,
         "inputs": [{"name": "images", "type": "IMAGE", "link": 4}],
         "outputs": [],
         "properties": {"Node name for S&R": "SaveImage"},
         "title": "5. РЕЗУЛЬТАТ (папка output)",
         "widgets_values": ["H3_UPSCALE"]},
    ]
    links = [[1, 1, 0, 3, 1, "IMAGE"],
             [2, 2, 0, 3, 0, "UPSCALE_MODEL"],
             [3, 3, 0, 4, 0, "IMAGE"],
             [4, 4, 0, 5, 0, "IMAGE"]]
    d = {"last_node_id": 5, "last_link_id": 4, "nodes": nodes, "links": links,
         "groups": [], "config": {}, "extra": {}, "version": 0.4}
    add_note(d, """### Апскейлер картинок

1. **INPUT IMAGE** — загрузите картинку (можно кадр из готового видео):
   если видео уже есть, вытащите кадр командой в терминале
   `ffmpeg -i result.mp4 -vf "select=eq(n\\,0)" -vframes 1 frame.png`
2. Модель **real_esrgan_x4plus (RealESRGAN x4+)** увеличивает в 4 раза.
3. **Увеличение x4** — сам апскейл. Нода *Точный размер* по умолчанию
   в режиме `disabled` (то есть отдаёт чистый x4). Хотите ровно 2K —
   поставьте в ней `width/height` и режим `lanczos`.
4. **Queue** → результат в `output/H3_UPSCALE_*.png`.

Этот workflow не использует модель H3 — он работает с любой картинкой
и не занимает VRAM под видео-модели.
""", (40, 540), (640, 400))
    os.makedirs(OUT, exist_ok=True)
    dst = os.path.join(OUT, "H3_STUDIO_4_UPSCALER.json")
    with open(dst, "w") as f:
        json.dump(d, f, ensure_ascii=False)
    print("written", dst, "nodes:", len(d["nodes"]))


if __name__ == "__main__":
    build("H3_STUDIO_1_FAST.json", "Minimax_h3_Studio.json", M["unet_fl2va"],
          NOTE_COMMON + "\n**Режим FAST**: 4 шага, 768x448, ~5 сек. Для быстрых проб.",
          GROUPS_A, fast, load_image=True, remove=DUMMY)

    build("H3_STUDIO_2_QUALITY.json", "Minimax_h3_Studio.json", M["unet_fl2va"],
          NOTE_COMMON + "\n**Режим QUALITY**: 8 шагов, 1344x768, ~12 сек. Финальное качество без 2K.",
          GROUPS_A, quality, load_image=True, remove=DUMMY)

    build("H3_STUDIO_3_2K_FINAL.json", "Minimax_h3_Studio_Regenerate_2K.json", M["unet_ref2va"],
          NOTE_COMMON + "\n**Режим 2K**: черновик 1344x768, затем latent-upscale и дорисовка\n"
          "Open-H3 Regenerate 2K (3 шага Euler). Самый долгий, самый качественный.",
          GROUPS_B, two_k, load_image=True, remove=DUMMY)

    build("H3_STUDIO_5_EDITING.json", "Minimax_h3_Studio.json", M["unet_ref2va"],
          NOTE_COMMON + "\n**Режим EDITING**: референсы загружаются ВНУТРИ ноды H3 Studio\n"
          "(слоты images: до 9 картинок). Меняйте фон/стиль/окружение, сохраняя персонажа.",
          GROUPS_A, editing, load_image=True, remove=DUMMY)
    build_upscaler()
    print("done")
    # проверка: все типы нод должны существовать в запущенном ComfyUI
    try:
        oi = json.load(open("/tmp/oi.json"))
        for f in sorted(os.listdir(OUT)):
            dd = json.load(open(os.path.join(OUT, f)))
            miss = sorted({n["type"] for n in dd["nodes"]} - set(oi) - {"MarkdownNote", "Reroute"})
            print("VALIDATE", f, "missing node types:", miss or "none")
    except FileNotFoundError:
        print("(validation skipped: /tmp/oi.json нет)")
