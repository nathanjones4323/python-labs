"""A model that runs on this machine, through Ollama: pick the strongest one
that fits, download it, and give it a context window the booking tools fit in.

Used by setup (`python -m tee_time_booker.setup`), not by the running bot,
which only talks to Ollama's OpenAI-compatible API (`config.OLLAMA_URL`).
Ollama itself is installed by the person (https://ollama.com/download);
nothing here installs software.
"""

import json
import os
import platform
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from tee_time_booker.config import LOCAL_MODEL_ALIAS

OLLAMA = "http://localhost:11434"
# The system prompt and about twenty tool schemas don't fit Ollama's default
# window, and the OpenAI-compatible API can't widen it per request.
CONTEXT_TOKENS = 32768
# Left for the OS, the booking browser and the context's cache.
HEADROOM_GB = 8


@dataclass(frozen=True)
class LocalModel:
    tag: str
    download_gb: float

    @property
    def needs_gb(self) -> float:
        return self.download_gb + HEADROOM_GB


# Strongest first. Tool-calling models from Ollama's library
# (ollama.com/search?c=tools) as of 2026-10; check there for newer ones.
MODELS = (
    LocalModel("ornith-1.5:122b", 77),
    LocalModel("qwen3.8:27b", 18),
    LocalModel("ornith-1.5:9b", 6.6),
    LocalModel("bonsai-2:9b", 3.2),
)


@dataclass(frozen=True)
class Machine:
    memory_gb: float
    # "Apple Silicon", "NVIDIA GPU" or "CPU only": what the model runs on.
    accelerator: str


def pick_model(memory_gb: float) -> LocalModel | None:
    """The strongest model that fits, or None when even the smallest doesn't."""
    return next((m for m in MODELS if m.needs_gb <= memory_gb), None)


def detect_machine() -> Machine:
    """What a model gets to use: unified memory on Apple Silicon, the GPU's
    memory with an NVIDIA card, and otherwise RAM (slow, but it works)."""
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return Machine(_ram_gb(), "Apple Silicon")
    if vram := _nvidia_vram_gb():
        return Machine(vram, "NVIDIA GPU")
    return Machine(_ram_gb(), "CPU only")


def _ram_gb() -> float:
    if platform.system() == "Darwin":
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=True
        )
        return int(out.stdout) / 2**30
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30


def _nvidia_vram_gb() -> float | None:
    if not shutil.which("nvidia-smi"):
        return None
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return None
    try:
        # MiB per GPU; one model goes on the biggest card.
        return max(int(line) for line in out.stdout.split()) / 1024
    except ValueError:
        return None


def ollama_running() -> bool:
    try:
        return httpx.get(f"{OLLAMA}/api/version", timeout=3).is_success
    except httpx.HTTPError:
        return False


def pull(tag: str, on_progress: Callable[[str, float | None], None]) -> None:
    """Download `tag`, telling `on_progress` each step and how far along it is."""
    with httpx.stream(
        "POST", f"{OLLAMA}/api/pull", json={"model": tag}, timeout=None
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            event = json.loads(line)
            if error := event.get("error"):
                raise RuntimeError(f"Ollama couldn't download {tag}: {error}")
            total, done = event.get("total"), event.get("completed")
            on_progress(
                event.get("status", ""), done / total if total and done else None
            )


def create_alias(tag: str) -> str:
    """The model the bot asks for: `tag` with a big enough context window."""
    response = httpx.post(
        f"{OLLAMA}/api/create",
        json={
            "model": LOCAL_MODEL_ALIAS,
            "from": tag,
            "parameters": {"num_ctx": CONTEXT_TOKENS},
            "stream": False,
        },
        timeout=120,
    )
    response.raise_for_status()
    return LOCAL_MODEL_ALIAS
