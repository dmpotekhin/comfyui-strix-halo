# Workflow catalog

Generated from `studio/registry/workflows/*.json` by
`studio/tools/gen_registry_docs.py`. Do not edit by hand — re-run the generator.

**Total entries: 127.** Every id below exists as a JSON file in the registry.

`test_status` is written by whoever ran the graph; it is not inferred.

| id | name | category | output | status | tested | params | models | notes |
|---|---|---|---|---|---|---|---|---|
| `asr-all-formats` | Transcript — txt + SRT + VTT + JSON at once | audio | text | ready_to_test | untested | 3 | `ggml-small.bin` | — |
| `asr-json` | Transcript — JSON with word timings | audio | text | ready_to_test | untested | 3 | `ggml-small.bin` | — |
| `asr-large` | Transcribe — large-v3 (accurate) | audio | text | ready_to_test | untested | 4 | `ggml-large-v3-q5_0.bin` | — |
| `asr-srt` | Subtitles — SRT from audio | audio | text | ready_to_test | untested | 3 | `ggml-small.bin` | — |
| `asr-transcribe` | Transcribe speech — whisper.cpp | audio | text | ready_to_test | untested | 2 | — | — |
| `asr-translate-en` | Transcribe + translate to English | audio | text | ready_to_test | untested | 5 | `ggml-large-v3-q5_0.bin` | — |
| `asr-vtt` | Subtitles — WebVTT from audio | audio | text | ready_to_test | untested | 3 | `ggml-small.bin` | — |
| `audio-fade` | Fade — in and out | audio | audio | ready_to_test | untested | 3 | — | — |
| `audio-flac` | Convert — FLAC (lossless) | audio | audio | ready_to_test | untested | 3 | — | — |
| `audio-from-video` | Extract audio track from a video | audio | audio | ready_to_test | untested | 1 | — | — |
| `audio-join` | Join — concatenate audio files | audio | audio | ready_to_test | untested | 1 | — | — |
| `audio-loudnorm-16` | Loudness — −16 LUFS (podcast/YouTube) | audio | audio | ready_to_test | untested | 3 | — | — |
| `audio-loudnorm-23` | Loudness — −23 LUFS (broadcast) | audio | audio | ready_to_test | untested | 3 | — | — |
| `audio-mp3` | Convert — MP3 192 kbps | audio | audio | ready_to_test | untested | 3 | — | — |
| `audio-ogg` | Convert — OGG Vorbis | audio | audio | ready_to_test | untested | 3 | — | — |
| `audio-slow-down` | Speed — 0.9× slower | audio | audio | ready_to_test | untested | 2 | — | — |
| `audio-speed-up` | Speed — 1.15× faster (no pitch change) | audio | audio | ready_to_test | untested | 2 | — | — |
| `audio-trim` | Trim — cut a section | audio | audio | ready_to_test | untested | 3 | — | — |
| `audio-trim-silence` | Cleanup — trim leading/trailing silence | audio | audio | ready_to_test | untested | 2 | — | — |
| `music-ace-step` | Song with vocals — ACE-Step 3.5B | audio | audio | ready_to_test | untested | 7 | `ace_step_v1_3.5b.safetensors` | — |
| `tts-en-narrator` | Speech — English, documentary narrator | audio | audio | ready_to_test | untested | 4 | `en_US-lessac-medium.onnx` | — |
| `tts-en-piper` | English speech — Piper | audio | audio | ready_to_test | untested | 3 | — | — |
| `tts-ru-fast` | Speech — Russian, brisk delivery | audio | audio | ready_to_test | untested | 3 | `ru_RU-irina-medium.onnx` | — |
| `tts-ru-piper` | Russian speech — Piper | audio | audio | ready_to_test | untested | 3 | — | — |
| `tts-ru-pitch-down` | Speech — Russian, deeper voice | audio | audio | ready_to_test | untested | 3 | `ru_RU-irina-medium.onnx` | — |
| `tts-ru-pitch-up` | Speech — Russian, higher voice | audio | audio | ready_to_test | untested | 3 | `ru_RU-irina-medium.onnx` | — |
| `tts-ru-slow` | Speech — Russian, slow narration | audio | audio | ready_to_test | untested | 3 | `ru_RU-irina-medium.onnx` | — |
| `character-ipadapter` | Consistent character — IP-Adapter reference | characters | image | ready_to_test | untested | 11 | `animagine-xl-4.0.safetensors`, `ip-adapter-plus_sdxl_vit-h.safetensors`, `CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors` | — |
| `image-resize` | Smart resize / prepare for upload | enhancement | image | ready_to_test | untested | 3 | — | — |
| `img-detail-2x` | Upscale 2× with recovered detail — RealESRGAN | enhancement | image | ready_to_test | untested | 2 | `RealESRGAN_x4.pth` | — |
| `img-detail-2x-x4plus` | Detail upscale — 2x (x4plus) | enhancement | image | ready_to_test | untested | 1 | `real_esrgan_x4plus.pth` | — |
| `img-detail-3x` | Upscale 3× with recovered detail — RealESRGAN | enhancement | image | ready_to_test | untested | 2 | `RealESRGAN_x4.pth` | — |
| `img-detail-3x-x4plus` | Detail upscale — 3x (x4plus) | enhancement | image | ready_to_test | untested | 1 | `real_esrgan_x4plus.pth` | — |
| `img-downscale-50` | Image — halve the size | enhancement | image | ready_to_test | untested | 2 | — | — |
| `img-upscale-2x` | Image — enlarge 2× (lanczos) | enhancement | image | ready_to_test | untested | 3 | — | — |
| `upscale-realesrgan` | Upscale image 4x — RealESRGAN | enhancement | image | ready_to_test | untested | 1 | `RealESRGAN_x4.pth` | — |
| `upscale-x4plus` | Upscale — Real-ESRGAN x4plus | enhancement | image | ready_to_test | untested | 1 | `real_esrgan_x4plus.pth` | — |
| `cutout-u2net` | Cutout — subject on transparent background (U²-Net) | image-editing | image | ready_to_test | untested | 1 | `u2net.onnx` | — |
| `img-convert-jpg` | Convert — to JPEG | image-editing | image | ready_to_test | untested | 4 | — | — |
| `img-convert-png` | Convert — to PNG (lossless) | image-editing | image | ready_to_test | untested | 4 | — | — |
| `img-convert-web-small` | Convert — WebP for the web (1280 px) | image-editing | image | ready_to_test | untested | 4 | — | — |
| `img-convert-webp` | Convert — to WebP | image-editing | image | ready_to_test | untested | 4 | — | — |
| `img-crop-16x9` | Crop — widescreen 16:9 | image-editing | image | ready_to_test | untested | 4 | — | — |
| `img-crop-1x1` | Crop — square 1:1 | image-editing | image | ready_to_test | untested | 4 | — | — |
| `img-crop-4x5` | Crop — portrait 4:5 | image-editing | image | ready_to_test | untested | 4 | — | — |
| `img-crop-9x16` | Crop — vertical 9:16 | image-editing | image | ready_to_test | untested | 4 | — | — |
| `img-resize-1280` | Resize — 1280 px wide | image-editing | image | ready_to_test | untested | 5 | — | — |
| `img-resize-2048` | Resize — 2048 px wide | image-editing | image | ready_to_test | untested | 5 | — | — |
| `img-resize-height-720` | Resize — 720 px tall | image-editing | image | ready_to_test | untested | 5 | — | — |
| `img-strip-exif` | Strip — remove EXIF and metadata | image-editing | image | ready_to_test | untested | 3 | — | — |
| `img-thumb-320` | Thumbnail — 320 px | image-editing | image | ready_to_test | untested | 3 | — | — |
| `img-thumb-640` | Thumbnail — 640 px | image-editing | image | ready_to_test | untested | 3 | — | — |
| `img-watermark-center` | Watermark — centre proof mark | image-editing | image | ready_to_test | untested | 7 | — | — |
| `img-watermark-corner` | Watermark — corner stamp | image-editing | image | ready_to_test | untested | 7 | — | — |
| `img2img-anime` | Restyle — photo to anime (Animagine) | image-editing | image | ready_to_test | untested | 7 | `animagine-xl-4.0.safetensors` | — |
| `img2img-restyle` | Image to Image — restyle / variation | image-editing | image | ready_to_test | untested | 7 | `animagine-xl-4.0.safetensors` | — |
| `img2img-soft` | Restyle — soft (denoise 0.35) | image-editing | image | ready_to_test | untested | 7 | `sd_xl_turbo_1.0_fp16.safetensors` | — |
| `img2img-strong` | Restyle — strong (denoise 0.75) | image-editing | image | ready_to_test | untested | 7 | `sd_xl_turbo_1.0_fp16.safetensors` | — |
| `img2img-turbo` | Fast Restyle — SDXL Turbo img2img | image-editing | image | ready_to_test | untested | 7 | `animagine-xl-4.0.safetensors` | — |
| `inpaint-remove` | Inpaint — remove an object | image-editing | image | ready_to_test | untested | 7 | `sd_xl_turbo_1.0_fp16.safetensors` | requires an alpha mask. —. |
| `inpaint-turbo` | Inpaint — replace part of an image (mask) | image-editing | image | available | passed (2026-10-26) | 7 | `sd_xl_turbo_1.0_fp16.safetensors` | requires an alpha mask. Job completed in 5.1 s on 2026-10-26 producing inpaint_00002_.png (512x512, 444 KB). Verified visually: the masked region (100,140)-(220,260) was regenerated wh. |
| `outpaint-turbo` | Outpaint — extend the frame | image-editing | image | ready_to_test | untested | 9 | `sd_xl_turbo_1.0_fp16.safetensors` | — |
| `sam-bg-blur` | Background blur — by point (MobileSAM) | image-editing | image | ready_to_test | untested | 7 | `mobilesam.encoder.onnx`, `mobilesam.decoder.onnx`, `sam-vit-base-encoder.onnx`, `sam-vit-base-decoder.onnx` | — |
| `sam-bg-replace` | Replace background — by point (MobileSAM + SDXL-Turbo) | image-editing | image | ready_to_test | untested | 12 | `mobilesam.encoder.onnx`, `mobilesam.decoder.onnx`, `sd_xl_turbo_1.0_fp16.safetensors`, `sam-vit-base-encoder.onnx`, `sam-vit-base-decoder.onnx` | — |
| `sam-cutout` | Cutout — by point (MobileSAM) | image-editing | image | ready_to_test | untested | 6 | `mobilesam.encoder.onnx`, `mobilesam.decoder.onnx`, `sam-vit-base-encoder.onnx`, `sam-vit-base-decoder.onnx` | — |
| `sam-layer-split` | Layers — subject + background (SAM) | image-editing | image | ready_to_test | untested | 6 | `mobilesam.encoder.onnx`, `mobilesam.decoder.onnx`, `sam-vit-base-encoder.onnx`, `sam-vit-base-decoder.onnx` | — |
| `sam-mask` | Mask — by point (MobileSAM) | image-editing | image | ready_to_test | untested | 6 | `mobilesam.encoder.onnx`, `mobilesam.decoder.onnx`, `sam-vit-base-encoder.onnx`, `sam-vit-base-decoder.onnx` | — |
| `sam-object-remove` | Remove an object — by point (MobileSAM + SDXL-Turbo) | image-editing | image | ready_to_test | untested | 12 | `mobilesam.encoder.onnx`, `mobilesam.decoder.onnx`, `sd_xl_turbo_1.0_fp16.safetensors`, `sam-vit-base-encoder.onnx`, `sam-vit-base-decoder.onnx` | — |
| `sam-overlay` | Mask preview — by point (MobileSAM) | image-editing | image | ready_to_test | untested | 6 | `mobilesam.encoder.onnx`, `mobilesam.decoder.onnx`, `sam-vit-base-encoder.onnx`, `sam-vit-base-decoder.onnx` | — |
| `sam-region-restyle` | Restyle only the subject (SAM + img2img) | image-editing | image | ready_to_test | untested | 12 | `mobilesam.encoder.onnx`, `mobilesam.decoder.onnx`, `sam-vit-base-encoder.onnx`, `sam-vit-base-decoder.onnx`, `sd_xl_turbo_1.0_fp16.safetensors` | — |
| `cn-canny-sdxl` | Edge-guided generation — SDXL Canny ControlNet | image-generation | image | ready_to_test | untested | 9 | `sd_xl_turbo_1.0_fp16.safetensors`, `controlnet-canny-sdxl-1.0.safetensors` | — |
| `cn-depth-sdxl` | Depth-guided generation — host depth map → SDXL Depth ControlNet | image-generation | image | ready_to_test | untested | 7 | `sd_xl_turbo_1.0_fp16.safetensors`, `controlnet-depth-sdxl-1.0.safetensors`, `depth-anything-v2-small.onnx` | — |
| `cn-dual-canny-depth` | ControlNet — canny + depth together | image-generation | image | ready_to_test | untested | 8 | `sd_xl_turbo_1.0_fp16.safetensors`, `controlnet-canny-sdxl-1.0.safetensors`, `controlnet-depth-sdxl-1.0.safetensors`, `depth-anything-v2-small.onnx` | — |
| `cn-triple-lock` | ControlNet — canny, gentle (0.35) | image-generation | image | ready_to_test | untested | 9 | `sd_xl_turbo_1.0_fp16.safetensors`, `controlnet-canny-sdxl-1.0.safetensors` | — |
| `format-cinema` | Format — cinema 1344×768 | image-generation | image | ready_to_test | untested | 8 | `sd_xl_turbo_1.0_fp16.safetensors` | — |
| `format-landscape` | Format — landscape 1216×832 | image-generation | image | ready_to_test | untested | 8 | `sd_xl_turbo_1.0_fp16.safetensors` | — |
| `format-portrait` | Format — portrait 832×1216 | image-generation | image | ready_to_test | untested | 8 | `sd_xl_turbo_1.0_fp16.safetensors` | — |
| `format-square` | Format — square 1024×1024 | image-generation | image | ready_to_test | untested | 8 | `sd_xl_turbo_1.0_fp16.safetensors` | — |
| `format-story` | Format — story 768×1344 | image-generation | image | ready_to_test | untested | 8 | `sd_xl_turbo_1.0_fp16.safetensors` | — |
| `img-batch-4` | Batch — four takes of one prompt | image-generation | image | ready_to_test | untested | 6 | `sd_xl_turbo_1.0_fp16.safetensors` | — |
| `img-hires-fix` | Image — hires fix (sample, upscale, resample) | image-generation | image | ready_to_test | untested | 8 | `sd_xl_turbo_1.0_fp16.safetensors` | — |
| `style-3d` | Style — 3D render | image-generation | image | ready_to_test | untested | 8 | `sd_xl_turbo_1.0_fp16.safetensors` | — |
| `style-anime` | Style — anime (Animagine XL 4.0) | image-generation | image | ready_to_test | untested | 8 | `animagine-xl-4.0.safetensors` | — |
| `style-cinematic` | Style — cinematic still | image-generation | image | ready_to_test | untested | 8 | `sd_xl_turbo_1.0_fp16.safetensors` | — |
| `style-photo` | Style — photographic | image-generation | image | ready_to_test | untested | 8 | `sd_xl_turbo_1.0_fp16.safetensors` | — |
| `style-product` | Style — product shot | image-generation | image | ready_to_test | untested | 8 | `sd_xl_turbo_1.0_fp16.safetensors` | — |
| `style-sketch` | Style — pencil sketch | image-generation | image | ready_to_test | untested | 8 | `sd_xl_turbo_1.0_fp16.safetensors` | — |
| `txt2img-animagine` | Text to Image — Animagine XL 4.0 (illustration) | image-generation | image | ready_to_test | untested | 9 | `animagine-xl-4.0.safetensors` | — |
| `txt2img-turbo` | Text to Image — SDXL Turbo (fast draft) | image-generation | image | ready_to_test | untested | 8 | `sd_xl_turbo_1.0_fp16.safetensors` | — |
| `txt2img-upscale` | Text to Image + RealESRGAN 4x upscale | image-generation | image | ready_to_test | untested | 8 | `animagine-xl-4.0.safetensors`, `RealESRGAN_x4.pth` | — |
| `vid-add-audio` | Narrate a clip — mux audio onto video | video | video | ready_to_test | untested | 2 | — | — |
| `vid-aspect-16x9-pad` | Video — 16:9 with bars | video | video | ready_to_test | untested | 3 | — | — |
| `vid-aspect-1x1-crop` | Video — square 1:1 crop | video | video | ready_to_test | untested | 3 | — | — |
| `vid-aspect-4x5-crop` | Video — 4:5 portrait crop | video | video | ready_to_test | untested | 3 | — | — |
| `vid-aspect-9x16-crop` | Video — 9:16 vertical crop | video | video | ready_to_test | untested | 3 | — | — |
| `vid-aspect-9x16-pad` | Video — 9:16 vertical with bars | video | video | ready_to_test | untested | 3 | — | — |
| `vid-burn-subs` | Video — burn subtitles into the picture | video | video | ready_to_test | untested | 4 | — | — |
| `vid-compress-tiny` | Video — compress hard (854 px) | video | video | ready_to_test | untested | 4 | — | — |
| `vid-compress-web` | Video — compress for the web | video | video | ready_to_test | untested | 4 | — | — |
| `vid-fps-24` | Video — convert to 24 fps | video | video | ready_to_test | untested | 2 | — | — |
| `vid-fps-30` | Video — convert to 30 fps | video | video | ready_to_test | untested | 2 | — | — |
| `vid-fps-60` | Video — convert to 60 fps | video | video | ready_to_test | untested | 2 | — | — |
| `vid-frames-1fps` | Frames — one per second | video | image | ready_to_test | untested | 3 | — | — |
| `vid-frames-contact` | Frames — contact sheet frames | video | image | ready_to_test | untested | 3 | — | — |
| `vid-join` | Video — join clips | video | video | ready_to_test | untested | 1 | — | — |
| `vid-mux-subs` | Video — attach subtitles | video | video | ready_to_test | untested | 3 | — | — |
| `vid-poster` | Video — poster frame | video | image | ready_to_test | untested | 2 | — | — |
| `vid-resize-1080` | Video — scale to 1080p width | video | video | ready_to_test | untested | 3 | — | — |
| `vid-resize-720` | Video — scale to 720p width | video | video | ready_to_test | untested | 3 | — | — |
| `vid-rotate-180` | Video — rotate 180° | video | video | ready_to_test | untested | 2 | — | — |
| `vid-rotate-90` | Video — rotate 90° | video | video | ready_to_test | untested | 2 | — | — |
| `vid-speed-2x` | Video — 2× speed | video | video | ready_to_test | untested | 2 | — | — |
| `vid-speed-half` | Video — half speed (slow motion) | video | video | ready_to_test | untested | 2 | — | — |
| `vid-to-gif` | Video to GIF | video | image | ready_to_test | untested | 3 | — | — |
| `vid-trim` | Video — cut a clip | video | video | ready_to_test | untested | 3 | — | — |
| `video-assemble` | Assemble images into an MP4 | video | video | ready_to_test | untested | 3 | — | — |
| `video-i2v-wan22` | Image to Video — Wan 2.2 TI2V-5B | video | video | ready_to_test | untested | 11 | `wan2.2_ti2v_5B_fp16.safetensors`, `umt5_xxl_fp8_e4m3fn_scaled.safetensors`, `wan2.2_vae.safetensors`, `CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors` | — |
| `video-t2v-wan22` | Text to Video — Wan 2.2 TI2V-5B | video | video | ready_to_test | untested | 10 | `wan2.2_ti2v_5B_fp16.safetensors`, `umt5_xxl_fp8_e4m3fn_scaled.safetensors`, `wan2.2_vae.safetensors` | — |
| `vlm-alt-text` | Image → alt text (accessibility) | vision-language | text | ready_to_test | untested | 4 | `moondream2-text-model-f16_ct-vicuna.gguf`, `moondream2-mmproj-f16-20250414.gguf` | — |
| `vlm-caption` | Image → caption (one line) | vision-language | text | ready_to_test | untested | 4 | `moondream2-text-model-f16_ct-vicuna.gguf`, `moondream2-mmproj-f16-20250414.gguf` | — |
| `vlm-caption-detailed` | Image → detailed description | vision-language | text | ready_to_test | untested | 4 | `moondream2-text-model-f16_ct-vicuna.gguf`, `moondream2-mmproj-f16-20250414.gguf` | — |
| `vlm-dataset-caption` | Image → training caption | vision-language | text | ready_to_test | untested | 4 | `moondream2-text-model-f16_ct-vicuna.gguf`, `moondream2-mmproj-f16-20250414.gguf` | — |
| `vlm-image-to-prompt` | Image → text-to-image prompt | vision-language | text | ready_to_test | untested | 4 | `moondream2-text-model-f16_ct-vicuna.gguf`, `moondream2-mmproj-f16-20250414.gguf` | — |
| `vlm-inventory` | Image → list of objects | vision-language | text | ready_to_test | untested | 4 | `moondream2-text-model-f16_ct-vicuna.gguf`, `moondream2-mmproj-f16-20250414.gguf` | — |
| `vlm-read-text` | Image → read the text in it | vision-language | text | ready_to_test | untested | 4 | `moondream2-text-model-f16_ct-vicuna.gguf`, `moondream2-mmproj-f16-20250414.gguf` | — |
| `vlm-translate-text` | Image → translate its text to English | vision-language | text | ready_to_test | untested | 4 | `moondream2-text-model-f16_ct-vicuna.gguf`, `moondream2-mmproj-f16-20250414.gguf` | — |
| `vlm-vqa` | Image → answer a question | vision-language | text | ready_to_test | untested | 4 | `moondream2-text-model-f16_ct-vicuna.gguf`, `moondream2-mmproj-f16-20250414.gguf` | — |

## Summary

- entries: 127
- with a recorded passing run: 1 (inpaint-turbo)
- never executed here: 126

## Known limitations

- One heavy job runs at a time; a second submission waits in the queue.
- Wan 2.2 video is slow on this iGPU: 30 steps at 640x640 measured 23 min 54 s of
  sampling alone, before VAE decode and encoding.
- Audio/TTS, Qwen-Image-Edit and ControlNet graphs are **not** in the registry.
  There are no such workflows to run; nothing here pretends otherwise.
