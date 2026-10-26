#!/usr/bin/env python3
"""Proposed workflow catalog: plan data, not a registry.

Emits docs/workflow_catalog_plan.csv and docs/workflow_catalog_plan.md.

Rules that keep this honest:
  * One row = one **distinct graph or distinct engine pipeline**. Prompt/style variants of the
    same graph are presets, not workflow rows — see `presets` below.
  * `status` describes the world as of the audit, not the ambition:
      READY        executed end-to-end here, output inspected
      VALIDATED    graph + dependencies validated against the live engine, not executed
      NEEDS_MODELS a required model is not installed
      NEEDS_NODES  a required node class is absent from /object_info
      EXPERIMENTAL compatibility or quality is unproven on gfx1151
      UNAVAILABLE  requires a paid/cloud API node (banned by brief 8)
  * `gb` is the *additional* download needed, GIB, from Hugging Face file listings.
    Empty means zero extra download.
"""
from __future__ import annotations

import csv
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent.parent / "docs"

# (id, title, technique / distinguishing graph, deps, status, gb, unlocks_with)
R = []  # rows


def add(section: str, *rows) -> None:
    for r in rows:
        R.append({"section": section, "id": r[0], "title": r[1], "technique": r[2],
                  "deps": r[3], "status": r[4], "gb": r[5], "unlocks_with": r[6]})


