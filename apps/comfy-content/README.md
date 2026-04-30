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
| `docker-compose.yml` | ComfyUI container for Linux + NVIDIA GPU (Docker path) |
| `scripts/extra_model_paths.yaml` | Tells native ComfyUI where `./models/` lives (M1 path) |
| `Makefile` | `make setup-mac`, `make start-mac`, `make start`, `make download-models`, `make run-workflow` |
| `output/` | Generated images and videos land here |

---

## Quick start

### Prerequisites (both paths)
- Python 3.10+ with `pip`
- `huggingface_hub` CLI: `pip install "huggingface_hub[cli]"`

### 1. Add the reference image

Place Nathan's reference photo at:
```
assets/nathan_reference.png
```

---

## Apple Silicon (M1 / M2 / M3)

Docker on Apple Silicon can't access the Metal GPU, so ComfyUI runs natively instead. PyTorch detects MPS automatically — no extra configuration needed.

**Expected performance:** ~2–5 minutes per image at 1024×1024 on M1.

### Setup (one-time)

```bash
cd apps/comfy-content
make download-models   # ~10 GB, downloads to ./models/
make setup-mac         # creates .venv, installs PyTorch + ComfyUI + custom nodes
```

### Run

```bash
make start-mac
```

Open `http://localhost:8188`. Load `workflows/tokyo_night_walk.json` and click **Queue Prompt**, or use:

```bash
make run-workflow
```

Output images appear in `./output/`.

---

## Linux + NVIDIA GPU (Docker)

### Prerequisites
- Docker + Docker Compose with NVIDIA Container Toolkit
- NVIDIA GPU with drivers

### Setup (one-time)

```bash
cd apps/comfy-content
make download-models   # ~10 GB
make start             # pulls the container and starts ComfyUI
make install-nodes     # installs custom nodes inside the container
```

### Run

```bash
make run-workflow
# or open http://localhost:8188 and load the workflow manually
```

```bash
make stop    # shut down the container
make logs    # tail logs
make shell   # bash into the container
```

---

## Models

This downloads (~8–12 GB total) to `./models/`:

| Model | License | Source |
|-------|---------|--------|
| SDXL Base 1.0 | CreativeML Open RAIL++-M | [stabilityai/stable-diffusion-xl-base-1.0](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0) |
| SDXL VAE fp16-fix | Apache-2.0 | [madebyollin/sdxl-vae-fp16-fix](https://huggingface.co/madebyollin/sdxl-vae-fp16-fix) |
| IP-Adapter Plus SDXL | Apache-2.0 | [h94/IP-Adapter](https://huggingface.co/h94/IP-Adapter) |
| CLIP ViT-H vision encoder | Apache-2.0 | [h94/IP-Adapter](https://huggingface.co/h94/IP-Adapter) |
| ControlNet OpenPose SDXL | Apache-2.0 | [thibaud/controlnet-openpose-sdxl-1.0](https://huggingface.co/thibaud/controlnet-openpose-sdxl-1.0) |
| DW-Pose (ONNX) | Apache-2.0 | [yzd-v/DWPose](https://huggingface.co/yzd-v/DWPose) |

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
