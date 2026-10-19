"""Shared fixtures. Unit tests run against `FakeRepository`; nothing here
can reach Garmin. The live tests in test_integration.py build their own."""

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from garmin_mcp.config import Settings
from garmin_mcp.server import App, build_app

from tests.fakes import FakeRepository


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A developer's own GARMIN_* settings or .env must not leak into tests.
    for var in (
        "GARMIN_EMAIL",
        "GARMIN_TOKENSTORE",
        "GARMIN_DRY_RUN",
        "GARMIN_STATE_DIR",
        "GARMINTOKENS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        tokenstore=tmp_path / "tokens",
        state_dir=tmp_path / "state",
        dry_run=False,
    )


@pytest.fixture
def repo() -> FakeRepository:
    return FakeRepository()


@pytest.fixture
def app(settings: Settings, repo: FakeRepository) -> App:
    return build_app(settings, repo)


@pytest.fixture
def tomorrow() -> date:
    return date.today() + timedelta(days=1)


def strength_spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "name": "Push day",
        "sport": "strength",
        "exercises": [
            {
                "name": "Barbell Bench Press",
                "sets": 4,
                "reps": 8,
                "weight_lb": 135,
                "rest_seconds": 90,
            },
            {"name": "push up", "sets": 3, "reps": 15},
            {"name": "Plank", "sets": 3, "duration_seconds": 45, "rest_seconds": 30},
        ],
    }
    return spec | overrides


def running_spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "name": "6 x 400",
        "sport": "running",
        "steps": [
            {
                "type": "warmup",
                "duration_seconds": 600,
                "target": {"type": "heart_rate_zone", "zone": 2},
            },
            {
                "type": "repeat",
                "repeat_count": 6,
                "steps": [
                    {
                        "type": "interval",
                        "distance_meters": 400,
                        "target": {
                            "type": "pace",
                            "fast_seconds": 230,
                            "slow_seconds": 240,
                        },
                    },
                    {"type": "recovery", "duration_seconds": 90},
                ],
            },
            {"type": "cooldown"},
        ],
    }
    return spec | overrides