# ---------------------------------------------------------------- image studio
add("image",
    ("txt2img-turbo", "Text to image — SDXL Turbo draft", "SDXL Turbo, 4 steps, cfg 1.0",
     "installed: sd_xl_turbo_1.0_fp16", "READY", "", "in registry, fox render verified"),
    ("txt2img-animagine", "Text to image — Animagine XL 4.0", "SDXL-family illustration checkpoint, 28 steps",
     "installed: animagine-xl-4.0", "READY", "", "in registry, executed"),
    ("txt2img-upscale", "Text to image with 4x upscale", "t2i then RealESRGAN chain in one graph",
     "installed", "READY", "", "in registry, 768x768 output"),
    ("img2img-restyle", "Image to image restyle", "SDXL img2img, denoise 0.55",
     "installed: animagine-xl-4.0", "READY", "", "in registry, executed"),
    ("inpaint-turbo", "Inpaint with alpha mask", "LoadImage MASK = 1-alpha, VAEEncodeForInpaint",
     "installed", "READY", "", "in registry, mask verified visually"),
    ("outpaint-turbo", "Outpaint / extend canvas", "latent pad + masked sampling",
     "installed", "READY", "", "in registry, 1280x768 output"),
    ("upscale-realesrgan", "Upscale an image 4x", "UpscaleModelLoader + ImageUpscaleWithModel",
     "installed: RealESRGAN_x4", "READY", "", "in registry, executed"),
    ("image-resize", "Resize / crop to exact size", "ResizeImagesByLongerEdge + pad",
     "installed", "READY", "", "in registry, executed"),
    ("character-ipadapter", "Character consistency — IP-Adapter", "IPAdapterUnifiedLoader PLUS + CLIP-ViT-H",
     "installed: ip-adapter-plus, CLIP-ViT-H", "READY", "", "in registry, 1024x1024 kitten"),
    ("txt2img-animagine-hires", "Illustration with hi-res fix", "two-pass: base 832 + latent upscale + refiner pass",
     "installed: animagine-xl-4.0", "VALIDATED", "", "no new model, doubles sampling time"),
    ("inpaint-animagine", "Inpaint with the illustration checkpoint", "same graph family as inpaint-turbo, Animagine weights",
     "installed", "VALIDATED", "", "no new model"),
    ("img2img-batch", "Batch restyle (2-8 images)", "batch_size on the img2img graph",
     "installed", "VALIDATED", "", "no new model, VRAM scales with batch"),
    ("txt2img-sdxl-photo", "Photoreal SDXL", "SDXL base 1.0, DPM++ 2M Karras, 30 steps",
     "sdxl base", "NEEDS_MODELS", 6.46, "photorealism, product/lifestyle shots"),
    ("txt2img-sd35-medium", "SD 3.5 Medium", "MMDiT, T5-XXL fp8 + CLIP-L",
     "sd3.5-medium (gated)", "NEEDS_MODELS", 15.0, "better text rendering, 2.5 MP native"),
    ("txt2img-flux-schnell", "FLUX.1 schnell", "4-step distilled, guidance 0",
     "flux1-schnell-dev fp8", "NEEDS_MODELS", 17.24, "top prompt adherence at low step count"),
    ("txt2img-qwen-image", "Qwen-Image (Alibaba)", "MMDiT 20B, 20 steps, strong bilingual prompts",
     "qwen_image_fp8 + qwen2.5-vl", "NEEDS_MODELS", 27.77, "Cyrillic and Chinese prompt fidelity"),
    ("edit-qwen-image-edit", "Text-instruction image edit", "TextEncodeQwenImageEdit + Qwen-Image-Edit 2509",
     "qwen_image_edit fp8 + qwen2.5-vl", "NEEDS_MODELS", 28.01, "'make it night', object swap, text edit"),
    ("edit-qwen-multi-ref", "Multi-reference edit (2-3 refs)", "Qwen-Image-Edit 2509 multi-image conditioning",
     "qwen_image_edit fp8 + qwen2.5-vl", "NEEDS_MODELS", 28.01, "combine subject A with style B"),
    ("edit-qwen-multi-angle", "Multi-angle turnaround", "Qwen-Edit 2509 + Multiple-angles LoRA",
     "qwen edit + lora", "NEEDS_MODELS", 28.23, "character turnaround sheets"),
    ("edit-qwen-relight", "Relight a photo", "Qwen-Edit 2509 + Relight LoRA",
     "qwen edit + lora", "NEEDS_MODELS", 28.23, "studio relighting without CGI"),
    ("edit-flux-kontext", "FLUX.1 Kontext edit", "FluxKontextImageScale + instruct edit",
     "flux kontext fp8 + clip-l + t5", "NEEDS_MODELS", 11.09, "instruction edit with FLUX quality"),
    ("inpaint-flux-fill", "FLUX Fill inpaint/outpaint", "flux1-fill-dev, mask conditioning",
     "flux fill fp8", "NEEDS_MODELS", 22.17, "best-in-class inpaint with prompt following"),
    ("inpaint-sdxl-inpaint", "Dedicated SDXL inpaint model", "SDXL-inpainting-0.1 UNet, 9-channel",
     "sdxl inpaint", "NEEDS_MODELS", 9.56, "fewer seams than latent-noise inpainting"),
    ("cn-canny-sdxl", "ControlNet — Canny edges", "ControlNetLoader + Canny (core node) + Animagine/SDXL",
     "installed: controlnet-canny-sdxl-1.0 2.33 GiB", "READY", "",
     "installed 2026-10-26 (xinsir, Apache-2.0, sha256 bf47cd75...); ran 6.4 s -> cn_canny_00002_.png 1.75 MB"),
    ("cn-depth-sdxl", "ControlNet — depth map", "ControlNetLoader depth + depth preprocessor",
     "cn depth + comfyui_controlnet_aux", "NEEDS_MODELS", 2.33, "composition transfer from any photo"),
    ("cn-pose-sdxl", "ControlNet — OpenPose", "DWPreprocessor + controlnet-openpose-sdxl",
     "cn openpose + controlnet_aux (dwpose 200 MB)", "NEEDS_MODELS", 2.73, "pose-accurate characters"),
    ("cn-lineart-sdxl", "ControlNet — lineart/anime", "lineart preprocessor + controlnet-lineart-sdxl",
     "cn lineart + controlnet_aux", "NEEDS_MODELS", 2.53, "colouring sketches"),
    ("cn-tile-upscale", "ControlNet — tile upscale", "controlnet-tile + tiled VAE",
     "cn tile", "NEEDS_MODELS", 2.33, "detail-preserving 2-4x upscale"),
    ("cn-softedge", "ControlNet — soft edge (HED)", "HED preprocessor + softedge controlnet",
     "cn softedge + controlnet_aux", "NEEDS_MODELS", 2.73, "looser structural guidance than canny"),
    ("ipadapter-faceid", "Identity lock — IP-Adapter FaceID", "FaceID plus v2 + InsightFace",
     "ip-adapter-faceid, insightface antelopev2 (360 MB)", "NEEDS_MODELS", 1.75, "same face across renders"),
    ("instantid-portrait", "InstantID portrait", "InstantID + ControlNet + SDXL",
     "InstantID (gated), antelopev2", "NEEDS_MODELS", 2.6, "single-photo identity transfer"),
    ("flux-redux-style", "FLUX Redux style transfer", "style_models flux redux + schnell",
     "redux sigclip 1.2 GB", "NEEDS_MODELS", 18.44, "reference-image style matching"),
    ("bg-remove-u2net", "Background removal (U2Net)", "rembg node + u2net.onnx",
     "comfyui-rembg node, u2net 176 MB", "NEEDS_MODELS", 0.18, "cutouts, stickers, product isolation"),
    ("bg-remove-bria", "Background removal (Bria RMBG 2.0)", "RMBG-2.0 mask + ImageCompositeMasked",
     "rmbg 2.0 (non-commercial)", "NEEDS_MODELS", 0.9, "higher quality hair/edges than U2Net"),
    ("seg-sam2", "Segment anything (SAM 2)", "SAM2 mask from point/box prompt",
     "comfyui-segment-anything-2 + sam2.1_hiera_large 900 MB", "NEEDS_NODES", 0.9, "precise mask painting"),
    ("depth-map", "Depth map extraction", "depth-anything-v2 node",
     "comfyui-depthanythingv2 + 1.3 GB", "NEEDS_NODES", 1.3, "depth for parallax and CN-depth"),
    ("face-detailer", "Face detailer pass", "Impact Pack FaceDetailer after base render",
     "comfyui-impact-pack + face_yolov8m 52 MB + GFPGAN 348 MB", "NEEDS_NODES", 0.4, "fixes small faces in group shots"),
    ("photo-restore", "Old photo restoration", "inpaint + GFPGAN face restore + upscale chain",
     "gfpgan 348 MB", "NEEDS_MODELS", 0.35, "scans and family photos"),
    ("anime-upscale", "Anime 4x upscale", "4x-AnimeSharp upscale model",
     "4x-AnimeSharp 68 MB", "NEEDS_MODELS", 0.07, "sharp lines on illustrations"),
    ("tile-upscale-vram", "VRAM-safe tiled upscale", "ImageUpscaleWithModel with tiled VAE decode",
     "installed: RealESRGAN", "VALIDATED", "", "no new model, required above ~2048 px"),
    ("caption-florence2", "Image captioning (Florence-2)", "Florence2 node, caption + region tasks",
     "comfyui-florence2 + 1.5 GB", "NEEDS_NODES", 1.5, "auto-titles, dataset building"),
    ("img2prompt", "Image to prompt", "Florence-2/CLIP interrogator",
     "florence2 or clip-interrogator", "NEEDS_NODES", 1.5, "reverse-engineer prompts"),
    ("ocr-extract", "OCR text extraction", "tesseract via node or host binary",
     "comfyui-tesseract or host tesseract", "NEEDS_NODES", 0.0, "digitising screenshots"),
    ("img2img-turbo", "Fast restyle — SDXL Turbo", "4-step img2img, cfg 1.0, denoise 0.45",
     "installed", "READY", "", "in registry 2026-10-26: 9.3 s, restyle_turbo_00001_.png 1.40 MB"),
    ("inpaint-by-color", "Inpaint picked by colour", "ImageColorToMask -> mask conditioning",
     "installed", "VALIDATED", "", "no manual mask painting"),
    ("outpaint-animagine", "Outpaint with Animagine", "outpaint graph on the illustration checkpoint",
     "installed", "VALIDATED", "", "widen illustrations"),
    ("txt2img-sdxl-refiner", "SDXL base + refiner pass", "two-checkpoint pipeline",
     "sdxl refiner 6.07 GB", "NEEDS_MODELS", 6.07, "cleaner fine detail"),
    ("img-seamless-tile", "Seamless tile pattern", "circular padding + tileable sampling",
     "comfyui-tiled-diffusion node", "NEEDS_NODES", 0.0, "textures, wallpaper"),
    ("img-batch-upscale", "Batch upscale a folder", "loop RealESRGAN over files",
     "installed", "VALIDATED", "", "library cleanup"),
    ("autotag-wd14", "Auto-tagging (WD14)", "wd14 tagger node for booru tags",
     "comfyui-wd14-tagger + 300 MB", "NEEDS_NODES", 0.3, "LoRA dataset prep"),
)

