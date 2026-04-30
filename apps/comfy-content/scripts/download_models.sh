#!/usr/bin/env bash
# Downloads all FOSS models required by the comfy-content workflows.
# Prerequisites: huggingface-cli  (pip install huggingface_hub[cli])
#                wget
# Optional:      HF_TOKEN env var for gated models

set -euo pipefail

MODELS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/models"

# ── helpers ───────────────────────────────────────────────────────────────────

hf_download() {
  local repo="$1" filename="$2" dest_dir="$3"
  mkdir -p "$dest_dir"
  if [ -f "$dest_dir/$filename" ]; then
    echo "  [skip] $filename already present"
    return
  fi
  echo "  [download] $repo  →  $dest_dir/$filename"
  huggingface-cli download "$repo" "$filename" --local-dir "$dest_dir" ${HF_TOKEN:+--token "$HF_TOKEN"}
}

# ── 1. SDXL base checkpoint ───────────────────────────────────────────────────
# License: CreativeML Open RAIL++-M  (free for non-commercial & commercial use)
# https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0
echo "==> SDXL base checkpoint"
hf_download \
  "stabilityai/stable-diffusion-xl-base-1.0" \
  "sd_xl_base_1.0.safetensors" \
  "$MODELS_DIR/checkpoints"

# ── 2. SDXL VAE (fp16-fixed, prevents grey/washed images) ────────────────────
# https://huggingface.co/madebyollin/sdxl-vae-fp16-fix
echo "==> SDXL VAE (fp16-fix)"
hf_download \
  "madebyollin/sdxl-vae-fp16-fix" \
  "sdxl_vae.safetensors" \
  "$MODELS_DIR/vae"

# ── 3. IP-Adapter Plus for SDXL (identity-preserving) ────────────────────────
# License: Apache-2.0  https://huggingface.co/h94/IP-Adapter
echo "==> IP-Adapter Plus SDXL"
hf_download \
  "h94/IP-Adapter" \
  "sdxl_models/ip-adapter-plus_sdxl_vit-h.safetensors" \
  "$MODELS_DIR/ipadapter"

# CLIP vision encoder required by IP-Adapter
echo "==> CLIP ViT-H vision encoder"
mkdir -p "$MODELS_DIR/clip_vision"
huggingface-cli download \
  "h94/IP-Adapter" \
  "models/image_encoder/model.safetensors" \
  --local-dir "$MODELS_DIR/clip_vision" \
  ${HF_TOKEN:+--token "$HF_TOKEN"}
# Rename to the path ComfyUI IPAdapter nodes expect
mv -f "$MODELS_DIR/clip_vision/models/image_encoder/model.safetensors" \
      "$MODELS_DIR/clip_vision/CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors" 2>/dev/null || true

# ── 4. ControlNet OpenPose for SDXL ──────────────────────────────────────────
# License: Apache-2.0  https://huggingface.co/thibaud/controlnet-openpose-sdxl-1.0
echo "==> ControlNet OpenPose SDXL"
hf_download \
  "thibaud/controlnet-openpose-sdxl-1.0" \
  "controlnet-openpose-sdxl-1.0.safetensors" \
  "$MODELS_DIR/controlnet"

# ── 5. DW-Pose preprocessor weights (used by ComfyUI-Advanced-ControlNet) ────
# These are fetched automatically by the node on first run, but downloading
# them ahead of time avoids runtime delays.
echo "==> DW-Pose weights"
mkdir -p "$MODELS_DIR/onnx"
declare -A DWPOSE_FILES=(
  ["dw-ll_ucoco_384.onnx"]="yzd-v/DWPose"
  ["yolox_l.onnx"]="yzd-v/DWPose"
)
for fname in "${!DWPOSE_FILES[@]}"; do
  hf_download "${DWPOSE_FILES[$fname]}" "$fname" "$MODELS_DIR/onnx"
done

# ── 6. AnimateDiff motion module (optional – for video output) ────────────────
# License: Apache-2.0  https://huggingface.co/guoyww/animatediff
# Uncomment the lines below if you want 16-frame walking animations.
# Note: AnimateDiff motion modules are for SD 1.5, not SDXL. To use them
# you'll also need an SD 1.5 checkpoint and the corresponding IP-Adapter weights.
#
# echo "==> AnimateDiff motion module (SD1.5)"
# hf_download \
#   "guoyww/animatediff" \
#   "mm_sd_v15_v2.ckpt" \
#   "$MODELS_DIR/animatediff_models"
#
# echo "==> SD 1.5 base (for AnimateDiff)"
# hf_download \
#   "runwayml/stable-diffusion-v1-5" \
#   "v1-5-pruned-emaonly.safetensors" \
#   "$MODELS_DIR/checkpoints"
#
# echo "==> IP-Adapter Plus SD1.5 (for AnimateDiff)"
# hf_download \
#   "h94/IP-Adapter" \
#   "models/ip-adapter-plus_sd15.safetensors" \
#   "$MODELS_DIR/ipadapter"

echo ""
echo "✓ All models downloaded to $MODELS_DIR"
echo ""
echo "Directory layout:"
find "$MODELS_DIR" -type f | sort
