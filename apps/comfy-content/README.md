# comfy-content

Generate personalized memes and short AI videos featuring yourself and your friends using only free/FOSS models running locally via [ComfyUI](https://github.com/comfyanonymous/ComfyUI).

**No paid APIs. All compute is local.**

---

## What's inside

| Path | Description |
|------|-------------|
| `workflows/tokyo_night_walk.json` | ComfyUI workflow: Nathan walking through neon-lit Tokyo at night |
| `assets/nathan_reference.png` | Reference photo used for identity consistency |
| `scripts/download_models.sh` | Downloads all required FOSS models from HuggingFace |
| `docker-compose.yml` | ComfyUI container with GPU passthrough |
| `Makefile` | `make start`, `make download-models`, `make run-workflow` |
| `output/` | Generated images and videos land here |

---

## Quick start

### Prerequisites
- Docker + Docker Compose
- NVIDIA GPU with drivers (or use `make start-cpu` for CPU-only, much slower)
- Python 3.8+ (for `make run-workflow`)
- `huggingface_hub` CLI: `pip install "huggingface_hub[cli]"`

### 1. Add the reference image

Place Nathan's reference photo at:
```
assets/nathan_reference.png
```

This file is mounted into ComfyUI's `input/` directory automatically.

### 2. Download models

```bash
make download-models
```

This downloads (~8–12 GB total) to `./models/`:

| Model | License | Source |
|-------|---------|--------|
| SDXL Base 1.0 | CreativeML Open RAIL++-M | [stabilityai/stable-diffusion-xl-base-1.0](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0) |
| SDXL VAE fp16-fix | Apache-2.0 | [madebyollin/sdxl-vae-fp16-fix](https://huggingface.co/madebyollin/sdxl-vae-fp16-fix) |
| IP-Adapter Plus SDXL | Apache-2.0 | [h94/IP-Adapter](https://huggingface.co/h94/IP-Adapter) |
| CLIP ViT-H vision encoder | Apache-2.0 | [h94/IP-Adapter](https://huggingface.co/h94/IP-Adapter) |
| ControlNet OpenPose SDXL | Apache-2.0 | [thibaud/controlnet-openpose-sdxl-1.0](https://huggingface.co/thibaud/controlnet-openpose-sdxl-1.0) |
| DW-Pose (ONNX) | Apache-2.0 | [yzd-v/DWPose](https://huggingface.co/yzd-v/DWPose) |

### 3. Start ComfyUI

```bash
make start
```

Open `http://localhost:8188` in your browser. Wait for the health check to pass (~60s on first pull).

### 4. Install custom nodes (first time only)

```bash
make install-nodes
```

This installs inside the running container:
- [ComfyUI_IPAdapter_plus](https://github.com/cubiq/ComfyUI_IPAdapter_plus)
- [ComfyUI-Advanced-ControlNet](https://github.com/Kosinkadink/ComfyUI-Advanced-ControlNet)
- [comfyui_controlnet_aux](https://github.com/Fannovel16/comfyui_controlnet_aux) (DW-Pose preprocessor)

### 5. Run the Tokyo workflow

**Option A — via the web UI:**
1. Open `http://localhost:8188`
2. Click **Load** → select `workflows/tokyo_night_walk.json`
3. Click **Queue Prompt**

**Option B — via CLI:**
```bash
make run-workflow
# or a different workflow:
make run-workflow WORKFLOW=my_other_workflow
```

Output images appear in `./output/`.

---

## Enabling video output (AnimateDiff)

The workflow currently generates a single high-quality SDXL still. To generate a 16-frame walking animation:

1. Uncomment the AnimateDiff section in `scripts/download_models.sh` and re-run `make download-models`.
2. Inside ComfyUI, install [ComfyUI-AnimateDiff-Evolved](https://github.com/Kosinkadink/ComfyUI-AnimateDiff-Evolved).
3. Create a new workflow based on `tokyo_night_walk.json` that wraps the KSampler in an `AnimateDiff Loader` + `AnimateDiff Sample Settings` node. The motion module `mm_sd_v15_v2.ckpt` should use SD 1.5 (not SDXL).

---

## Adding new workflows / reference images

1. Drop new reference images into `assets/` — they'll appear in ComfyUI's `input/` folder automatically.
2. Build your workflow in the ComfyUI UI, then **Save (API format)** → drop the JSON into `workflows/`.
3. Run it with `make run-workflow WORKFLOW=your_workflow_name`.

---

## Stopping

```bash
make stop
```

Models and custom nodes persist across restarts via Docker volumes and the `./models/` bind mount.