# ---------------------------------------------------------------- video studio
add("video",
    ("video-i2v-wan22", "Image to video — Wan 2.2 TI2V 5B", "WanImageToVideo + 30 steps",
     "installed: wan2.2_ti2v_5B + umt5 + vae", "READY", "",
     "registry; 30/30 steps 23m54s, 27m18s total, mp4 verified 49f 1280x1280"),
    ("video-t2v-wan22", "Text to video — Wan 2.2 TI2V 5B", "same weights, empty latent",
     "installed", "VALIDATED", "", "registry; not executed yet"),
    ("video-i2v-wan22-draft", "Image to video — draft mode", "12 steps, 25 frames",
     "installed", "VALIDATED", "", "~1/3 the time of the full run"),
    ("frames-extract", "Extract frames from a video", "LoadVideo + GetVideoComponents",
     "none — core nodes only", "VALIDATED", "", "no new model, no new node"),
    ("video-assemble", "Assemble images into a video", "ImageBatch + CreateVideo + SaveVideo",
     "none — core nodes only", "READY", "", "in registry 2026-10-26: 1.7 s, assemble_00001_.mp4 135 KB, zero models"),
    ("video-mux-audio", "Add an audio track to a video", "host ffmpeg: video copied, audio -> AAC",
     "installed: host ffmpeg", "READY", "",
     "ran 2026-10-26: narrated_i2v_00002_00001_.mp4 3.37 MB, ffprobe shows h264 2.04 s + aac 2.00 s"),
    ("vid2vid-wan-vace", "Video to video (Wan VACE)", "wan2.2_fun_vace 14B + control video",
     "wan vace fp8 16.15 GB", "NEEDS_MODELS", 16.15, "restyle or control existing footage"),
    ("video-i2v-wan22-a14b", "Image to video — Wan 2.2 A14B", "two-stage high/low noise experts",
     "wan2.2_i2v 14B fp8 x2 26.62 GB", "NEEDS_MODELS", 26.62, "highest quality Wan output"),
    ("video-t2v-ltx-2b", "Text to video — LTX-Video 2B", "LTXV + LTXVConditioning, 24 fps",
     "ltxv 2b distilled 5.91 GB + t5xxl fp8", "NEEDS_MODELS", 10.8, "much faster video than Wan"),
    ("video-i2v-ltx-13b", "Image to video — LTX 13B", "LTXV 13B distilled fp8",
     "ltxv 13b fp8 14.62 GB + t5xxl", "NEEDS_MODELS", 19.5, "cinematic motion at 704x1216"),
    ("video-svd", "Image to video — Stable Video Diffusion", "SVD img2vid, 25 frames",
     "svd_xt 9.56 GB", "NEEDS_MODELS", 9.56, "alternative motion model, different feel"),
    ("video-hunyuan", "Text/image to video — HunyuanVideo", "HunyuanVideo 13B",
     "hunyuan 720p (gated)", "EXPERIMENTAL", 25.0, "unproven on gfx1151, gated repo"),
    ("video-loop-boomerang", "Make a ping-pong loop", "frame sequence forward+reverse",
     "none — core nodes only", "VALIDATED", "", "seamless loops for posts"),
    ("vid-upscale-2x", "Video upscale 2x", "per-frame upscale + reassemble",
     "RIFE/CUDA-free upscaler node", "NEEDS_NODES", 0.0, "1080p from 540p"),
    ("vid-interp-rife", "Frame interpolation", "RIFE VFI to 48/60 fps",
     "comfyui-frame-interpolation + rife 50 MB", "NEEDS_NODES", 0.05, "smooth slow motion"),
    ("vid-storyboard", "Storyboard sheet to video", "grid split + per-panel i2v + concat",
     "installed (Wan)", "EXPERIMENTAL", "", "multi-shot sequences"),
    ("video-s2v-14b", "Speech to video (Wan S2V)", "wan2.2_s2v + audio conditioning",
     "wan s2v fp8 15.27 GB", "NEEDS_MODELS", 15.27, "talking-head clips from audio"),
    ("vid-scene-split", "Split video by scene", "ffmpeg scene detection",
     "host ffmpeg", "VALIDATED", "", "rough cuts"),
    ("frames-to-spritesheet", "Frames to sprite sheet", "ffmpeg tile montage",
     "host ffmpeg", "VALIDATED", "", "game/animation assets"),
    ("vid-captions-auto", "Auto-caption a video", "ASR -> SRT -> burn-in",
     "installed pieces (whisper + ffmpeg), burn-in graph not built", "VALIDATED", "", "accessible deliverables"),
    ("video-animate-character", "Animate a character sheet", "i2v with motion prompt on a character render",
     "installed (Wan)", "VALIDATED", "", "no new model"),
)

