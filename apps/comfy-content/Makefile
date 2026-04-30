.PHONY: start stop download-models run-workflow install-nodes install-nodes-mac \
        setup-mac start-mac logs shell help

WORKFLOW     ?= tokyo_night_walk
COMFYUI_URL  ?= http://localhost:8188
COMFYUI_DIR  := comfyui
VENV         := .venv
PYTHON       := $(VENV)/bin/python3
PIP          := $(VENV)/bin/pip

help:
	@echo "comfy-content — personalized meme & video generation with ComfyUI"
	@echo ""
	@echo "── Apple Silicon (M1/M2/M3) ──────────────────────────────────────"
	@echo "  make setup-mac                One-time setup: venv + ComfyUI + nodes"
	@echo "  make start-mac                Launch ComfyUI natively with MPS"
	@echo "  make install-nodes-mac        Install/update custom nodes (native)"
	@echo ""
	@echo "── Linux + NVIDIA GPU (Docker) ───────────────────────────────────"
	@echo "  make start                    Start ComfyUI container (GPU)"
	@echo "  make stop                     Stop ComfyUI container"
	@echo "  make install-nodes            Install custom nodes in container"
	@echo "  make logs                     Tail container logs"
	@echo "  make shell                    Shell into the container"
	@echo ""
	@echo "── Shared ────────────────────────────────────────────────────────"
	@echo "  make download-models          Download all required FOSS models"
	@echo "  make run-workflow             Queue a workflow via the ComfyUI API"
	@echo "                                  WORKFLOW=tokyo_night_walk (default)"

# ── Model downloads (hardware-agnostic) ──────────────────────────────────────

download-models:
	@echo "==> Downloading FOSS models..."
	@./scripts/download_models.sh

# ── Apple Silicon native path ─────────────────────────────────────────────────

setup-mac:
	@echo "==> Creating Python venv..."
	python3 -m venv $(VENV)
	@echo "==> Installing PyTorch with MPS support (Apple Silicon)..."
	$(PIP) install --upgrade pip
	$(PIP) install torch torchvision torchaudio
	@echo "==> Cloning ComfyUI..."
	[ -d $(COMFYUI_DIR) ] || git clone https://github.com/comfyanonymous/ComfyUI.git $(COMFYUI_DIR)
	@echo "==> Installing ComfyUI dependencies..."
	$(PIP) install -r $(COMFYUI_DIR)/requirements.txt
	@$(MAKE) install-nodes-mac
	@echo ""
	@echo "✓ Setup complete. Run: make start-mac"

install-nodes-mac:
	@echo "==> Installing custom nodes..."
	[ -d $(COMFYUI_DIR)/custom_nodes/ComfyUI_IPAdapter_plus ] || \
	  git clone https://github.com/cubiq/ComfyUI_IPAdapter_plus.git \
	            $(COMFYUI_DIR)/custom_nodes/ComfyUI_IPAdapter_plus
	[ -d $(COMFYUI_DIR)/custom_nodes/ComfyUI-Advanced-ControlNet ] || \
	  git clone https://github.com/Kosinkadink/ComfyUI-Advanced-ControlNet.git \
	            $(COMFYUI_DIR)/custom_nodes/ComfyUI-Advanced-ControlNet
	[ -d $(COMFYUI_DIR)/custom_nodes/comfyui_controlnet_aux ] || \
	  git clone https://github.com/Fannovel16/comfyui_controlnet_aux.git \
	            $(COMFYUI_DIR)/custom_nodes/comfyui_controlnet_aux
	$(PIP) install -q \
	  -r $(COMFYUI_DIR)/custom_nodes/ComfyUI_IPAdapter_plus/requirements.txt \
	  -r $(COMFYUI_DIR)/custom_nodes/ComfyUI-Advanced-ControlNet/requirements.txt \
	  -r $(COMFYUI_DIR)/custom_nodes/comfyui_controlnet_aux/requirements.txt

start-mac:
	@echo "==> Starting ComfyUI natively (MPS / Apple Silicon)..."
	@echo "    UI will be at $(COMFYUI_URL)"
	$(PYTHON) $(COMFYUI_DIR)/main.py \
	  --use-pytorch-cross-attention \
	  --input-directory  $$(pwd)/assets \
	  --output-directory $$(pwd)/output \
	  --extra-model-paths-config $$(pwd)/scripts/extra_model_paths.yaml

# ── Linux + NVIDIA Docker path ────────────────────────────────────────────────

start:
	@echo "==> Starting ComfyUI with NVIDIA GPU (Docker)..."
	docker compose up -d
	@echo "ComfyUI will be available at $(COMFYUI_URL) (first start may take ~60s)"

stop:
	docker compose down

install-nodes:
	@echo "==> Installing custom nodes inside the container..."
	@echo "    (ComfyUI must be running — run 'make start' first)"
	docker compose exec comfyui bash -c "\
		cd /opt/ComfyUI/custom_nodes && \
		[ -d ComfyUI-IPAdapter-plus ] || git clone https://github.com/cubiq/ComfyUI_IPAdapter_plus.git ComfyUI-IPAdapter-plus && \
		[ -d ComfyUI-Advanced-ControlNet ] || git clone https://github.com/Kosinkadink/ComfyUI-Advanced-ControlNet.git ComfyUI-Advanced-ControlNet && \
		[ -d comfyui_controlnet_aux ] || git clone https://github.com/Fannovel16/comfyui_controlnet_aux.git comfyui_controlnet_aux && \
		pip install -q -r ComfyUI-IPAdapter-plus/requirements.txt \
		              -r ComfyUI-Advanced-ControlNet/requirements.txt \
		              -r comfyui_controlnet_aux/requirements.txt"
	@echo "==> Restarting ComfyUI to load new nodes..."
	docker compose restart comfyui

logs:
	docker compose logs -f comfyui

shell:
	docker compose exec comfyui bash

# ── Shared ────────────────────────────────────────────────────────────────────

run-workflow:
	@echo "==> Queuing workflow: $(WORKFLOW)"
	@python3 - <<'EOF'
import json, urllib.request, sys, pathlib

url = "$(COMFYUI_URL)/prompt"
wf_path = pathlib.Path("workflows/$(WORKFLOW).json")
if not wf_path.exists():
    print(f"ERROR: workflow not found: {wf_path}", file=sys.stderr)
    sys.exit(1)

payload = {"prompt": json.loads(wf_path.read_text())}
data = json.dumps(payload).encode()
req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req) as resp:
    result = json.loads(resp.read())
    print(f"Queued prompt_id: {result.get('prompt_id')}")
    print(f"Track at: $(COMFYUI_URL)")
EOF
