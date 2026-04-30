.PHONY: start stop download-models run-workflow install-nodes logs shell help

WORKFLOW ?= tokyo_night_walk
COMFYUI_URL ?= http://localhost:8188

help:
	@echo "comfy-content — personalized meme & video generation with ComfyUI"
	@echo ""
	@echo "  make download-models          Download all required FOSS models"
	@echo "  make start                    Start ComfyUI (GPU)"
	@echo "  make start-cpu                Start ComfyUI (CPU-only, slow)"
	@echo "  make stop                     Stop ComfyUI"
	@echo "  make install-nodes            Install required ComfyUI custom nodes"
	@echo "  make run-workflow             Queue a workflow via the ComfyUI API"
	@echo "                                  WORKFLOW=tokyo_night_walk (default)"
	@echo "  make logs                     Tail ComfyUI container logs"
	@echo "  make shell                    Open a shell inside the ComfyUI container"

download-models:
	@echo "==> Downloading FOSS models..."
	@./scripts/download_models.sh

start:
	@echo "==> Starting ComfyUI with GPU support..."
	docker compose up -d
	@echo "ComfyUI will be available at $(COMFYUI_URL) (first start may take ~60s)"

start-cpu:
	@echo "==> Starting ComfyUI (CPU-only)..."
	docker compose up -d --no-deps
	@echo "ComfyUI will be available at $(COMFYUI_URL)"

stop:
	docker compose down

install-nodes:
	@echo "==> Installing required custom nodes inside the container..."
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

logs:
	docker compose logs -f comfyui

shell:
	docker compose exec comfyui bash