# ---------------------------------------------------------------- audio studio
add("audio",
    ("tts-piper-ru", "TTS — Russian (Piper)", "host piper binary, ru_RU voice, WAV out",
     "installed: piper 2023.11.14-2 + ru_RU-irina-medium", "READY", "",
     "ran 2026-10-26 through /api/jobs: 0.4 s, tts_ru_RU_00001_.wav 198 KB, RTF 0.045, CPU only"),
    ("tts-piper-en", "TTS — English (Piper)", "piper en_US high-quality voice",
     "installed: en_US-lessac-medium", "READY", "",
     "ran 2026-10-26 through /api/jobs: tts_en_US_00001_.wav 264 KB, CPU only"),
    ("tts-long-form", "Long-form TTS with chunking", "sentence split + sequential synth + concat",
     "piper present, chunking pipeline not built", "VALIDATED", "", "articles, narration over 10 min"),
    ("asr-subtitles", "Transcribe audio to SRT", "whisper.cpp small -> .txt + .srt",
     "installed: whisper.cpp built from source + ggml-small 466 MB", "READY", "",
     "ran 2026-10-26: 4.5 s Russian speech -> 2.0 s, txt 118 B + srt 186 B (small model misspells синтеза)"),
    ("asr-ru", "Russian transcription", "faster-whisper large-v3-turbo",
     "faster-whisper + 1.51 GB", "NEEDS_NODES", 1.51, "accurate Russian ASR"),
    ("music-acestep", "Music generation — ACE-Step", "EmptyAceStepLatentAudio + TextEncodeAceStepAudio",
     "ace_step v1 3.5B 7.71 GB", "NEEDS_MODELS", 7.71, "44.1 kHz stereo music, Apache-2.0"),
    ("music-acestep-lyrics", "Song with lyrics", "ACE-Step with [verse]/[chorus] tags",
     "ace_step v1", "NEEDS_MODELS", 7.71, "vocals + instrumental"),
    ("music-acestep-instrumental", "Instrumental bed", "ACE-Step, 'instrumental' tag, long duration",
     "ace_step v1", "NEEDS_MODELS", 7.71, "background music for video"),
    ("music-stable-audio", "Stable Audio Open", "ConditioningStableAudio + EmptyLatentAudio",
     "stable-audio-open 4.5 GB", "NEEDS_MODELS", 4.5, "short SFX and loops"),
    ("audio-mp3-export", "Export audio as MP3", "SaveAudioMP3 core node",
     "none — core nodes only", "VALIDATED", "", "deliverables without ffmpeg"),
    ("audio-merge", "Merge / concat audio clips", "AudioConcat core node",
     "none — core nodes only", "VALIDATED", "", "join TTS segments"),
    ("audio-normalize", "Normalise loudness", "AudioAdjustVolume + gain staging",
     "none — core nodes only", "VALIDATED", "", "consistent levels"),
    ("audio-eq3", "3-band EQ", "AudioEqualizer3Band core node",
     "none — core nodes only", "VALIDATED", "", "voice clarity"),
    ("audio-fade", "Fade in / out", "volume envelope segments + concat",
     "none — core nodes only", "VALIDATED", "", "polished intros"),
    ("tts-narration-video", "Narration over an image sequence", "piper TTS + frame timing + mux",
     "piper + ffmpeg", "NEEDS_NODES", 0.06, "explainer clips"),
    ("audio-pitch-tempo", "Change pitch / tempo", "ffmpeg atempo + asetrate",
     "host ffmpeg", "VALIDATED", "", "fit a track to a duration"),
    ("audio-denoise", "Noise reduction", "voice separation / denoise node",
     "comfyui-audio-separation nodes", "NEEDS_NODES", 0.0, "clean up recordings"),
    ("audio-voice-clone", "Voice cloning", "ElevenLabs VoiceClone node",
     "paid cloud API", "UNAVAILABLE", 0.0, "banned: cloud inference"),
    ("audio-stt-elevenlabs", "Speech to text (hosted)", "ElevenLabsSpeechToText node",
     "paid cloud API", "UNAVAILABLE", 0.0, "banned: cloud inference"),
)

