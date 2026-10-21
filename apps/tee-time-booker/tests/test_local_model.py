"""Choosing a local model for the machine, without Ollama."""

import pytest
from tee_time_booker.bot import local_model
from tee_time_booker.bot.local_model import HEADROOM_GB, MODELS, pick_model


def test_the_strongest_model_that_fits_is_picked():
    for model in MODELS:
        assert pick_model(model.needs_gb) == model
    # Just short of a model's need gets the next one down.
    assert pick_model(MODELS[0].needs_gb - 0.1) == MODELS[1]
    assert pick_model(512) == MODELS[0]


def test_too_little_memory_picks_nothing():
    assert pick_model(MODELS[-1].needs_gb - 0.1) is None


def test_models_are_listed_strongest_first():
    sizes = [m.download_gb for m in MODELS]
    assert sizes == sorted(sizes, reverse=True)
    assert all(m.needs_gb == m.download_gb + HEADROOM_GB for m in MODELS)


@pytest.mark.parametrize(
    ("system", "arch", "vram", "accelerator", "gb"),
    [
        ("Darwin", "arm64", 24.0, "Apple Silicon", 36.0),
        ("Linux", "x86_64", 24.0, "NVIDIA GPU", 24.0),
        ("Linux", "x86_64", None, "CPU only", 36.0),
    ],
)
def test_the_memory_a_model_gets(monkeypatch, system, arch, vram, accelerator, gb):
    monkeypatch.setattr(local_model.platform, "system", lambda: system)
    monkeypatch.setattr(local_model.platform, "machine", lambda: arch)
    monkeypatch.setattr(local_model, "_ram_gb", lambda: 36.0)
    monkeypatch.setattr(local_model, "_nvidia_vram_gb", lambda: vram)
    machine = local_model.detect_machine()
    assert (machine.accelerator, machine.memory_gb) == (accelerator, gb)
