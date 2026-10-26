# Existing Workflows and run history

## 1. Files on disk

`workflows/` is bind-mounted to `/opt/ComfyUI/user/default/workflows`, i.e. it is where the
ComfyUI **web editor** keeps saved graphs.

| File | Size | Format | Note |
|---|---|---|---|
| `smoke_sdxl_turbo.api.json` | 1.2 KiB | **API format** (flat `{id: {class_type, inputs}}`) | the only real workflow; submit-ready |
| `_mount_probe.json` | 29 B | marker | leftover from earlier mount verification |
| `_mount_probe_disk.json` | 51 B | marker | idem, written from the host side |

So: **exactly one workflow exists**, and it is in API format (not the editor's
`nodes/links` format). It is the SDXL Turbo text-to-image smoke test: 7 nodes
(`CheckpointLoaderSimple` → 2× `CLIPTextEncode` → `EmptyLatentImage` → `KSampler`
(4 steps, cfg 1.0, euler/normal, seed 42) → `VAEDecode` → `SaveImage`), 512×512.

This matters for the design: the editor format and the API format are different. The studio
registry stores **API-format graphs** (no conversion layer, no graph-format guessing). Editor
graphs can be exported to API format from ComfyUI's "Export (API)" menu and dropped in.

## 2. What has actually been run — `/history` evidence

`/history` (65 retained entries, pruned by ComfyUI) grouped by graph shape and outcome:

| Graph shape | Success | Error | Nodes distinguishing it |
|---|---|---|---|
| **IP-Adapter SDXL** | **47** | 0 | `IPAdapterAdvanced` + `CheckpointLoaderSimple` + `KSampler` + `SaveImage` |
| **SDXL text-to-image** | **10** | 0 | `CheckpointLoaderSimple` + `KSampler` + `SaveImage` |
| **Wan 2.2 image-to-video** | **7** | 1 | `UNETLoader` + `WanImageToVideo` + `KSampler` + `SaveVideo` |

Outputs referenced by history: **57 PNG + 7 MP4**.

Conclusions (evidence-based, not assumed):

1. **SDXL still-image generation is proven on this box** (10/10).
2. **IP-Adapter reference conditioning is proven** (47/47) — this is the best-tested path
   available and becomes the backbone of character-consistency workflows.
3. **Wan 2.2 image-to-video is proven** (7/8; the single error is consistent with a
   parameters problem such as a frame count not satisfying `4n+1`, or an OOM, but the error
   text was not preserved — recorded as *unknown cause*, not guessed).
4. **Text-to-video on Wan has NOT been demonstrated** in retained history (all 8 Wan runs
   include `WanImageToVideo`, an image-conditioned node). T2V must be treated as
   **ready-to-test**, not verified.

## 3. Assets on disk that imply prior work

| Path | Content | Interpretation |
|---|---|---|
| `input/` | `ref_ginger*.png`, `ref_raccoon*.png`, `ref_whites_w.png`, `mult.jpg`, `example.png`, text files | reference images for IP-Adapter experiments (matches the 47 IP-Adapter runs) |
| `clips/` | 17 files, 52 MiB: `clip_01_s777.mp4`, `clip_01_s777_30st_480.mp4`, `..._style.mp4`, `card_title.png`, `card_end.png`, `montage_seam_check.png` | a short-video assembly experiment: per-clip renders, a 480p variant, a "style" variant, title/end cards, and a seam-quality check |
| `frames/` | empty | frame extraction scratch dir |
| `output/` | empty except `_persist_test.txt` and the placeholder | outputs are cleaned up after collection (clips were moved to `clips/`) |
| `output/_persist_test.txt` | 49 B | confirms the output bind mount persists |

The `clips/` naming (`s777` = seed 777, `30st` = 30 steps, `480` = 480p) shows the previous
work was already exploring exactly the trade-offs the studio must expose: seed, steps,
resolution. The studio's execution form is designed around precisely those parameters.

## 4. Custom nodes

`custom_nodes/` contains one pack plus support files:

- `ComfyUI_IPAdapter_plus/` — 35 nodes verified live. Its `models/` subfolder contains only
  `legacy_directory_do_not_use.txt`, i.e. the adapter file correctly lives in
  `models/ipadapter/`, not in the pack.
- `websocket_image_save.py` — a single-file example node (class `SaveImageWebsocket`).
- `example_node.py.example` — stock example, not loaded as a node.

No other packs, so no hidden dependencies and no version-conflict surface.

## 5. Workflow format decision for the studio

| Concern | Decision |
|---|---|
| Format stored in registry | API format only (`{node_id: {class_type, inputs}}`) |
| Conversion from editor format | not attempted; documented as "Export (API)" in ComfyUI |
| Validation | every graph is checked against live `/object_info` before it is offered |
| Provenance | each registry workflow records `tested_at`, `test_status`, `test_evidence` |
| Untested graphs | allowed, but forced to status `Ready to test` / `Missing models` — **never** `Available` |

The catalog's `Available` status is reserved for workflows that have a recorded successful
run on this machine with the evidence (prompt_id or output file) stored alongside.