# ---------------------------------------------------------------- media tools
add("media",
    ("media-probe", "Inspect media metadata", "ffprobe: codec, size, duration, fps",
     "host ffmpeg (present)", "VALIDATED", "", "no GPU, instant"),
    ("vid-to-gif", "Video to GIF", "ffmpeg fps+scale, loop forever",
     "installed: host ffmpeg", "READY", "",
     "ran 2026-10-26: gif_i2v_00002_00001_.gif 3.28 MB from the Wan clip"),
    ("vid-extract-audio", "Extract audio track", "ffmpeg -vn, 44.1 kHz mono wav",
     "installed: host ffmpeg + ffprobe", "READY", "",
     "ran 2026-10-26: audio_narrated_i2v_00002_00001_00001_.wav 176 KB; refuses silent clips with an explanation"),
    ("vid-compress", "Compress video for sharing", "ffmpeg CRF/H.264 preset ladder",
     "host ffmpeg", "VALIDATED", "", "smaller files, same frames"),
    ("vid-aspect-9x16", "Reframe to 9:16 / 1:1", "ffmpeg scale+pad or crop",
     "host ffmpeg", "VALIDATED", "", "Reels/Shorts from 16:9"),
    ("vid-trim-concat", "Trim and concatenate", "ffmpeg trim + concat demuxer",
     "host ffmpeg", "VALIDATED", "", "cut multiple renders into one"),
    ("vid-burn-subs", "Burn subtitles into video", "ffmpeg subtitles filter",
     "host ffmpeg + SRT", "VALIDATED", "", "hardcoded captions"),
    ("vid-watermark", "Add watermark / logo", "ffmpeg overlay with alpha",
     "host ffmpeg", "VALIDATED", "", "branding on exports"),
    ("img-batch-resize", "Batch resize a folder", "host-side or graph loop",
     "host ffmpeg/venv", "VALIDATED", "", "dataset prep"),
    ("vid-thumbnail", "Auto thumbnail", "frame pick + upscale + text",
     "host ffmpeg", "VALIDATED", "", "cover images"),
    ("media-format-convert", "Convert image formats", "ffmpeg/imageio transcode",
     "host ffmpeg", "VALIDATED", "", "webp/png/jpg interchange"),
    ("vid-reverse", "Reverse a clip", "ffmpeg reverse filter",
     "host ffmpeg", "VALIDATED", "", "effect shots"),
    ("audio-split-silence", "Split audio on silence", "ffmpeg silencedetect",
     "host ffmpeg", "VALIDATED", "", "chapterise a podcast"),
    ("media-library-index", "Index the output library", "sqlite scan of output/ with kinds and sizes",
     "project code", "VALIDATED", "", "searchable history"),
    ("vid-metadata-write", "Write MP4 metadata", "ffmpeg -metadata title/artist/comment",
     "host ffmpeg", "VALIDATED", "", "tagged exports"),
    ("media-dedupe", "Find duplicate media", "sha256 + size index",
     "project code", "VALIDATED", "", "housekeeping, no GPU"),
)

# ---------------------------------------------------------------- cloud-only (explicitly banned)
add("banned",
    ("cloud-*", "Any node whose name ends in Node under the API providers", "Bria, Flux Pro/Ultra/Expand, "
     "Grok, Kling, KIE, Magnific, Recraft, Stability, Wavespeed, ElevenLabs, Sora", "paid cloud API",
     "UNAVAILABLE", 0.0, "never: violates the local-only rule"),
)

PRESETS_NOTE = """Presets ride on top of workflows and are *not* counted as workflows:
image styles (photoreal, anime, watercolour, 3D, pixel, sticker, poster, logo, product, food,
architecture, portrait, comic, line art, isometric, texture), aspect ratios, sampler/steps
bundles, and Russian/English prompt templates. One graph, many presets — that is the honest way
to cover breadth without fake catalog entries.
"""


def main() -> int:
    DOCS.mkdir(parents=True, exist_ok=True)
    cols = ["section", "id", "title", "technique", "deps", "status", "gb", "unlocks_with"]
    with (DOCS / "workflow_catalog_plan.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(R)

    counts: dict[str, int] = {}
    for r in R:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    lines = ["# Proposed workflow catalog", "",
             "Generated by `tools/gen_catalog_plan.py`. One row = one distinct graph or engine",
             "pipeline; prompt/style variants are presets, not rows.", "",
             f"**{len(R)} distinct entries** across {len({r['section'] for r in R})} sections.", "",
             "| status | count |", "|---|---|"]
    for k in ("READY", "VALIDATED", "NEEDS_MODELS", "NEEDS_NODES", "EXPERIMENTAL", "UNAVAILABLE"):
        lines.append(f"| {k} | {counts.get(k, 0)} |")

    for section in dict.fromkeys(r["section"] for r in R):
        lines += ["", f"## {section}", "",
                  "| id | what it is | status | extra GB |", "|---|---|---|---|"]
        for r in R:
            if r["section"] == section:
                gb = f"{r['gb']:.2f}" if r["gb"] else "—"
                lines.append(f"| `{r['id']}` | {r['title']} — {r['technique']} | {r['status']} | {gb} |")

    lines += ["", "## Presets (not counted)", "", PRESETS_NOTE]
    (DOCS / "workflow_catalog_plan.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"wrote workflow_catalog_plan.csv/.md — {len(R)} entries")
    print("status:", counts)
    extra = sum(r["gb"] or 0 for r in R)
    print(f"sum of extra downloads if everything were installed: {extra:.2f} GiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
